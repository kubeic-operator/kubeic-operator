import sys
from unittest.mock import patch, MagicMock

# Mock kopf before importing the policy module to prevent decorator side-effects
# in environments where kopf's runtime isn't fully initialised (e.g. CI).
# Decorators pass through the original function unchanged.
_mock_kopf = MagicMock()
_mock_kopf.on.create.side_effect = lambda *a, **kw: lambda f: f
_mock_kopf.on.update.side_effect = lambda *a, **kw: lambda f: f
_mock_kopf.on.delete.side_effect = lambda *a, **kw: lambda f: f
_mock_kopf.Meta = MagicMock
sys.modules.setdefault("kopf", _mock_kopf)

from kubeic_operator.handlers.policy import (
    _reconcile_all_namespaces,
    on_policy_change,
    on_policy_delete,
)


def _make_namespace(name, labels=None):
    ns = MagicMock()
    ns.metadata.name = name
    ns.metadata.labels = labels
    return ns


class TestReconcileAllNamespacesNoAuditable:
    """When no namespaces should be audited, deploy_checker is never called."""

    @patch("kubeic_operator.handlers.policy.get_secret_names_for_namespace")
    @patch("kubeic_operator.handlers.policy.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.policy.client")
    def test_no_namespaces_audited(
        self, mock_client, mock_should_audit, mock_get_policy, mock_deploy, mock_secrets
    ):
        namespaces = [_make_namespace("kube-system"), _make_namespace("kube-public")]
        mock_client.CoreV1Api.return_value.list_namespace.return_value.items = namespaces
        mock_get_policy.return_value = {}

        _reconcile_all_namespaces()

        assert mock_should_audit.call_count == 2
        mock_deploy.assert_not_called()


class TestReconcileAllNamespacesMixed:
    """deploy_checker is called only for auditable namespaces."""

    @patch("kubeic_operator.handlers.policy.get_secret_names_for_namespace")
    @patch("kubeic_operator.handlers.policy.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace._should_audit")
    @patch("kubeic_operator.handlers.policy.client")
    def test_only_auditable_namespaces_get_checker(
        self, mock_client, mock_should_audit, mock_get_policy, mock_deploy, mock_secrets
    ):
        ns_app = _make_namespace("my-app", {"env": "prod"})
        ns_system = _make_namespace("kube-system")
        namespaces = [ns_app, ns_system]
        mock_client.CoreV1Api.return_value.list_namespace.return_value.items = namespaces

        mock_get_policy.return_value = {}
        mock_should_audit.side_effect = lambda name, labels, policy: name == "my-app"
        mock_secrets.return_value = ["my-secret"]

        _reconcile_all_namespaces()

        assert mock_should_audit.call_count == 2
        mock_deploy.assert_called_once()
        assert mock_deploy.call_args.kwargs["namespace"] == "my-app"


class TestReconcileAllNamespacesPolicySettings:
    """Policy settings (interval, credential source) are forwarded to deploy_checker."""

    @patch("kubeic_operator.handlers.policy.get_secret_names_for_namespace")
    @patch("kubeic_operator.handlers.policy.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.policy.client")
    def test_passes_policy_settings(
        self, mock_client, mock_should_audit, mock_get_policy, mock_deploy, mock_secrets
    ):
        namespaces = [_make_namespace("my-app")]
        mock_client.CoreV1Api.return_value.list_namespace.return_value.items = namespaces

        mock_get_policy.return_value = {
            "availability": {"intervalMinutes": 60},
            "credentialSource": {"type": "workloadIdentity"},
        }
        mock_secrets.return_value = ["pull-secret"]

        _reconcile_all_namespaces()

        mock_deploy.assert_called_once_with(
            namespace="my-app",
            check_interval_minutes=60,
            credential_source="workloadIdentity",
            secret_names=["pull-secret"],
        )

    @patch("kubeic_operator.handlers.policy.get_secret_names_for_namespace")
    @patch("kubeic_operator.handlers.policy.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.policy.client")
    def test_defaults_when_policy_empty(
        self, mock_client, mock_should_audit, mock_get_policy, mock_deploy, mock_secrets
    ):
        namespaces = [_make_namespace("my-app")]
        mock_client.CoreV1Api.return_value.list_namespace.return_value.items = namespaces

        mock_get_policy.return_value = {}
        mock_secrets.return_value = None

        _reconcile_all_namespaces()

        mock_deploy.assert_called_once_with(
            namespace="my-app",
            check_interval_minutes=30,
            credential_source="pullSecret",
            secret_names=None,
        )


class TestOnPolicyChange:
    @patch("kubeic_operator.handlers.policy._reconcile_all_namespaces")
    def test_calls_reconcile(self, mock_reconcile):
        on_policy_change(body={}, meta=MagicMock())
        mock_reconcile.assert_called_once()


class TestOnPolicyDelete:
    @patch("kubeic_operator.handlers.policy._reconcile_all_namespaces")
    def test_calls_reconcile(self, mock_reconcile):
        on_policy_delete(body={}, meta=MagicMock())
        mock_reconcile.assert_called_once()
