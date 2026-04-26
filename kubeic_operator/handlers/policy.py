import logging

import kopf

logger = logging.getLogger("kubeic-operator.handlers.policy")

GROUP = "imageaudit.kubeic.io"
VERSION = "v1alpha1"
PLURAL = "imageauditpolicies"


def _reconcile_after_policy_change() -> None:
    """Reconcile checker state across all namespaces after a policy change."""
    from kubeic_operator.main import _reconcile_checkers, _write_iap_status

    namespace_status = _reconcile_checkers()
    if namespace_status:
        _write_iap_status(namespace_status)


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
def on_policy_change(body: dict, meta: kopf.Meta, **kwargs) -> None:
    """Reconcile all namespaces when a policy is created or updated."""
    _reconcile_after_policy_change()


@kopf.on.delete(GROUP, VERSION, PLURAL)
def on_policy_delete(body: dict, meta: kopf.Meta, **kwargs) -> None:
    """Handle policy deletion. If cluster-defaults is removed, redeploy with defaults."""
    _reconcile_after_policy_change()
