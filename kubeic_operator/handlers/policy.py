import logging

import kopf
from kubernetes import client

from kubeic_operator.deployer import deploy_checker, get_secret_names_for_namespace

logger = logging.getLogger("kubeic-operator.handlers.policy")

GROUP = "imageaudit.kubeic.io"
VERSION = "v1alpha1"
PLURAL = "imageauditpolicies"


def _reconcile_all_namespaces() -> None:
    """Re-deploy checkers across all audited namespaces after a policy change."""
    v1 = client.CoreV1Api()
    namespaces = v1.list_namespace()

    for ns in namespaces.items:
        name = ns.metadata.name
        labels = ns.metadata.labels or {}

        # Skip system namespaces
        from kubeic_operator.handlers.namespace import _should_audit
        from kubeic_operator.handlers.namespace import _get_effective_policy

        policy = _get_effective_policy(name)
        if not _should_audit(name, labels, policy):
            continue

        availability = policy.get("availability", {})
        interval = availability.get("intervalMinutes", 30)
        cred_source = policy.get("credentialSource", {}).get("type", "pullSecret")

        deploy_checker(
            namespace=name,
            check_interval_minutes=interval,
            credential_source=cred_source,
            secret_names=get_secret_names_for_namespace(name),
        )
        logger.info("Reconciled checker in %s after policy change", name)


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
def on_policy_change(body: dict, meta: kopf.Meta, **kwargs) -> None:
    """Reconcile all namespaces when a policy is created or updated."""
    _reconcile_all_namespaces()


@kopf.on.delete(GROUP, VERSION, PLURAL)
def on_policy_delete(body: dict, meta: kopf.Meta, **kwargs) -> None:
    """Handle policy deletion. If cluster-defaults is removed, redeploy with defaults."""
    _reconcile_all_namespaces()
