import logging
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import ApiException as K8sApiException

from kubeic_operator.deployer import (
    _rollout_complete,
    _rollout_lock,
    wait_for_checker_ready,
    deploy_checker_serialised,
    teardown_checker_serialised,
    _build_service_account,
    _build_role,
    _build_role_binding,
    _build_service,
    _build_deployment,
    _selector_labels,
    _common_labels,
    _parse_json_env,
    _parse_bool_env,
    _parse_int_env,
    deploy_checker,
    teardown_checker,
    get_secret_names_for_namespace,
    CHECKER_SERVICE_ACCOUNT,
    CHECKER_ROLE,
    CHECKER_ROLE_BINDING,
    CHECKER_DEPLOYMENT,
    CHECKER_SERVICE,
)


class TestLabels:
    def test_selector_labels_are_subset_of_common_labels(self):
        sel = _selector_labels()
        common = _common_labels()
        assert sel.items() <= common.items()

    def test_common_labels_include_version_and_managed_by(self):
        common = _common_labels()
        assert "app.kubernetes.io/version" in common
        assert "app.kubernetes.io/managed-by" in common

    def test_selector_labels_exclude_mutable_fields(self):
        sel = _selector_labels()
        assert "app.kubernetes.io/version" not in sel


class TestBuildServiceAccount:
    def test_has_correct_name_and_namespace(self):
        sa = _build_service_account("my-ns")
        assert sa.metadata.name == CHECKER_SERVICE_ACCOUNT
        assert sa.metadata.namespace == "my-ns"

    def test_has_managed_by_label(self):
        sa = _build_service_account("my-ns")
        assert sa.metadata.labels["app.kubernetes.io/managed-by"] == "kubeic-operator"

    def test_has_instance_label(self):
        sa = _build_service_account("my-ns")
        assert "app.kubernetes.io/instance" in sa.metadata.labels


class TestBuildRole:
    def test_default_has_pod_and_unrestricted_secret_rules(self):
        role = _build_role("my-ns")
        assert len(role.rules) == 2
        resources = {r.resources[0] for r in role.rules}
        assert "pods" in resources
        assert "secrets" in resources
        secret_rule = next(r for r in role.rules if "secrets" in r.resources)
        assert secret_rule.verbs == ["get"]
        assert secret_rule.resource_names is None

    def test_empty_secret_names_omits_secret_rule(self):
        role = _build_role("my-ns", secret_names=[])
        assert len(role.rules) == 1
        assert role.rules[0].resources == ["pods"]

    def test_explicit_secret_names_restricts_access(self):
        role = _build_role("my-ns", secret_names=["my-pull-secret", "other-secret"])
        assert len(role.rules) == 2
        secret_rule = next(r for r in role.rules if "secrets" in r.resources)
        assert secret_rule.verbs == ["get"]
        assert secret_rule.resource_names == ["my-pull-secret", "other-secret"]


class TestGetSecretNamesForNamespace:
    @patch("kubeic_operator.deployer.NAMESPACE_SECRETS", {"prod": ["secret-a"]})
    @patch("kubeic_operator.deployer.NO_SECRET_NAMESPACES", {"kube-system"})
    def test_returns_explicit_names_when_configured(self):
        assert get_secret_names_for_namespace("prod") == ["secret-a"]

    @patch("kubeic_operator.deployer.NAMESPACE_SECRETS", {})
    @patch("kubeic_operator.deployer.NO_SECRET_NAMESPACES", {"kube-system"})
    def test_returns_empty_for_no_secret_namespace(self):
        assert get_secret_names_for_namespace("kube-system") == []

    @patch("kubeic_operator.deployer.NAMESPACE_SECRETS", {})
    @patch("kubeic_operator.deployer.NO_SECRET_NAMESPACES", set())
    def test_returns_none_for_unconfigured_namespace(self):
        assert get_secret_names_for_namespace("my-app") is None


class TestBuildRoleBinding:
    def test_binds_sa_to_role(self):
        rb = _build_role_binding("my-ns")
        assert rb.role_ref.name == CHECKER_ROLE
        assert rb.role_ref.kind == "Role"
        assert len(rb.subjects) == 1
        assert rb.subjects[0].name == CHECKER_SERVICE_ACCOUNT
        assert rb.subjects[0].namespace == "my-ns"


