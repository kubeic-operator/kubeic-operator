import re
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_PRERELEASE_PATTERNS = [
    "alpha",
    "beta",
    "rc",
    "dev",
    "nightly",
    "snapshot",
    "canary",
    "unstable",
    "latest",
]

# OS/distro variant suffixes that are stripped from the end of a tag before
# pre-release pattern matching. This prevents stable distribution variants
# like "1.2.3-alpine" or "1.2.3-ubuntu22.04" from being misclassified.
# The suffix must be preceded by a separator and appear at the end of the tag.
_STABLE_OS_SUFFIX_RE = re.compile(
    r"[.\-_]("
    r"alpine[\d.]*"
    r"|slim"
    r"|ubuntu[\d.]*"
    r"|debian[\d.]*"
    r"|bookworm|bullseye|buster|stretch|jessie"
    r"|focal|jammy|noble|bionic|xenial"
    r"|centos[\d.]*"
    r"|rhel[\d.]*"
    r"|ubi[\d.]*"
    r"|windowsservercore[\d.]*|nanoserver[\d.]*"
    r")$",
    re.IGNORECASE,
)


@dataclass
class PrereleaseFinding:
    image: str
    image_base: str
    tag: str
    namespace: str
    pod: str
    container: str
    is_prerelease: bool
    age_days: float


def _parse_image(image_str: str) -> tuple[str, str]:
    """Split image into (base, tag). Handles registry/path:tag and registry/path (implies :latest)."""
    # Handle digest references (sha256:...) - not a tag
    if "@" in image_str:
        base, digest = image_str.rsplit("@", 1)
        return base, digest

    if ":" in image_str:
        # Split on last : to handle registry ports (registry:5000/image:tag)
        last_colon = image_str.rfind(":")
        # If there's a / after the last colon, it's a port not a tag
        if "/" in image_str[last_colon:]:
            return image_str, "latest"
        return image_str[:last_colon], image_str[last_colon + 1:]

    return image_str, "latest"


def is_prerelease_tag(tag: str, patterns: list[str] | None = None) -> bool:
    """Check if a tag matches any pre-release pattern."""
    if patterns is None:
        patterns = DEFAULT_PRERELEASE_PATTERNS

    # Strip leading 'v' and trailing OS/distro variant suffix before checking.
    # e.g. "1.2.3-alpine" -> "1.2.3", "1.0.0-rc.alpine" -> "1.0.0-rc"
    check_tag = _STABLE_OS_SUFFIX_RE.sub("", tag.lstrip("v").lower())

    for pattern in patterns:
        # Match pattern as a word boundary segment in the tag
        # e.g. "alpha" matches "1.0-alpha", "1.0alpha1", "alpha", but not "alphabet"
        regex = rf"(?:^|[.\-_+]){re.escape(pattern)}(?:[.\-_+]|$|\d+)"
        if re.search(regex, check_tag, re.IGNORECASE):
            return True

    # Also match if tag IS exactly the pattern
    if check_tag in [p.lower() for p in patterns]:
        return True

    return False


def calculate_age_days(pod_start_time: str) -> float:
    """Calculate age in days from a pod start time (ISO 8601 string)."""
    start = datetime.fromisoformat(pod_start_time.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (now - start).total_seconds() / 86400


def check_prerelease(
    pods: list[dict],
    max_age_days: float = 7,
    patterns: list[str] | None = None,
) -> list[PrereleaseFinding]:
    """Scan pods and return findings for pre-release tagged images.

    Each pod dict should have: metadata.name, metadata.namespace,
    metadata.creationTimestamp or status.startTime, spec.containers[*].image,
    spec.initContainers[*].image (optional).
    """
    findings: list[PrereleaseFinding] = []

    for pod in pods:
        pod_name = pod["metadata"]["name"]
        namespace = pod["metadata"]["namespace"]
        start_time = pod.get("status", {}).get("startTime") or pod["metadata"].get("creationTimestamp", "")

        if not start_time:
            continue

        age_days = calculate_age_days(start_time)

        containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])

        for container_list in [containers, init_containers]:
            for container in container_list:
                image_str = container["image"]
                base, tag = _parse_image(image_str)
                is_pre = is_prerelease_tag(tag, patterns)

                if is_pre:
                    findings.append(PrereleaseFinding(
                        image=image_str,
                        image_base=base,
                        tag=tag,
                        namespace=namespace,
                        pod=pod_name,
                        container=container["name"],
                        is_prerelease=True,
                        age_days=age_days,
                    ))

    return findings


def filter_violations(findings: list[PrereleaseFinding], max_age_days: float) -> list[PrereleaseFinding]:
    """Return only findings that exceed the max age threshold."""
    return [f for f in findings if f.age_days > max_age_days]
