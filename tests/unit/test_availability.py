from unittest.mock import patch, MagicMock
import subprocess

from kubeic_checker.availability import check_availability, write_auth_config


def _make_pod(name, namespace, image):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"containers": [{"name": "main", "image": image}]},
    }


class TestCheckAvailability:
    @patch("kubeic_checker.availability.subprocess.run")
    def test_image_available(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}")
        pods = [_make_pod("pod-1", "default", "nginx:1.25")]
        results = check_availability(pods)
        assert len(results) == 1
        assert results[0].available is True
        assert results[0].error is None

    @patch("kubeic_checker.availability.subprocess.run")
    def test_image_unavailable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="image not found")
        pods = [_make_pod("pod-1", "default", "nginx:nonexistent")]
        results = check_availability(pods)
        assert len(results) == 1
        assert results[0].available is False
        assert "image not found" in results[0].error

    @patch("kubeic_checker.availability.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("skopeo", 30)
        pods = [_make_pod("pod-1", "default", "slow-registry.corp.com/app:v1")]
        results = check_availability(pods)
        assert results[0].available is False
        assert "timed out" in results[0].error

    @patch("kubeic_checker.availability.subprocess.run")
    def test_skopeo_not_installed(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        pods = [_make_pod("pod-1", "default", "nginx:1.25")]
        results = check_availability(pods)
        assert results[0].available is False
        assert "not found" in results[0].error

    @patch("kubeic_checker.availability.subprocess.run")
    def test_auth_file_passed_to_skopeo(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}")
        pods = [_make_pod("pod-1", "default", "nginx:1.25")]
        check_availability(pods, auth_file="/tmp/config.json")
        cmd = mock_run.call_args[0][0]
        assert "--authfile" in cmd
        assert "/tmp/config.json" in cmd

    @patch("kubeic_checker.availability.subprocess.run")
    def test_multiple_containers(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}")
        pods = [{
            "metadata": {"name": "pod-1", "namespace": "default"},
            "spec": {
                "containers": [{"name": "main", "image": "nginx:1.25"}],
                "initContainers": [{"name": "init", "image": "busybox:latest"}],
            },
        }]
        results = check_availability(pods)
        assert len(results) == 2


class TestWriteAuthConfig:
    def test_writes_auth_from_user_pass(self, tmp_path):
        secrets = {
            "https://registry.example.com": {
                "username": "user",
                "password": "pass",
            }
        }
        path = str(tmp_path / "config.json")
        write_auth_config(secrets, path)

        import json
        with open(path) as f:
            config = json.load(f)

        assert "auths" in config
        assert "https://registry.example.com" in config["auths"]
        assert "auth" in config["auths"]["https://registry.example.com"]

    def test_writes_auth_from_base64(self, tmp_path):
        import base64
        auth = base64.b64encode(b"user:pass").decode()
        secrets = {"registry.corp.com": {"auth": auth}}
        path = str(tmp_path / "config.json")
        write_auth_config(secrets, path)

        import json
        with open(path) as f:
            config = json.load(f)

        assert config["auths"]["registry.corp.com"]["auth"] == auth