class TestBuildService:
    def test_has_metrics_port(self):
        svc = _build_service("my-ns")
        assert svc.metadata.name == CHECKER_SERVICE
        assert svc.metadata.namespace == "my-ns"
        assert len(svc.spec.ports) == 1
        assert svc.spec.ports[0].port == 9090
        assert svc.spec.ports[0].name == "metrics"

    def test_selector_uses_stable_labels_only(self):
        svc = _build_service("my-ns")
        assert svc.spec.selector == _selector_labels()
        assert "app.kubernetes.io/version" not in svc.spec.selector


class TestBuildDeployment:
    def test_has_correct_env_vars(self):
        deploy = _build_deployment("my-ns", check_interval_minutes=15, credential_source="workloadIdentity")
        container = deploy.spec.template.spec.containers[0]
        env = {e.name: e.value for e in container.env}
        assert env["NAMESPACE"] == "my-ns"
        assert env["CHECK_INTERVAL_MINUTES"] == "15"
        assert env["CREDENTIAL_SOURCE"] == "workloadIdentity"

    def test_match_labels_use_selector_labels_only(self):
        deploy = _build_deployment("my-ns")
        assert deploy.spec.selector.match_labels == _selector_labels()
        assert "app.kubernetes.io/version" not in deploy.spec.selector.match_labels

    def test_pod_template_labels_use_common_labels(self):
        deploy = _build_deployment("my-ns")
        pod_labels = deploy.spec.template.metadata.labels
        assert "app.kubernetes.io/version" in pod_labels
        assert "app.kubernetes.io/instance" in pod_labels

    def test_metrics_port(self):
        deploy = _build_deployment("my-ns")
        container = deploy.spec.template.spec.containers[0]
        assert container.ports[0].container_port == 9090

    def test_no_prometheus_scrape_annotations(self):
        deploy = _build_deployment("my-ns")
        annotations = deploy.spec.template.metadata.annotations
        assert annotations is None or "prometheus.io/scrape" not in (annotations or {})

    def test_resource_requests_and_limits_are_set(self):
        deploy = _build_deployment("my-ns")
        resources = deploy.spec.template.spec.containers[0].resources
        assert resources.requests["cpu"] is not None
        assert resources.requests["memory"] is not None
        assert resources.limits["cpu"] is not None
        assert resources.limits["memory"] is not None

    def test_container_security_context_drops_all_capabilities(self):
        deploy = _build_deployment("my-ns")
        sc = deploy.spec.template.spec.containers[0].security_context
        assert "ALL" in sc.capabilities.drop

    def test_container_security_context_no_privilege_escalation(self):
        deploy = _build_deployment("my-ns")
        sc = deploy.spec.template.spec.containers[0].security_context
        assert sc.allow_privilege_escalation is False

    def test_container_security_context_non_root_readonly_fs(self):
        deploy = _build_deployment("my-ns")
        sc = deploy.spec.template.spec.containers[0].security_context
        assert sc.run_as_non_root is True
        assert sc.read_only_root_filesystem is True

    def test_pod_security_context_non_root_with_seccomp(self):
        deploy = _build_deployment("my-ns")
        pod_sc = deploy.spec.template.spec.security_context
        assert pod_sc.run_as_non_root is True
        assert pod_sc.seccomp_profile.type == "RuntimeDefault"

    def test_pod_anti_affinity_prefers_spreading_across_nodes(self):
        deploy = _build_deployment("my-ns")
        terms = (deploy.spec.template.spec.affinity.pod_anti_affinity
                 .preferred_during_scheduling_ignored_during_execution)
        assert len(terms) == 1
        term = terms[0].pod_affinity_term
        assert term.topology_key == "kubernetes.io/hostname"
        assert term.label_selector.match_labels == _selector_labels()

    def test_anti_affinity_selects_all_namespaces(self):
        # An empty namespaceSelector means "all namespaces". Without it the term
        # would only consider pods in the checker's own namespace, where it is
        # always alone — the same reason topologySpreadConstraints cannot work.
        deploy = _build_deployment("my-ns")
        term = (deploy.spec.template.spec.affinity.pod_anti_affinity
                .preferred_during_scheduling_ignored_during_execution[0].pod_affinity_term)
        assert term.namespace_selector is not None
        assert term.namespace_selector.match_labels is None
        assert term.namespace_selector.match_expressions is None

    def test_termination_grace_period_is_shortened(self):
        # The checker installs no SIGTERM handler, so it always burns the whole
        # grace period. The default 30s is dead time that keeps N terminating
        # pods holding node slots after a mass teardown.
        deploy = _build_deployment("my-ns")
        assert deploy.spec.template.spec.termination_grace_period_seconds == 5

    def test_anti_affinity_is_preferred_not_required(self):
        # Checkers always outnumber nodes, so a required term would leave them
        # permanently Pending.
        deploy = _build_deployment("my-ns")
        anti = deploy.spec.template.spec.affinity.pod_anti_affinity
        assert anti.required_during_scheduling_ignored_during_execution is None
    def test_revision_history_limit_is_set(self):
        # Unset would inherit the Kubernetes default of 10 per namespace.
        deploy = _build_deployment("my-ns")
        assert deploy.spec.revision_history_limit is not None
        assert deploy.spec.revision_history_limit == 2

    @patch("kubeic_operator.deployer.CHECKER_REVISION_HISTORY_LIMIT", 5)
    def test_revision_history_limit_is_configurable(self):
        deploy = _build_deployment("my-ns")
        assert deploy.spec.revision_history_limit == 5

    def test_revision_history_limit_does_not_touch_pod_template(self):
        # revisionHistoryLimit lives on spec, not spec.template, so changing it
        # patches the Deployment without triggering a pod rollout.
        deploy = _build_deployment("my-ns")
        template = deploy.spec.template.to_dict()
        assert "revision_history_limit" not in template
        assert "revisionHistoryLimit" not in template


