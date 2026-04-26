import time

import pytest

CHECKER_DEPLOYMENT = "kubeic-checker"


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


def test_checker_deployed_in_default_namespace(kubectl):
    _wait_for_checker(kubectl, "default")


def test_checker_rbac_created(kubectl):
    resources = [
        ("serviceaccount", "kubeic-checker"),
        ("role", "kubeic-checker"),
        ("rolebinding", "kubeic-checker"),
        ("service", "kubeic-checker-metrics"),
    ]
    for kind, name in resources:
        result = kubectl("get", kind, name, "-n", "default", check=False)
        assert result.returncode == 0, f"{kind}/{name} not found in default namespace"


def test_checker_deployed_on_new_namespace(kubectl, test_namespace):
    _wait_for_checker(kubectl, test_namespace, timeout=120)


def test_excluded_namespace_no_checker(kubectl):
    ns = "excluded-test-ns"
    kubectl("apply", "-f", "-", input=f"""apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
  labels:
    audit: disabled
""")
    time.sleep(15)

    result = kubectl(
        "get", "deployment", CHECKER_DEPLOYMENT,
        "-n", ns, check=False,
    )
    assert result.returncode != 0, f"Checker should NOT exist in excluded namespace {ns}"

    kubectl("delete", "namespace", ns, "--wait=false", check=False, timeout=10)
