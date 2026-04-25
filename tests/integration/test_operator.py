from conftest import get_operator_pod_name, OPERATOR_NS, OPERATOR_SELECTOR


def test_operator_deployment_available(kubectl, operator_namespace):
    result = kubectl(
        "get", "deployment", "kubeic-operator",
        "-n", operator_namespace,
        "-o", "jsonpath={.status.conditions[?(@.type=='Available')].status}",
    )
    assert result.stdout.strip() == "True"


def test_operator_pod_running(kubectl, operator_namespace):
    result = kubectl(
        "get", "pods", "-n", operator_namespace,
        "-l", OPERATOR_SELECTOR,
        "-o", "jsonpath={.items[0].status.phase}",
    )
    assert result.stdout.strip() == "Running"


def test_operator_logs_show_startup(kubectl, operator_namespace):
    pod = get_operator_pod_name(kubectl, operator_namespace)
    logs = kubectl("logs", pod, "-n", operator_namespace)
    assert "Prometheus metrics server started on port 9090" in logs.stdout


def test_operator_logs_show_bootstrap(kubectl, operator_namespace):
    pod = get_operator_pod_name(kubectl, operator_namespace)
    logs = kubectl("logs", pod, "-n", operator_namespace)
    assert "Bootstrapped checker in namespace" in logs.stdout
