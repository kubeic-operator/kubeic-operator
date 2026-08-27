import json
import logging
import os
import time
import tempfile

from kubernetes import client, config
from prometheus_client import start_http_server

from kubeic_checker.availability import check_availability, plan_inspections, write_auth_config
from kubeic_checker.credentials import (
    resolve_all_credentials, build_image_credentials, ResolvedCredential,
)
from kubeic_operator.metrics import update_availability_metrics, kube_image_credential_valid
from kubeic_operator.checks.prerelease import should_skip

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
SKIP_ANNOTATION = os.environ.get("SKIP_ANNOTATION", "")
# perNamespace (this checker owns one namespace) or central (one checker for the
# whole cluster). Only affects the scope of the pod list; everything downstream
# is already keyed by namespace.
CHECKER_MODE = os.environ.get("CHECKER_MODE", "perNamespace")
CENTRAL_MODE = CHECKER_MODE == "central"
POD_PAGE_SIZE = int(os.environ.get("POD_PAGE_SIZE", "500"))

# How long the FIRST sweep after startup may take, in seconds. Every later cycle
# is paced across the whole interval; this one is not.
#
# Metrics are published only when a sweep finishes, so pacing the first sweep
# like the rest would mean a restarted checker exports nothing for a full
# interval. That is far longer than any alert's `for`, so every firing alert
# resolves on restart and the downstream tickets close — which is exactly what a
# 30 minute blackout on sca1 did during the 0.3.0-alpha.8 rollout (#74).
#
# Deliberately a short pacing window rather than no pacing at all: running the
# first sweep flat out would reinstate the startup burst that #72 removed.
FIRST_CYCLE_SECONDS = int(os.environ.get("FIRST_CYCLE_SECONDS", "60"))


def _parse_excluded_namespaces() -> frozenset[str]:
    raw = os.environ.get("EXCLUDED_NAMESPACES", "")
    return frozenset(ns.strip() for ns in raw.split(",") if ns.strip())


def _parse_exclude_labels() -> dict[str, str]:
    """Namespace labels that opt a namespace out of auditing.

    Falls back to no exclusions on malformed input rather than raising: crashing
    the checker at import would stop all auditing, which is worse than auditing
    a namespace that asked not to be.
    """
    raw = os.environ.get("EXCLUDE_LABELS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse EXCLUDE_LABELS as JSON, ignoring namespace label exclusions")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("EXCLUDE_LABELS must be a JSON object, got %s", type(parsed).__name__)
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


# Both only apply in central mode. With one checker per namespace, an excluded
# namespace simply never gets a checker, so the exclusion is enforced by the
# operator not deploying anything. A central checker lists the whole cluster and
# has to apply the same rule itself, or switching mode would silently start
# auditing every namespace that had opted out.
EXCLUDED_NAMESPACES = _parse_excluded_namespaces()
EXCLUDE_LABELS = _parse_exclude_labels()

# Last successfully computed exclusion set, used only when a later namespace
# list fails. See _resolve_excluded_namespaces.
_last_excluded: frozenset[str] | None = None


def _pace_sleep(seconds: float) -> None:
    """The pacer's sleep, deliberately separate from the end-of-cycle sleep.

    Two distinct seams: tests neutralise this one to run a cycle instantly,
    while still using the cycle sleep to break out of the infinite loop. Calling
    time.sleep through the module rather than binding it as a default argument
    also keeps it patchable — a default would capture the real function at
    import.
    """
    time.sleep(seconds)


