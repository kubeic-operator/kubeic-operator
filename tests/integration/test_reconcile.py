import time

import pytest

CHECKER_DEPLOYMENT = "kubeic-checker"
# Scan interval is set to 15s in CI; allow generous margin
RECONCILE_TIMEOUT = 90


def _wait_for_checker(kubectl, namespace, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = kubectl(
            "get", "deployment", CHECKER_DEPLOYMENT,
            "-n", namespace, check=False, timeout=10,
        )
        if result.returncode == 0:
            break
        time.sleep(3)
    else:
        pytest.fail(f"Checker deployment never appeared in {namespace} after {timeout}s")

    kubectl(
        "wait", "--for=condition=available",
        f"deployment/{CHECKER_DEPLOYMENT}",
        "-n", namespace, "--timeout=60s",
    )


def _wait_for_checker_gone(kubectl, namespace, timeout=RECONCILE_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = kubectl(
            "get", "deployment", CHECKER_DEPLOYMENT,
            "-n", namespace, check=False, timeout=10,
        )
        if result.returncode != 0:
            return
        time.sleep(3)
    pytest.fail(f"Checker still present in {namespace} after {timeout}s")


def test_checker_removed_after_label_change(kubectl):
    """Add audit:disabled label → reconciliation removes the checker."""
    ns = f"reconcile-test-{int(time.time())}"
    kubectl("create", "namespace", ns)
    try:
        _wait_for_checker(kubectl, ns)

        # Exclude the namespace
        kubectl("label", "namespace", ns, "audit=disabled")
        _wait_for_checker_gone(kubectl, ns)
    finally:
        kubectl("delete", "namespace", ns, "--wait=false", check=False, timeout=10)


def test_checker_redeployed_after_label_removed(kubectl):
    """Remove audit:disabled label → reconciliation redeploys the checker."""
    ns = f"reconcile-restore-{int(time.time())}"
    kubectl("apply", "-f", "-", input=f"""apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
  labels:
    audit: disabled
""")
    try:
        time.sleep(10)
        # Confirm no checker initially
        result = kubectl(
            "get", "deployment", CHECKER_DEPLOYMENT,
            "-n", ns, check=False,
        )
        assert result.returncode != 0, "Checker should not exist in excluded namespace"

        # Remove the exclusion label
        kubectl("label", "namespace", ns, "audit-")
        _wait_for_checker(kubectl, ns, timeout=RECONCILE_TIMEOUT)
    finally:
        kubectl("delete", "namespace", ns, "--wait=false", check=False, timeout=10)