class TestDeployChecker:
    @patch("kubeic_operator.deployer.client")
    def test_creates_all_resources_when_not_found(self, mock_client):
        mock_v1 = MagicMock()
        mock_rbac = MagicMock()
        mock_apps = MagicMock()

        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac
        mock_client.AppsV1Api.return_value = mock_apps

        not_found = K8sApiException(status=404)
        mock_v1.read_namespaced_service_account.side_effect = not_found
        mock_rbac.read_namespaced_role.side_effect = not_found
        mock_rbac.read_namespaced_role_binding.side_effect = not_found
        mock_v1.read_namespaced_service.side_effect = not_found
        mock_apps.read_namespaced_deployment.side_effect = not_found

        deploy_checker("test-ns")

        mock_v1.create_namespaced_service_account.assert_called_once()
        mock_rbac.create_namespaced_role.assert_called_once()
        mock_rbac.create_namespaced_role_binding.assert_called_once()
        mock_v1.create_namespaced_service.assert_called_once()
        mock_apps.create_namespaced_deployment.assert_called_once()

    @patch("kubeic_operator.deployer.client")
    def test_patches_existing_resources(self, mock_client):
        mock_v1 = MagicMock()
        mock_rbac = MagicMock()
        mock_apps = MagicMock()

        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac
        mock_client.AppsV1Api.return_value = mock_apps

        deploy_checker("test-ns")

        mock_v1.patch_namespaced_service_account.assert_called_once()
        mock_rbac.patch_namespaced_role.assert_called_once()
        mock_rbac.patch_namespaced_role_binding.assert_called_once()
        mock_v1.patch_namespaced_service.assert_called_once()
        mock_apps.patch_namespaced_deployment.assert_called_once()


