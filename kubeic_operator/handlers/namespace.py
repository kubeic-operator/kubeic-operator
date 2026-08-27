import logging

import kopf
from kubernetes import client

from kubeic_operator.deployer import (
    CENTRAL_MODE,
    CHECKER_ENABLED,
    EXCLUDED_NAMESPACES,
    deploy_checker_serialised,
    ensure_central_secret_access,
    get_secret_names_for_namespace,
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
    """Whether this namespace's images should be audited at all.

    Independent of *how* they get audited. In central mode this still answers
    True for an ordinary namespace — the auditing is done by the one cluster-wide
    checker — and it is what decides whether that checker is granted access to
    the namespace's pull secrets.
    """
    if not CHECKER_ENABLED:
        return False

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


def _should_deploy_checker(namespace: str, labels: dict | None, policy: dict) -> bool:
    """Whether a per-namespace checker Deployment should exist in this namespace.

    Single choke point for "should a checker pod exist here", so neither
    disabling checkers nor switching to central mode needs a teardown path of
    its own: _reconcile_checkers already removes a checker wherever this returns
    False and one exists. Switching mode to central makes this False everywhere
    at once, and the next reconcile pass drains the cluster.
    """
    if CENTRAL_MODE:
        return False
    return _should_audit(namespace, labels, policy)


# No @kopf.on.resume here: it is unreachable in this operator. Kopf reads the
# previous essence from the diffbase storage (processing.py), and
# _InMemoryDiffBaseStorage starts empty in every fresh process, so on the first
# listing after a restart each pre-existing namespace still takes the
# `old is None -> Reason.CREATE` branch, which kopf documents as "creation never
# mixes with resuming". (Before #68 fetch() returned None *unconditionally*, so
# every later event took that branch too. That is fixed: a label edit is now an
# UPDATE, which nothing here claims.)
#
# So this handler fires once per namespace at startup, which is why it must not
# deploy unpaced. Note "fires", not "has fired": nothing in this module was
# imported at module scope until handlers/__init__.py started doing it, so the
# decorators never ran and no handler here has yet seen a real cluster.
@kopf.on.create("", "v1", "namespaces")
def on_namespace_create(body: dict, meta: kopf.Meta, **kwargs) -> None:
    """Deploy a checker for a namespace, unless a paced rollout is under way."""
    namespace = meta.name
    labels = meta.labels or {}

    policy = _get_effective_policy(namespace)

    if not _should_audit(namespace, labels, policy):
        return

    if CENTRAL_MODE:
        # No pod to deploy — the cluster-wide checker picks the namespace up on
        # its next sweep. It cannot read the namespace's pull secrets until it
        # is bound in, though, and waiting for the next reconcile pass to do
        # that would make a new namespace's private images look unavailable for
        # up to a full scan interval.
        ensure_central_secret_access(namespace, get_secret_names_for_namespace(namespace))
        return

    availability = policy.get("availability", {})
    interval = availability.get("intervalMinutes", 30)
    cred_source = policy.get("credentialSource", {}).get("type", "pullSecret")

    # blocking=False: kopf fires this concurrently for every namespace on
    # startup, so all but one return immediately instead of bursting. Whatever
    # is skipped here is deployed by the next reconcile pass. The guard is a
    # non-blocking Lock.acquire and so is sound by construction, but see the
    # note above — it has never been exercised against a real cluster.
    deploy_checker_serialised(
        namespace,
        blocking=False,
        check_interval_minutes=interval,
        credential_source=cred_source,
        secret_names=get_secret_names_for_namespace(namespace),
    )


# No delete handler. There was one, and it could never have fired: kopf reaches
# a DELETE cause only while its own finalizer is still on the object
# (causes.py — `deletion_is_ongoing and not deletion_is_blocked` returns FREE
# first), and this operator places no finalizer. `optional=True` sets
# requires_finalizer=False (kopf/on.py), and on_startup sets
# settings.persistence.finalizer = None outright. A deleting namespace
# therefore resolves to Reason.FREE, which matches no handler.
#
# It cannot be made to work here either: blocking deletion needs a finalizer,
# which needs the namespaces patch permission this operator deliberately does
# not hold. Kubernetes garbage-collects everything in a deleted namespace
# regardless, which is what the handler's own docstring said, so nothing is
# lost by removing it.
