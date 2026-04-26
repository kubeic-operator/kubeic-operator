from unittest.mock import patch, MagicMock

from kubernetes.client import ApiException


def _make_namespace(name, labels=None):
    ns = MagicMock()
    ns.metadata.name = name
    ns.metadata.labels = labels or {}
    return ns


def _404():
    return ApiException(status=404, reason="Not Found")


class TestReconcileCheckers:
    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_deploys_checker_when_missing(self, mock_core, mock_apps_cls, mock_policy,
                                          mock_should, mock_deploy, mock_teardown, mock_secrets):
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _404()
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("my-app"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_deploy.assert_called_once()
        mock_teardown.assert_not_called()
        assert result["my-app"]["deployed"] is True

    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_teardowns_checker_when_excluded(self, mock_core, mock_apps_cls, mock_policy,
                                             mock_should, mock_deploy, mock_teardown):
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("excluded-ns", {"audit": "disabled"}),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_teardown.assert_called_once_with("excluded-ns")
        mock_deploy.assert_not_called()
        assert result["excluded-ns"]["deployed"] is False

    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_no_action_when_state_correct(self, mock_core, mock_apps_cls, mock_policy,
                                          mock_should, mock_deploy, mock_teardown):
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("my-app"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_deploy.assert_not_called()
        mock_teardown.assert_not_called()
        assert result["my-app"]["deployed"] is True

    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_no_action_when_excluded_and_no_checker(self, mock_core, mock_apps_cls,
                                                    mock_policy, mock_should,
                                                    mock_deploy, mock_teardown):
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _404()
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("excluded-ns"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_deploy.assert_not_called()
        mock_teardown.assert_not_called()
        assert "excluded-ns" not in result