class TestTeardownChecker:
    @patch("kubeic_operator.deployer.client")
    def test_deletes_all_resources_including_service(self, mock_client):
        mock_v1 = MagicMock()
        mock_rbac = MagicMock()
        mock_apps = MagicMock()

        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac
        mock_client.AppsV1Api.return_value = mock_apps

        teardown_checker("test-ns")

        mock_apps.delete_namespaced_deployment.assert_called_once_with(CHECKER_DEPLOYMENT, "test-ns")
        mock_v1.delete_namespaced_service.assert_called_once_with(CHECKER_SERVICE, "test-ns")
        mock_rbac.delete_namespaced_role_binding.assert_called_once_with(CHECKER_ROLE_BINDING, "test-ns")
        mock_rbac.delete_namespaced_role.assert_called_once_with(CHECKER_ROLE, "test-ns")
        mock_v1.delete_namespaced_service_account.assert_called_once_with(CHECKER_SERVICE_ACCOUNT, "test-ns")

    @patch("kubeic_operator.deployer.client")
    def test_ignores_404_on_delete(self, mock_client):
        mock_v1 = MagicMock()
        mock_rbac = MagicMock()
        mock_apps = MagicMock()

        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac
        mock_client.AppsV1Api.return_value = mock_apps

        not_found = K8sApiException(status=404)
        mock_apps.delete_namespaced_deployment.side_effect = not_found

        teardown_checker("test-ns")
        mock_v1.delete_namespaced_service.assert_called_once()


class TestParseJsonEnv:
    def test_returns_default_when_env_not_set(self):
        result = _parse_json_env("NONEXISTENT_TEST_KEY_12345")
        assert result == {}

    def test_parses_valid_json(self):
        with patch("kubeic_operator.deployer.os.environ.get", return_value='{"key": "value"}'):
            result = _parse_json_env("TEST_KEY")
        assert result == {"key": "value"}

    def test_returns_empty_dict_and_logs_warning_on_invalid_json(self, caplog):
        with patch("kubeic_operator.deployer.os.environ.get", return_value="not-json"):
            with caplog.at_level(logging.WARNING, logger="kubeic-operator.deployer"):
                result = _parse_json_env("TEST_KEY")
        assert result == {}
        assert "Failed to parse env TEST_KEY as JSON" in caplog.text


def _fake_deployment(generation=2, observed=2, spec_replicas=1,
                     replicas=1, updated=1, available=1):
    d = MagicMock()
    d.metadata.generation = generation
    d.spec.replicas = spec_replicas
    d.status.observed_generation = observed
    d.status.replicas = replicas
    d.status.updated_replicas = updated
    d.status.available_replicas = available
    return d


class TestRolloutComplete:
    def test_complete_when_new_pod_is_up_and_old_one_gone(self):
        assert _rollout_complete(_fake_deployment()) is True

    def test_incomplete_until_controller_observes_the_patch(self):
        # The critical case: right after a patch the OLD pod is still Ready, so
        # a readyReplicas check would return instantly and serialise nothing.
        assert _rollout_complete(_fake_deployment(generation=3, observed=2)) is False

    def test_incomplete_while_old_replicas_still_terminating(self):
        # maxSurge takes a 1-replica Deployment to 2 pods mid-rollout.
        assert _rollout_complete(_fake_deployment(replicas=2, updated=1)) is False

    def test_incomplete_when_not_every_replica_updated(self):
        assert _rollout_complete(
            _fake_deployment(spec_replicas=2, replicas=2, updated=1, available=2)
        ) is False

    def test_incomplete_when_new_pod_not_yet_available(self):
        assert _rollout_complete(_fake_deployment(available=0)) is False

    def test_absent_status_fields_count_as_zero(self):
        assert _rollout_complete(
            _fake_deployment(observed=None, replicas=None, updated=None, available=None)
        ) is False


class TestWaitForCheckerReady:
    @patch("kubeic_operator.deployer.time.sleep")
    @patch("kubeic_operator.deployer.client.AppsV1Api")
    def test_polls_until_rollout_completes(self, mock_apps_cls, mock_sleep):
        mock_apps_cls.return_value.read_namespaced_deployment.side_effect = [
            _fake_deployment(generation=3, observed=2),
            _fake_deployment(generation=3, observed=3),
        ]
        assert wait_for_checker_ready("my-ns", timeout=30) is True
        assert mock_sleep.call_count == 1

    @patch("kubeic_operator.deployer.client.AppsV1Api")
    def test_returns_false_on_timeout_rather_than_raising(self, mock_apps_cls):
        # One wedged namespace must not stall every namespace behind it.
        mock_apps_cls.return_value.read_namespaced_deployment.return_value = (
            _fake_deployment(generation=3, observed=2)
        )
        assert wait_for_checker_ready("my-ns", timeout=0) is False

    @patch("kubeic_operator.deployer.client.AppsV1Api")
    def test_returns_false_on_api_error(self, mock_apps_cls):
        mock_apps_cls.return_value.read_namespaced_deployment.side_effect = (
            K8sApiException(status=404)
        )
        assert wait_for_checker_ready("my-ns") is False


