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


def _run_skopeo_inspect(image: str, auth_file: str | None = None) -> tuple[bool, str | None]:
    """Run skopeo inspect against an image. Returns (success, error_message)."""
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
            return True, None
        return False, result.stderr.strip() or f"skopeo exited with code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "skopeo inspect timed out after 30s"
    except FileNotFoundError:
        return False, "skopeo binary not found"
    except Exception as exc:
        return False, str(exc)


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
            available, error = _run_skopeo_inspect(image, auth_file)
            registry, image_name, _ = _parse_image(image)

            results.append(AvailabilityResult(
                image=image,
                registry=registry,
                image_name=image_name,
                namespace=namespace,
                pod=pod_name,
                container=container["name"],
                available=available,
                error=error,
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
