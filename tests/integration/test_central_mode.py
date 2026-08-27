"""Integration coverage for checker.mode=central, in a real cluster.

Marked `central` and excluded from the default integration run, because it needs
the release upgraded to central mode. CI upgrades and then runs only this module.

Three things here cannot be shown by any unit test:

  * that the operator is *permitted* to create the per-namespace secret Role at
    all. Kubernetes RBAC refuses to let a subject mint permissions it does not
    itself hold, and a mocked RbacAuthorizationV1Api will happily pretend
    otherwise. The operator does hold cluster-wide secrets:get, so this should
    work — but "should" is exactly the word that precedes an outage.
  * that the Helm-owned Deployment and the operator-owned RBAC, which know about
    each other only through env vars and a name, actually line up.
  * that one checker with only a ClusterRole can really list the whole cluster
    and publish series for namespaces other than its own.
"""
import time

import pytest

pytestmark = pytest.mark.central

CENTRAL_NAME = "kubeic-operator-checker"
CENTRAL_GRANT = "kubeic-checker-central"
PER_NAMESPACE_CHECKER = "kubeic-checker"
CHECKER_SELECTOR = "app.kubernetes.io/component=checker"

# Served by ECR's Docker Hub mirror rather than Docker Hub itself, so the runner
# does not spend its anonymous pull budget.
REAL_IMAGE = "public.ecr.aws/docker/library/busybox:1.36"

# Chart defaults: kube-system takes no secret grant, kube-public is not audited.
NO_SECRET_NAMESPACE = "kube-system"
EXCLUDED_NAMESPACE = "kube-public"


def _samples(metrics_text, metric_name):
    found = {}
    for line in metrics_text.splitlines():
        if not line.startswith(f"{metric_name}{{"):
            continue
        labels, _, value = line.rpartition(" ")
        found[labels] = float(value)
    return found


def _pod_with_image(kubectl, namespace, name="probe"):
    kubectl("apply", "-n", namespace, "-f", "-", input=f"""apiVersion: v1
kind: Pod
metadata:
  name: {name}
spec:
  containers:
    - name: main
      image: {REAL_IMAGE}
      command: ["sleep", "3600"]
""")


@pytest.fixture(scope="module")
def central_pod(kubectl, operator_namespace):
    """The one central checker pod, once it is Running."""
    kubectl(
        "wait", "--for=condition=available", f"deployment/{CENTRAL_NAME}",
        "-n", operator_namespace, "--timeout=180s",
    )
    result = kubectl(
        "get", "pods", "-n", operator_namespace, "-l", CHECKER_SELECTOR,
        "--field-selector=status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}", timeout=20,
    )
    name = result.stdout.strip()
    if not name:
        pytest.fail("No running central checker pod")
    return name


@pytest.fixture(scope="module")
def audited_namespace(kubectl):
    """A normal namespace with a resolvable image, so it must be audited."""
    name = f"central-in-{int(time.time())}"
    kubectl("create", "namespace", name)
    _pod_with_image(kubectl, name)
    yield name
    kubectl("delete", "namespace", name, "--wait=false", check=False, timeout=10)


@pytest.fixture(scope="module")
def opted_out_namespace(kubectl):
    """A namespace carrying the chart's default opt-out label.

    Same image as the audited namespace, so if it shows up in the metrics that
    is the exclusion failing, not the image being unresolvable.
    """
    name = f"central-out-{int(time.time())}"
    kubectl("create", "namespace", name)
    kubectl("label", "namespace", name, "audit=disabled")
    _pod_with_image(kubectl, name)
    yield name
    kubectl("delete", "namespace", name, "--wait=false", check=False, timeout=10)


