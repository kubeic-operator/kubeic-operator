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
    registry: str
    image_name: str
    tag: str
    namespace: str
    pod: str
    container: str
    is_prerelease: bool
    age_days: float


def _parse_registry(image_base: str) -> tuple[str, str]:
    """Split an image base (no tag/digest) into (registry, image_name).

    Docker Hub images without an explicit registry get 'docker.io' and a
    'library/' prefix for official images.

    Examples:
        nginx                                  -> (docker.io, library/nginx)
        myuser/myapp                           -> (docker.io, myuser/myapp)
        quay.io/myorg/myapp                    -> (quay.io, myorg/myapp)
        registry.k8s.io/ingress-nginx/ctrl     -> (registry.k8s.io, ingress-nginx/ctrl)
        myregistry.corp.com:5000/app           -> (myregistry.corp.com:5000, app)
    """
    parts = image_base.split("/")
    if len(parts) == 1:
        # Bare image name like "nginx" — Docker Hub official
        return "docker.io", f"library/{image_base}"
    if "." in parts[0] or ":" in parts[0]:
        # Explicit registry (has a dot or port)
        return parts[0], "/".join(parts[1:])
    # No explicit registry but has a slash, e.g. "myuser/myapp" — Docker Hub
    return "docker.io", image_base


def _parse_image(image_str: str) -> tuple[str, str, str]:
    """Split image into (registry, image_name, tag).

    For images with both a tag and a digest (e.g. repo:v1.0@sha256:abc),
    the tag is extracted and the digest is stripped.
    For images with only a digest (e.g. repo@sha256:abc), the digest is used as the tag.
    """
    # Strip digest, but first check if there's a tag before the @
    if "@" in image_str:
        before_at = image_str.split("@", 1)[0]
        # Try to extract a tag from the part before the digest
        last_colon = before_at.rfind(":")
        if last_colon != -1 and "/" not in before_at[last_colon:]:
            # Has a tag before the digest: repo:tag@sha256:...
            base = before_at[:last_colon]
            tag = before_at[last_colon + 1:]
            registry, image_name = _parse_registry(base)
            return registry, image_name, tag
        # No tag, just a digest: repo@sha256:...
        registry, image_name = _parse_registry(before_at)
        return registry, image_name, image_str.split("@", 1)[1]

    if ":" in image_str:
        # Split on last : to handle registry ports (registry:5000/image:tag)
        last_colon = image_str.rfind(":")
        # If there's a / after the last colon, it's a port not a tag
        if "/" in image_str[last_colon:]:
            registry, image_name = _parse_registry(image_str)
            return registry, image_name, "latest"
        base = image_str[:last_colon]
        tag = image_str[last_colon + 1:]
        registry, image_name = _parse_registry(base)
        return registry, image_name, tag

    registry, image_name = _parse_registry(image_str)
    return registry, image_name, "latest"


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
                registry, image_name, tag = _parse_image(image_str)
                is_pre = is_prerelease_tag(tag, patterns)

                if is_pre:
                    findings.append(PrereleaseFinding(
                        image=image_str,
                        registry=registry,
                        image_name=image_name,
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
