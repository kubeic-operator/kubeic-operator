import logging
import os
import threading
import time
from datetime import datetime, timezone

import kopf
from kubernetes import client, config as k8s_config
from prometheus_client import start_http_server

# Imported for the side effect, which is the whole point: importing the package
# runs the @kopf.on.* decorators, and `kopf run` watches only what is registered
# by the time this module — its entrypoint — finishes loading. Deleting this line
# silently stops every handler from ever firing, with no error anywhere.
# See kubeic_operator/handlers/__init__.py.
from kubeic_operator import handlers  # noqa: F401
from kubeic_operator.checks.prerelease import check_prerelease, filter_violations
from kubeic_operator.checks.spread import aggregate_version_spread
from kubeic_operator.metrics import (
    kube_image_checker_reconcile_failures_total,
    update_prerelease_metrics,
    update_spread_metrics,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("kubeic-operator")

METRICS_PORT = int(os.environ.get("METRICS_PORT", "9090"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "300"))


class _NoWriteProgressStorage(kopf.ProgressStorage):
    """Progress storage that keeps state in memory and writes nothing to Kubernetes.

    Kopf's default storage writes annotations and finalizers to watched objects,
    requiring namespaces/patch permissions we deliberately do not grant. Namespace
    handlers are idempotent so losing state on restart is acceptable.

    Methods must be plain (non-async) — Kopf calls them synchronously.
    """

    def fetch(self, **kwargs):
        return None

    def store(self, **kwargs):
        pass

    def purge(self, **kwargs):
        pass

    def touch(self, **kwargs):
        pass

    def clear(self, *, essence):
        # clear() is a transform, not a void method: kopf feeds both sides of
        # its diff through it (processing.py) and expects the essence back with
        # any progress annotations stripped. Returning None nulled `new` as well
        # as `old`, which meant diffbase_storage.store() was never reached —
        # it is guarded by `if cause.new is not None`. That defeated diff-base
        # tracking entirely, whatever the diff-base storage did. See #65.
        #
        # We write no progress annotations, so there is nothing to strip; defer
        # to the base implementation, which deep-copies so callers cannot mutate
        # a stored essence.
        return super().clear(essence=essence)


class _InMemoryDiffBaseStorage(kopf.DiffBaseStorage):
    """Diff-base storage kept in this process rather than on the object.

    Kopf's default AnnotationsDiffBaseStorage records the last-handled essence
    as an annotation on every watched object, which needs the namespace patch
    permission this operator deliberately does not hold.

    Returning None unconditionally — the previous behaviour — is not a neutral
    stand-in for that. Kopf reads "no stored essence" as `Reason.CREATE`
    (causes.py: `if old is None`), so *every* namespace event re-ran the create
    handler: a label edit, an annotation change, the initial listing. Storing
    the essence in memory restores real CREATE/UPDATE/NOOP detection without
    needing to write to the object.

    Losing this on restart is deliberate and safe. Bootstrap and reconcile
    converge every namespace anyway, and the create handler is idempotent and
    rollout-serialised. Note that it also means RESUME stays unreachable: a
    fresh process has no essence for pre-existing namespaces, so they are still
    classified as CREATE on the first listing after a restart.

    Methods must be plain (non-async) — Kopf calls them synchronously.
    """

    def __init__(self) -> None:
        super().__init__()
        # Plain dict: get/set/pop are atomic under the GIL, which is enough for
        # the concurrent handler invocations kopf makes.
        self._essences: dict[str, object] = {}

    @staticmethod
    def _uid(body) -> str | None:
        return (body.get("metadata") or {}).get("uid")

    def fetch(self, *, body):
        uid = self._uid(body)
        return self._essences.get(uid) if uid else None

    def store(self, *, body, patch, essence):
        uid = self._uid(body)
        if uid:
            self._essences[uid] = essence

    def retain(self, live_uids) -> int:
        """Drop essences for objects that no longer exist, returning the count.

        Kopf offers no eviction hook on this interface, so reconcile prunes
        against the namespace list it already fetches. Without this the dict
        grows for the operator's whole lifetime on clusters that churn
        namespaces, such as per-build CI namespaces.
        """
        stale = self._essences.keys() - set(live_uids)
        for uid in stale:
            self._essences.pop(uid, None)
        return len(stale)

    def __len__(self) -> int:
        return len(self._essences)


DIFFBASE_STORAGE = _InMemoryDiffBaseStorage()


def _get_default_policy() -> dict:
    """Read the cluster-defaults policy from the operator namespace."""
    operator_ns = os.environ.get("OPERATOR_NAMESPACE", "kubeic-operator")
    api = client.CustomObjectsApi()
    try:
        policy = api.get_namespaced_custom_object(
            "imageaudit.kubeic.io", "v1alpha1", operator_ns,
            "imageauditpolicies", "cluster-defaults",
        )
        return policy.get("spec", {})
    except client.ApiException:
        logger.debug("No cluster-defaults policy found, using built-in defaults")
        return {}


def _error_class(exc: Exception) -> str:
    """Whether a failure came from the API server or from our own code.

    An ApiException is an operational condition — a 403 from an admission
    webhook, a 409, a 503 — and retrying next pass is the right response. Any
    other exception reaching a reconcile handler is a defect in this operator
    and wants a human. Both stay non-fatal for the remaining namespaces; the
    label is what tells them apart.
    """
    return "api" if isinstance(exc, client.ApiException) else "internal"


def _record_failure(namespace: str, operation: str, exc: Exception) -> None:
    """Log with a traceback and count a reconcile failure."""
    logger.exception("Failed to %s checker in %s", operation, namespace)
    kube_image_checker_reconcile_failures_total.labels(
        namespace=namespace, operation=operation, error_class=_error_class(exc),
    ).inc()


def _failure_reason(exc: Exception) -> str:
    """Compact one-line description of an exception for the IAP status field.

    ApiException stringifies to a multi-line dump of headers and body, which
    would swamp the status object, so newlines are collapsed and the result
    truncated.
    """
    text = f"{type(exc).__name__}: {exc}".strip().replace("\n", " ")
    return text[:200]


def _not_audited_reason(labels: dict, policy: dict) -> str:
    """Why a namespace is not being audited, for the IAP status field."""
    from kubeic_operator.deployer import CHECKER_ENABLED

    if not CHECKER_ENABLED:
        return "checkers disabled"
    excluded_labels = policy.get("namespaceSelector", {}).get("excludeLabels", {})
    for key, value in excluded_labels.items():
        if labels.get(key) == value:
            return f"excluded by label {key}={value}"
    return "excluded"


def _central_checker_state(apps_v1) -> str:
    """State of the Helm-owned central checker Deployment: ready, unavailable or missing.

    Read rather than reconciled — the operator deliberately does not own this
    Deployment. Reporting it is still worth a call per pass: in central mode
    every namespace's coverage depends on this one pod, so without it the IAP
    status could only say "no checker here" for the whole cluster.
    """
    from kubeic_operator.deployer import CENTRAL_CHECKER_DEPLOYMENT

    operator_ns = os.environ.get("OPERATOR_NAMESPACE", "kubeic-operator")
    try:
        deployment = apps_v1.read_namespaced_deployment(CENTRAL_CHECKER_DEPLOYMENT, operator_ns)
    except client.ApiException as exc:
        if exc.status != 404:
            logger.warning("Cannot read central checker Deployment %s: %s", CENTRAL_CHECKER_DEPLOYMENT, exc)
            return "unavailable"
        logger.warning(
            "Central mode is enabled but Deployment %s/%s does not exist; no images are being audited",
            operator_ns, CENTRAL_CHECKER_DEPLOYMENT,
        )
        return "missing"
    return "ready" if (deployment.status.available_replicas or 0) > 0 else "unavailable"


def _central_namespace_status(
    audited: bool, labels: dict, policy: dict, central_state: str, grant_error: str | None,
) -> dict:
    """Per-namespace IAP status when one cluster-wide checker covers every namespace.

    `deployed` keeps meaning "this namespace is being audited", so here it
    tracks the single central Deployment rather than a checker of the
    namespace's own. Reporting it per namespace rather than once keeps the
    status field's shape identical across modes, so anything reading it does
    not need to know which mode the cluster is in.
    """
    if not audited:
        return {"deployed": False, "reason": _not_audited_reason(labels, policy)}
    if grant_error:
        # The checker is running but cannot read this namespace's pull secrets,
        # so private images here will read as unavailable. Not "deployed".
        return {"deployed": False, "reason": f"secret grant failed: {grant_error}"}
    if central_state == "missing":
        return {"deployed": False, "reason": "central checker Deployment not found"}
    if central_state != "ready":
        return {"deployed": False, "reason": "central checker unavailable"}
    return {"deployed": True, "reason": "audited by central checker"}


def _bootstrap_checkers() -> None:
    from kubeic_operator.deployer import (
        CENTRAL_MODE, CHECKER_ENABLED, deploy_checker_serialised, get_secret_names_for_namespace,
    )
    from kubeic_operator.handlers.namespace import _should_deploy_checker, _get_effective_policy

    if not CHECKER_ENABLED:
        logger.info("Checkers disabled (checker.enabled=false); skipping bootstrap")
        return

    if CENTRAL_MODE:
        # Nothing to pace: there are no per-namespace checker pods to create.
        # Reconcile, which runs immediately after this, converges the central
        # checker's per-namespace secret grants and drains any checkers left
        # over from perNamespace mode.
        logger.info("Central mode (checker.mode=central); skipping per-namespace bootstrap")
        return

    v1 = client.CoreV1Api()
    try:
        namespaces = v1.list_namespace().items
    except client.ApiException as exc:
        logger.error("Failed to list namespaces during bootstrap: %s", exc)
        return

    for ns in namespaces:
        name = ns.metadata.name
        labels = ns.metadata.labels or {}
        policy = _get_effective_policy(name)
        if not _should_deploy_checker(name, labels, policy):
            continue
        interval = policy.get("availability", {}).get("intervalMinutes", 30)
        cred_source = policy.get("credentialSource", {}).get("type", "pullSecret")
        try:
            # blocking=True: wait our turn and then for the rollout, so the
            # whole bootstrap is one checker at a time.
            deploy_checker_serialised(name, blocking=True,
                                      check_interval_minutes=interval, credential_source=cred_source,
                                      secret_names=get_secret_names_for_namespace(name))
            logger.info("Bootstrapped checker in namespace %s", name)
        except Exception as exc:
            _record_failure(name, "deploy", exc)


def _reconcile_checkers() -> dict:
    """Ensure checker state matches desired state for all namespaces.

    Returns a dict of namespace -> {deployed, reason} for status reporting.
    """
    from kubeic_operator.deployer import (
        CENTRAL_MODE, CHECKER_DEPLOYMENT, CHECKER_ENABLED, deploy_checker_serialised,
        ensure_central_secret_access, teardown_central_secret_access,
        teardown_checker_serialised,
        get_secret_names_for_namespace,
    )
    from kubeic_operator.handlers.namespace import (
        _should_audit, _should_deploy_checker, _get_effective_policy,
    )

    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    try:
        namespaces = v1.list_namespace().items
    except client.ApiException as exc:
        logger.error("Failed to list namespaces during reconcile: %s", exc)
        return {}

    # Kopf has no eviction hook on the diff-base interface, so prune here
    # against the namespace list this pass already fetched.
    evicted = DIFFBASE_STORAGE.retain({ns.metadata.uid for ns in namespaces})
    if evicted:
        logger.debug("Evicted %d diff-base entries for deleted namespaces", evicted)

    # One probe per pass, not per namespace: in central mode every namespace's
    # coverage comes from this single Deployment.
    central_state = _central_checker_state(apps_v1) if CENTRAL_MODE and CHECKER_ENABLED else "missing"

    namespace_status = {}
    for ns in namespaces:
        name = ns.metadata.name
        labels = ns.metadata.labels or {}
        policy = _get_effective_policy(name)
        audited = _should_audit(name, labels, policy)
        should = _should_deploy_checker(name, labels, policy)

        # Central mode binds the one checker into each audited namespace so it
        # can read that namespace's pull secrets, and unbinds it everywhere
        # else. Done before the pod bookkeeping below because the two are
        # independent: during a switch from perNamespace the grants need to
        # exist while the old checkers are still draining.
        grant_error = None
        if CENTRAL_MODE:
            try:
                if audited:
                    ensure_central_secret_access(name, get_secret_names_for_namespace(name))
                elif teardown_central_secret_access(name):
                    logger.info("Reconciled: revoked central checker secret access to %s", name)
            except Exception as exc:
                _record_failure(name, "secret-grant", exc)
                grant_error = _failure_reason(exc)

        checker_exists = False
        try:
            apps_v1.read_namespaced_deployment(CHECKER_DEPLOYMENT, name)
            checker_exists = True
        except client.ApiException as exc:
            if exc.status != 404:
                # Previously this re-raised, which escaped the loop entirely:
                # one namespace answering 403 or 503 left every namespace after
                # it unreconciled, and aborted the pass before the status was
                # written, so the staleness was silent. Skip this namespace and
                # let the next pass retry it.
                _record_failure(name, "probe", exc)
                # In central mode this probe only looks for a leftover
                # perNamespace checker, so failing it says nothing about whether
                # the namespace is being audited — the central checker answers
                # that. The failure is still counted above.
                namespace_status[name] = (
                    _central_namespace_status(audited, labels, policy, central_state, grant_error)
                    if CENTRAL_MODE
                    else {"deployed": False, "reason": f"state unknown: {_failure_reason(exc)}"}
                )
                continue

        if should and not checker_exists:
            interval = policy.get("availability", {}).get("intervalMinutes", 30)
            cred_source = policy.get("credentialSource", {}).get("type", "pullSecret")
            try:
                # Paced for the same reason as bootstrap: on a fresh cluster
                # reconcile is the one creating every checker.
                deploy_checker_serialised(name, blocking=True,
                                          check_interval_minutes=interval,
                                          credential_source=cred_source,
                                          secret_names=get_secret_names_for_namespace(name))
                logger.info("Reconciled: deployed checker in %s", name)
                namespace_status[name] = {"deployed": True}
            except Exception as exc:
                # Status is written inside the branches so it records the
                # outcome rather than the intent. Reporting deployed: true here
                # made a namespace that failed every pass indistinguishable
                # from a healthy one.
                _record_failure(name, "deploy", exc)
                namespace_status[name] = {
                    "deployed": False,
                    "reason": f"deploy failed: {_failure_reason(exc)}",
                }
        elif not should and checker_exists:
            try:
                # Serialised so a mass teardown (checker.enabled: false turns
                # every namespace off at once) cannot interleave with a deploy
                # still in flight from the namespace handler.
                teardown_checker_serialised(name, blocking=True)
                logger.info("Reconciled: removed checker from %s", name)
            except Exception as exc:
                _record_failure(name, "teardown", exc)
                # The checker is still there, so deployed stays true.
                namespace_status[name] = {
                    "deployed": True,
                    "reason": f"teardown failed: {_failure_reason(exc)}",
                }
                continue
            namespace_status[name] = {
                "deployed": False,
                "reason": "central mode" if CENTRAL_MODE else _not_audited_reason(labels, policy),
            }
        elif should:
            namespace_status[name] = {"deployed": True}

        # In central mode the branches above only ever drain checkers left from
        # perNamespace mode, so whatever they concluded describes a pod that is
        # meant to be gone. The namespace's real coverage is the central
        # checker, so that is what the status reports.
        if CENTRAL_MODE:
            namespace_status[name] = _central_namespace_status(
                audited, labels, policy, central_state, grant_error,
            )

    return namespace_status


def _write_iap_status(namespace_status: dict) -> None:
    """Patch the cluster-defaults IAP status with reconcile results."""
    operator_ns = os.environ.get("OPERATOR_NAMESPACE", "kubeic-operator")
    api = client.CustomObjectsApi()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "status": {
            "lastReconcileTime": now,
            "namespaces": namespace_status,
        },
    }
    try:
        api.patch_namespaced_custom_object_status(
            "imageaudit.kubeic.io", "v1alpha1", operator_ns,
            "imageauditpolicies", "cluster-defaults", body,
        )
    except client.ApiException as exc:
        logger.warning("Failed to write IAP status: %s", exc)


