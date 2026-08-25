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
POD_PAGE_SIZE = int(os.environ.get("POD_PAGE_SIZE", "500"))


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


def _get_pods(namespace: str = "", *, page_size: int = POD_PAGE_SIZE) -> list[dict]:
    """List auditable pods, paginated. An empty namespace lists the whole cluster.

    Paginated rather than one call because an unpaginated cluster-wide list
    deserialises every pod at once, and CPython does not return freed heap to
    the OS — that transient peak would become the pod's resident floor for the
    rest of its life. Each page is trimmed and discarded, so only the small
    dicts accumulate.
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
    """
    config.load_incluster_config()
    secrets_client = client.CoreV1Api()

    scope = "the whole cluster" if CHECKER_MODE == "central" else f"namespace {NAMESPACE}"
    logger.info(
        "Starting checker for %s (mode=%s, target cycle=%ds)",
        scope, CHECKER_MODE, CHECK_INTERVAL,
    )

    while True:
        cycle_deadline = time.monotonic() + CHECK_INTERVAL
        try:
            pods = _get_pods("" if CHECKER_MODE == "central" else NAMESPACE)
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
                    cycle_deadline, len(plan) + len(_probeable_credentials(creds)),
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

        # Pacing spends most of the interval inside the sweep. Anything left —
        # an empty cluster, a very small namespace, or a cycle that failed
        # early — is slept out here so the cycle length stays the interval. A
        # sweep that overran leaves this negative and the next cycle starts at
        # once.
        time_left = cycle_deadline - time.monotonic()
        if time_left > 0:
            time.sleep(time_left)


if __name__ == "__main__":
    start_http_server(METRICS_PORT)
    logger.info("Metrics server started on port %d", METRICS_PORT)
    run_check_loop()
