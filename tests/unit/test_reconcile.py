import logging
from unittest.mock import patch, MagicMock

import pytest
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
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_deploys_checker_when_missing(self, mock_core, mock_apps_cls, mock_policy,
                                          mock_should, mock_deploy, mock_wait, mock_teardown, mock_secrets):
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
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_teardowns_checker_when_excluded(self, mock_core, mock_apps_cls, mock_policy,
                                             mock_should, mock_deploy, mock_wait, mock_teardown):
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("excluded-ns", {"audit": "disabled"}),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_teardown.assert_called_once_with("excluded-ns")
        mock_deploy.assert_not_called()
        assert result["excluded-ns"]["deployed"] is False

    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_no_action_when_state_correct(self, mock_core, mock_apps_cls, mock_policy,
                                          mock_should, mock_deploy, mock_wait, mock_teardown):
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("my-app"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_deploy.assert_not_called()
        mock_teardown.assert_not_called()
        assert result["my-app"]["deployed"] is True

    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_no_action_when_excluded_and_no_checker(self, mock_core, mock_apps_cls,
                                                    mock_policy, mock_should,
                                                    mock_deploy, mock_wait, mock_teardown):
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
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={
        "namespaceSelector": {"excludeLabels": {"audit": "disabled"}},
    })
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_excluded_reason_includes_matching_label(self, mock_core, mock_apps_cls,
                                                     mock_policy, mock_should,
                                                     mock_deploy, mock_wait, mock_teardown):
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
    def test_terminated_pods_excluded_from_cluster_audit(self, mock_core_cls, mock_policy,
                                                          mock_prerelease, mock_filter,
                                                          mock_spread, mock_pre_metrics,
                                                          mock_spread_metrics):
        # Lingering Succeeded/Failed pods (dead CI job pods) must not feed
        # prerelease/spread checks — nothing is running their images.
        def pod(name, phase):
            p = MagicMock()
            p.metadata.name = name
            p.metadata.namespace = "default"
            p.metadata.creation_timestamp = None
            p.metadata.annotations = {}
            p.status.start_time = None
            p.status.phase = phase
            p.spec.containers = []
            p.spec.init_containers = []
            return p

        mock_core_cls.return_value.list_pod_for_all_namespaces.return_value.items = [
            pod("running", "Running"), pod("done", "Succeeded"), pod("dead", "Failed"),
        ]

        from kubeic_operator.main import _run_cluster_audit
        _run_cluster_audit()

        audited = [p["metadata"]["name"] for p in mock_prerelease.call_args[0][0]]
        assert audited == ["running"]

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


def _failure_count(namespace, operation, error_class="internal"):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(
        "kube_image_checker_reconcile_failures_total",
        {"namespace": namespace, "operation": operation, "error_class": error_class},
    ) or 0.0


