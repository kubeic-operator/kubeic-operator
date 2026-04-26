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