class TestDeployCheckerSerialised:
    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    def test_deploys_then_waits_for_readiness(self, mock_deploy, mock_wait):
        assert deploy_checker_serialised(
            "my-ns", blocking=True, check_interval_minutes=30,
        ) is True
        mock_deploy.assert_called_once_with(namespace="my-ns", check_interval_minutes=30)
        mock_wait.assert_called_once_with("my-ns")

    @patch("kubeic_operator.deployer.deploy_checker")
    def test_non_blocking_skips_entirely_while_a_rollout_holds_the_lock(self, mock_deploy):
        # This is what stops the namespace handler fanning out: kopf fires it
        # for every namespace at once and all but one return here.
        with _rollout_lock:
            assert deploy_checker_serialised("my-ns", blocking=False) is False
        mock_deploy.assert_not_called()

    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker")
    def test_releases_the_lock_on_success(self, mock_deploy, mock_wait):
        deploy_checker_serialised("my-ns", blocking=True)
        assert _rollout_lock.acquire(blocking=False)
        _rollout_lock.release()

    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.deploy_checker", side_effect=RuntimeError("boom"))
    def test_releases_the_lock_when_deploy_raises(self, mock_deploy, mock_wait):
        # A namespace that fails must not wedge the rollout permanently.
        with pytest.raises(RuntimeError):
            deploy_checker_serialised("my-ns", blocking=True)
        assert _rollout_lock.acquire(blocking=False)
        _rollout_lock.release()
class TestParseBoolEnv:
    def test_returns_default_when_env_not_set(self):
        assert _parse_bool_env("NONEXISTENT_TEST_KEY_12345") is True
        assert _parse_bool_env("NONEXISTENT_TEST_KEY_12345", default=False) is False

    def test_empty_string_falls_back_to_default(self):
        # Helm renders a missing value as "" rather than omitting the env var,
        # so "" must not read as false and silently disable checkers.
        with patch("kubeic_operator.deployer.os.environ.get", return_value=""):
            assert _parse_bool_env("TEST_KEY") is True
        with patch("kubeic_operator.deployer.os.environ.get", return_value="   "):
            assert _parse_bool_env("TEST_KEY") is True

    def test_parses_truthy_values(self):
        for raw in ("true", "True", "TRUE", "1", "yes", "on", " true "):
            with patch("kubeic_operator.deployer.os.environ.get", return_value=raw):
                assert _parse_bool_env("TEST_KEY") is True, raw

    def test_parses_falsy_values(self):
        for raw in ("false", "False", "0", "no", "off", "nonsense"):
            with patch("kubeic_operator.deployer.os.environ.get", return_value=raw):
                assert _parse_bool_env("TEST_KEY") is False, raw

class TestParseIntEnv:
    def test_returns_default_when_env_not_set(self):
        assert _parse_int_env("NONEXISTENT_TEST_KEY_12345", 2) == 2

    def test_parses_valid_int(self):
        with patch("kubeic_operator.deployer.os.environ.get", return_value="7"):
            assert _parse_int_env("TEST_KEY", 2) == 7

    def test_empty_falls_back_to_default(self):
        with patch("kubeic_operator.deployer.os.environ.get", return_value="  "):
            assert _parse_int_env("TEST_KEY", 2) == 2

    def test_non_numeric_falls_back_and_warns(self, caplog):
        with patch("kubeic_operator.deployer.os.environ.get", return_value="ten"):
            with caplog.at_level(logging.WARNING, logger="kubeic-operator.deployer"):
                assert _parse_int_env("TEST_KEY", 2) == 2
        assert "Failed to parse env TEST_KEY as int" in caplog.text

    def test_below_minimum_falls_back_and_warns(self, caplog):
        with patch("kubeic_operator.deployer.os.environ.get", return_value="-1"):
            with caplog.at_level(logging.WARNING, logger="kubeic-operator.deployer"):
                assert _parse_int_env("TEST_KEY", 2) == 2
        assert "below minimum" in caplog.text

    def test_zero_is_allowed(self):
        # 0 is a legitimate revisionHistoryLimit (keep no old ReplicaSets).
        with patch("kubeic_operator.deployer.os.environ.get", return_value="0"):
            assert _parse_int_env("TEST_KEY", 2) == 0


