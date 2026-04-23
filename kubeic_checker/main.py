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


def _check_credential_validity(creds: list[ResolvedCredential], namespace: str) -> None:
    """Test each resolved credential against its registry and emit a validity metric."""
    from kubeic_checker.availability import _run_skopeo_inspect

    kube_image_credential_valid.clear()
    seen: set[str] = set()

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

        if CREDENTIAL_TEST_IMAGE:
            test_image = CREDENTIAL_TEST_IMAGE
        elif "/" in cred.registry and ("." in cred.registry.split("/")[0] or ":" in cred.registry.split("/")[0]):
            test_image = f"{cred.registry}/alpine:latest"
        else:
            test_image = f"{cred.registry}/library/alpine:latest"

        valid, _ = _run_skopeo_inspect(test_image, auth_file=auth_path)

        secret_name = cred.source.split(":")[-1] if ":" in cred.source else cred.source
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
                _check_credential_validity(creds, NAMESPACE)

                unavailable = [r for r in results if not r.available]
                if unavailable:
                    logger.warning(
                        "%d/%d images unavailable in %s",
                        len(unavailable), len(results), NAMESPACE,
                    )
                else:
                    logger.info("All %d images available in %s", len(results), NAMESPACE)

        except Exception:
            logger.exception("Check cycle failed")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    start_http_server(METRICS_PORT)
    logger.info("Metrics server started on port %d", METRICS_PORT)
    run_check_loop()
