import logging
from kubernetes import client, config

from kubeic_operator.deployer import teardown_checker, RELEASE_NAME

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("kubeic-operator.cleanup")


def run() -> None:
    config.load_incluster_config()
    apps_v1 = client.AppsV1Api()

    label_selector = (
        "app.kubernetes.io/component=checker,"
        f"app.kubernetes.io/instance={RELEASE_NAME}"
    )
    deployments = apps_v1.list_deployment_for_all_namespaces(label_selector=label_selector)
    namespaces = {d.metadata.namespace for d in deployments.items}

    logger.info("Found checker deployments in %d namespace(s): %s", len(namespaces), sorted(namespaces))

    for namespace in sorted(namespaces):
        logger.info("Tearing down checker in %s", namespace)
        teardown_checker(namespace)

    logger.info("Cleanup complete")


if __name__ == "__main__":
    run()
