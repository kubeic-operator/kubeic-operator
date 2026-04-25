import subprocess
import time

import pytest

OPERATOR_NS = "kubeic-operator"
OPERATOR_SELECTOR = "app.kubernetes.io/component=operator"


@pytest.fixture(scope="session")
def kubectl():
    def _kubectl(*args, check=True, timeout=60):
        result = subprocess.run(
            ["kubectl"] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        if check and result.returncode != 0:
            pytest.fail(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
        return result
    return _kubectl


@pytest.fixture(scope="session")
def operator_namespace():
    return OPERATOR_NS


@pytest.fixture(scope="session", autouse=True)
def wait_for_operator(kubectl, operator_namespace):
    kubectl(
        "wait", "--for=condition=available", "deployment/kubeic-operator",
        "-n", operator_namespace, "--timeout=180s",
    )
    # Give the operator time to bootstrap checkers in existing namespaces
    time.sleep(10)


@pytest.fixture
def test_namespace(kubectl):
    name = f"test-{int(time.time())}"
    kubectl("create", "namespace", name)
    yield name
    kubectl("delete", "namespace", name, check=False, timeout=30)


@pytest.fixture(scope="session")
def operator_pod(kubectl, operator_namespace):
    result = kubectl(
        "get", "pods", "-n", operator_namespace,
        "-l", OPERATOR_SELECTOR,
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    return result.stdout.strip()
