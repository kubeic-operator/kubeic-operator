import logging
import os
import time
import tempfile

from kubernetes import client, config
from prometheus_client import start_http_server

from kubeic_checker.availability import check_availability, write_auth_config
from kubeic_checker.credentials import resolve_all_credentials, ResolvedCredential
from kubeic_operator.metrics import update_availability_metrics, kube_image_credential_valid
from kubeic_operator.checks.prerelease import should_skip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("image-audit-checker")

NAMESPACE = os.environ.get("NAMESPACE", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30")) * 60
CREDENTIAL_SOURCE = os.environ.get("CREDENTIAL_SOURCE", "pullSecret")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9090"))
CREDENTIAL_TEST_IMAGE = os.environ.get("CREDENTIAL_TEST_IMAGE", "")
SKIP_ANNOTATION = os.environ.get("SKIP_ANNOTATION", "")


def _get_pods(namespace: str) -> list[dict]:
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace)

    result = []
    for pod in pods.items:
        result.append({
            "metadata": {"name": pod.metadata.name, "namespace": namespace, "annotations": pod.metadata.annotations or {}},
            "spec": {
                "containers": [{"name": c.name, "image": c.image} for c in (pod.spec.containers or [])],
                "initContainers": [{"name": c.name, "image": c.image} for c in (pod.spec.init_containers or [])],
                "imagePullSecrets": [{"name": s.name} for s in (pod.spec.image_pull_secrets or [])],
            },
        })
    return result


def _build_auth_file(creds: list[ResolvedCredential]) -> str | None:
    """Build a Docker auth config file from resolved credentials."""
    all_creds: dict[str, dict] = {}
    for cred in creds:
        # Normalize registry to hostname only — skopeo matches by hostname,
        # but docker configs from GitLab/GHCR may include paths (e.g. ghcr.io/org).
        host = cred.registry.split("/")[0]
        all_creds[host] = {
            k: v for k, v in {
                "username": cred.username,
                "password": cred.password,
                "auth": cred.auth,
            }.items() if v is not None
        }

    if not all_creds:
        return None

    fd, path = tempfile.mkstemp(suffix=".json", prefix="image-audit-auth-")
    os.close(fd)
    write_auth_config(all_creds, path)
    return path