class TestCentralCheckerRuns:
    def test_the_chart_owned_deployment_becomes_available(self, kubectl, operator_namespace):
        kubectl(
            "wait", "--for=condition=available", f"deployment/{CENTRAL_NAME}",
            "-n", operator_namespace, "--timeout=180s",
        )

    def test_it_is_owned_by_helm_not_the_operator(self, kubectl, operator_namespace):
        # If the operator ever started managing this, its ClusterRole would have
        # to be widened to mint cluster-scoped RBAC. The label is the guard.
        result = kubectl(
            "get", "deployment", CENTRAL_NAME, "-n", operator_namespace,
            "-o", "jsonpath={.metadata.labels['app\\.kubernetes\\.io/managed-by']}",
        )
        assert result.stdout.strip() == "Helm"

    def test_per_namespace_checkers_are_drained(self, kubectl, poll_for):
        # No teardown routine was written for this. _should_deploy_checker turns
        # False everywhere and the ordinary reconcile path removes them.
        def drained():
            result = kubectl(
                "get", "deployments", "--all-namespaces",
                "-l", CHECKER_SELECTOR,
                "-o", f"jsonpath={{.items[?(@.metadata.name=='{PER_NAMESPACE_CHECKER}')].metadata.namespace}}",
                timeout=30,
            )
            return "drained" if not result.stdout.strip() else None

        poll_for(drained, timeout=180, interval=10, what="per-namespace checkers to drain")


class TestPerNamespaceSecretGrants:
    def test_the_operator_can_create_the_grant(self, kubectl, audited_namespace, poll_for):
        # The RBAC escalation-prevention question: a subject may only create a
        # Role whose permissions it already holds. The operator holds
        # cluster-wide secrets:get, so this is allowed — but only a real API
        # server can confirm it.
        def granted():
            result = kubectl(
                "get", "role", CENTRAL_GRANT, "-n", audited_namespace,
                check=False, timeout=15,
            )
            return result.returncode == 0

        poll_for(granted, timeout=120, interval=5,
                 what=f"central secret Role in {audited_namespace}")

    def test_the_grant_covers_only_secrets(self, kubectl, audited_namespace):
        # Pods and namespaces come from the ClusterRole. Repeating them here
        # would widen what the operator mints per namespace for no reason.
        result = kubectl(
            "get", "role", CENTRAL_GRANT, "-n", audited_namespace,
            "-o", "jsonpath={.rules[*].resources[*]}",
        )
        assert result.stdout.split() == ["secrets"]

    def test_the_binding_names_the_central_service_account(
        self, kubectl, audited_namespace, operator_namespace,
    ):
        # Pointing this at a ServiceAccount in the audited namespace would name
        # something that does not exist: RBAC would grant nothing while looking
        # entirely correct.
        result = kubectl(
            "get", "rolebinding", CENTRAL_GRANT, "-n", audited_namespace,
            "-o", "jsonpath={.subjects[0].kind}/{.subjects[0].namespace}/{.subjects[0].name}",
        )
        assert result.stdout.strip() == f"ServiceAccount/{operator_namespace}/{CENTRAL_NAME}"

    def test_the_binding_points_at_the_local_role(self, kubectl, audited_namespace):
        # Not a ClusterRole: roleRef is immutable, so moving a namespace between
        # unrestricted and name-restricted access has to be a change of the
        # Role's rules, never of the binding.
        result = kubectl(
            "get", "rolebinding", CENTRAL_GRANT, "-n", audited_namespace,
            "-o", "jsonpath={.roleRef.kind}/{.roleRef.name}",
        )
        assert result.stdout.strip() == f"Role/{CENTRAL_GRANT}"

    def test_no_grant_where_secrets_are_withheld(self, kubectl):
        # noSecretNamespaces survives the move to central mode. This is the
        # whole reason the ClusterRole has no cluster-wide secrets:get.
        result = kubectl(
            "get", "role", CENTRAL_GRANT, "-n", NO_SECRET_NAMESPACE,
            check=False, timeout=15,
        )
        assert result.returncode != 0, (
            f"{NO_SECRET_NAMESPACE} is in noSecretNamespaces but has a secret grant"
        )

    def test_no_grant_in_an_excluded_namespace(self, kubectl):
        result = kubectl(
            "get", "role", CENTRAL_GRANT, "-n", EXCLUDED_NAMESPACE,
            check=False, timeout=15,
        )
        assert result.returncode != 0, (
            f"{EXCLUDED_NAMESPACE} is excluded but has a secret grant"
        )

    def test_the_central_checker_holds_no_cluster_wide_secret_access(self, kubectl):
        # The single assertion that makes the per-namespace machinery worth its
        # weight. If this ever passes trivially because someone added secrets to
        # the ClusterRole, the blast radius silently becomes the whole cluster.
        result = kubectl(
            "get", "clusterrole", CENTRAL_NAME,
            "-o", "jsonpath={.rules[*].resources[*]}",
        )
        assert "secrets" not in result.stdout.split()


