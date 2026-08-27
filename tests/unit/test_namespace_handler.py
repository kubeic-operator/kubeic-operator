from unittest.mock import patch, MagicMock

from kubernetes.client import ApiException

from kubeic_operator.handlers.namespace import (
    _get_effective_policy,
    _should_audit,
    _should_deploy_checker,
    on_namespace_create,
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

    @patch("kubeic_operator.handlers.namespace.CHECKER_ENABLED", False)
    def test_returns_false_for_every_namespace_when_checkers_disabled(self):
        # The gate lives here so _reconcile_checkers tears down existing
        # checkers via its normal not-should-but-exists path.
        assert _should_audit("my-app", {}, {}) is False
        assert _should_audit("another", {"audit": "enabled"}, {}) is False

    @patch("kubeic_operator.handlers.namespace.CHECKER_ENABLED", True)
    def test_normal_namespace_still_audited_when_checkers_enabled(self):
        assert _should_audit("my-app", {}, {}) is True


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
    @patch("kubeic_operator.handlers.namespace.deploy_checker_serialised")
    def test_deploys_checker_for_normal_namespace(self, mock_deploy, mock_policy, mock_secrets):
        mock_policy.return_value = {}
        meta = MagicMock()
        meta.name = "my-app"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)
        mock_deploy.assert_called_once()

    @patch("kubeic_operator.handlers.namespace.EXCLUDED_NAMESPACES", {"kube-system"})
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace.deploy_checker_serialised")
    def test_skips_excluded_namespace(self, mock_deploy, mock_policy):
        mock_policy.return_value = {}
        meta = MagicMock()
        meta.name = "kube-system"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)
        mock_deploy.assert_not_called()

    @patch("kubeic_operator.handlers.namespace.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy")
    @patch("kubeic_operator.handlers.namespace.deploy_checker_serialised")
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
            "my-app",
            blocking=False,
            check_interval_minutes=60,
            credential_source="workloadIdentity",
            secret_names=None,
        )


class TestShouldDeployChecker:
    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", False)
    def test_matches_should_audit_in_per_namespace_mode(self):
        assert _should_deploy_checker("my-app", {}, {}) is True

    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", False)
    @patch("kubeic_operator.handlers.namespace.EXCLUDED_NAMESPACES", {"kube-public"})
    def test_still_honours_exclusions_in_per_namespace_mode(self):
        assert _should_deploy_checker("kube-public", {}, {}) is False

    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", True)
    def test_false_for_every_namespace_in_central_mode(self):
        # This is what makes switching mode free: _reconcile_checkers already
        # removes a checker wherever this is False and one exists, so the whole
        # per-namespace fleet drains with no separate migration path.
        assert _should_deploy_checker("my-app", {}, {}) is False
        assert _should_deploy_checker("another", {"audit": "enabled"}, {}) is False

    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", True)
    def test_central_mode_does_not_stop_a_namespace_being_audited(self):
        # The two predicates are deliberately different answers: no pod here,
        # but the namespace is still audited by the central checker, which is
        # what grants it secret access.
        assert _should_deploy_checker("my-app", {}, {}) is False
        assert _should_audit("my-app", {}, {}) is True

    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", True)
    @patch("kubeic_operator.handlers.namespace.CHECKER_ENABLED", False)
    def test_disabled_checkers_still_win_in_central_mode(self):
        assert _should_deploy_checker("my-app", {}, {}) is False
        assert _should_audit("my-app", {}, {}) is False


class TestOnNamespaceCreateCentralMode:
    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", True)
    @patch("kubeic_operator.handlers.namespace.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.handlers.namespace.ensure_central_secret_access")
    @patch("kubeic_operator.handlers.namespace.deploy_checker_serialised")
    def test_grants_secret_access_and_deploys_no_pod(
        self, mock_deploy, mock_grant, mock_policy, mock_secrets,
    ):
        meta = MagicMock()
        meta.name = "my-app"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)

        # Done eagerly rather than left to the next reconcile: until the central
        # checker is bound in, this namespace's private images read as
        # unavailable, and a scan interval is long enough to alert on.
        mock_grant.assert_called_once_with("my-app", None)
        mock_deploy.assert_not_called()

    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", True)
    @patch("kubeic_operator.handlers.namespace.EXCLUDED_NAMESPACES", {"kube-public"})
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.handlers.namespace.ensure_central_secret_access")
    def test_grants_nothing_for_an_excluded_namespace(self, mock_grant, mock_policy):
        meta = MagicMock()
        meta.name = "kube-public"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)

        mock_grant.assert_not_called()

    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", True)
    @patch("kubeic_operator.handlers.namespace.get_secret_names_for_namespace", return_value=[])
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.handlers.namespace.ensure_central_secret_access")
    def test_passes_through_no_secret_configuration(self, mock_grant, mock_policy, mock_secrets):
        # noSecretNamespaces keeps working in central mode; the deployer reads []
        # as "remove any grant".
        meta = MagicMock()
        meta.name = "kube-system"
        meta.labels = {}

        on_namespace_create(body={}, meta=meta)

        mock_grant.assert_called_once_with("kube-system", [])
