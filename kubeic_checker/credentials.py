import base64
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("image-audit-checker.credentials")


@dataclass
class ResolvedCredential:
    registry: str
    username: str | None = None
    password: str | None = None
    auth: str | None = None  # base64-encoded user:pass
    source: str = ""  # e.g. "pod:imagePullSecret:my-secret"


def _decode_docker_secret(secret_data: dict) -> dict[str, dict]:
    """Decode a .dockerconfigjson secret into {registry: {username, password, auth}}."""
    config_json = secret_data.get(".dockerconfigjson", "")
    if not config_json:
        return {}

    try:
        config = json.loads(base64.b64decode(config_json))
    except (json.JSONDecodeError, ValueError):
        return {}

    auths = config.get("auths", {})
    result: dict[str, dict] = {}

    for registry, entry in auths.items():
        auth_str = entry.get("auth", "")
        if auth_str:
            try:
                decoded = base64.b64decode(auth_str).decode()
                username, password = decoded.split(":", 1)
                result[registry] = {"username": username, "password": password, "auth": auth_str}
            except (ValueError, Exception):
                continue

    return result


def resolve_all_credentials(
    pods: list[dict],
    secrets_client,
    credential_source_type: str = "pullSecret",
) -> list[ResolvedCredential]:
    """Resolve registry credentials by reading only secrets referenced as imagePullSecrets by pods.

    Each unique secret name is read exactly once, regardless of how many pods reference it.
    No secrets are read unless a pod in this namespace explicitly lists them as imagePullSecrets.
    """
    if credential_source_type == "workloadIdentity" or not pods:
        return []

    namespace = pods[0]["metadata"]["namespace"]

    secret_names: set[str] = set()
    for pod in pods:
        for ref in pod.get("spec", {}).get("imagePullSecrets", []):
            name = ref.get("name", "")
            if name:
                secret_names.add(name)

    credentials: list[ResolvedCredential] = []
    for secret_name in secret_names:
        try:
            secret = secrets_client.read_namespaced_secret(secret_name, namespace)
            decoded = _decode_docker_secret(secret.data or {})
            for registry, creds in decoded.items():
                credentials.append(ResolvedCredential(
                    registry=registry,
                    username=creds.get("username"),
                    password=creds.get("password"),
                    auth=creds.get("auth"),
                    source=f"pod:imagePullSecret:{secret_name}",
                ))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to decode secret %s in %s: %s", secret_name, namespace, e)
        except Exception as e:
            logger.warning("Failed to read secret %s in %s: %s", secret_name, namespace, type(e).__name__)
            continue

    return credentials


def _normalize_registry_host(registry: str) -> str:
    """Reduce a docker-config auths key or image registry to a comparable host.

    Docker Hub has several historical spellings that must compare equal.
    """
    host = registry.removeprefix("https://").removeprefix("http://").split("/")[0]
    if host in ("index.docker.io", "registry-1.docker.io", "docker.io", ""):
        return "docker.io"
    return host


def build_image_credentials(
    pods: list[dict],
    creds: list[ResolvedCredential],
) -> dict[str, list[ResolvedCredential]]:
    """Map each image to the credentials of the pull secrets its pods reference.

    Mirrors kubelet semantics: a pod's imagePullSecrets are tried in order for
    its own images. Credentials from secrets a pod does not reference are never
    used for that pod's images — this is what prevents one namespace-wide
    credential from masking (or being masked by) another for the same registry
    host when per-project deploy tokens are in play.
    """
    by_secret: dict[str, list[ResolvedCredential]] = {}
    for cred in creds:
        secret_name = cred.source.split(":")[-1]
        by_secret.setdefault(secret_name, []).append(cred)

    image_creds: dict[str, list[ResolvedCredential]] = {}
    for pod in pods:
        secret_names = [
            ref.get("name", "")
            for ref in pod.get("spec", {}).get("imagePullSecrets", [])
            if ref.get("name")
        ]
        containers = list(pod.get("spec", {}).get("containers", [])) + list(
            pod.get("spec", {}).get("initContainers", [])
        )
        for container in containers:
            image = container["image"]
            image_host = _normalize_registry_host(registry_from_image(image))
            candidates = image_creds.setdefault(image, [])
            for secret_name in secret_names:
                for cred in by_secret.get(secret_name, []):
                    if _normalize_registry_host(cred.registry) != image_host:
                        continue
                    if any(c.source == cred.source and c.registry == cred.registry for c in candidates):
                        continue
                    candidates.append(cred)

    return image_creds


def registry_from_image(image: str) -> str:
    """Extract registry hostname from an image string.

    Examples:
        nginx -> "" (Docker Hub, no explicit registry)
        quay.io/app/image -> quay.io
        myregistry.corp.com:5000/image -> myregistry.corp.com:5000
    """
    parts = image.split("/")
    if len(parts) == 1:
        return "https://index.docker.io/v1/"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
        return parts[0]
    return "https://index.docker.io/v1/"
