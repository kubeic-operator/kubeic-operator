import json
import os
import subprocess
import time
from dataclasses import dataclass

from kubeic_operator.checks.prerelease import _parse_image


@dataclass
class AvailabilityResult:
    image: str
    registry: str
    image_name: str
    namespace: str
    pod: str
    container: str
    available: bool
    error: str | None = None
    digest_match: bool | None = None  # None when image has no pinned digest
    registry_digest: str | None = None
    pinned_digest: str | None = None


def _classify_error(stderr: str | None, returncode: int | None = None) -> str:
    """Classify a skopeo error into auth_failure, not_found, or network."""
    msg = (stderr or "").lower()
    if any(s in msg for s in ("unauthorized", "authentication required", "access denied", "401", "403")):
        return "auth_failure"
    if any(s in msg for s in ("not found", "manifest unknown", "unknown blob", "404")):
        return "not_found"
    if any(s in msg for s in ("timed out", "timeout", "connection refused", "i/o timeout", "no route to host", "no such host")):
        return "network"
    return "unknown"


def _run_skopeo_inspect(
    image: str, auth_file: str | None = None, retries: int = 3,
    backoff_delays: list[float] | None = None,
) -> tuple[bool, str | None, dict | None, str]:
    """Run skopeo inspect against an image with retry.

    Returns (success, error_message, parsed_json, error_class).
    """
    if backoff_delays is None:
        backoff_delays = [0, 10, 30]

    cmd = ["skopeo", "inspect", "--retry-times", "2", f"docker://{image}"]

    if auth_file:
        cmd.extend(["--authfile", auth_file])

    last_error: str | None = None
    last_error_class: str = "unknown"
    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                try:
                    inspect_data = json.loads(result.stdout)
                except (json.JSONDecodeError, ValueError):
                    inspect_data = None
                return True, None, inspect_data, ""
            last_error = result.stderr.strip() or f"skopeo exited with code {result.returncode}"
            last_error_class = _classify_error(last_error, result.returncode)
            if last_error_class == "auth_failure":
                return False, last_error, None, last_error_class
        except subprocess.TimeoutExpired:
            last_error = "skopeo inspect timed out after 30s"
            last_error_class = "network"
        except FileNotFoundError:
            return False, "skopeo binary not found", None, "unknown"
        except Exception as exc:
            last_error = str(exc)
            last_error_class = "unknown"

        if attempt < retries - 1 and attempt < len(backoff_delays):
            time.sleep(backoff_delays[attempt])

    return False, last_error, None, last_error_class


def _run_skopeo_list_tags(
    image: str, auth_file: str | None = None,
) -> tuple[bool, str | None, str]:
    """Run skopeo list-tags to test repo-level access.

    Returns (success, error_message, error_class).
    """
    repo = image.split("@")[0].split(":")[0]
    cmd = ["skopeo", "list-tags", f"docker://{repo}"]
    if auth_file:
        cmd.extend(["--authfile", auth_file])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True, None, ""
        error = result.stderr.strip() or f"skopeo list-tags exited with code {result.returncode}"
        return False, error, _classify_error(error, result.returncode)
    except subprocess.TimeoutExpired:
        return False, "skopeo list-tags timed out after 30s", "network"
    except FileNotFoundError:
        return False, "skopeo binary not found", "unknown"
    except Exception as exc:
        return False, str(exc), "unknown"


def check_availability(
    pods: list[dict],
    auth_file: str | None = None,
) -> list[AvailabilityResult]:
    """Check image availability for all containers in the given pods.

    Each unique image is inspected once and the result is reused for all
    containers referencing it.

    Args:
        pods: List of pod dicts with metadata and spec.
        auth_file: Path to a docker config JSON file for registry auth.

    Returns:
        One AvailabilityResult per container.
    """
    results: list[AvailabilityResult] = []

    # Inspect each unique image once
    seen_images: dict[str, tuple[bool, str | None, dict | None, str]] = {}
    for pod in pods:
        containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])
        for container in list(containers) + list(init_containers):
            image = container["image"]
            if image not in seen_images:
                inspect_image = image.split("@")[0] if "@" in image else image
                seen_images[image] = _run_skopeo_inspect(inspect_image, auth_file)

    for pod in pods:
        pod_name = pod["metadata"]["name"]
        namespace = pod["metadata"]["namespace"]
        containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])

        for container in list(containers) + list(init_containers):
            image = container["image"]
            pinned_digest: str | None = None
            if "@" in image:
                _, pinned_digest = image.split("@", 1)

            available, error, inspect_data, _ = seen_images[image]
            registry, image_name, _ = _parse_image(image)

            digest_match: bool | None = None
            registry_digest: str | None = None
            if available and inspect_data and pinned_digest:
                registry_digest = inspect_data.get("Digest")
                if registry_digest:
                    digest_match = registry_digest == pinned_digest

            results.append(AvailabilityResult(
                image=image,
                registry=registry,
                image_name=image_name,
                namespace=namespace,
                pod=pod_name,
                container=container["name"],
                available=available,
                error=error,
                digest_match=digest_match,
                registry_digest=registry_digest,
                pinned_digest=pinned_digest,
            ))

    return results


def write_auth_config(secrets: dict[str, dict], path: str) -> None:
    """Write a Docker config.json from resolved pull secrets.

    Args:
        secrets: Map of registry hostname to {"username": ..., "password": ...}
                 or {"auth": base64_encoded_auth}.
        path: Where to write the config file.
    """
    auths = {}
    for registry, creds in secrets.items():
        if "auth" in creds:
            auths[registry] = {"auth": creds["auth"]}
        else:
            import base64
            token = base64.b64encode(
                f"{creds['username']}:{creds['password']}".encode()
            ).decode()
            auths[registry] = {"auth": token}

    config = {"auths": auths}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
