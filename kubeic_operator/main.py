import logging
import os
import threading
import time

import kopf
from kubernetes import client, config as k8s_config
from prometheus_client import start_http_server

from kubeic_operator.checks.prerelease import check_prerelease, filter_violations
from kubeic_operator.checks.spread import aggregate_version_spread
from kubeic_operator.metrics import update_prerelease_metrics, update_spread_metrics

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

    def clear(self, **kwargs):
        pass


class _NoWriteDiffBaseStorage(kopf.DiffBaseStorage):
    """Diff-base storage that discards state and writes nothing to Kubernetes.

    Kopf's default AnnotationsDiffBaseStorage patches watched objects to record
    the last-seen state for change detection. Our namespace handlers don't use
    on.update, so diff-base tracking is unnecessary and the patch permission
    is deliberately withheld.

    Methods must be plain (non-async) — Kopf calls them synchronously.
    """

    def fetch(self, **kwargs):
        return None

    def store(self, **kwargs):
        pass


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


def _bootstrap_checkers() -> None:
    from kubeic_operator.deployer import deploy_checker, get_secret_names_for_namespace
    from kubeic_operator.handlers.namespace import _should_audit, _get_effective_policy

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
        if not _should_audit(name, labels, policy):
            continue
        interval = policy.get("availability", {}).get("intervalMinutes", 30)
        cred_source = policy.get("credentialSource", {}).get("type", "pullSecret")
        try:
            deploy_checker(namespace=name,
                           check_interval_minutes=interval, credential_source=cred_source,
                           secret_names=get_secret_names_for_namespace(name))
            logger.info("Bootstrapped checker in namespace %s", name)
        except Exception as exc:
            logger.error("Failed to bootstrap checker in %s: %s", name, exc)


def _run_cluster_audit() -> None:
    v1 = client.CoreV1Api()
    try:
        pods = v1.list_pod_for_all_namespaces()
    except client.ApiException as exc:
        logger.error("Failed to list pods: %s", exc)
        return

    pod_list = []
    for pod in pods.items:
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
    update_prerelease_metrics(prerelease_findings, violation_count=len(violations))
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
    while True:
        time.sleep(SCAN_INTERVAL)
        try:
            _run_cluster_audit()
        except Exception:
            logger.exception("Cluster audit failed")


@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, **kwargs):
    # Kopf defaults to writing progress state as annotations on watched objects
    # and adding finalizers to track in-flight handlers. Both require namespace
    # patch permissions we intentionally don't hold. In-memory storage is fine
    # for namespace handlers — they complete quickly and are idempotent on retry.
    settings.persistence.progress_storage = _NoWriteProgressStorage()
    settings.persistence.diffbase_storage = _NoWriteDiffBaseStorage()
    settings.persistence.finalizer = None

    start_http_server(METRICS_PORT)
    logger.info("Prometheus metrics server started on port %d", METRICS_PORT)
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    threading.Thread(target=_audit_loop, daemon=True, name="audit-loop").start()
    _bootstrap_checkers()
