import hashlib
import json
import logging
import os
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import dataclass

from kubeic_operator.checks.prerelease import _parse_image

logger = logging.getLogger("image-audit-checker.availability")


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
    error_class: str = ""  # auth_failure | not_found | network | unknown
    digest_match: bool | None = None  # None when image has no pinned digest
    registry_digest: str | None = None
    pinned_digest: str | None = None
    created: str | None = None  # image publication timestamp (registry Created field)


def _classify_error(stderr: str | None, returncode: int | None = None) -> str:
    """Classify a skopeo error into auth_failure, not_found, or network."""
    msg = (stderr or "").lower()
    # "denied" catches GitLab's "requested access to the resource is denied"
    # and the registry token service's "access forbidden".
    if any(s in msg for s in ("unauthorized", "authentication required", "denied", "forbidden", "401", "403")):
        return "auth_failure"
    if any(s in msg for s in ("not found", "manifest unknown", "unknown blob", "404")):
        return "not_found"
    # Rate limiting is transient registry-side pushback, grouped with network
    # so it routes as retryable infra rather than a content problem.
    if any(s in msg for s in ("timed out", "timeout", "connection refused", "i/o timeout", "no route to host", "no such host", "toomanyrequests", "too many requests", "429")):
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

    # --no-tags: without it skopeo paginates the repo's full tag list, which
    # exceeds the 30s timeout on repos with thousands of tags (kyverno,
    # gitlab-org) and surfaces as a false "network" unavailability.
    cmd = ["skopeo", "inspect", "--no-tags", "--retry-times", "2", f"docker://{image}"]

    if auth_file:
        cmd.extend(["--authfile", auth_file])

    last_error: str | None = None
    last_error_class: str = "unknown"
    for attempt in range(retries):
        try:
            result = subprocess.run(  # nosec B603
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # nosec B603
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


def _inspect_with_candidates(
    ref: str, candidates: list,
) -> tuple[bool, str | None, dict | None, str]:
    """Inspect an image trying each candidate credential in order.

    Mirrors kubelet pull behaviour: candidates are the pod's imagePullSecrets
    for this image's registry, tried in order; only an auth failure moves on
    to the next credential. No candidates → unauthenticated inspect.
    """
    if not candidates:
        return _run_skopeo_inspect(ref)

    result: tuple[bool, str | None, dict | None, str] | None = None
    for i, cred in enumerate(candidates):
        entry = {"auth": cred.auth} if cred.auth else {
            "username": cred.username, "password": cred.password,
        }
        fd, auth_path = tempfile.mkstemp(suffix=".json", prefix="image-audit-auth-")
        os.close(fd)
        try:
            write_auth_config({cred.registry: entry}, auth_path)
            result = _run_skopeo_inspect(ref, auth_file=auth_path)
        finally:
            try:
                os.unlink(auth_path)
            except OSError:
                pass
        if result[3] != "auth_failure":
            if i > 0:
                logger.info(
                    "Credential %s succeeded for %s after %d auth failure(s) with earlier secrets",
                    cred.source, ref, i,
                )
            return result
    logger.warning(
        "All %d candidate credentials failed auth for %s", len(candidates), ref,
    )
    return result


def _inspect_ref(image: str) -> str:
    """Choose the registry reference to inspect for an image.

    repo:tag@sha256:...  -> repo:tag   (check the tag; digest compared separately)
    repo@sha256:...      -> unchanged  (no tag — inspect the pinned digest itself,
                                        otherwise skopeo would default to :latest)
    repo:tag / repo      -> unchanged
    """
    if "@" not in image:
        return image
    before_at = image.split("@", 1)[0]
    last_colon = before_at.rfind(":")
    if last_colon != -1 and "/" not in before_at[last_colon:]:
        return before_at
    return image


def _credential_fingerprint(cred) -> str:
    """Stable identifier for what a credential *is*, not where it came from.

    A pull secret replicated into fifty namespaces is fifty ResolvedCredentials
    with different namespaces and the same token. Keying on namespace or secret
    name would re-inspect the same image fifty times for an identical answer;
    keying on the material dedupes them. Hashed rather than used directly so no
    token ends up as a dict key.
    """
    material = cred.auth if cred.auth else f"{cred.username}:{cred.password}"
    return hashlib.sha256(f"{cred.registry}\x00{material}".encode()).hexdigest()[:16]


def _dedupe_key(image: str, candidates: list) -> tuple:
    """Key under which an inspect result may be shared between containers.

    An image inspected with one set of credentials says nothing about the same
    image inspected with another — a private repo can be readable with one
    deploy token and 403 with the next — so the credentials are part of the key.
    Public images resolve to an empty candidate tuple and therefore still share
    a single inspect across the whole cluster, which is where nearly all of the
    saving is.

    Order is preserved, not sorted: candidates are tried in the pod's
    imagePullSecrets order and the first non-auth-failure wins, so [A, B] and
    [B, A] can genuinely produce different results and must not share a result.
    """
    return (image, tuple(_credential_fingerprint(c) for c in candidates))


def plan_inspections(
    pods: list[dict],
    image_creds: dict[tuple[str, str], list] | None = None,
) -> dict[tuple, tuple[str, list]]:
    """The distinct inspections a pod list implies: key -> (ref, candidates).

    Exposed so a caller can size the work before starting it, which is what
    lets the sweep be paced evenly across its interval rather than run flat out
    and then idle.
    """
    plan: dict[tuple, tuple[str, list]] = {}
    for pod in pods:
        namespace = pod["metadata"]["namespace"]
        containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])
        for container in list(containers) + list(init_containers):
            image = container["image"]
            candidates = (
                image_creds.get((namespace, image), []) if image_creds is not None else []
            )
            key = _dedupe_key(image, candidates)
            if key not in plan:
                plan[key] = (_inspect_ref(image), candidates)
    return plan


