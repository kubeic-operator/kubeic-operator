import json
import subprocess
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


def _run_skopeo_inspect(
    image: str, auth_file: str | None = None,
) -> tuple[bool, str | None, dict | None]:
    """Run skopeo inspect against an image.

    Returns (success, error_message, parsed_json).
    parsed_json contains the full skopeo inspect output including the Digest field.
    """
    cmd = ["skopeo", "inspect", "--retry-times", "2", f"docker://{image}"]

    if auth_file:
        cmd.extend(["--authfile", auth_file])

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
            return True, None, inspect_data
        return False, result.stderr.strip() or f"skopeo exited with code {result.returncode}", None
    except subprocess.TimeoutExpired:
        return False, "skopeo inspect timed out after 30s", None
    except FileNotFoundError:
        return False, "skopeo binary not found", None
    except Exception as exc:
        return False, str(exc), None


def check_availability(
    pods: list[dict],
    auth_file: str | None = None,
) -> list[AvailabilityResult]:
    """Check image availability for all containers in the given pods.

    Args:
        pods: List of pod dicts with metadata and spec.
        auth_file: Path to a docker config JSON file for registry auth.

    Returns:
        One AvailabilityResult per container.
    """
    results: list[AvailabilityResult] = []

    for pod in pods:
        pod_name = pod["metadata"]["name"]
        namespace = pod["metadata"]["namespace"]
        containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])

        for container in list(containers) + list(init_containers):
            image = container["image"]
            pinned_digest: str | None = None
            if "@" in image:
                inspect_image, pinned_digest = image.split("@", 1)
            else:
                inspect_image = image

            available, error, inspect_data = _run_skopeo_inspect(inspect_image, auth_file)
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
    with open(path, "w") as f:
        json.dump(config, f)
