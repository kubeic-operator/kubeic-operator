from unittest.mock import patch, MagicMock

from kubeic_operator.cleanup import run


class TestRun:
    @patch("kubeic_operator.cleanup.teardown_checker")
    @patch("kubeic_operator.cleanup.client")
    @patch("kubeic_operator.cleanup.config")
    def test_no_checker_deployments(self, mock_config, mock_client, mock_teardown):
        mock_apps_v1 = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps_v1
        mock_apps_v1.list_deployment_for_all_namespaces.return_value = MagicMock(items=[])

        run()

        mock_config.load_incluster_config.assert_called_once()
        mock_teardown.assert_not_called()

    @patch("kubeic_operator.cleanup.teardown_checker")
    @patch("kubeic_operator.cleanup.client")
    @patch("kubeic_operator.cleanup.config")
    def test_checker_deployments_in_multiple_namespaces(self, mock_config, mock_client, mock_teardown):
        mock_apps_v1 = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps_v1
        mock_apps_v1.list_deployment_for_all_namespaces.return_value = MagicMock(items=[
            MagicMock(metadata=MagicMock(namespace="zebra")),
            MagicMock(metadata=MagicMock(namespace="alpha")),
            MagicMock(metadata=MagicMock(namespace="mid")),
            MagicMock(metadata=MagicMock(namespace="alpha")),
        ])

        run()

        torn_down = {c.args[0] for c in mock_teardown.call_args_list}
        assert torn_down == {"alpha", "mid", "zebra"}
        assert mock_teardown.call_count == 3

    @patch("kubeic_operator.cleanup.teardown_checker")
    @patch("kubeic_operator.cleanup.client")
    @patch("kubeic_operator.cleanup.config")
    def test_checker_deployment_in_single_namespace(self, mock_config, mock_client, mock_teardown):
        mock_apps_v1 = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps_v1
        mock_apps_v1.list_deployment_for_all_namespaces.return_value = MagicMock(items=[
            MagicMock(metadata=MagicMock(namespace="my-app")),
        ])

        run()

        mock_teardown.assert_called_once_with("my-app")


def _role(name, namespace):
    role = MagicMock()
    role.metadata.name = name
    role.metadata.namespace = namespace
    return role


class TestRunCentralGrants:
    """Uninstall in central mode.

    The Deployment sweep above finds nothing — central mode puts no checker
    Deployment outside the release namespace — but the operator will have left a
    Role and RoleBinding in every audited namespace. Without this, `helm
    uninstall` orphans RBAC across the whole cluster.
    """

    def _run(self, deployments=(), roles=()):
        from kubeic_operator.cleanup import run

        with (
            patch("kubeic_operator.cleanup.teardown_checker") as mock_teardown,
            patch("kubeic_operator.cleanup.teardown_central_secret_access") as mock_revoke,
            patch("kubeic_operator.cleanup.client") as mock_client,
            patch("kubeic_operator.cleanup.config"),
        ):
            mock_client.AppsV1Api.return_value.list_deployment_for_all_namespaces.return_value = (
                MagicMock(items=list(deployments))
            )
            mock_client.RbacAuthorizationV1Api.return_value.list_role_for_all_namespaces.return_value = (
                MagicMock(items=list(roles))
            )
            run()
            return mock_teardown, mock_revoke

    def test_revokes_every_central_grant(self):
        from kubeic_operator.deployer import CENTRAL_CHECKER_ROLE

        _, mock_revoke = self._run(roles=[
            _role(CENTRAL_CHECKER_ROLE, "zebra"),
            _role(CENTRAL_CHECKER_ROLE, "alpha"),
        ])

        assert {c.args[0] for c in mock_revoke.call_args_list} == {"alpha", "zebra"}

    def test_ignores_per_namespace_checker_roles(self):
        # Both carry the same component=checker label, so the listing returns
        # both. Revoking on the per-namespace Role's namespace would be wrong —
        # teardown_checker already owns those, and in perNamespace mode there is
        # no central grant to remove at all.
        from kubeic_operator.deployer import CENTRAL_CHECKER_ROLE, CHECKER_ROLE

        _, mock_revoke = self._run(roles=[
            _role(CHECKER_ROLE, "per-ns-only"),
            _role(CENTRAL_CHECKER_ROLE, "central-only"),
        ])

        assert [c.args[0] for c in mock_revoke.call_args_list] == ["central-only"]

    def test_revokes_nothing_when_no_central_grants_exist(self):
        from kubeic_operator.deployer import CHECKER_ROLE

        _, mock_revoke = self._run(roles=[_role(CHECKER_ROLE, "app")])

        mock_revoke.assert_not_called()

    def test_cleans_up_both_layers_after_a_mode_switch(self):
        # A cluster part-way through draining from perNamespace to central has
        # leftover checker Deployments AND central grants. Uninstall must not
        # leave either behind.
        from kubeic_operator.deployer import CENTRAL_CHECKER_ROLE

        mock_teardown, mock_revoke = self._run(
            deployments=[MagicMock(metadata=MagicMock(namespace="draining"))],
            roles=[_role(CENTRAL_CHECKER_ROLE, "granted")],
        )

        assert [c.args[0] for c in mock_teardown.call_args_list] == ["draining"]
        assert [c.args[0] for c in mock_revoke.call_args_list] == ["granted"]

    def test_selects_roles_by_the_same_label_as_deployments(self):
        # A different selector would silently miss grants on a release whose
        # instance label does not match.
        from kubeic_operator.cleanup import run
        from kubeic_operator.deployer import RELEASE_NAME

        with (
            patch("kubeic_operator.cleanup.teardown_checker"),
            patch("kubeic_operator.cleanup.teardown_central_secret_access"),
            patch("kubeic_operator.cleanup.client") as mock_client,
            patch("kubeic_operator.cleanup.config"),
        ):
            apps = mock_client.AppsV1Api.return_value
            rbac = mock_client.RbacAuthorizationV1Api.return_value
            apps.list_deployment_for_all_namespaces.return_value = MagicMock(items=[])
            rbac.list_role_for_all_namespaces.return_value = MagicMock(items=[])
            run()

        selector = rbac.list_role_for_all_namespaces.call_args.kwargs["label_selector"]
        assert "app.kubernetes.io/component=checker" in selector
        assert f"app.kubernetes.io/instance={RELEASE_NAME}" in selector
        assert selector == apps.list_deployment_for_all_namespaces.call_args.kwargs["label_selector"]