class TestReconcileFailureVisibility:
    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker", side_effect=RuntimeError("boom"))
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_failed_deploy_is_reported_as_not_deployed(
        self, mock_core, mock_apps_cls, mock_policy, mock_should,
        mock_deploy, mock_wait, mock_teardown, mock_secrets,
    ):
        # The bug this replaces: status said deployed: true even when every
        # single pass failed, so the IAP could not distinguish a healthy
        # namespace from a permanently broken one.
        before = _failure_count("failing-ns", "deploy")
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _404()
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("failing-ns"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert result["failing-ns"]["deployed"] is False
        assert "deploy failed" in result["failing-ns"]["reason"]
        assert "RuntimeError" in result["failing-ns"]["reason"]
        assert _failure_count("failing-ns", "deploy") == before + 1

    @patch("kubeic_operator.deployer.teardown_checker", side_effect=RuntimeError("boom"))
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_failed_teardown_still_reports_the_checker_as_deployed(
        self, mock_core, mock_apps_cls, mock_policy, mock_should, mock_teardown,
    ):
        # Teardown failed, so the checker is still running — reporting
        # deployed: false would be the same class of lie in the other direction.
        before = _failure_count("stuck-ns", "teardown")
        mock_apps_cls.return_value = MagicMock()
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("stuck-ns"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert result["stuck-ns"]["deployed"] is True
        assert "teardown failed" in result["stuck-ns"]["reason"]
        assert _failure_count("stuck-ns", "teardown") == before + 1

    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_one_failing_namespace_does_not_stop_the_others(
        self, mock_core, mock_apps_cls, mock_policy, mock_should,
        mock_deploy, mock_wait, mock_secrets,
    ):
        # Continue-on-failure is deliberate and must survive this change.
        mock_deploy.side_effect = [RuntimeError("boom"), None, None]
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _404()
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("bad"), _make_namespace("good-1"), _make_namespace("good-2"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert result["bad"]["deployed"] is False
        assert result["good-1"]["deployed"] is True
        assert result["good-2"]["deployed"] is True

    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker", side_effect=RuntimeError("boom"))
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_failure_is_logged_with_a_traceback(
        self, mock_core, mock_apps_cls, mock_policy, mock_should,
        mock_deploy, mock_wait, mock_secrets, caplog,
    ):
        # logger.error("...: %s", exc) prints only str(exc); for a TypeError or
        # AttributeError that is a bare message with no origin.
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _404()
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("failing-ns"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        with caplog.at_level(logging.ERROR, logger="kubeic-operator"):
            _reconcile_checkers()

        records = [r for r in caplog.records if "Failed to deploy checker" in r.getMessage()]
        assert records and records[0].exc_info is not None


class TestProbeResilience:
    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_a_non_404_probe_error_does_not_abort_the_whole_pass(
        self, mock_core, mock_apps_cls, mock_policy, mock_should,
        mock_deploy, mock_wait, mock_secrets,
    ):
        # This used to re-raise, escaping the loop: every namespace after the
        # failing one went unreconciled, and the pass aborted before the status
        # was written, so the staleness was silent.
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = [
            ApiException(status=403, reason="Forbidden"),
            _404(),
            _404(),
        ]
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("forbidden"), _make_namespace("after-1"), _make_namespace("after-2"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert result["forbidden"]["deployed"] is False
        assert "state unknown" in result["forbidden"]["reason"]
        # The namespaces behind it were still reconciled.
        assert result["after-1"]["deployed"] is True
        assert result["after-2"]["deployed"] is True
        assert mock_deploy.call_count == 2

    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_probe_failure_is_counted_as_an_api_error(
        self, mock_core, mock_apps_cls, mock_policy, mock_should,
    ):
        before = _failure_count("probe-ns", "probe", "api")
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = ApiException(status=503)
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("probe-ns"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        _reconcile_checkers()

        assert _failure_count("probe-ns", "probe", "api") == before + 1

    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_a_404_still_means_no_checker_rather_than_a_failure(
        self, mock_core, mock_apps_cls, mock_policy, mock_should,
    ):
        # 404 is the normal "not deployed yet" answer and must not be counted.
        before = _failure_count("fresh-ns", "probe", "api")
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _404()
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("fresh-ns"),
        ]

        with patch("kubeic_operator.deployer.deploy_checker"), \
             patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True), \
             patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None):
            from kubeic_operator.main import _reconcile_checkers
            result = _reconcile_checkers()

        assert result["fresh-ns"]["deployed"] is True
        assert _failure_count("fresh-ns", "probe", "api") == before


class TestErrorClass:
    def test_api_exceptions_are_operational(self):
        from kubeic_operator.main import _error_class
        assert _error_class(ApiException(status=403)) == "api"
        assert _error_class(ApiException(status=503)) == "api"

    def test_anything_else_is_an_operator_defect(self):
        # The distinction #66 wanted from narrowing the except clauses, without
        # giving up continue-on-failure across namespaces.
        from kubeic_operator.main import _error_class
        assert _error_class(TypeError("bad")) == "internal"
        assert _error_class(AttributeError("bad")) == "internal"
        assert _error_class(RuntimeError("bad")) == "internal"


class TestDiffBaseEviction:
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_reconcile_prunes_essences_for_namespaces_that_are_gone(
        self, mock_core, mock_apps_cls, mock_policy, mock_should,
    ):
        # Kopf has no eviction hook on the diff-base interface, so reconcile
        # prunes against the namespace list it already fetches. Without this the
        # dict grows for the operator's whole lifetime.
        from kubeic_operator.main import DIFFBASE_STORAGE

        live, dead = _make_namespace("live"), _make_namespace("dead")
        live.metadata.uid = "uid-live"
        dead.metadata.uid = "uid-dead"

        DIFFBASE_STORAGE.store(
            body={"metadata": {"uid": "uid-live"}}, patch={}, essence={"n": 1},
        )
        DIFFBASE_STORAGE.store(
            body={"metadata": {"uid": "uid-dead"}}, patch={}, essence={"n": 2},
        )

        mock_apps_cls.return_value = MagicMock()
        mock_core.return_value.list_namespace.return_value.items = [live]

        from kubeic_operator.main import _reconcile_checkers
        _reconcile_checkers()

        assert DIFFBASE_STORAGE.fetch(body={"metadata": {"uid": "uid-live"}}) == {"n": 1}
        assert DIFFBASE_STORAGE.fetch(body={"metadata": {"uid": "uid-dead"}}) is None


class TestFailureReason:
    def test_collapses_newlines_and_includes_the_type(self):
        from kubeic_operator.main import _failure_reason
        reason = _failure_reason(ValueError("line one\nline two"))
        assert "\n" not in reason
        assert reason.startswith("ValueError: ")

    def test_truncates_long_messages(self):
        # ApiException stringifies to a full dump of headers and body.
        from kubeic_operator.main import _failure_reason
        assert len(_failure_reason(ValueError("x" * 5000))) == 200


class _StopLoop(Exception):
    """Breaks out of the audit loop's infinite while."""


class TestRolloutPacing:
    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.handlers.namespace._should_audit", return_value=True)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_bootstrap_waits_for_each_checker_before_starting_the_next(
        self, mock_core, mock_policy, mock_should, mock_secrets,
    ):
        # The whole point of #61: deploys must not overlap. Asserting the call
        # order proves serialisation, where asserting "wait was called" would
        # still pass if all three deploys fired first.
        calls = []
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("a"), _make_namespace("b"), _make_namespace("c"),
        ]

        def _deploy(**kwargs):
            calls.append(("deploy", kwargs["namespace"]))

        def _wait(namespace, **kwargs):
            calls.append(("wait", namespace))
            return True

        with patch("kubeic_operator.deployer.deploy_checker", side_effect=_deploy), \
             patch("kubeic_operator.deployer.wait_for_checker_ready", side_effect=_wait):
            from kubeic_operator.main import _bootstrap_checkers
            _bootstrap_checkers()

        assert calls == [
            ("deploy", "a"), ("wait", "a"),
            ("deploy", "b"), ("wait", "b"),
            ("deploy", "c"), ("wait", "c"),
        ]

    @patch("kubeic_operator.main._write_iap_status")
    @patch("kubeic_operator.main._reconcile_checkers", return_value={})
    @patch("kubeic_operator.main._bootstrap_checkers")
    @patch("kubeic_operator.main._run_cluster_audit")
    @patch("kubeic_operator.main.time.sleep", side_effect=_StopLoop)
    def test_audit_loop_bootstraps_before_its_first_sleep(
        self, mock_sleep, mock_audit, mock_bootstrap, mock_reconcile, mock_status,
    ):
        # Bootstrap moved off the kopf startup handler onto this thread, so it
        # must run immediately rather than after the first SCAN_INTERVAL.
        from kubeic_operator.main import _audit_loop
        with pytest.raises(_StopLoop):
            _audit_loop()

        mock_bootstrap.assert_called_once()
        mock_reconcile.assert_called_once()
        # Metrics must not wait on the paced rollout.
        mock_audit.assert_called_once()

    @patch("kubeic_operator.main._write_iap_status")
    @patch("kubeic_operator.main._reconcile_checkers", return_value={})
    @patch("kubeic_operator.main._bootstrap_checkers", side_effect=RuntimeError("boom"))
    @patch("kubeic_operator.main._run_cluster_audit")
    @patch("kubeic_operator.main.time.sleep", side_effect=_StopLoop)
    def test_audit_loop_survives_a_failing_bootstrap(
        self, mock_sleep, mock_audit, mock_bootstrap, mock_reconcile, mock_status,
    ):
        # A crash here would kill the only thread that deploys checkers.
        from kubeic_operator.main import _audit_loop
        with pytest.raises(_StopLoop):
            _audit_loop()

        mock_reconcile.assert_called_once()
class TestCheckerEnabledFlag:
    @patch("kubeic_operator.deployer.CHECKER_ENABLED", False)
    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace.CHECKER_ENABLED", False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_reconcile_tears_down_existing_checkers_when_disabled(
        self, mock_core, mock_apps_cls, mock_policy, mock_deploy, mock_teardown, mock_secrets,
    ):
        # Deployment read succeeds -> a checker exists in this namespace.
        mock_apps_cls.return_value = MagicMock()
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("my-app"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_teardown.assert_called_once_with("my-app")
        mock_deploy.assert_not_called()
        assert result["my-app"]["deployed"] is False
        assert result["my-app"]["reason"] == "checkers disabled"

    @patch("kubeic_operator.deployer.CHECKER_ENABLED", False)
    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.handlers.namespace.CHECKER_ENABLED", False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_reconcile_deploys_nothing_when_disabled_and_none_exist(
        self, mock_core, mock_apps_cls, mock_policy, mock_deploy, mock_teardown,
    ):
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _404()
        mock_apps_cls.return_value = mock_apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("my-app"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        _reconcile_checkers()

        mock_deploy.assert_not_called()
        mock_teardown.assert_not_called()

    @patch("kubeic_operator.deployer.CHECKER_ENABLED", False)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_bootstrap_short_circuits_when_disabled(self, mock_core, mock_deploy):
        from kubeic_operator.main import _bootstrap_checkers
        _bootstrap_checkers()

        mock_deploy.assert_not_called()
        mock_core.return_value.list_namespace.assert_not_called()


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


def _central_deployment(available=1):
    """A central checker Deployment as the API server would return it."""
    dep = MagicMock()
    dep.status.available_replicas = available
    return dep


def _apps_api(central=None, checker_exists=False, checker_error=None):
    """AppsV1Api mock answering the two distinct reads reconcile makes.

    Reconcile reads the central checker Deployment in the operator namespace and
    the per-namespace checker Deployment in each namespace. Dispatching on the
    name rather than returning one canned answer is what makes these tests
    exercise the real branching instead of a single mock.
    """
    from kubeic_operator.deployer import CENTRAL_CHECKER_DEPLOYMENT

    def read(name, namespace):
        if name == CENTRAL_CHECKER_DEPLOYMENT:
            if central is None:
                raise _404()
            return central
        if checker_error is not None:
            raise checker_error
        if checker_exists:
            return MagicMock()
        raise _404()

    api = MagicMock()
    api.read_namespaced_deployment.side_effect = read
    return api


def _raise(exc):
    raise exc


def _central_patches(fn):
    """The patch stack every central-mode reconcile test needs.

    CENTRAL_MODE is read from two modules — deployer for reconcile itself, and
    handlers.namespace for _should_deploy_checker — so both are patched.
    _should_audit and _should_deploy_checker are deliberately NOT patched: the
    point of these tests is that the real predicates disagree in central mode.

    Listed innermost-first, which is the order mock arguments arrive in. The
    two CENTRAL_MODE entries pass an explicit value, so they contribute no
    argument and sit at the end.
    """
    for decorator in [
        patch("kubeic_operator.deployer.deploy_checker"),
        patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True),
        patch("kubeic_operator.deployer.teardown_checker"),
        patch("kubeic_operator.deployer.ensure_central_secret_access"),
        patch("kubeic_operator.deployer.teardown_central_secret_access", return_value=False),
        patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None),
        patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={}),
        patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", True),
        patch("kubeic_operator.deployer.CENTRAL_MODE", True),
    ]:
        fn = decorator(fn)
    return fn