def _check_credential_validity(
    creds: list[ResolvedCredential], namespace: str, pods: list[dict],
) -> None:
    """Test each credential using repo-level list-tags to verify auth access.

    Only marks credentials invalid on authentication failures.
    Missing images or network errors do not affect credential validity.
    """
    from kubeic_checker.availability import _run_skopeo_list_tags, _run_skopeo_inspect
    from kubeic_checker.credentials import registry_from_image

    kube_image_credential_valid.clear()
    seen: set[str] = set()

    # Build secret_name -> set of images used by pods that reference that secret
    secret_images: dict[str, set[str]] = {}
    for pod in pods:
        pull_secrets = [
            ref.get("name", "")
            for ref in pod.get("spec", {}).get("imagePullSecrets", [])
        ]
        containers = list(pod.get("spec", {}).get("containers", [])) + list(
            pod.get("spec", {}).get("initContainers", [])
        )
        images = {
            c["image"].split("@")[0] if "@" in c["image"] else c["image"]
            for c in containers
        }
        for secret_name in pull_secrets:
            if secret_name:
                secret_images.setdefault(secret_name, set()).update(images)

    for cred in creds:
        key = f"{namespace}/{cred.registry}/{cred.source}"
        if key in seen:
            continue
        seen.add(key)

        auth_data = {}
        if cred.auth:
            host = cred.registry.split("/")[0]
            auth_data[host] = {"auth": cred.auth}
        elif cred.username and cred.password:
            import base64
            token = base64.b64encode(f"{cred.username}:{cred.password}".encode()).decode()
            host = cred.registry.split("/")[0]
            auth_data[host] = {"auth": token}
        else:
            continue

        auth_fd, auth_path = tempfile.mkstemp(suffix=".json", prefix="cred-check-")
        os.close(auth_fd)
        try:
            write_auth_config(auth_data, auth_path)

            secret_name = cred.source.split(":")[-1] if ":" in cred.source else cred.source

            # Find a matching image from pods to get the repo path
            pod_images = secret_images.get(secret_name, set())
            cred_host = cred.registry.split("/")[0]
            matching = [
                img for img in pod_images if registry_from_image(img) == cred_host
            ]

            if CREDENTIAL_TEST_IMAGE:
                # Fallback: use configured test image
                ok, _, err_class = _run_skopeo_list_tags(CREDENTIAL_TEST_IMAGE, auth_file=auth_path)
                if err_class == "auth_failure":
                    valid = False
                elif ok:
                    valid = True
                else:
                    # Can't determine from list-tags, fall back to inspect
                    _, _, _, inspect_err = _run_skopeo_inspect(CREDENTIAL_TEST_IMAGE, auth_file=auth_path)
                    valid = inspect_err != "auth_failure"
            elif matching:
                # Use list-tags on the repo to verify credential access
                ok, _, err_class = _run_skopeo_list_tags(matching[0], auth_file=auth_path)
                if err_class == "auth_failure":
                    valid = False
                elif ok:
                    valid = True
                else:
                    # Network/unknown error from list-tags — fall back to inspect
                    # to try to determine if it's an auth issue
                    valid = any(
                        _run_skopeo_inspect(img, auth_file=auth_path)[3] != "auth_failure"
                        for img in matching
                    )
            else:
                valid = False
        finally:
            try:
                os.unlink(auth_path)
            except OSError:
                pass

        kube_image_credential_valid.labels(
            registry=cred.registry, namespace=namespace, secret_name=secret_name
        ).set(1 if valid else 0)


def run_check_loop():
    """Main checker loop: resolve creds, check availability, expose metrics."""
    config.load_incluster_config()
    secrets_client = client.CoreV1Api()

    logger.info("Starting checker for namespace %s (interval=%ds)", NAMESPACE, CHECK_INTERVAL)

    while True:
        try:
            pods = _get_pods(NAMESPACE)
            if not pods:
                logger.info("No pods found in %s", NAMESPACE)
            else:
                # Filter out pods annotated to skip availability/digest/credentials
                if SKIP_ANNOTATION:
                    auditable_pods = [
                        p for p in pods
                        if not should_skip(p, SKIP_ANNOTATION, "availability")
                    ]
                else:
                    auditable_pods = pods

                creds = resolve_all_credentials(auditable_pods, secrets_client, CREDENTIAL_SOURCE)
                auth_file = _build_auth_file(creds)
                try:
                    results = check_availability(auditable_pods, auth_file=auth_file)
                finally:
                    if auth_file:
                        try:
                            os.unlink(auth_file)
                        except OSError:
                            pass
                update_availability_metrics(results, namespace=NAMESPACE)
                _check_credential_validity(creds, NAMESPACE, auditable_pods)

                unavailable = [r for r in results if not r.available]
                digest_mismatches = [r for r in results if r.digest_match is False]
                if unavailable:
                    logger.warning(
                        "%d/%d images unavailable in %s",
                        len(unavailable), len(results), NAMESPACE,
                    )
                if digest_mismatches:
                    for r in digest_mismatches:
                        logger.warning(
                            "Digest mismatch for %s in %s/%s: pinned=%s registry=%s",
                            r.image, NAMESPACE, r.pod,
                            r.pinned_digest, r.registry_digest,
                        )
                if not unavailable and not digest_mismatches:
                    logger.info("All %d images available in %s", len(results), NAMESPACE)

        except Exception as e:
            logger.error("Check cycle failed: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    start_http_server(METRICS_PORT)
    logger.info("Metrics server started on port %d", METRICS_PORT)
    run_check_loop()
