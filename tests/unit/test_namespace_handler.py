from unittest.mock import patch, MagicMock

from kubeic_operator.handlers.namespace import (
    _should_audit,
    on_namespace_create,
    on_namespace_delete,
)


class TestShouldAudit:
    @patch("kubeic_operator.handlers.namespace.EXCLUDED_NAMESPACES", {"kube-public"})
    def test_excludes_configured_namespaces(self):
        assert _should_audit("kube-public", {}, {}) is False

    def test_allows_normal_namespace(self):
        assert _should_audit("my-app", {}, {}) is True

    def test_respects_exclude_labels(self):
        policy = {"namespaceSelector": {"excludeLabels": {"audit": "disabled"}}}
        labels = {"audit": "disabled"}
        assert _should_audit("my-app", labels, policy) is False

    def test_allows_namespace_without_exclude_label(self):
        policy = {"namespaceSelector": {"excludeLabels": {"audit": "disabled"}}}
        labels = {"audit": "enabled"}
        assert _should_audit("my-app", labels, policy) is True

    def test_allows_when_no_labels_on_namespace(self):
        policy = {"namespaceSelector": {"excludeLabels": {"audit": "disabled"}}}
        assert _should_audit("my-app", None, policy) is True


class TestOnNamespaceCreate:
    @patch("kubeic_operator.handlers.namespace.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace.deploy_checker")
    def test_deploys_checker_for_normal_namespace(self, mock_deploy, mock_policy, mock_secrets):
        mock_policy.return_value = {}
        meta = MagicMock()
        meta.name = "my-app"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)
        mock_deploy.assert_called_once()

    @patch("kubeic_operator.handlers.namespace.EXCLUDED_NAMESPACES", {"kube-system"})
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace.deploy_checker")
    def test_skips_excluded_namespace(self, mock_deploy, mock_policy):
        mock_policy.return_value = {}
        meta = MagicMock()
        meta.name = "kube-system"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)
        mock_deploy.assert_not_called()

    @patch("kubeic_operator.handlers.namespace.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace.deploy_checker")
    def test_passes_policy_settings_to_deployer(self, mock_deploy, mock_policy, mock_secrets):
        mock_policy.return_value = {
            "availability": {"intervalMinutes": 60},
            "credentialSource": {"type": "workloadIdentity"},
        }
        meta = MagicMock()
        meta.name = "my-app"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)
        mock_deploy.assert_called_once_with(
            namespace="my-app",
            check_interval_minutes=60,
            credential_source="workloadIdentity",
            secret_names=None,
        )


class TestOnNamespaceDelete:
    @patch("kubeic_operator.handlers.namespace.teardown_checker")
    def test_tears_down_checker(self, mock_teardown):
        meta = MagicMock()
        meta.name = "my-app"

        on_namespace_delete(body={}, meta=meta)
        mock_teardown.assert_called_once_with("my-app")
