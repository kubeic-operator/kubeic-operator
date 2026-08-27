"""Integration coverage for what the checker actually reports.

The rest of the integration suite proves the operator *deploys* checkers. These
prove a deployed checker really runs skopeo against a registry and publishes
what it found — none of which the unit tests can show, because they mock the
subprocess away.
"""
import json
import time

import pytest

CHECKER_SELECTOR = "app.kubernetes.io/component=checker"
CHECKER_DEPLOYMENT = "kubeic-checker"

# Served by ECR's Docker Hub mirror rather than Docker Hub itself, so the CI
# runner does not spend its anonymous pull budget on it.
REAL_IMAGE = "public.ecr.aws/docker/library/busybox:1.36"
MISSING_IMAGE = "public.ecr.aws/docker/library/busybox:this-tag-does-not-exist-kubeic"


def _wait_for_checker(kubectl, namespace, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = kubectl(
            "get", "deployment", CHECKER_DEPLOYMENT, "-n", namespace,
            check=False, timeout=10,
        )
        if result.returncode == 0:
            kubectl(
                "wait", "--for=condition=available",
                f"deployment/{CHECKER_DEPLOYMENT}", "-n", namespace,
                "--timeout=90s",
            )
            return
        time.sleep(3)
    pytest.fail(f"Checker deployment never appeared in {namespace} after {timeout}s")


def _checker_pod(kubectl, namespace):
    result = kubectl(
        "get", "pods", "-n", namespace, "-l", CHECKER_SELECTOR,
        "--field-selector=status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}", timeout=20,
    )
    name = result.stdout.strip()
    if not name:
        pytest.fail(f"No running checker pod in {namespace}")
    return name


def _read_metrics(kubectl, namespace, pod):
    """Scrape the checker's own /metrics from inside its pod.

    urllib rather than curl or wget: the checker image is python:3.13-slim plus
    skopeo, so python is the only HTTP client guaranteed to be there.
    """
    result = kubectl(
        "exec", pod, "-n", namespace, "--",
        "python", "-c",
        "import urllib.request;"
        "print(urllib.request.urlopen('http://localhost:9090/metrics', timeout=10).read().decode())",
        timeout=45, check=False,
    )
    return result.stdout


def _samples(metrics_text, metric_name):
    """{labels-string: float} for one metric, ignoring HELP/TYPE lines."""
    found = {}
    for line in metrics_text.splitlines():
        if not line.startswith(f"{metric_name}{{"):
            continue
        labels, _, value = line.rpartition(" ")
        found[labels] = float(value)
    return found


def _poll_for(fn, timeout, interval=10, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"Timed out after {timeout}s waiting for {what}; last saw: {last!r}")


@pytest.fixture
def namespace_with_pods(kubectl):
    """A namespace holding one resolvable image and one that cannot exist."""
    name = f"metrics-{int(time.time())}"
    kubectl("create", "namespace", name)
    kubectl("apply", "-n", name, "-f", "-", input=f"""apiVersion: v1
kind: Pod
metadata:
  name: good
spec:
  containers:
    - name: main
      image: {REAL_IMAGE}
      command: ["sleep", "3600"]
---
apiVersion: v1
kind: Pod
metadata:
  name: bad
spec:
  containers:
    - name: main
      image: {MISSING_IMAGE}
      command: ["sleep", "3600"]
""")
    yield name
    kubectl("delete", "namespace", name, "--wait=false", check=False, timeout=10)


@pytest.fixture
def namespace_with_pull_secret(kubectl):
    """A namespace whose pod references a pull secret, so credentials get probed."""
    name = f"creds-{int(time.time())}"
    kubectl("create", "namespace", name)
    docker_config = json.dumps({
        "auths": {"public.ecr.aws": {"auth": "a3ViZWljLXRlc3Q6bm90LWEtcmVhbC10b2tlbg=="}}
    })
    kubectl(
        "create", "secret", "generic", "dummy-pull-secret", "-n", name,
        "--type=kubernetes.io/dockerconfigjson",
        f"--from-literal=.dockerconfigjson={docker_config}",
    )
    kubectl("apply", "-n", name, "-f", "-", input=f"""apiVersion: v1
kind: Pod
metadata:
  name: with-secret
spec:
  imagePullSecrets:
    - name: dummy-pull-secret
  containers:
    - name: main
      image: {REAL_IMAGE}
      command: ["sleep", "3600"]
""")
    yield name
    kubectl("delete", "namespace", name, "--wait=false", check=False, timeout=10)


def test_checker_reports_real_registry_results(kubectl, namespace_with_pods):
    """skopeo actually runs, and tells the two images apart.

    Nothing else in the suite exercises the subprocess — the unit tests mock it
    — so this is the only place a broken skopeo invocation would surface.
    """
    ns = namespace_with_pods
    _wait_for_checker(kubectl, ns)
    pod = _checker_pod(kubectl, ns)

    def both_images_reported():
        samples = _samples(_read_metrics(kubectl, ns, pod), "kube_image_available")
        good = {k: v for k, v in samples.items() if REAL_IMAGE in k}
        bad = {k: v for k, v in samples.items() if MISSING_IMAGE in k}
        return (good, bad) if good and bad else None

    # intervalMinutes=1 in CI, and the sweep is paced across that interval, so
    # allow several cycles.
    good, bad = _poll_for(
        both_images_reported, timeout=180, what="both images in kube_image_available",
    )

    assert all(v == 1 for v in good.values()), f"resolvable image reported unavailable: {good}"
    assert all(v == 0 for v in bad.values()), f"missing tag reported available: {bad}"
    assert all('error_class="not_found"' in k for k in bad), (
        f"missing tag should classify as not_found, got: {list(bad)}"
    )
    assert all('error_class=""' in k for k in good), (
        f"available image should carry no error_class, got: {list(good)}"
    )


def test_credential_series_never_disappear_mid_cycle(kubectl, namespace_with_pull_secret):
    """kube_image_credential_valid must not vanish while a paced sweep runs.

    The gauge used to be cleared at the top of the credential check and
    repopulated as each probe finished. Once the sweep was paced across the
    interval that left a hole of minutes, and Prometheus reads an absent series
    as stale — so RegistryCredentialInvalid (critical, for: interval + 10m)
    would reset its pending timer every cycle and never fire.
    """
    ns = namespace_with_pull_secret
    _wait_for_checker(kubectl, ns)
    pod = _checker_pod(kubectl, ns)

    def credentials_published():
        samples = _samples(_read_metrics(kubectl, ns, pod), "kube_image_credential_valid")
        return samples or None

    first = _poll_for(
        credentials_published, timeout=180, what="kube_image_credential_valid to appear",
    )

    # Sample repeatedly across more than one paced cycle (interval is 60s in
    # CI). With the pre-fix behaviour at least one of these lands in the hole.
    for _ in range(6):
        time.sleep(20)
        later = _samples(_read_metrics(kubectl, ns, pod), "kube_image_credential_valid")
        assert later, (
            "kube_image_credential_valid went empty mid-cycle; Prometheus would "
            f"mark these stale. First read was: {first}"
        )


def test_policy_status_lists_the_namespace_as_deployed(kubectl, operator_namespace, test_namespace):
    """The IAP status is how an operator sees which namespaces are audited."""
    def deployed():
        result = kubectl(
            "get", "imageauditpolicy", "cluster-defaults", "-n", operator_namespace,
            "-o", "jsonpath={.status.namespaces['" + test_namespace + "'].deployed}",
            check=False, timeout=20,
        )
        return result.stdout.strip() or None

    value = _poll_for(
        deployed, timeout=150, what=f"{test_namespace} to appear in IAP status",
    )
    assert value == "true", (
        f"{test_namespace} reported as {value!r} rather than deployed"
    )