class TestCentralCheckerReporting:
    def test_it_publishes_series_for_namespaces_other_than_its_own(
        self, kubectl, operator_namespace, central_pod, audited_namespace, read_metrics, poll_for,
    ):
        # Proves the cluster-wide list actually works under the ClusterRole, and
        # that metrics are labelled by subject namespace rather than by the
        # checker's own.
        #
        # The poll waits for *this* namespace specifically, not for any series at
        # all. The checker snapshots the pod list once per cycle, so a namespace
        # created after the current cycle began cannot appear until the next one —
        # and polling for "some series exist" returns on the first scrape, from a
        # cycle that predates the fixture.
        def audited_namespace_covered():
            samples = _samples(read_metrics(operator_namespace, central_pod), "kube_image_available")
            namespaces = {
                line.split('namespace="', 1)[1].split('"', 1)[0]
                for line in samples if 'namespace="' in line
            }
            return namespaces if audited_namespace in namespaces else None

        namespaces = poll_for(
            audited_namespace_covered, timeout=300, interval=15,
            what=f"central checker to publish series for {audited_namespace}",
        )
        assert len(namespaces) > 1, f"only saw {namespaces}; expected several namespaces"

    def test_an_opted_out_namespace_never_appears(
        self, kubectl, operator_namespace, central_pod, audited_namespace,
        opted_out_namespace, read_metrics, poll_for,
    ):
        # The parity gap central mode had to close. In perNamespace mode the
        # exclusion is enforced by never deploying a checker; here the checker
        # has to apply it to a cluster-wide pod list itself.
        def audited_namespace_present():
            samples = _samples(read_metrics(operator_namespace, central_pod), "kube_image_available")
            return any(f'namespace="{audited_namespace}"' in line for line in samples)

        # Wait until the sweep has demonstrably covered the comparison namespace,
        # so "absent" means excluded rather than "not got there yet".
        poll_for(audited_namespace_present, timeout=240, interval=15,
                 what=f"{audited_namespace} to be audited")

        metrics = read_metrics(operator_namespace, central_pod)
        offending = [
            line for line in _samples(metrics, "kube_image_available")
            if f'namespace="{opted_out_namespace}"' in line
        ]
        assert not offending, f"opted-out namespace was audited: {offending}"

    def test_policy_status_reports_central_coverage(
        self, kubectl, operator_namespace, audited_namespace, poll_for,
    ):
        # Without this the IAP would report deployed: false for every namespace
        # in central mode, which reads as a cluster-wide outage.
        def status_reason():
            # Bracket notation, not dotted: the generated namespace name contains
            # hyphens, which kubectl's jsonpath parses as part of the path.
            result = kubectl(
                "get", "imageauditpolicy", "cluster-defaults", "-n", operator_namespace,
                "-o", f"jsonpath={{.status.namespaces['{audited_namespace}'].reason}}",
                check=False, timeout=20,
            )
            return result.stdout.strip() or None

        assert poll_for(
            status_reason, timeout=120, interval=10, what="IAP status for the namespace",
        ) == "audited by central checker"
