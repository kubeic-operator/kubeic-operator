from prometheus_client import Gauge

# --- Operator metrics (cluster-wide) ---

kube_image_is_prerelease = Gauge(
    "kube_image_is_prerelease",
    "Whether the image tag is a pre-release version (0 or 1)",
    ["image", "image_base", "tag", "namespace", "pod", "container"],
)

kube_image_prerelease_age_days = Gauge(
    "kube_image_prerelease_age_days",
    "Age in days of a pod running a pre-release image",
    ["image", "image_base", "tag", "namespace", "pod", "container"],
)

kube_image_version_count = Gauge(
    "kube_image_version_count",
    "Number of distinct versions running for an image base",
    ["image_base"],
)

kube_image_version_pod_count = Gauge(
    "kube_image_version_pod_count",
    "Number of pods running a specific image version",
    ["image_base", "tag", "namespace"],
)

kube_image_version_spread_violation = Gauge(
    "kube_image_version_spread_violation",
    "Whether this image base exceeds the version spread threshold (0 or 1)",
    ["image_base"],
)

kube_image_total_prerelease_violations = Gauge(
    "kube_image_total_prerelease_violations",
    "Total number of pre-release images exceeding their max age threshold",
)

kube_image_total_spread_violations = Gauge(
    "kube_image_total_spread_violations",
    "Total number of image bases exceeding version spread threshold",
)

# --- Checker metrics (per-namespace) ---

kube_image_available = Gauge(
    "kube_image_available",
    "Whether the image is reachable in the registry (0 or 1)",
    ["image", "image_base", "namespace", "pod", "container"],
)

kube_image_credential_valid = Gauge(
    "kube_image_credential_valid",
    "Whether the registry credentials are valid (0 or 1)",
    ["registry", "namespace", "secret_name"],
)

kube_image_total_unavailable = Gauge(
    "kube_image_total_unavailable",
    "Total number of images that are unreachable in the registry",
    ["namespace"],
)


def update_prerelease_metrics(findings: list, violation_count: int = 0) -> None:
    """Update all pre-release Prometheus gauges from findings.

    Args:
        findings: List of PrereleaseFinding objects.
        violation_count: Number of findings exceeding the max age threshold.
    """
    kube_image_is_prerelease.clear()
    kube_image_prerelease_age_days.clear()

    for f in findings:
        labels = {
            "image": f.image,
            "image_base": f.image_base,
            "tag": f.tag,
            "namespace": f.namespace,
            "pod": f.pod,
            "container": f.container,
        }
        kube_image_is_prerelease.labels(**labels).set(1)
        kube_image_prerelease_age_days.labels(**labels).set(f.age_days)

    kube_image_total_prerelease_violations.set(violation_count)


def update_spread_metrics(findings: list) -> None:
    """Update all version spread Prometheus gauges from findings.

    Args:
        findings: List of VersionSpreadFinding objects.
    """
    kube_image_version_count.clear()
    kube_image_version_pod_count.clear()
    kube_image_version_spread_violation.clear()

    violation_count = 0
    for f in findings:
        kube_image_version_count.labels(image_base=f.image_base).set(f.version_count)
        kube_image_version_spread_violation.labels(image_base=f.image_base).set(
            1 if f.violates_threshold else 0
        )
        if f.violates_threshold:
            violation_count += 1

        for tag, ns_counts in f.version_pod_counts.items():
            for ns, count in ns_counts.items():
                kube_image_version_pod_count.labels(
                    image_base=f.image_base, tag=tag, namespace=ns
                ).set(count)

    kube_image_total_spread_violations.set(violation_count)


def update_availability_metrics(results: list, namespace: str = "") -> None:
    """Update availability Prometheus gauges from check results.

    Args:
        results: List of AvailabilityResult objects.
        namespace: The namespace being checked (for the total label).
    """
    kube_image_available.clear()

    unavailable_count = 0
    for r in results:
        value = 1 if r.available else 0
        kube_image_available.labels(
            image=r.image, image_base=r.image_base,
            namespace=r.namespace, pod=r.pod, container=r.container,
        ).set(value)
        if not r.available:
            unavailable_count += 1

    kube_image_total_unavailable.labels(namespace=namespace).set(unavailable_count)
