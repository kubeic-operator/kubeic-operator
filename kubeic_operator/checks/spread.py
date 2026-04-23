from collections import defaultdict
from dataclasses import dataclass

from kubeic_operator.checks.prerelease import _parse_image


@dataclass
class VersionSpreadFinding:
    registry: str
    image_name: str
    versions: list[str]
    version_count: int
    violates_threshold: bool

    # Per-version pod counts for detailed metrics
    version_pod_counts: dict[str, dict[str, int]]  # tag -> {namespace: count}


def aggregate_version_spread(
    pods: list[dict],
    threshold: int = 3,
) -> list[VersionSpreadFinding]:
    """Group running pods by image (registry + image_name) and detect version spread violations.

    Returns one finding per image that has more than 1 version running.
    """
    # (registry, image_name) -> {tag -> {namespace -> count}}
    image_versions: dict[tuple[str, str], dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )

    for pod in pods:
        namespace = pod["metadata"]["namespace"]
        containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])

        for container in list(containers) + list(init_containers):
            image_str = container["image"]
            registry, image_name, tag = _parse_image(image_str)
            image_versions[(registry, image_name)][tag][namespace] += 1

    findings: list[VersionSpreadFinding] = []

    for (registry, image_name), tags in sorted(image_versions.items()):
        version_count = len(tags)
        if version_count < 2:
            continue

        # Convert defaultdicts to plain dicts for the dataclass
        version_pod_counts = {
            tag: dict(ns_counts) for tag, ns_counts in tags.items()
        }

        findings.append(VersionSpreadFinding(
            registry=registry,
            image_name=image_name,
            versions=sorted(tags.keys()),
            version_count=version_count,
            violates_threshold=version_count > threshold,
            version_pod_counts=version_pod_counts,
        ))

    return findings
