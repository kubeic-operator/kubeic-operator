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
    _reconcile_after_policy_change,
    on_policy_change,
    on_policy_delete,
)


class TestReconcileAfterPolicyChange:
    @patch("kubeic_operator.main._write_iap_status")
    @patch("kubeic_operator.main._reconcile_checkers")
    def test_calls_reconcile_and_writes_status(self, mock_reconcile, mock_write_status):
        mock_reconcile.return_value = {"ns-1": "deployed"}

        _reconcile_after_policy_change()

        mock_reconcile.assert_called_once()
        mock_write_status.assert_called_once_with({"ns-1": "deployed"})

    @patch("kubeic_operator.main._write_iap_status")
    @patch("kubeic_operator.main._reconcile_checkers")
    def test_skips_status_when_no_namespaces(self, mock_reconcile, mock_write_status):
        mock_reconcile.return_value = {}

        _reconcile_after_policy_change()

        mock_reconcile.assert_called_once()
        mock_write_status.assert_not_called()


class TestOnPolicyChange:
    @patch("kubeic_operator.handlers.policy._reconcile_after_policy_change")
    def test_calls_reconcile(self, mock_reconcile):
        on_policy_change(body={}, meta=MagicMock())
        mock_reconcile.assert_called_once()


class TestOnPolicyDelete:
    @patch("kubeic_operator.handlers.policy._reconcile_after_policy_change")
    def test_calls_reconcile(self, mock_reconcile):
        on_policy_delete(body={}, meta=MagicMock())
        mock_reconcile.assert_called_once()