class TestCentralModeReconcile:
    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_drains_per_namespace_checkers_without_a_teardown_path(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # The whole migration story: nothing calls a "switch to central" routine.
        # _should_deploy_checker turns False everywhere, and the existing
        # not-wanted-but-exists branch removes the checkers on its own.
        mock_apps_cls.return_value = _apps_api(central=_central_deployment(), checker_exists=True)
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("app-a"), _make_namespace("app-b"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert {c.args[0] for c in mock_teardown.call_args_list} == {"app-a", "app-b"}
        mock_deploy.assert_not_called()
        # Status must not read as "no checker here" — these namespaces are being
        # audited, just not by a checker of their own.
        assert result["app-a"] == {"deployed": True, "reason": "audited by central checker"}

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_never_deploys_a_checker_even_where_none_exists(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        mock_apps_cls.return_value = _apps_api(central=_central_deployment(), checker_exists=False)
        mock_core.return_value.list_namespace.return_value.items = [_make_namespace("app-a")]

        from kubeic_operator.main import _reconcile_checkers
        _reconcile_checkers()

        mock_deploy.assert_not_called()
        mock_teardown.assert_not_called()

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_grants_secret_access_to_each_audited_namespace(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        mock_apps_cls.return_value = _apps_api(central=_central_deployment())
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("app-a"), _make_namespace("app-b"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        _reconcile_checkers()

        assert {c.args[0] for c in mock_ensure.call_args_list} == {"app-a", "app-b"}
        mock_revoke.assert_not_called()

    @_central_patches
    @patch("kubeic_operator.handlers.namespace.EXCLUDED_NAMESPACES", {"kube-public"})
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_revokes_secret_access_from_an_excluded_namespace(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # The central checker must not keep read access to a namespace that has
        # been excluded since the grant was made.
        mock_apps_cls.return_value = _apps_api(central=_central_deployment())
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("app-a"), _make_namespace("kube-public"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert [c.args[0] for c in mock_ensure.call_args_list] == ["app-a"]
        assert [c.args[0] for c in mock_revoke.call_args_list] == ["kube-public"]
        assert result["kube-public"] == {"deployed": False, "reason": "excluded"}

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_revokes_secret_access_from_a_label_excluded_namespace(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        mock_policy.return_value = {"namespaceSelector": {"excludeLabels": {"audit": "disabled"}}}
        mock_apps_cls.return_value = _apps_api(central=_central_deployment())
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("opted-out", labels={"audit": "disabled"}),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_ensure.assert_not_called()
        assert [c.args[0] for c in mock_revoke.call_args_list] == ["opted-out"]
        # The reason must name the label, not just say "excluded" — that detail
        # is how someone finds out why a namespace went quiet.
        assert result["opted-out"]["reason"] == "excluded by label audit=disabled"

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_honours_no_secret_namespaces(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # The reason central mode does not simply take a cluster-wide
        # secrets:get. kube-system is still audited, but with no secret access.
        # What [] and None resolve from is covered by TestGetSecretNamesForNamespace;
        # what matters here is that reconcile passes them straight through.
        mock_secrets.side_effect = lambda ns: [] if ns == "kube-system" else None
        mock_apps_cls.return_value = _apps_api(central=_central_deployment())
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("kube-system"), _make_namespace("app-a"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        by_namespace = {c.args[0]: c.args[1] for c in mock_ensure.call_args_list}
        assert by_namespace == {"kube-system": [], "app-a": None}
        assert result["kube-system"]["deployed"] is True

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_reports_a_missing_central_deployment_rather_than_claiming_coverage(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # Helm owns that Deployment, so the operator cannot create it. Saying
        # "deployed" while nothing is auditing would hide a broken install.
        mock_apps_cls.return_value = _apps_api(central=None)
        mock_core.return_value.list_namespace.return_value.items = [_make_namespace("app-a")]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert result["app-a"] == {
            "deployed": False, "reason": "central checker Deployment not found",
        }

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_reports_an_unavailable_central_deployment(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # Present but no ready replica: crash-looping, unschedulable, OOMKilled.
        mock_apps_cls.return_value = _apps_api(central=_central_deployment(available=0))
        mock_core.return_value.list_namespace.return_value.items = [_make_namespace("app-a")]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert result["app-a"] == {"deployed": False, "reason": "central checker unavailable"}

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_probes_the_central_deployment_once_per_pass(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # Every namespace's coverage is the same one Deployment, so re-reading
        # it per namespace would be one wasted API call per namespace per pass.
        from kubeic_operator.deployer import CENTRAL_CHECKER_DEPLOYMENT

        apps = _apps_api(central=_central_deployment())
        mock_apps_cls.return_value = apps
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace(f"app-{i}") for i in range(5)
        ]

        from kubeic_operator.main import _reconcile_checkers
        _reconcile_checkers()

        central_reads = [
            c for c in apps.read_namespaced_deployment.call_args_list
            if c.args[0] == CENTRAL_CHECKER_DEPLOYMENT
        ]
        assert len(central_reads) == 1

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_a_failed_grant_is_counted_and_does_not_stop_the_pass(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # One namespace answering 403 must not leave every namespace behind it
        # unreconciled — the failure mode #67/#69 were about.
        before = _failure_count("broken", "secret-grant", error_class="api")
        mock_ensure.side_effect = lambda ns, names: (
            _raise(ApiException(status=403)) if ns == "broken" else None
        )
        mock_apps_cls.return_value = _apps_api(central=_central_deployment())
        mock_core.return_value.list_namespace.return_value.items = [
            _make_namespace("broken"), _make_namespace("healthy"),
        ]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert _failure_count("broken", "secret-grant", error_class="api") == before + 1
        # The namespace after the failure was still processed.
        assert result["healthy"] == {"deployed": True, "reason": "audited by central checker"}
        # And the broken one is not reported as covered: without secret access
        # its private images will read as unavailable.
        assert result["broken"]["deployed"] is False
        assert "secret grant failed" in result["broken"]["reason"]

    @_central_patches
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_a_failed_checker_probe_does_not_mask_central_coverage(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # That probe only looks for a leftover perNamespace checker. Failing it
        # says nothing about whether the namespace is being audited, so the
        # status must still come from the central checker.
        before = _failure_count("app-a", "probe", error_class="api")
        mock_apps_cls.return_value = _apps_api(
            central=_central_deployment(), checker_error=ApiException(status=403),
        )
        mock_core.return_value.list_namespace.return_value.items = [_make_namespace("app-a")]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        assert _failure_count("app-a", "probe", error_class="api") == before + 1
        assert result["app-a"] == {"deployed": True, "reason": "audited by central checker"}

    @_central_patches
    @patch("kubeic_operator.handlers.namespace.CHECKER_ENABLED", False)
    @patch("kubeic_operator.deployer.CHECKER_ENABLED", False)
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_disabled_checkers_revoke_grants_and_skip_the_central_probe(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # checker.enabled: false has to win over the mode, and must not leave
        # secret grants behind for a checker that is no longer running.
        from kubeic_operator.deployer import CENTRAL_CHECKER_DEPLOYMENT

        apps = _apps_api(central=_central_deployment())
        mock_apps_cls.return_value = apps
        mock_core.return_value.list_namespace.return_value.items = [_make_namespace("app-a")]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_ensure.assert_not_called()
        assert [c.args[0] for c in mock_revoke.call_args_list] == ["app-a"]
        assert result["app-a"] == {"deployed": False, "reason": "checkers disabled"}
        assert not [
            c for c in apps.read_namespaced_deployment.call_args_list
            if c.args[0] == CENTRAL_CHECKER_DEPLOYMENT
        ]


class TestPerNamespaceModeTouchesNoCentralRbac:
    @patch("kubeic_operator.deployer.CENTRAL_MODE", False)
    @patch("kubeic_operator.handlers.namespace.CENTRAL_MODE", False)
    @patch("kubeic_operator.handlers.namespace._get_effective_policy", return_value={})
    @patch("kubeic_operator.deployer.get_secret_names_for_namespace", return_value=None)
    @patch("kubeic_operator.deployer.teardown_central_secret_access")
    @patch("kubeic_operator.deployer.ensure_central_secret_access")
    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.main.client.AppsV1Api")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_default_mode_makes_no_central_calls(
        self, mock_core, mock_apps_cls, mock_deploy, mock_wait, mock_teardown,
        mock_ensure, mock_revoke, mock_secrets, mock_policy,
    ):
        # The feature ships inert. In the default mode nothing central is read,
        # written or probed, and checkers deploy exactly as before.
        from kubeic_operator.deployer import CENTRAL_CHECKER_DEPLOYMENT

        apps = _apps_api(checker_exists=False)
        mock_apps_cls.return_value = apps
        mock_core.return_value.list_namespace.return_value.items = [_make_namespace("app-a")]

        from kubeic_operator.main import _reconcile_checkers
        result = _reconcile_checkers()

        mock_ensure.assert_not_called()
        mock_revoke.assert_not_called()
        assert not [
            c for c in apps.read_namespaced_deployment.call_args_list
            if c.args[0] == CENTRAL_CHECKER_DEPLOYMENT
        ]
        mock_deploy.assert_called_once()
        assert result["app-a"] == {"deployed": True}


class TestBootstrapInCentralMode:
    @patch("kubeic_operator.deployer.CENTRAL_MODE", True)
    @patch("kubeic_operator.deployer.deploy_checker")
    @patch("kubeic_operator.main.client.CoreV1Api")
    def test_short_circuits_before_listing_namespaces(self, mock_core, mock_deploy):
        # Bootstrap exists only to pace the creation of per-namespace checker
        # pods. There are none to create, and reconcile — which runs straight
        # after — is what converges the grants.
        from kubeic_operator.main import _bootstrap_checkers
        _bootstrap_checkers()

        mock_deploy.assert_not_called()
        mock_core.return_value.list_namespace.assert_not_called()


class TestCentralNamespaceStatus:
    def test_reports_the_label_that_excluded_a_namespace(self):
        from kubeic_operator.main import _central_namespace_status

        status = _central_namespace_status(
            audited=False,
            labels={"audit": "disabled"},
            policy={"namespaceSelector": {"excludeLabels": {"audit": "disabled"}}},
            central_state="ready",
            grant_error=None,
        )
        assert status == {"deployed": False, "reason": "excluded by label audit=disabled"}

    def test_a_grant_error_outranks_a_healthy_central_checker(self):
        # The checker is up but blind to this namespace's pull secrets, so
        # reporting it as covered would be wrong.
        from kubeic_operator.main import _central_namespace_status

        status = _central_namespace_status(
            audited=True, labels={}, policy={}, central_state="ready", grant_error="403 Forbidden",
        )
        assert status["deployed"] is False
        assert "403 Forbidden" in status["reason"]

    def test_exclusion_outranks_a_grant_error(self):
        # An excluded namespace has its grant revoked, and a revoke failure
        # should read as an exclusion, not as a coverage problem.
        from kubeic_operator.main import _central_namespace_status

        status = _central_namespace_status(
            audited=False, labels={}, policy={}, central_state="ready", grant_error="boom",
        )
        assert status == {"deployed": False, "reason": "excluded"}
