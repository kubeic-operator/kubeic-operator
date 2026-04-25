from conftest import get_operator_pod_name, OPERATOR_NS


def test_operator_metrics_endpoint(kubectl, operator_namespace):
    pod = get_operator_pod_name(kubectl, operator_namespace)
    result = kubectl(
        "exec", pod, "-n", operator_namespace, "--",
        "python", "-c",
        "import urllib.request; print(urllib.request.urlopen('http://localhost:9090/metrics').read().decode()[:2000])",
    )
    assert "kube_image_" in result.stdout