class _Pacer:
    """Spreads a known quantity of work evenly across the time left in a cycle.

    The checker used to do all its work back to back and then sleep out the
    interval. Per namespace that is a few seconds of burst against a long idle;
    for one checker covering a whole cluster it is many minutes of solid skopeo
    followed by an equally long idle, on a single pod.

    Pacing instead of batching also removes the reason to run skopeo
    concurrently. Workers would have multiplied peak memory by the number of
    them — and memory is the entire remaining argument for a central checker,
    so buying throughput with it would be self-defeating.

    Recomputed every call rather than fixed up front, so a slow item shortens
    the following gaps instead of overrunning. If the work outlasts the
    interval the delay simply reaches zero and the sweep runs flat out, which
    is the right degradation: it refreshes less often rather than overlapping
    with the next cycle.
    """

    def __init__(self, deadline: float, total: int) -> None:
        self._deadline = deadline
        self._remaining = max(total, 0)

    def __call__(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            return
        time_left = self._deadline - time.monotonic()
        if time_left <= 0:
            return
        _pace_sleep(time_left / self._remaining)


def _trim_pod(pod) -> dict | None:
    """Reduce an API pod to the fields the audit needs, or None to skip it.

    Terminated pods never pull their image again; auditing them makes no
    availability claim worth alerting on — and CI job pods carry ephemeral pull
    secrets whose tokens die with the job, so checking a Failed job pod produces
    a guaranteed-false auth_failure.
    """
    if pod.status.phase in ("Succeeded", "Failed"):
        return None
    return {
        "metadata": {
            "name": pod.metadata.name,
            # From the object, not from a caller-supplied value: a cluster-wide
            # list spans namespaces.
            "namespace": pod.metadata.namespace,
            "annotations": pod.metadata.annotations or {},
        },
        "spec": {
            "containers": [{"name": c.name, "image": c.image} for c in (pod.spec.containers or [])],
            "initContainers": [{"name": c.name, "image": c.image} for c in (pod.spec.init_containers or [])],
            "imagePullSecrets": [{"name": s.name} for s in (pod.spec.image_pull_secrets or [])],
        },
    }


def _resolve_excluded_namespaces() -> frozenset[str]:
    """Namespaces the central checker must not audit.

    Mirrors the operator's _should_audit: the static exclusion list, plus any
    namespace carrying one of the configured exclude labels. Reading the labels
    needs a namespace list, which is why the central checker's ClusterRole
    grants namespaces get/list.

    Namespace-scoped ImageAuditPolicy overrides are deliberately NOT consulted.
    The chart-level policy is the only source, so a namespace excluded solely by
    its own ImageAuditPolicy is still audited in central mode — there is no
    per-namespace checker left to configure from a per-namespace policy.

    A failed namespace list falls back to the previous cycle's answer, and
    raises if there is no previous answer, rather than quietly auditing
    namespaces that had opted out. The caller treats that as a failed cycle and
    retries on the next one.
    """
    global _last_excluded

    excluded = set(EXCLUDED_NAMESPACES)
    if not EXCLUDE_LABELS:
        return frozenset(excluded)

    try:
        namespaces = client.CoreV1Api().list_namespace()
    except Exception as exc:
        if _last_excluded is None:
            raise
        logger.warning(
            "Cannot list namespaces (%s); reusing the previous exclusion set of %d namespaces",
            type(exc).__name__, len(_last_excluded),
        )
        return _last_excluded

    for ns in namespaces.items:
        labels = ns.metadata.labels or {}
        for key, value in EXCLUDE_LABELS.items():
            if labels.get(key) == value:
                excluded.add(ns.metadata.name)
                break

    _last_excluded = frozenset(excluded)
    return _last_excluded


def _get_pods(
    namespace: str = "", *, page_size: int = POD_PAGE_SIZE, exclude: frozenset[str] = frozenset(),
) -> list[dict]:
    """List auditable pods, paginated. An empty namespace lists the whole cluster.

    Paginated rather than one call because an unpaginated cluster-wide list
    deserialises every pod at once, and CPython does not return freed heap to
    the OS — that transient peak would become the pod's resident floor for the
    rest of its life. Each page is trimmed and discarded, so only the small
    dicts accumulate.

    Excluded namespaces are dropped before trimming, so an opted-out namespace
    costs nothing beyond the bytes the API server already sent.
    """
    v1 = client.CoreV1Api()
    result: list[dict] = []
    continue_token = None

    while True:
        kwargs = {"limit": page_size}
        if continue_token:
            kwargs["_continue"] = continue_token
        if namespace:
            page = v1.list_namespaced_pod(namespace, **kwargs)
        else:
            page = v1.list_pod_for_all_namespaces(**kwargs)

        for pod in page.items:
            if pod.metadata.namespace in exclude:
                continue
            trimmed = _trim_pod(pod)
            if trimmed is not None:
                result.append(trimmed)

        # V1ListMeta._continue is a string or absent.
        continue_token = getattr(page.metadata, "_continue", None)
        if not continue_token:
            return result


def _probeable_credentials(creds: list[ResolvedCredential]) -> list[tuple[str, ResolvedCredential]]:
    """The credentials _check_credential_validity will actually probe.

    Deduplicated by namespace, registry and source, and excluding any that
    carry neither an auth blob nor a username and password — those are dropped
    without a skopeo call.

    Shared with the caller so the pacer is sized on the work that will really
    happen. Sizing it on the raw credential count makes every gap shorter than
    it should be, so the sweep finishes early and the cycle goes back to
    burst-then-idle for the part that was over-counted.
    """
    seen: set[str] = set()
    probeable: list[tuple[str, ResolvedCredential]] = []
    for cred in creds:
        namespace = cred.namespace or NAMESPACE
        key = f"{namespace}/{cred.registry}/{cred.source}"
        if key in seen:
            continue
        seen.add(key)
        if not cred.auth and not (cred.username and cred.password):
            continue
        probeable.append((namespace, cred))
    return probeable


def _check_credential_validity(
    creds: list[ResolvedCredential], pods: list[dict], pacer=None,
) -> None:
    """Test each credential using repo-level list-tags to verify auth access.

    Only marks credentials invalid on authentication failures.
    Missing images or network errors do not affect credential validity.

    Namespace comes from each credential rather than from an ambient value, so
    the same secret name in two namespaces is tested — and reported — twice
    rather than collapsing into one result.

    Results are collected and published in a single clear-then-set at the end.
    Clearing up front and setting each series as its probe finished was fine
    when the sweep was instantaneous, but under pacing it leaves the series
    absent from /metrics for most of the cycle. Prometheus treats an absent
    series as stale, so RegistryCredentialInvalid — critical, with a `for` of
    interval + 10m — would have its pending timer reset every cycle and could
    never fire.
    """
    from kubeic_checker.availability import _run_skopeo_list_tags, _run_skopeo_inspect
    from kubeic_checker.credentials import registry_from_image

    # (namespace, secret_name) -> images used by pods in that namespace which
    # reference that secret. Keyed by namespace too, or a repo path from one
    # namespace would be used to probe another namespace's credential.
    secret_images: dict[tuple[str, str], set[str]] = {}
    for pod in pods:
        pod_ns = pod["metadata"]["namespace"]
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
                secret_images.setdefault((pod_ns, secret_name), set()).update(images)

    samples: list[tuple[dict, int]] = []
    for namespace, cred in _probeable_credentials(creds):
        auth_data = {}
        host = cred.registry.split("/")[0]
        if cred.auth:
            auth_data[host] = {"auth": cred.auth}
        else:
            import base64
            token = base64.b64encode(f"{cred.username}:{cred.password}".encode()).decode()
            auth_data[host] = {"auth": token}

        auth_fd, auth_path = tempfile.mkstemp(suffix=".json", prefix="cred-check-")
        os.close(auth_fd)
        try:
            write_auth_config(auth_data, auth_path)

            secret_name = cred.source.split(":")[-1] if ":" in cred.source else cred.source

            # Find a matching image from pods to get the repo path
            pod_images = secret_images.get((namespace, secret_name), set())
            cred_host = cred.registry.split("/")[0]
            matching = [
                img for img in pod_images if registry_from_image(img) == cred_host
            ]

            if CREDENTIAL_TEST_IMAGE:
                # Fallback: use configured test image
                ok, _, err_class = _run_skopeo_list_tags(CREDENTIAL_TEST_IMAGE, auth_file=auth_path)
                if err_class == "auth_failure":
                    valid = False
                elif ok:
                    valid = True
                else:
                    # Can't determine from list-tags, fall back to inspect
                    _, _, _, inspect_err = _run_skopeo_inspect(CREDENTIAL_TEST_IMAGE, auth_file=auth_path)
                    valid = inspect_err != "auth_failure"
            elif matching:
                # Use list-tags on the repo to verify credential access
                ok, _, err_class = _run_skopeo_list_tags(matching[0], auth_file=auth_path)
                if err_class == "auth_failure":
                    valid = False
                elif ok:
                    valid = True
                else:
                    # Network/unknown error from list-tags — fall back to inspect
                    # to try to determine if it's an auth issue
                    valid = any(
                        _run_skopeo_inspect(img, auth_file=auth_path)[3] != "auth_failure"
                        for img in matching
                    )
            else:
                valid = False
        finally:
            try:
                os.unlink(auth_path)
            except OSError:
                pass

        samples.append((
            {
                "registry": cred.registry.split("/")[0],
                "namespace": namespace,
                "secret_name": secret_name,
            },
            1 if valid else 0,
        ))

        if pacer is not None:
            pacer()

    # Atomic swap: the previous cycle's series stay visible right up until the
    # new set replaces them.
    kube_image_credential_valid.clear()
    for labels, value in samples:
        kube_image_credential_valid.labels(**labels).set(value)


def run_check_loop():
    """Main checker loop: resolve creds, check availability, expose metrics.

    Work inside a cycle is paced to fill the interval rather than run flat out
    and then idle, so one checker covering a whole cluster presents a level load
    instead of a long burst. Metrics are still published once per cycle, by
    clear-and-repopulate, so a scrape never sees a half-built set.

    The trade is staleness: results are now gathered across the cycle instead of
    at a single instant, so the oldest data point can be a full interval older
    at publish time than it was before. Halving intervalMinutes restores the old
    latency while keeping the level load.

    The first sweep after startup is the exception, paced against a much shorter
    deadline. Results are only published once a sweep completes, so pacing it
    like the rest leaves a restarted checker exporting nothing for a whole
    interval — long enough for every alert built on these metrics to resolve
    (#74).
    """
    config.load_incluster_config()
    secrets_client = client.CoreV1Api()

    scope = "the whole cluster" if CENTRAL_MODE else f"namespace {NAMESPACE}"
    logger.info(
        "Starting checker for %s (mode=%s, target cycle=%ds)",
        scope, CHECKER_MODE, CHECK_INTERVAL,
    )
    if CENTRAL_MODE:
        logger.info(
            "Excluding %d named namespaces and any namespace labelled %s",
            len(EXCLUDED_NAMESPACES), EXCLUDE_LABELS or "(none configured)",
        )

    first_cycle = True
    while True:
        cycle_deadline = time.monotonic() + CHECK_INTERVAL
        # The pacer's deadline is not the cycle's. Every cycle still lasts a full
        # interval — the sleep at the bottom sees to that — but the first sweep
        # is compressed so there is something to scrape within a minute of
        # startup rather than half an hour.
        pace_deadline = (
            time.monotonic() + min(FIRST_CYCLE_SECONDS, CHECK_INTERVAL)
            if first_cycle else cycle_deadline
        )
        try:
            if CENTRAL_MODE:
                # Recomputed every cycle: namespaces are created, deleted and
                # relabelled while this pod lives, and a 30 minute cycle is long
                # enough for a fresh opt-out to matter.
                pods = _get_pods("", exclude=_resolve_excluded_namespaces())
            else:
                pods = _get_pods(NAMESPACE)
            if not pods:
                logger.info("No pods found in %s", scope)
            else:
                # Filter out pods annotated to skip availability/digest/credentials
                if SKIP_ANNOTATION:
                    auditable_pods = [
                        p for p in pods
                        if not should_skip(p, SKIP_ANNOTATION, "availability")
                    ]
                else:
                    auditable_pods = pods

                creds = resolve_all_credentials(auditable_pods, secrets_client, CREDENTIAL_SOURCE)
                # Per-image credential candidates (kubelet semantics): each
                # image is checked with the pull secrets its own pods reference,
                # tried in order on auth failure — never a single merged
                # last-wins credential per registry host.
                image_creds = build_image_credentials(auditable_pods, creds)

                # Size the whole cycle before starting it so both halves are
                # paced against one deadline; otherwise the credential checks
                # would burst at the end of every cycle.
                plan = plan_inspections(auditable_pods, image_creds)
                pacer = _Pacer(
                    pace_deadline, len(plan) + len(_probeable_credentials(creds)),
                )

                results = check_availability(
                    auditable_pods, image_creds=image_creds, pacer=pacer,
                )
                update_availability_metrics(results)
                _check_credential_validity(creds, auditable_pods, pacer=pacer)

                unavailable = [r for r in results if not r.available]
                digest_mismatches = [r for r in results if r.digest_match is False]
                if unavailable:
                    logger.warning(
                        "%d/%d images unavailable in %s",
                        len(unavailable), len(results), scope,
                    )
                    seen_errors: set[tuple[str, str]] = set()
                    for r in unavailable:
                        key = (r.image, r.error_class or "unknown")
                        if key in seen_errors:
                            continue
                        seen_errors.add(key)
                        logger.warning(
                            "Image unavailable: image=%s error_class=%s error=%s",
                            r.image, r.error_class or "unknown",
                            (r.error or "").strip().replace("\n", " ")[:200],
                        )
                if digest_mismatches:
                    for r in digest_mismatches:
                        logger.warning(
                            "Digest mismatch for %s in %s/%s: pinned=%s registry=%s",
                            r.image, r.namespace, r.pod,
                            r.pinned_digest, r.registry_digest,
                        )
                if not unavailable and not digest_mismatches:
                    logger.info("All %d images available in %s", len(results), scope)

        except Exception as e:
            logger.error("Check cycle failed: %s", e)

        # Cleared even when the cycle raised. A failed first sweep has already
        # cost the fast-publish window, and repeating it every cycle would mean
        # a checker that fails once never paces again.
        first_cycle = False

        # Pacing spends most of the interval inside the sweep. Anything left —
        # an empty cluster, a very small namespace, a compressed first sweep, or
        # a cycle that failed early — is slept out here so the cycle length
        # stays the interval. A sweep that overran leaves this negative and the
        # next cycle starts at once.
        time_left = cycle_deadline - time.monotonic()
        if time_left > 0:
            time.sleep(time_left)


if __name__ == "__main__":
    start_http_server(METRICS_PORT)
    logger.info("Metrics server started on port %d", METRICS_PORT)
    run_check_loop()