class TestTeardownCheckerSerialised:
    @patch("kubeic_operator.deployer.teardown_checker")
    def test_tears_down_under_the_lock(self, mock_teardown):
        assert teardown_checker_serialised("my-ns", blocking=True) is True
        mock_teardown.assert_called_once_with("my-ns")

    @patch("kubeic_operator.deployer.teardown_checker")
    def test_non_blocking_skips_while_a_deploy_holds_the_lock(self, mock_teardown):
        # checker.enabled: false makes every namespace fail _should_audit at
        # once, so a mass teardown can collide with an in-flight deploy.
        with _rollout_lock:
            assert teardown_checker_serialised("my-ns", blocking=False) is False
        mock_teardown.assert_not_called()

    @patch("kubeic_operator.deployer.teardown_checker", side_effect=RuntimeError("boom"))
    def test_releases_the_lock_when_teardown_raises(self, mock_teardown):
        with pytest.raises(RuntimeError):
            teardown_checker_serialised("my-ns", blocking=True)
        assert _rollout_lock.acquire(blocking=False)
        _rollout_lock.release()

    @patch("kubeic_operator.deployer.wait_for_checker_ready", return_value=True)
    @patch("kubeic_operator.deployer.teardown_checker")
    @patch("kubeic_operator.deployer.deploy_checker")
    def test_shares_one_lock_with_deployment(self, mock_deploy, mock_teardown, mock_wait):
        # Deploy and teardown must contend for the same lock, or reconcile could
        # tear down a namespace the handler is mid-way through deploying.
        seen = []

        def _deploy(**kwargs):
            seen.append(teardown_checker_serialised(kwargs["namespace"], blocking=False))

        mock_deploy.side_effect = _deploy
        deploy_checker_serialised("my-ns", blocking=True)

        assert seen == [False]
        mock_teardown.assert_not_called()


class TestAnnotationMerge:
    @patch("kubeic_operator.deployer._build_deployment")
    @patch("kubeic_operator.deployer.client")
    def test_stale_annotations_set_to_none_on_patch(self, mock_client, mock_build):
        mock_v1 = MagicMock()
        mock_rbac = MagicMock()
        mock_apps = MagicMock()

        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac
        mock_client.AppsV1Api.return_value = mock_apps

        desired_deploy = MagicMock()
        desired_deploy.spec.template.metadata.annotations = {"keep-me": "value"}
        mock_build.return_value = desired_deploy

        existing = MagicMock()
        existing.spec.template.metadata.annotations = {
            "keep-me": "value",
            "stale-annotation": "should-be-removed",
        }
        mock_apps.read_namespaced_deployment.return_value = existing

        deploy_checker("test-ns")

        patch_call = mock_apps.patch_namespaced_deployment.call_args
        patched = patch_call[0][2]
        annotations = patched.spec.template.metadata.annotations

        assert annotations["keep-me"] == "value"
        assert annotations["stale-annotation"] is None

    @patch("kubeic_operator.deployer._build_deployment")
    @patch("kubeic_operator.deployer.client")
    def test_all_existing_annotations_cleaned_when_desired_is_empty(self, mock_client, mock_build):
        mock_v1 = MagicMock()
        mock_rbac = MagicMock()
        mock_apps = MagicMock()

        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac
        mock_client.AppsV1Api.return_value = mock_apps

        desired_deploy = MagicMock()
        desired_deploy.spec.template.metadata.annotations = {}
        mock_build.return_value = desired_deploy

        existing = MagicMock()
        existing.spec.template.metadata.annotations = {
            "old-1": "a",
            "old-2": "b",
        }
        mock_apps.read_namespaced_deployment.return_value = existing

        deploy_checker("test-ns")

        patch_call = mock_apps.patch_namespaced_deployment.call_args
        patched = patch_call[0][2]
        annotations = patched.spec.template.metadata.annotations

        assert annotations["old-1"] is None
        assert annotations["old-2"] is None
