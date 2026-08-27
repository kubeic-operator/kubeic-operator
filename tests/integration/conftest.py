import subprocess
import time

import pytest

OPERATOR_NS = "kubeic-operator"
OPERATOR_SELECTOR = "app.kubernetes.io/component=operator"


@pytest.fixture(scope="session")
def kubectl():
    def _kubectl(*args, check=True, timeout=60, input=None):
        result = subprocess.run(
            ["kubectl"] + list(args),
            capture_output=True, text=True, timeout=timeout,
            input=input,
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
    kubectl("delete", "namespace", name, "--wait=false", check=False, timeout=10)


@pytest.fixture(scope="session")
def read_metrics(kubectl):
    """Scrape a pod's own /metrics from inside it.

    urllib rather than curl or wget: the checker image is python:3.13-slim plus
    skopeo, so python is the only HTTP client guaranteed to be present.
    """
    def _read(namespace, pod):
        # check=False returned "" on a failed exec, which is indistinguishable
        # from a successful scrape of a checker that has not published yet.
        # Callers poll this predicate, so a pod that had gone away produced a
        # full-length timeout reporting "last saw: None" — the failure the
        # 2026-08-27 run could not be diagnosed from. Retry a couple of times
        # for a genuine blip, then say what actually went wrong.
        attempts = []
        for _ in range(3):
            result = kubectl(
                "exec", pod, "-n", namespace, "--",
                "python", "-c",
                "import urllib.request;"
                "print(urllib.request.urlopen('http://localhost:9090/metrics', timeout=10).read().decode())",
                timeout=45, check=False,
            )
            if result.returncode == 0:
                return result.stdout
            attempts.append(f"rc={result.returncode} {result.stderr.strip()[:200]}")
        pytest.fail(
            f"Could not scrape /metrics from {namespace}/{pod} after 3 attempts: "
            + " | ".join(attempts)
        )
    return _read


@pytest.fixture(scope="session")
def poll_for():
    """Retry until a callable returns something truthy, then return it."""
    def _poll(fn, timeout, interval=10, what="condition"):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = fn()
            if last:
                return last
            time.sleep(interval)
        pytest.fail(f"Timed out after {timeout}s waiting for {what}; last saw: {last!r}")
    return _poll


@pytest.fixture(scope="session")
def operator_pod(kubectl, operator_namespace):
    result = kubectl(
        "get", "pods", "-n", operator_namespace,
        "-l", OPERATOR_SELECTOR,
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    return result.stdout.strip()
