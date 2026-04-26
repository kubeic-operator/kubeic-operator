from prometheus_client import Gauge

# --- Operator metrics (cluster-wide) ---

kube_image_is_prerelease = Gauge(
    "kube_image_is_prerelease",
    "Whether the image tag is a pre-release version (0 or 1)",
    ["image", "registry", "image_name", "tag", "namespace", "pod", "container"],
)

kube_image_prerelease_age_days = Gauge(
    "kube_image_prerelease_age_days",
    "Age in days of a pod running a pre-release image",
    ["image", "registry", "image_name", "tag", "namespace", "pod", "container"],
)

kube_image_prerelease_violation = Gauge(
    "kube_image_prerelease_violation",
    "Whether this pre-release image exceeds the max age threshold (0 or 1)",
    ["registry", "image_name", "namespace", "pod", "container"],
)

kube_image_version_count = Gauge(
    "kube_image_version_count",
    "Number of distinct versions running for an image",
    ["registry", "image_name"],
)

kube_image_version_pod_count = Gauge(
    "kube_image_version_pod_count",
    "Number of pods running a specific image version",
    ["registry", "image_name", "tag", "namespace"],
)

kube_image_version_spread_violation = Gauge(
    "kube_image_version_spread_violation",
    "Whether this image exceeds the version spread threshold (0 or 1)",
    ["registry", "image_name"],
)

# --- Checker metrics (per-namespace) ---

kube_image_available = Gauge(
    "kube_image_available",
    "Whether the image is reachable in the registry (0 or 1)",
    ["image", "registry", "image_name", "namespace", "pod", "container"],
)

kube_image_credential_valid = Gauge(
    "kube_image_credential_valid",
    "Whether the registry credentials are valid (0 or 1)",
    ["registry", "namespace", "secret_name"],
)

kube_image_digest_match = Gauge(
    "kube_image_digest_match",
    "Whether the registry digest matches the pinned digest (1=match, 0=mismatch, absent=no digest pinned)",
    ["image", "registry", "image_name", "namespace", "pod", "container"],
)


def update_prerelease_metrics(findings: list, violations: list = None) -> None:
    """Update all pre-release Prometheus gauges from findings.

    Args:
        findings: List of PrereleaseFinding objects.
        violations: List of PrereleaseFinding objects exceeding the max age threshold.
    """
    kube_image_is_prerelease.clear()
    kube_image_prerelease_age_days.clear()
    kube_image_prerelease_violation.clear()

    for f in findings:
        labels = {
            "image": f.image,
            "registry": f.registry,
            "image_name": f.image_name,
            "tag": f.tag,
            "namespace": f.namespace,
            "pod": f.pod,
            "container": f.container,
        }
        kube_image_is_prerelease.labels(**labels).set(1)
        kube_image_prerelease_age_days.labels(**labels).set(f.age_days)

    for v in violations or []:
        kube_image_prerelease_violation.labels(
            registry=v.registry, image_name=v.image_name,
            namespace=v.namespace, pod=v.pod, container=v.container,
        ).set(1)


def update_spread_metrics(findings: list) -> None:
    """Update all version spread Prometheus gauges from findings.

    Args:
        findings: List of VersionSpreadFinding objects.
    """
    kube_image_version_count.clear()
    kube_image_version_pod_count.clear()
    kube_image_version_spread_violation.clear()

    for f in findings:
        kube_image_version_count.labels(
            registry=f.registry, image_name=f.image_name,
        ).set(f.version_count)
        kube_image_version_spread_violation.labels(
            registry=f.registry, image_name=f.image_name,
        ).set(1 if f.violates_threshold else 0)

        for tag, ns_counts in f.version_pod_counts.items():
            for ns, count in ns_counts.items():
                kube_image_version_pod_count.labels(
                    registry=f.registry, image_name=f.image_name,
                    tag=tag, namespace=ns,
                ).set(count)


def update_availability_metrics(results: list) -> None:
    """Update availability Prometheus gauges from check results.

    Args:
        results: List of AvailabilityResult objects.
    """
    kube_image_available.clear()
    kube_image_digest_match.clear()

    for r in results:
        value = 1 if r.available else 0
        kube_image_available.labels(
            image=r.image, registry=r.registry, image_name=r.image_name,
            namespace=r.namespace, pod=r.pod, container=r.container,
        ).set(value)

        if r.digest_match is not None:
            kube_image_digest_match.labels(
                image=r.image, registry=r.registry, image_name=r.image_name,
                namespace=r.namespace, pod=r.pod, container=r.container,
            ).set(1 if r.digest_match else 0)
