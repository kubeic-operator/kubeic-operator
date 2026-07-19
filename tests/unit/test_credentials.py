import base64
import json
import logging
from unittest.mock import MagicMock

from kubeic_checker.credentials import (
    _decode_docker_secret,
    resolve_all_credentials,
    registry_from_image,
)


def _encode_docker_config(auths: dict[str, str]) -> str:
    """Helper to create a base64-encoded .dockerconfigjson."""
    config = {"auths": {}}
    for registry, user_pass in auths.items():
        auth = base64.b64encode(user_pass.encode()).decode()
        config["auths"][registry] = {"auth": auth}
    return base64.b64encode(json.dumps(config).encode()).decode()


def _make_pod(secret_names: list[str] = (), namespace: str = "default") -> dict:
    return {
        "metadata": {"name": "test-pod", "namespace": namespace},
        "spec": {
            "containers": [{"name": "main", "image": "nginx:1.25"}],
            "imagePullSecrets": [{"name": n} for n in secret_names],
        },
    }


def _mock_secret(registry: str, user_pass: str) -> MagicMock:
    s = MagicMock()
    s.data = {".dockerconfigjson": _encode_docker_config({registry: user_pass})}
    return s


class TestDecodeDockerSecret:
    def test_decodes_valid_secret(self):
        data = {".dockerconfigjson": _encode_docker_config({"registry.example.com": "user:pass"})}
        result = _decode_docker_secret(data)
        assert "registry.example.com" in result
        assert result["registry.example.com"]["username"] == "user"
        assert result["registry.example.com"]["password"] == "pass"

    def test_returns_empty_on_missing_key(self):
        assert _decode_docker_secret({}) == {}

    def test_returns_empty_on_invalid_json(self):
        result = _decode_docker_secret({".dockerconfigjson": base64.b64encode(b"not json").decode()})
        assert result == {}

    def test_returns_empty_on_none_value(self):
        result = _decode_docker_secret({".dockerconfigjson": None})
        assert result == {}

    def test_skips_non_base64_auth_content(self):
        config = json.dumps({"auths": {"r.io": {"auth": "not-valid-base64!!!"}}})
        data = {".dockerconfigjson": base64.b64encode(config.encode()).decode()}
        result = _decode_docker_secret(data)
        assert result == {}

    def test_skips_auth_without_colon_separator(self):
        token = base64.b64encode(b"no_colon_here").decode()
        config = json.dumps({"auths": {"r.io": {"auth": token}}})
        data = {".dockerconfigjson": base64.b64encode(config.encode()).decode()}
        result = _decode_docker_secret(data)
        assert result == {}


class TestResolveAllCredentials:
    def test_resolves_secret_referenced_by_pod(self):
        mock_client = MagicMock()
        mock_client.read_namespaced_secret.return_value = _mock_secret("r.io", "u:p")

        creds = resolve_all_credentials([_make_pod(["my-pull-secret"])], mock_client)

        assert len(creds) == 1
        assert creds[0].registry == "r.io"
        assert creds[0].username == "u"
        assert "my-pull-secret" in creds[0].source

    def test_deduplicates_shared_secret_across_pods(self):
        mock_client = MagicMock()
        mock_client.read_namespaced_secret.return_value = _mock_secret("r.io", "u:p")

        pods = [_make_pod(["shared-secret"]), _make_pod(["shared-secret"])]
        resolve_all_credentials(pods, mock_client)

        mock_client.read_namespaced_secret.assert_called_once()

    def test_reads_multiple_distinct_secrets_once_each(self):
        mock_client = MagicMock()
        mock_client.read_namespaced_secret.side_effect = [
            _mock_secret("r1.io", "u1:p1"),
            _mock_secret("r2.io", "u2:p2"),
        ]

        creds = resolve_all_credentials([_make_pod(["secret-a", "secret-b"])], mock_client)

        assert len(creds) == 2
        assert mock_client.read_namespaced_secret.call_count == 2

    def test_skips_unavailable_secret(self):
        mock_client = MagicMock()
        mock_client.read_namespaced_secret.side_effect = Exception("not found")

        creds = resolve_all_credentials([_make_pod(["missing-secret"])], mock_client)
        assert creds == []

    def test_pod_without_pull_secrets(self):
        creds = resolve_all_credentials([_make_pod([])], MagicMock())
        assert creds == []

    def test_empty_pod_list_returns_empty(self):
        creds = resolve_all_credentials([], MagicMock())
        assert creds == []

    def test_workload_identity_returns_empty(self):
        creds = resolve_all_credentials(
            [_make_pod(["any-secret"])], MagicMock(), credential_source_type="workloadIdentity"
        )
        assert creds == []

    def test_only_reads_secrets_named_in_pod_specs(self):
        mock_client = MagicMock()
        mock_client.read_namespaced_secret.return_value = _mock_secret("r.io", "u:p")

        resolve_all_credentials([_make_pod(["regcred"])], mock_client)

        mock_client.read_namespaced_secret.assert_called_once_with("regcred", "default")

    def test_logs_warning_on_unavailable_secret(self, caplog):
        mock_client = MagicMock()
        mock_client.read_namespaced_secret.side_effect = Exception("not found")

        with caplog.at_level(logging.WARNING, logger="image-audit-checker.credentials"):
            creds = resolve_all_credentials([_make_pod(["missing-secret"])], mock_client)

        assert creds == []
        assert "Failed to read secret missing-secret" in caplog.text


