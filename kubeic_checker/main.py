import logging
import os
import time
import tempfile

from kubernetes import client, config
from prometheus_client import start_http_server

from kubeic_checker.availability import check_availability, write_auth_config
from kubeic_checker.credentials import resolve_all_credentials, ResolvedCredential
from kubeic_operator.metrics import update_availability_metrics, kube_image_credential_valid

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


def _get_pods(namespace: str) -> list[dict]:
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace)

    result = []
    for pod in pods.items:
        result.append({
            "metadata": {"name": pod.metadata.name, "namespace": namespace},
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
        all_creds[cred.registry] = {
            k: v for k, v in {
                "username": cred.username,
                "password": cred.password,
                "auth": cred.auth,
            }.items() if v is not None
        }

    if not all_creds:
        return None

    path = os.path.join(tempfile.gettempdir(), "image-audit-auth.json")
    write_auth_config(all_creds, path)
    return path


def _check_credential_validity(
    creds: list[ResolvedCredential], namespace: str, pods: list[dict],
) -> None:
    """Test each credential against the actual images from pods that reference its secret."""
    from kubeic_checker.availability import _run_skopeo_inspect
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
            auth_data[cred.registry] = {"auth": cred.auth}
        elif cred.username and cred.password:
            import base64
            token = base64.b64encode(f"{cred.username}:{cred.password}".encode()).decode()
            auth_data[cred.registry] = {"auth": token}
        else:
            continue

        auth_path = os.path.join(tempfile.gettempdir(), f"cred-check-{hash(key)}.json")
        write_auth_config(auth_data, auth_path)

        secret_name = cred.source.split(":")[-1] if ":" in cred.source else cred.source

        # Test against the actual images from pods that reference this secret
        pod_images = secret_images.get(secret_name, set())
        matching = [
            img for img in pod_images if registry_from_image(img) == cred.registry
        ]

        if CREDENTIAL_TEST_IMAGE:
            valid, _, _ = _run_skopeo_inspect(CREDENTIAL_TEST_IMAGE, auth_file=auth_path)
        elif matching:
            # Credential is valid if it can inspect at least one of the actual images
            valid = any(
                _run_skopeo_inspect(img, auth_file=auth_path)[0]
                for img in matching
            )
        else:
            valid = False

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
                creds = resolve_all_credentials(pods, secrets_client, CREDENTIAL_SOURCE)
                auth_file = _build_auth_file(creds)
                results = check_availability(pods, auth_file=auth_file)
                update_availability_metrics(results, namespace=NAMESPACE)
                _check_credential_validity(creds, NAMESPACE, pods)

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

        except Exception:
            logger.exception("Check cycle failed")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    start_http_server(METRICS_PORT)
    logger.info("Metrics server started on port %d", METRICS_PORT)
    run_check_loop()
