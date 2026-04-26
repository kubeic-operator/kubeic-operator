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

    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={
        "namespaceSelector": {"excludeLabels": {"audit": "disabled"}},
    })
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_excluded_reason_includes_matching_label(self, mock_core, mock_apps_cls,
                                                     mock_policy, mock_should,
                                                     mock_deploy, mock_teardown):
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("my-ns", {"audit": "disabled"}),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert result["my-ns"]["reason"] == "excluded by label audit=disabled"


class TestRunClusterAudit:
    @patch("kubeic_operator.main.update_spread_metrics")
    @patch("kubeic_operator.main.update_prerelease_metrics")
    @patch("kubeic_operator.main.aggregate_version_spread", return_value=[])
    @patch("kubeic_operator.main.filter_violations", return_value=[])
    @patch("kubeic_operator.main.check_prerelease", return_value=[])
    @patch("kubeic_operator.main._get_default_policy", return_value={})
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_calls_prerelease_check_with_policy_settings(self, mock_core_cls, mock_policy,
                                                          mock_prerelease, mock_filter,
                                                          mock_spread, mock_pre_metrics,
                                                          mock_spread_metrics):
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.namespace = "default"
        mock_pod.metadata.creation_timestamp = None
        mock_pod.metadata.annotations = {}
        mock_pod.status.start_time = None
        mock_pod.spec.containers = []
        mock_pod.spec.init_containers = []
        mock_core_cls.return_value.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        from kubeic_operator.main import _run_cluster_audit
        _run_cluster_audit()

        mock_prerelease.assert_called_once()
        mock_filter.assert_called_once()
        mock_spread.assert_called_once()
        mock_pre_metrics.assert_called_once()
        mock_spread_metrics.assert_called_once()

    @patch("kubeic_operator.main.update_spread_metrics")
    @patch("kubeic_operator.main.update_prerelease_metrics")
    @patch("kubeic_operator.main.aggregate_version_spread", return_value=[])
    @patch("kubeic_operator.main.filter_violations", return_value=[])
    @patch("kubeic_operator.main.check_prerelease", return_value=[])
    @patch("kubeic_operator.main._get_default_policy", return_value={
        "prerelease": {"maxAgeDays": 14},
        "versionSpread": {"threshold": 5},
        "skipAnnotation": "kubeic.io/skip",
    })
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_passes_policy_config_to_checks(self, mock_core_cls, mock_policy,
                                             mock_prerelease, mock_filter,
                                             mock_spread, mock_pre_metrics,
                                             mock_spread_metrics):
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.namespace = "default"
        mock_pod.metadata.creation_timestamp = None
        mock_pod.metadata.annotations = {}
        mock_pod.status.start_time = None
        mock_pod.spec.containers = []
        mock_pod.spec.init_containers = []
        mock_core_cls.return_value.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        from kubeic_operator.main import _run_cluster_audit
        _run_cluster_audit()

        prerelease_call = mock_prerelease.call_args
        assert prerelease_call.kwargs["max_age_days"] == 14
        assert prerelease_call.kwargs["skip_annotation"] == "kubeic.io/skip"

        spread_call = mock_spread.call_args
        assert spread_call.kwargs["threshold"] == 5
        assert spread_call.kwargs["skip_annotation"] == "kubeic.io/skip"

    @patch("kubeic_operator.main.update_spread_metrics")
    @patch("kubeic_operator.main.update_prerelease_metrics")
    @patch("kubeic_operator.main.aggregate_version_spread", return_value=[])
    @patch("kubeic_operator.main.filter_violations", return_value=[])
    @patch("kubeic_operator.main.check_prerelease", return_value=[])
    @patch("kubeic_operator.main._get_default_policy", return_value={})
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_handles_api_failure_gracefully(self, mock_core_cls, mock_policy,
                                             mock_prerelease, mock_filter,
                                             mock_spread, mock_pre_metrics,
                                             mock_spread_metrics):
        mock_core_cls.return_value.list_pod_for_all_namespaces.side_effect = ApiException(status=500)

        from kubeic_operator.main import _run_cluster_audit
        _run_cluster_audit()

        mock_prerelease.assert_not_called()
        mock_spread.assert_not_called()


class TestWriteIapStatus:
    @patch("kubeic_operator.main.client.CustomObjectsApi")
    def test_patches_status_with_reconcile_results(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api

        from kubeic_operator.main import _write_iap_status
        _write_iap_status({"my-ns": {"deployed": True}})

        mock_api.patch_namespaced_custom_object_status.assert_called_once()
        call_kwargs = mock_api.patch_namespaced_custom_object_status.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body") or call_kwargs[0][5]
        assert body["status"]["namespaces"]["my-ns"]["deployed"] is True
        assert "lastReconcileTime" in body["status"]

    @patch("kubeic_operator.main.client.CustomObjectsApi")
    def test_logs_warning_on_api_failure(self, mock_api_cls):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        mock_api.patch_namespaced_custom_object_status.side_effect = ApiException(status=403)

        from kubeic_operator.main import _write_iap_status
        _write_iap_status({"my-ns": {"deployed": True}})

        mock_api.patch_namespaced_custom_object_status.assert_called_once()
