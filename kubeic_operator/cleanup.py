import logging
from kubernetes import client, config

from kubeic_operator.deployer import (
    CENTRAL_CHECKER_ROLE,
    RELEASE_NAME,
    teardown_central_secret_access,
    teardown_checker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("kubeic-operator.cleanup")


def run() -> None:
    config.load_incluster_config()
    apps_v1 = client.AppsV1Api()
    rbac_v1 = client.RbacAuthorizationV1Api()

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

    # Central mode leaves no checker Deployment outside the release namespace, so
    # the sweep above finds nothing — but the operator will have created a secret
    # Role and RoleBinding in every audited namespace. Those are found by name,
    # not by looking for a Deployment that was never there.
    roles = rbac_v1.list_role_for_all_namespaces(label_selector=label_selector)
    central_namespaces = {
        r.metadata.namespace for r in roles.items if r.metadata.name == CENTRAL_CHECKER_ROLE
    }

    if central_namespaces:
        logger.info(
            "Found central checker secret grants in %d namespace(s): %s",
            len(central_namespaces), sorted(central_namespaces),
        )
    for namespace in sorted(central_namespaces):
        logger.info("Revoking central checker secret access to %s", namespace)
        teardown_central_secret_access(namespace)

    logger.info("Cleanup complete")


if __name__ == "__main__":
    run()
