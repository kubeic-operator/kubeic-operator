from collections import defaultdict
from dataclasses import dataclass

from kubeic_operator.checks.prerelease import _parse_image


@dataclass
class VersionSpreadFinding:
    image_base: str
    versions: list[str]
    version_count: int
    violates_threshold: bool

    # Per-version pod counts for detailed metrics
    version_pod_counts: dict[str, dict[str, int]]  # tag -> {namespace: count}


def _get_image_base(image_str: str) -> str:
    """Extract the image base (registry/path without tag or digest)."""
    base, _ = _parse_image(image_str)
    # Strip digest if present
    if "@" in base:
        base = base.split("@")[0]
    return base


def aggregate_version_spread(
    pods: list[dict],
    threshold: int = 3,
) -> list[VersionSpreadFinding]:
    """Group running pods by image base and detect version spread violations.

    Returns one finding per image base that has more than 1 version running.
    """
    # image_base -> {tag -> {namespace -> count}}
    image_versions: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )

    for pod in pods:
        namespace = pod["metadata"]["namespace"]
        containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])

        for container in list(containers) + list(init_containers):
            image_str = container["image"]
            base, tag = _parse_image(image_str)
            image_versions[base][tag][namespace] += 1

    findings: list[VersionSpreadFinding] = []

    for image_base, tags in sorted(image_versions.items()):
        version_count = len(tags)
        if version_count < 2:
            continue

        # Convert defaultdicts to plain dicts for the dataclass
        version_pod_counts = {
            tag: dict(ns_counts) for tag, ns_counts in tags.items()
        }

        findings.append(VersionSpreadFinding(
            image_base=image_base,
            versions=sorted(tags.keys()),
            version_count=version_count,
            violates_threshold=version_count > threshold,
            version_pod_counts=version_pod_counts,
        ))

    return findings
