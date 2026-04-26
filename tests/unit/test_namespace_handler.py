from unittest.mock import patch, MagicMock

from kubernetes.client import ApiException

from kubeic_operator.handlers.namespace import (
    _get_effective_policy,
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


class TestGetEffectivePolicy:
    @patch("kubeic_operator.handlers.namespace.client.CustomObjectsApi")
    def test_namespace_scoped_policy_takes_priority(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [{"spec": {"prerelease": {"maxAgeDays": 14}}}]
        }

        result = _get_effective_policy("my-ns")

        assert result == {"prerelease": {"maxAgeDays": 14}}
        mock_api.list_namespaced_custom_object.assert_called_once_with(
            "imageaudit.kubeic.io", "v1alpha1", "my-ns", "imageauditpolicies",
        )
        mock_api.get_namespaced_custom_object.assert_not_called()

    @patch("kubeic_operator.handlers.namespace.client.CustomObjectsApi")
    def test_falls_back_to_cluster_defaults(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        mock_api.list_namespaced_custom_object.return_value = {"items": []}
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {"prerelease": {"maxAgeDays": 7}}
        }

        result = _get_effective_policy("my-ns")

        assert result == {"prerelease": {"maxAgeDays": 7}}
        mock_api.get_namespaced_custom_object.assert_called_once()

    @patch("kubeic_operator.handlers.namespace.client.CustomObjectsApi")
    def test_returns_empty_dict_when_no_policy_found(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        mock_api.list_namespaced_custom_object.return_value = {"items": []}
        mock_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        result = _get_effective_policy("my-ns")

        assert result == {}

    @patch("kubeic_operator.handlers.namespace.client.CustomObjectsApi")
    def test_namespace_policy_404_falls_back_to_cluster_defaults(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        mock_api.list_namespaced_custom_object.side_effect = ApiException(status=404)
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {"availability": {"intervalMinutes": 60}}
        }

        result = _get_effective_policy("my-ns")

        assert result == {"availability": {"intervalMinutes": 60}}

    @patch("kubeic_operator.handlers.namespace.client.CustomObjectsApi")
    def test_non_404_api_error_still_falls_back(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        mock_api.list_namespaced_custom_object.side_effect = ApiException(status=500)
        mock_api.get_namespaced_custom_object.return_value = {
            "spec": {"credentialSource": {"type": "workloadIdentity"}}
        }

        result = _get_effective_policy("my-ns")

        assert result == {"credentialSource": {"type": "workloadIdentity"}}


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
