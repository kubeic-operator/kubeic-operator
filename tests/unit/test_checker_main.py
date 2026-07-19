from unittest.mock import MagicMock, patch

from kubeic_checker.main import _get_pods, _check_credential_validity


def _make_mock_pod(
    name="my-pod",
    annotations=None,
    containers=None,
    init_containers=None,
    image_pull_secrets=None,
):
    """Build a MagicMock that looks like a kubernetes client V1Pod."""
    if containers is None:
        containers = []
    if init_containers is None:
        init_containers = []
    if image_pull_secrets is None:
        image_pull_secrets = []

    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.annotations = annotations

    mock_containers = []
    for c in containers:
        mc = MagicMock()
        mc.name = c["name"]
        mc.image = c["image"]
        mock_containers.append(mc)
    pod.spec.containers = mock_containers

    mock_init = []
    for c in init_containers:
        mc = MagicMock()
        mc.name = c["name"]
        mc.image = c["image"]
        mock_init.append(mc)
    pod.spec.init_containers = mock_init if init_containers is not None else None

    mock_secrets = []
    for s in image_pull_secrets:
        ms = MagicMock()
        ms.name = s["name"]
        mock_secrets.append(ms)
    pod.spec.image_pull_secrets = mock_secrets if image_pull_secrets is not None else None

    return pod


class TestGetPods:
    @patch("kubeic_checker.main.client")
    def test_returns_empty_list_when_no_pods(self, mock_client_module):
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value.items = []
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("my-ns")

        assert result == []
        mock_v1.list_namespaced_pod.assert_called_once_with("my-ns")

    @patch("kubeic_checker.main.client")
    def test_returns_pod_dicts_with_all_fields(self, mock_client_module):
        pod = _make_mock_pod(
            name="app-pod",
            annotations={"note": "test"},
            containers=[{"name": "main", "image": "nginx:1.25"}],
            init_containers=[{"name": "init", "image": "busybox:1.36"}],
            image_pull_secrets=[{"name": "my-secret"}],
        )
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value.items = [pod]
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("prod")

        assert len(result) == 1
        p = result[0]
        assert p["metadata"]["name"] == "app-pod"
        assert p["metadata"]["namespace"] == "prod"
        assert p["metadata"]["annotations"] == {"note": "test"}
        assert p["spec"]["containers"] == [{"name": "main", "image": "nginx:1.25"}]
        assert p["spec"]["initContainers"] == [{"name": "init", "image": "busybox:1.36"}]
        assert p["spec"]["imagePullSecrets"] == [{"name": "my-secret"}]

    @patch("kubeic_checker.main.client")
    def test_handles_none_init_containers_and_image_pull_secrets(self, mock_client_module):
        pod = _make_mock_pod(
            name="bare-pod",
            containers=[{"name": "app", "image": "alpine:3.19"}],
            init_containers=None,
            image_pull_secrets=None,
        )
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value.items = [pod]
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("default")

        assert len(result) == 1
        p = result[0]
        assert p["spec"]["containers"] == [{"name": "app", "image": "alpine:3.19"}]
        assert p["spec"]["initContainers"] == []
        assert p["spec"]["imagePullSecrets"] == []

    @patch("kubeic_checker.main.client")
    def test_returns_multiple_pods(self, mock_client_module):
        pods = [
            _make_mock_pod(name="pod-a", containers=[{"name": "c1", "image": "img1"}]),
            _make_mock_pod(name="pod-b", containers=[{"name": "c2", "image": "img2"}]),
        ]
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value.items = pods
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("test-ns")

        assert len(result) == 2
        assert result[0]["metadata"]["name"] == "pod-a"
        assert result[1]["metadata"]["name"] == "pod-b"

    @patch("kubeic_checker.main.client")
    def test_annotations_default_to_empty_dict_when_none(self, mock_client_module):
        pod = _make_mock_pod(name="no-anno", annotations=None)
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value.items = [pod]
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("ns")

        assert result[0]["metadata"]["annotations"] == {}


class TestCheckCredentialValidity:
    def _make_cred(self, registry="r.io", source="pod:imagePullSecret:my-secret",
                   auth="dG9rZW4=", username=None, password=None):
        cred = MagicMock()
        cred.registry = registry
        cred.source = source
        cred.auth = auth
        cred.username = username
        cred.password = password
        return cred

    def _make_pods(self, secrets_images):
        """Build pod dicts. secrets_images = {"secret-name": {"image": "r.io/app/img"}}"""
        pods = []
        for secret_name, images in secrets_images.items():
            pod = {
                "metadata": {"name": "pod1", "namespace": "ns"},
                "spec": {
                    "containers": [{"name": "c", "image": img} for img in images],
                    "initContainers": [],
                    "imagePullSecrets": [{"name": secret_name}],
                },
            }
            pods.append(pod)
        return pods

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_valid_credential(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (True, None, "")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], "ns", pods)

        mock_gauge.labels.assert_called_once_with(
            registry="r.io", namespace="ns", secret_name="my-secret",
        )
        mock_gauge.labels.return_value.set.assert_called_once_with(1)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_auth_failure_marks_invalid(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (False, "unauthorized", "auth_failure")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], "ns", pods)

        mock_gauge.labels.return_value.set.assert_called_once_with(0)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_network_error_falls_back_to_inspect(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (False, "timeout", "network")
        mock_inspect.return_value = (True, None, {}, "")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], "ns", pods)

        mock_gauge.labels.return_value.set.assert_called_once_with(1)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_no_matching_images_marks_invalid(self, mock_list_tags, mock_inspect, mock_gauge):
        cred = self._make_cred()
        pods = self._make_pods({"other-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], "ns", pods)

        mock_gauge.labels.return_value.set.assert_called_once_with(0)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_skips_creds_without_auth_or_credentials(self, mock_list_tags, mock_inspect, mock_gauge):
        cred = MagicMock()
        cred.registry = "r.io"
        cred.source = "pod:imagePullSecret:my-secret"
        cred.auth = None
        cred.username = None
        cred.password = None

        _check_credential_validity([cred], "ns", [])

        mock_gauge.labels.assert_not_called()

    @patch("kubeic_checker.main.CREDENTIAL_TEST_IMAGE", "r.io/test/img")
    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_uses_credential_test_image_when_set(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (True, None, "")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], "ns", pods)

        # Should be called with the test image, not a pod image
        call_args = mock_list_tags.call_args[0]
        assert call_args[0] == "r.io/test/img"
