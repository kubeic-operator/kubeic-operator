import logging

import kopf
from kubernetes import client

from kubeic_operator.deployer import (
    EXCLUDED_NAMESPACES,
    deploy_checker_serialised,
    get_secret_names_for_namespace,
    teardown_checker,
)

logger = logging.getLogger("kubeic-operator.handlers.namespace")


def _get_effective_policy(namespace: str) -> dict:
    """Get the effective ImageAuditPolicy for a namespace.

    Checks for a namespace-scoped policy first, falls back to cluster-defaults
    in the operator's namespace.
    """
    api = client.CustomObjectsApi()
    group = "imageaudit.kubeic.io"
    version = "v1alpha1"
    plural = "imageauditpolicies"

    # Try namespace-scoped policy first
    try:
        policies = api.list_namespaced_custom_object(
            group, version, namespace, plural,
        )
        items = policies.get("items", [])
        if items:
            return items[0].get("spec", {})
    except client.ApiException as e:
        if e.status != 404:
            logger.warning("Failed to read policy for namespace %s: %s", namespace, e)

    # Fall back to cluster-defaults in operator namespace
    operator_ns = _get_operator_namespace()
    try:
        policy = api.get_namespaced_custom_object(
            group, version, operator_ns, plural, "cluster-defaults",
        )
        return policy.get("spec", {})
    except client.ApiException as e:
        if e.status != 404:
            logger.warning("Failed to read cluster-defaults policy: %s", e)

    logger.debug("No policy found for namespace %s, using defaults", namespace)
    return {}


def _get_operator_namespace() -> str:
    import os
    return os.environ.get("OPERATOR_NAMESPACE", "kubeic-operator")


def _should_audit(namespace: str, labels: dict | None, policy: dict) -> bool:
    if namespace in EXCLUDED_NAMESPACES:
        return False

    selector = policy.get("namespaceSelector", {})
    exclude_labels = selector.get("excludeLabels", {})
    if exclude_labels and labels:
        for key, value in exclude_labels.items():
            if labels.get(key) == value:
                logger.info("Namespace %s excluded by label %s=%s", namespace, key, value)
                return False

    return True


# No @kopf.on.resume here: it is unreachable in this operator. Kopf reads the
# previous essence from the diffbase storage (processing.py), and
# _NoWriteDiffBaseStorage.fetch() always returns None, so every event takes the
# `old is None -> Reason.CREATE` branch — including pre-existing namespaces on
# the initial listing, which kopf explicitly documents as "creation never mixes
# with resuming". This handler therefore fires for every namespace at startup,
# which is why it must not deploy unpaced.
@kopf.on.create("", "v1", "namespaces")
def on_namespace_create(body: dict, meta: kopf.Meta, **kwargs) -> None:
    """Deploy a checker for a namespace, unless a paced rollout is under way."""
    namespace = meta.name
    labels = meta.labels or {}

    policy = _get_effective_policy(namespace)

    if not _should_audit(namespace, labels, policy):
        return

    availability = policy.get("availability", {})
    interval = availability.get("intervalMinutes", 30)
    cred_source = policy.get("credentialSource", {}).get("type", "pullSecret")

    # blocking=False: kopf fires this concurrently for every namespace on
    # startup, so all but one return immediately instead of bursting. Whatever
    # is skipped here is deployed by the next reconcile pass.
    deploy_checker_serialised(
        namespace,
        blocking=False,
        check_interval_minutes=interval,
        credential_source=cred_source,
        secret_names=get_secret_names_for_namespace(namespace),
    )


@kopf.on.delete("", "v1", "namespaces", optional=True)
def on_namespace_delete(body: dict, meta: kopf.Meta, **kwargs) -> None:
    """Tear down checker when a namespace is deleted."""
    teardown_checker(meta.name)
