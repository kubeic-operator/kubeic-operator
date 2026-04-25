CRD_NAME = "imageauditpolicies.imageaudit.kubeic.io"


def test_crd_established(kubectl):
    result = kubectl(
        "get", "crd", CRD_NAME,
        "-o", "jsonpath={.status.acceptedNames.kind}",
    )
    assert result.stdout.strip() == "ImageAuditPolicy"


def test_cluster_defaults_policy_exists(kubectl, operator_namespace):
    result = kubectl(
        "get", "imageauditpolicy", "cluster-defaults",
        "-n", operator_namespace,
    )
    assert result.returncode == 0


def test_namespace_policy_creation(kubectl, test_namespace):
    policy = f"""apiVersion: imageaudit.kubeic.io/v1alpha1
kind: ImageAuditPolicy
metadata:
  name: test-policy
  namespace: {test_namespace}
spec:
  prerelease:
    maxAgeDays: 30
"""
    result = kubectl("apply", "-f", "-", check=False, input=policy)
    assert result.returncode == 0, result.stderr

    readback = kubectl(
        "get", "imageauditpolicy", "test-policy",
        "-n", test_namespace,
        "-o", "jsonpath={{.spec.prerelease.maxAgeDays}}",
    )
    assert readback.stdout.strip() == "30"