def check_availability(
    pods: list[dict],
    auth_file: str | None = None,
    image_creds: dict[tuple[str, str], list] | None = None,
    pacer=None,
) -> list[AvailabilityResult]:
    """Check image availability for all containers in the given pods.

    Each unique (image, credential set) is inspected once and the result reused
    for every container that resolves to the same pair.

    Args:
        pods: List of pod dicts with metadata and spec. May span namespaces.
        auth_file: Path to a docker config JSON file for registry auth.
            Ignored when image_creds is provided.
        image_creds: Map of (namespace, image) -> ordered ResolvedCredential
            candidates (from build_image_credentials). Candidates are tried per
            image in kubelet order, advancing only on auth failure.
        pacer: Optional zero-argument callable invoked after each inspection.
            Used to spread the sweep across its interval instead of running it
            flat out; see _Pacer in the checker entrypoint.

    Returns:
        One AvailabilityResult per container.
    """
    results: list[AvailabilityResult] = []

    # Inspect each unique image once per distinct credential set.
    seen_images: dict[tuple, tuple[bool, str | None, dict | None, str]] = {}
    for key, (ref, candidates) in plan_inspections(pods, image_creds).items():
        if image_creds is not None:
            seen_images[key] = _inspect_with_candidates(ref, candidates)
        else:
            seen_images[key] = _run_skopeo_inspect(ref, auth_file)
        if pacer is not None:
            pacer()

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

            if image_creds is not None:
                key = _dedupe_key(image, image_creds.get((namespace, image), []))
            else:
                key = (image, ())
            available, error, inspect_data, error_class = seen_images[key]
            registry, image_name, _ = _parse_image(image)

            digest_match: bool | None = None
            registry_digest: str | None = None
            if available and inspect_data and pinned_digest:
                registry_digest = inspect_data.get("Digest")
                if registry_digest:
                    digest_match = registry_digest == pinned_digest

            created = inspect_data.get("Created") if available and inspect_data else None

            results.append(AvailabilityResult(
                image=image,
                registry=registry,
                image_name=image_name,
                namespace=namespace,
                pod=pod_name,
                container=container["name"],
                available=available,
                error=error,
                error_class=error_class,
                digest_match=digest_match,
                registry_digest=registry_digest,
                pinned_digest=pinned_digest,
                created=created,
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