def _run_cluster_audit() -> None:
    v1 = client.CoreV1Api()
    try:
        pods = v1.list_pod_for_all_namespaces()
    except client.ApiException as exc:
        logger.error("Failed to list pods: %s", exc)
        return

    pod_list = []
    for pod in pods.items:
        # Terminated pods (Succeeded/Failed) never pull again and lingering
        # ones (e.g. dead CI job pods) skew prerelease/spread with images
        # nothing is running.
        if pod.status.phase in ("Succeeded", "Failed"):
            continue
        pod_list.append({
            "metadata": {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "creationTimestamp": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else "",
                "annotations": pod.metadata.annotations or {},
            },
            "status": {
                "startTime": pod.status.start_time.isoformat() if pod.status.start_time else "",
            },
            "spec": {
                "containers": [{"name": c.name, "image": c.image} for c in (pod.spec.containers or [])],
                "initContainers": [{"name": c.name, "image": c.image} for c in (pod.spec.init_containers or [])],
            },
        })

    policy = _get_default_policy()
    max_age_days = policy.get("prerelease", {}).get("maxAgeDays", 7)
    stable_suffixes = policy.get("prerelease", {}).get("stableSuffixes")
    skip_annotation = policy.get("skipAnnotation") or None
    spread_threshold = policy.get("versionSpread", {}).get("threshold", 3)

    prerelease_findings = check_prerelease(pod_list, max_age_days=max_age_days, stable_suffixes=stable_suffixes, skip_annotation=skip_annotation)
    violations = filter_violations(prerelease_findings, max_age_days=max_age_days)
    update_prerelease_metrics(prerelease_findings, violations=violations)
    if violations:
        logger.warning("Found %d pre-release violations (max_age=%dd)", len(violations), max_age_days)

    spread_findings = aggregate_version_spread(pod_list, threshold=spread_threshold, skip_annotation=skip_annotation)
    update_spread_metrics(spread_findings)
    spread_violations = [f for f in spread_findings if f.violates_threshold]
    if spread_violations:
        logger.warning("Found %d version spread violations (threshold=%d)", len(spread_violations), spread_threshold)

    logger.info("Cluster audit complete: %d pre-release findings, %d spread findings",
                len(prerelease_findings), len(spread_findings))


