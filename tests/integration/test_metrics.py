def test_operator_metrics_endpoint(kubectl, operator_namespace, operator_pod):
    result = kubectl(
        "exec", operator_pod, "-n", operator_namespace, "--",
        "python", "-c",
        "import urllib.request; print(urllib.request.urlopen('http://localhost:9090/metrics').read().decode()[:2000])",
    )
    assert "kube_image_" in result.stdout