class TestRegistryFromImage:
    def test_docker_hub_official(self):
        assert registry_from_image("nginx") == "https://index.docker.io/v1/"

    def test_docker_hub_user_repo(self):
        assert registry_from_image("myuser/myapp") == "https://index.docker.io/v1/"

    def test_private_registry(self):
        assert registry_from_image("quay.io/org/app") == "quay.io"

    def test_registry_with_port(self):
        assert registry_from_image("myregistry.corp.com:5000/app") == "myregistry.corp.com:5000"


class TestBuildImageCredentials:
    def _cred(self, registry, secret, user="u"):
        from kubeic_checker.credentials import ResolvedCredential
        return ResolvedCredential(
            registry=registry, username=user, password="p",
            source=f"pod:imagePullSecret:{secret}",
        )

    def _pod(self, image, secrets):
        return {
            "metadata": {"name": "pod-1", "namespace": "default"},
            "spec": {
                "containers": [{"name": "main", "image": image}],
                "imagePullSecrets": [{"name": s} for s in secrets],
            },
        }

    def test_candidates_follow_pod_secret_order(self):
        from kubeic_checker.credentials import build_image_credentials
        creds = [
            self._cred("r.corp.io", "secret-b", user="b"),
            self._cred("r.corp.io", "secret-a", user="a"),
        ]
        pods = [self._pod("r.corp.io/org/app:v1", ["secret-a", "secret-b"])]
        result = build_image_credentials(pods, creds)
        users = [c.username for c in result["r.corp.io/org/app:v1"]]
        assert users == ["a", "b"]  # pod's imagePullSecrets order, not resolve order

    def test_unreferenced_secret_not_used(self):
        # The core of the last-cred-wins fix: a secret no pod references must
        # never be a candidate for that pod's images.
        from kubeic_checker.credentials import build_image_credentials
        creds = [
            self._cred("r.corp.io", "referenced"),
            self._cred("r.corp.io", "unreferenced"),
        ]
        pods = [self._pod("r.corp.io/org/app:v1", ["referenced"])]
        result = build_image_credentials(pods, creds)
        sources = [c.source for c in result["r.corp.io/org/app:v1"]]
        assert sources == ["pod:imagePullSecret:referenced"]

    def test_other_registry_not_matched(self):
        from kubeic_checker.credentials import build_image_credentials
        creds = [self._cred("ghcr.io", "secret-a")]
        pods = [self._pod("r.corp.io/org/app:v1", ["secret-a"])]
        result = build_image_credentials(pods, creds)
        assert result["r.corp.io/org/app:v1"] == []

    def test_docker_hub_spellings_match(self):
        from kubeic_checker.credentials import build_image_credentials
        creds = [self._cred("https://index.docker.io/v1/", "hub-secret")]
        pods = [self._pod("nginx:1.25", ["hub-secret"])]
        result = build_image_credentials(pods, creds)
        assert len(result["nginx:1.25"]) == 1

    def test_dedupes_across_pods(self):
        from kubeic_checker.credentials import build_image_credentials
        creds = [self._cred("r.corp.io", "secret-a")]
        pods = [
            self._pod("r.corp.io/org/app:v1", ["secret-a"]),
            self._pod("r.corp.io/org/app:v1", ["secret-a"]),
        ]
        result = build_image_credentials(pods, creds)
        assert len(result["r.corp.io/org/app:v1"]) == 1

    def test_init_containers_included(self):
        from kubeic_checker.credentials import build_image_credentials
        creds = [self._cred("r.corp.io", "secret-a")]
        pod = self._pod("r.corp.io/org/app:v1", ["secret-a"])
        pod["spec"]["initContainers"] = [{"name": "init", "image": "r.corp.io/org/init:v1"}]
        result = build_image_credentials([pod], creds)
        assert len(result["r.corp.io/org/init:v1"]) == 1