def _audit_loop() -> None:
    # Publish the cluster-wide metrics first. The pre-release and version-spread
    # checks need nothing from the checkers, and the paced rollout below can take
    # many minutes on a large cluster — running it first would leave the operator
    # exporting no metrics for that whole window.
    try:
        _run_cluster_audit()
    except Exception:
        logger.exception("Initial cluster audit failed")

    # Bootstrap runs here rather than in on_startup because it is now paced:
    # readiness-gating 50+ namespaces takes minutes, and kopf does not begin
    # watching anything until the startup handler returns. Blocking there would
    # leave the operator blind to namespace events for the whole rollout.
    try:
        _bootstrap_checkers()
    except Exception:
        logger.exception("Checker bootstrap failed")
    try:
        namespace_status = _reconcile_checkers()
        if namespace_status:
            _write_iap_status(namespace_status)
    except Exception:
        logger.exception("Initial checker reconciliation failed")

    while True:
        time.sleep(SCAN_INTERVAL)
        try:
            _run_cluster_audit()
        except Exception:
            logger.exception("Cluster audit failed")
        try:
            namespace_status = _reconcile_checkers()
            if namespace_status:
                _write_iap_status(namespace_status)
        except Exception:
            logger.exception("Checker reconciliation failed")


@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, **kwargs):
    # Kopf defaults to writing progress state as annotations on watched objects
    # and adding finalizers to track in-flight handlers. Both require namespace
    # patch permissions we intentionally don't hold. In-memory storage is fine
    # for namespace handlers — they complete quickly and are idempotent on retry.
    settings.persistence.progress_storage = _NoWriteProgressStorage()
    settings.persistence.diffbase_storage = DIFFBASE_STORAGE
    settings.persistence.finalizer = None

    start_http_server(METRICS_PORT)
    logger.info("Prometheus metrics server started on port %d", METRICS_PORT)
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    # The audit thread owns bootstrap and reconcile. Returning promptly is what
    # lets kopf start watching namespaces while the paced rollout is still
    # working through the cluster.
    threading.Thread(target=_audit_loop, daemon=True, name="audit-loop").start()
