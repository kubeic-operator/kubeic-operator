import time

CHECKER_DEPLOYMENT = "kubeic-checker"


def _wait_for_checker(kubectl, namespace, timeout=120):
    kubectl(
        "wait", "--for=condition=available",
        f"deployment/{CHECKER_DEPLOYMENT}",
        "-n", namespace, f"--timeout={timeout}s",
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
    kubectl("create", "namespace", ns, check=False)
    kubectl("label", "namespace", ns, "audit=disabled", check=False)
    time.sleep(15)

    result = kubectl(
        "get", "deployment", CHECKER_DEPLOYMENT,
        "-n", ns, check=False,
    )
    assert result.returncode != 0, f"Checker should NOT exist in excluded namespace {ns}"

    kubectl("delete", "namespace", ns, check=False, timeout=30)
