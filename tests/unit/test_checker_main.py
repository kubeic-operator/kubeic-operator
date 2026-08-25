from unittest.mock import MagicMock, patch

import pytest

from kubeic_checker.main import _get_pods, _check_credential_validity


def _make_mock_pod(
    name="my-pod",
    # Matches the NAMESPACE the cycle tests patch in. _trim_pod reads the
    # namespace off the pod now, so a mismatch makes any assertion on a
    # "testns" sample vacuous — it looks for a series that could never exist.
    namespace="testns",
    annotations=None,
    containers=None,
    init_containers=None,
    image_pull_secrets=None,
    phase="Running",
):
    """Build a MagicMock that looks like a kubernetes client V1Pod."""
    if containers is None:
        containers = []
    if init_containers is None:
        init_containers = []
    if image_pull_secrets is None:
        image_pull_secrets = []

    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.annotations = annotations
    pod.status.phase = phase

    mock_containers = []
    for c in containers:
        mc = MagicMock()
        mc.name = c["name"]
        mc.image = c["image"]
        mock_containers.append(mc)
    pod.spec.containers = mock_containers

    mock_init = []
    for c in init_containers:
        mc = MagicMock()
        mc.name = c["name"]
        mc.image = c["image"]
        mock_init.append(mc)
    pod.spec.init_containers = mock_init if init_containers is not None else None

    mock_secrets = []
    for s in image_pull_secrets:
        ms = MagicMock()
        ms.name = s["name"]
        mock_secrets.append(ms)
    pod.spec.image_pull_secrets = mock_secrets if image_pull_secrets is not None else None

    return pod


def _pod_page(items):
    """A V1PodList-shaped mock: items plus an exhausted continue token.

    _get_pods pages until the token is falsy, so a bare MagicMock page (whose
    ._continue is a truthy Mock) loops forever. The page mock has to be real.
    """
    page = MagicMock(items=items)
    page.metadata._continue = None
    return page


class TestGetPods:
    @patch("kubeic_checker.main.client")
    def test_returns_empty_list_when_no_pods(self, mock_client_module):
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page([])
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("my-ns")

        assert result == []
        mock_v1.list_namespaced_pod.assert_called_once_with("my-ns", limit=500)

    @patch("kubeic_checker.main.client")
    def test_returns_pod_dicts_with_all_fields(self, mock_client_module):
        pod = _make_mock_pod(
            name="app-pod",
            namespace="prod",
            annotations={"note": "test"},
            containers=[{"name": "main", "image": "nginx:1.25"}],
            init_containers=[{"name": "init", "image": "busybox:1.36"}],
            image_pull_secrets=[{"name": "my-secret"}],
        )
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page([pod])
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("prod")

        assert len(result) == 1
        p = result[0]
        assert p["metadata"]["name"] == "app-pod"
        assert p["metadata"]["namespace"] == "prod"
        assert p["metadata"]["annotations"] == {"note": "test"}
        assert p["spec"]["containers"] == [{"name": "main", "image": "nginx:1.25"}]
        assert p["spec"]["initContainers"] == [{"name": "init", "image": "busybox:1.36"}]
        assert p["spec"]["imagePullSecrets"] == [{"name": "my-secret"}]

    @patch("kubeic_checker.main.client")
    def test_handles_none_init_containers_and_image_pull_secrets(self, mock_client_module):
        pod = _make_mock_pod(
            name="bare-pod",
            containers=[{"name": "app", "image": "alpine:3.19"}],
            init_containers=None,
            image_pull_secrets=None,
        )
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page([pod])
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("default")

        assert len(result) == 1
        p = result[0]
        assert p["spec"]["containers"] == [{"name": "app", "image": "alpine:3.19"}]
        assert p["spec"]["initContainers"] == []
        assert p["spec"]["imagePullSecrets"] == []

    @patch("kubeic_checker.main.client")
    def test_returns_multiple_pods(self, mock_client_module):
        pods = [
            _make_mock_pod(name="pod-a", containers=[{"name": "c1", "image": "img1"}]),
            _make_mock_pod(name="pod-b", containers=[{"name": "c2", "image": "img2"}]),
        ]
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page(pods)
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("test-ns")

        assert len(result) == 2
        assert result[0]["metadata"]["name"] == "pod-a"
        assert result[1]["metadata"]["name"] == "pod-b"

    @patch("kubeic_checker.main.client")
    def test_annotations_default_to_empty_dict_when_none(self, mock_client_module):
        pod = _make_mock_pod(name="no-anno", annotations=None)
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page([pod])
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("ns")

        assert result[0]["metadata"]["annotations"] == {}


class TestCheckCredentialValidity:
    def _make_cred(self, registry="r.io", source="pod:imagePullSecret:my-secret",
                   auth="dG9rZW4=", username=None, password=None):
        cred = MagicMock()
        cred.registry = registry
        cred.source = source
        cred.auth = auth
        cred.username = username
        cred.password = password
        cred.namespace = "ns"
        return cred

    def _make_pods(self, secrets_images):
        """Build pod dicts. secrets_images = {"secret-name": {"image": "r.io/app/img"}}"""
        pods = []
        for secret_name, images in secrets_images.items():
            pod = {
                "metadata": {"name": "pod1", "namespace": "ns"},
                "spec": {
                    "containers": [{"name": "c", "image": img} for img in images],
                    "initContainers": [],
                    "imagePullSecrets": [{"name": secret_name}],
                },
            }
            pods.append(pod)
        return pods

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_valid_credential(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (True, None, "")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], pods)

        mock_gauge.labels.assert_called_once_with(
            registry="r.io", namespace="ns", secret_name="my-secret",
        )
        mock_gauge.labels.return_value.set.assert_called_once_with(1)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_auth_failure_marks_invalid(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (False, "unauthorized", "auth_failure")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], pods)

        mock_gauge.labels.return_value.set.assert_called_once_with(0)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_network_error_falls_back_to_inspect(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (False, "timeout", "network")
        mock_inspect.return_value = (True, None, {}, "")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], pods)

        mock_gauge.labels.return_value.set.assert_called_once_with(1)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_no_matching_images_marks_invalid(self, mock_list_tags, mock_inspect, mock_gauge):
        cred = self._make_cred()
        pods = self._make_pods({"other-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], pods)

        mock_gauge.labels.return_value.set.assert_called_once_with(0)

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_skips_creds_without_auth_or_credentials(self, mock_list_tags, mock_inspect, mock_gauge):
        cred = MagicMock()
        cred.registry = "r.io"
        cred.source = "pod:imagePullSecret:my-secret"
        cred.auth = None
        cred.username = None
        cred.password = None

        _check_credential_validity([cred], [])

        mock_gauge.labels.assert_not_called()

    @patch("kubeic_checker.main.CREDENTIAL_TEST_IMAGE", "r.io/test/img")
    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_uses_credential_test_image_when_set(self, mock_list_tags, mock_inspect, mock_gauge):
        mock_list_tags.return_value = (True, None, "")
        cred = self._make_cred()
        pods = self._make_pods({"my-secret": {"r.io/app/img"}})

        _check_credential_validity([cred], pods)

        # Should be called with the test image, not a pod image
        call_args = mock_list_tags.call_args[0]
        assert call_args[0] == "r.io/test/img"


class _FakeRegistry:
    """Simulates a registry that scopes each repo to a specific credential —
    the per-project deploy-token behaviour (GitLab) behind issue #43."""

    def __init__(self, repo_tokens: dict, public_repos: set = ()):
        self.repo_tokens = repo_tokens  # docker://ref -> required auth token
        self.public_repos = set(public_repos)
        self.inspect_calls = []  # (ref, token_or_None)

    def _auth_token(self, cmd):
        import json as _json
        if "--authfile" not in cmd:
            return None
        with open(cmd[cmd.index("--authfile") + 1]) as f:
            auths = _json.load(f)["auths"]
        return next(iter(auths.values()))["auth"]

    def __call__(self, cmd, **kwargs):
        result = MagicMock()
        if "list-tags" in cmd:
            result.returncode = 0
            result.stdout = '{"Tags": ["v1"]}'
            return result
        ref = next(c for c in cmd if c.startswith("docker://"))
        token = self._auth_token(cmd)
        self.inspect_calls.append((ref, token))
        if ref in self.public_repos or (token is not None and self.repo_tokens.get(ref) == token):
            result.returncode = 0
            result.stdout = '{"Digest": "sha256:abc", "Created": "2026-07-01T00:00:00Z"}'
        else:
            result.returncode = 1
            result.stderr = "requested access to the resource is denied"
        return result


def _tok(name: str) -> str:
    """A docker-config auth value must be base64(user:pass) to survive
    _decode_docker_secret — arbitrary strings are silently dropped."""
    import base64
    return base64.b64encode(f"{name}:pw".encode()).decode()


def _docker_secret(registry: str, token: str) -> MagicMock:
    import base64
    import json as _json
    config = {"auths": {registry: {"auth": token}}}
    secret = MagicMock()
    secret.data = {".dockerconfigjson": base64.b64encode(_json.dumps(config).encode()).decode()}
    return secret


def _run_one_cycle():
    """Run exactly one run_check_loop cycle (sleep breaks the loop)."""
    import pytest
    from kubeic_checker.main import run_check_loop
    # _pace_sleep is neutralised so the paced sweep runs instantly; the
    # end-of-cycle time.sleep still raises to break the infinite loop.
    with (
        patch("kubeic_checker.main._pace_sleep"),
        patch("kubeic_checker.main.time.sleep", side_effect=KeyboardInterrupt),
    ):
        with pytest.raises(KeyboardInterrupt):
            run_check_loop()


class TestPacer:
    def test_spreads_work_evenly_across_the_interval(self):
        from kubeic_checker.main import _Pacer
        slept = []
        with patch("kubeic_checker.main.time.monotonic", return_value=0.0), \
             patch("kubeic_checker.main._pace_sleep", side_effect=slept.append):
            pacer = _Pacer(deadline=100.0, total=5)
            for _ in range(5):
                pacer()
        # monotonic frozen, so each gap is time_left / items_left:
        # 100/4, 100/3, 100/2, 100/1, then nothing on the last item.
        assert slept == [25.0, pytest.approx(33.333, rel=1e-3), 50.0, 100.0]

    def test_a_slow_item_shortens_the_gap_that_follows_it(self):
        # The gap is recomputed from the clock every call, so an item that ate
        # most of the interval leaves a correspondingly smaller gap behind it
        # rather than the sweep overrunning.
        from kubeic_checker.main import _Pacer

        def gap_after(elapsed):
            slept = []
            with (
                patch("kubeic_checker.main.time.monotonic", return_value=elapsed),
                patch("kubeic_checker.main._pace_sleep", side_effect=slept.append),
            ):
                _Pacer(deadline=100.0, total=4)()
            return slept[0]

        assert gap_after(0.0) == pytest.approx(100.0 / 3)
        assert gap_after(90.0) == pytest.approx(10.0 / 3)
        assert gap_after(90.0) < gap_after(0.0)

    def test_runs_flat_out_once_the_interval_is_spent(self):
        from kubeic_checker.main import _Pacer
        slept = []
        with patch("kubeic_checker.main.time.monotonic", return_value=200.0), \
             patch("kubeic_checker.main._pace_sleep", side_effect=slept.append):
            pacer = _Pacer(deadline=100.0, total=5)
            for _ in range(4):
                pacer()
        # Overrunning must not overlap the next cycle — it just stops waiting.
        assert slept == []

    def test_never_sleeps_after_the_last_item(self):
        from kubeic_checker.main import _Pacer
        slept = []
        with patch("kubeic_checker.main.time.monotonic", return_value=0.0), \
             patch("kubeic_checker.main._pace_sleep", side_effect=slept.append):
            pacer = _Pacer(deadline=100.0, total=1)
            pacer()
        assert slept == []

    def test_zero_work_does_not_divide_by_zero(self):
        from kubeic_checker.main import _Pacer
        with patch("kubeic_checker.main.time.monotonic", return_value=0.0), \
             patch("kubeic_checker.main._pace_sleep") as slept:
            _Pacer(deadline=100.0, total=0)()
        slept.assert_not_called()


class TestPodListingScope:
    @patch("kubeic_checker.main.client")
    def test_central_mode_lists_the_whole_cluster(self, mock_client_module):
        mock_v1 = MagicMock()
        mock_v1.list_pod_for_all_namespaces.return_value = _pod_page([])
        mock_client_module.CoreV1Api.return_value = mock_v1

        _get_pods("")

        mock_v1.list_pod_for_all_namespaces.assert_called_once_with(limit=500)
        mock_v1.list_namespaced_pod.assert_not_called()

    @patch("kubeic_checker.main.client")
    def test_pages_until_the_continue_token_is_exhausted(self, mock_client_module):
        # Unpaginated, a cluster-wide list deserialises every pod at once and
        # that peak becomes the pod's resident floor.
        first = MagicMock(items=[_make_mock_pod(name="a", namespace="ns-a")])
        first.metadata._continue = "tok-1"
        second = MagicMock(items=[_make_mock_pod(name="b", namespace="ns-b")])
        second.metadata._continue = ""
        mock_v1 = MagicMock()
        mock_v1.list_pod_for_all_namespaces.side_effect = [first, second]
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("")

        assert [p["metadata"]["name"] for p in result] == ["a", "b"]
        assert mock_v1.list_pod_for_all_namespaces.call_args_list[1][1]["_continue"] == "tok-1"

    @patch("kubeic_checker.main.client")
    def test_each_pod_keeps_its_own_namespace(self, mock_client_module):
        mock_v1 = MagicMock()
        mock_v1.list_pod_for_all_namespaces.return_value = _pod_page([
            _make_mock_pod(name="a", namespace="ns-a"),
            _make_mock_pod(name="b", namespace="ns-b"),
        ])
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("")

        assert [p["metadata"]["namespace"] for p in result] == ["ns-a", "ns-b"]


class TestRunCheckLoopCycle:
    """End-to-end cycle tests: pods + secrets + a fake registry with
    per-repo credential scoping, through the real resolve -> build ->
    check -> metrics pipeline."""

    def setup_method(self):
        from kubeic_operator import metrics
        metrics.kube_image_available.clear()
        metrics.kube_image_credential_valid.clear()

    teardown_method = setup_method

    def _run(self, pods, secrets, registry):
        def read_secret(name, ns):
            value = secrets[name]
            if isinstance(value, Exception):
                raise value
            return value

        # The pod's own namespace is what everything downstream keys on now,
        # so it has to agree with the patched NAMESPACE below.
        for pod in pods:
            pod.metadata.namespace = "testns"

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page(pods)
        mock_v1.read_namespaced_secret.side_effect = read_secret
        with patch("kubeic_checker.main.config"), \
             patch("kubeic_checker.main.client") as mock_client, \
             patch("kubeic_checker.main.NAMESPACE", "testns"), \
             patch("kubeic_checker.availability.time.sleep"), \
             patch("kubeic_checker.availability.subprocess.run", side_effect=registry):
            mock_client.CoreV1Api.return_value = mock_v1
            _run_one_cycle()

    def test_each_image_checked_with_its_own_pods_secret(self):
        # The #43 production scenario: two per-project tokens on one registry
        # host. The merged-auth-file behaviour checked one of these with the
        # other's token and reported a false auth_failure.
        registry = _FakeRegistry({
            "docker://r.corp.io/org/app-a:v1": _tok("token-a"),
            "docker://r.corp.io/org/app-b:v1": _tok("token-b"),
        })
        pods = [
            _make_mock_pod(name="app-a", containers=[{"name": "main", "image": "r.corp.io/org/app-a:v1"}],
                           image_pull_secrets=[{"name": "secret-a"}]),
            _make_mock_pod(name="app-b", containers=[{"name": "main", "image": "r.corp.io/org/app-b:v1"}],
                           image_pull_secrets=[{"name": "secret-b"}]),
        ]
        secrets = {
            "secret-a": _docker_secret("r.corp.io", _tok("token-a")),
            "secret-b": _docker_secret("r.corp.io", _tok("token-b")),
        }
        self._run(pods, secrets, registry)

        from prometheus_client import REGISTRY
        for img, pod in (("r.corp.io/org/app-a:v1", "app-a"), ("r.corp.io/org/app-b:v1", "app-b")):
            v = REGISTRY.get_sample_value("kube_image_available", {
                "image": img, "registry": "r.corp.io",
                "image_name": img.split("/", 1)[1].rsplit(":", 1)[0],
                "namespace": "testns", "pod": pod, "container": "main", "error_class": "",
            })
            assert v == 1, f"{img} should be available with its own pod credential"
        # each image was inspected with exactly its own secret token
        tokens = {ref: tok for ref, tok in registry.inspect_calls}
        assert tokens["docker://r.corp.io/org/app-a:v1"] == _tok("token-a")
        assert tokens["docker://r.corp.io/org/app-b:v1"] == _tok("token-b")

    def test_wrong_first_secret_falls_through_to_working_one(self):
        registry = _FakeRegistry({"docker://r.corp.io/org/app-c:v1": _tok("token-right")})
        pods = [
            _make_mock_pod(name="app-c", containers=[{"name": "main", "image": "r.corp.io/org/app-c:v1"}],
                           image_pull_secrets=[{"name": "wrong"}, {"name": "right"}]),
        ]
        secrets = {
            "wrong": _docker_secret("r.corp.io", _tok("token-wrong")),
            "right": _docker_secret("r.corp.io", _tok("token-right")),
        }
        self._run(pods, secrets, registry)
        attempts = [t for r, t in registry.inspect_calls if r == "docker://r.corp.io/org/app-c:v1"]
        assert attempts == [_tok("token-wrong"), _tok("token-right")]

    def test_pod_list_api_failure_does_not_crash_cycle(self):
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.side_effect = RuntimeError("apiserver unavailable")
        with patch("kubeic_checker.main.config"), \
             patch("kubeic_checker.main.client") as mock_client, \
             patch("kubeic_checker.main.NAMESPACE", "testns"):
            mock_client.CoreV1Api.return_value = mock_v1
            _run_one_cycle()  # KeyboardInterrupt from sleep, NOT RuntimeError

    def test_forbidden_secret_degrades_to_unauthenticated(self):
        # RBAC-denied secret read: the cycle continues and the image is
        # checked without credentials rather than crashing.
        registry = _FakeRegistry({}, public_repos={"docker://r.corp.io/org/public:v1"})
        pods = [
            _make_mock_pod(name="pub", containers=[{"name": "main", "image": "r.corp.io/org/public:v1"}],
                           image_pull_secrets=[{"name": "denied-secret"}]),
        ]
        secrets = {"denied-secret": RuntimeError("secrets is forbidden")}
        self._run(pods, secrets, registry)
        assert registry.inspect_calls == [("docker://r.corp.io/org/public:v1", None)]

    def test_skip_annotation_excludes_pod_from_availability(self):
        registry = _FakeRegistry({}, public_repos={"docker://r.corp.io/org/checked:v1"})
        skipped = _make_mock_pod(name="skipped", annotations={"kubeic.io/skip": "availability"},
                                 containers=[{"name": "main", "image": "r.corp.io/org/skipped:v1"}])
        checked = _make_mock_pod(name="checked",
                                 containers=[{"name": "main", "image": "r.corp.io/org/checked:v1"}])
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page([skipped, checked])
        with patch("kubeic_checker.main.config"), \
             patch("kubeic_checker.main.client") as mock_client, \
             patch("kubeic_checker.main.NAMESPACE", "testns"), \
             patch("kubeic_checker.main.SKIP_ANNOTATION", "kubeic.io/skip"), \
             patch("kubeic_checker.availability.subprocess.run", side_effect=registry):
            mock_client.CoreV1Api.return_value = mock_v1
            _run_one_cycle()
        refs = [r for r, _ in registry.inspect_calls]
        assert "docker://r.corp.io/org/checked:v1" in refs
        assert "docker://r.corp.io/org/skipped:v1" not in refs


class TestCredentialValidityPublishIsAtomic:
    """The gauge must not vanish while a paced sweep is in progress.

    Clearing up front and setting each series as its probe finished was
    harmless when the sweep was instantaneous. Under pacing it leaves the
    series absent for most of the cycle, and Prometheus reads an absent series
    as stale — so RegistryCredentialInvalid (critical, for: interval + 10m)
    would reset its pending timer every cycle and could never fire.
    """

    def setup_method(self):
        from kubeic_operator import metrics
        metrics.kube_image_credential_valid.clear()

    teardown_method = setup_method

    @staticmethod
    def _cred(secret, namespace="ns", registry="r.io", auth="dG9rZW4="):
        from kubeic_checker.credentials import ResolvedCredential
        return ResolvedCredential(
            registry=registry, auth=auth,
            source=f"pod:imagePullSecret:{secret}", namespace=namespace,
        )

    @staticmethod
    def _pods(secret, image="r.io/app/img", namespace="ns"):
        return [{
            "metadata": {"name": "pod1", "namespace": namespace},
            "spec": {
                "containers": [{"name": "c", "image": image}],
                "initContainers": [],
                "imagePullSecrets": [{"name": secret}],
            },
        }]

    @patch("kubeic_checker.availability._run_skopeo_list_tags", return_value=(True, None, ""))
    def test_previous_cycles_series_stay_visible_until_the_new_set_lands(self, _tags):
        from prometheus_client import REGISTRY
        from kubeic_operator import metrics

        stale_labels = {"registry": "r.io", "namespace": "ns", "secret_name": "from-last-cycle"}
        metrics.kube_image_credential_valid.labels(**stale_labels).set(0)

        mid_loop = []

        def snapshot():
            mid_loop.append(
                REGISTRY.get_sample_value("kube_image_credential_valid", stale_labels)
            )

        creds = [self._cred("secret-a"), self._cred("secret-b")]
        pods = self._pods("secret-a") + self._pods("secret-b")
        _check_credential_validity(creds, pods, pacer=snapshot)

        # Sampled after each probe: the old series is still being served
        # throughout, rather than a hole opening at the top of the function.
        assert mid_loop and all(v == 0 for v in mid_loop), mid_loop
        # ...and is replaced only once the new set is published.
        assert REGISTRY.get_sample_value("kube_image_credential_valid", stale_labels) is None
        assert REGISTRY.get_sample_value("kube_image_credential_valid", {
            "registry": "r.io", "namespace": "ns", "secret_name": "secret-a",
        }) == 1

    @patch("kubeic_checker.availability._run_skopeo_list_tags", return_value=(True, None, ""))
    def test_pacer_fires_once_per_credential_actually_probed(self, _tags):
        # The pacer is sized on _probeable_credentials, so it has to be
        # advanced exactly that many times or the gaps are miscomputed and the
        # sweep drifts back towards burst-then-idle.
        from kubeic_checker.main import _probeable_credentials

        creds = [
            self._cred("secret-a"),
            self._cred("secret-a"),                  # duplicate: same ns/registry/source
            self._cred("secret-c", auth=None),       # no auth material: never probed
            self._cred("secret-b"),
        ]
        pods = self._pods("secret-a") + self._pods("secret-b")

        calls = []
        _check_credential_validity(creds, pods, pacer=lambda: calls.append(1))

        assert len(_probeable_credentials(creds)) == 2
        assert len(calls) == 2

    def test_probeable_credentials_skips_duplicates_and_empty_material(self):
        from kubeic_checker.main import _probeable_credentials

        creds = [
            self._cred("secret-a"),
            self._cred("secret-a"),
            self._cred("secret-a", namespace="other-ns"),   # same name, different ns
            self._cred("secret-c", auth=None),
        ]
        probeable = _probeable_credentials(creds)

        assert [ns for ns, _ in probeable] == ["ns", "other-ns"]


class TestCredentialValidityBranches:
    def _cred(self, registry="r.io", secret="my-secret", auth="dG9rZW4=",
              username=None, password=None):
        cred = MagicMock()
        cred.registry = registry
        cred.source = f"pod:imagePullSecret:{secret}"
        cred.auth = auth
        cred.username = username
        cred.password = password
        cred.namespace = "ns"
        return cred

    def _pods(self, image="r.io/app/img", secret="my-secret"):
        return [{
            "metadata": {"name": "pod1", "namespace": "ns"},
            "spec": {
                "containers": [{"name": "c", "image": image}],
                "initContainers": [],
                "imagePullSecrets": [{"name": secret}],
            },
        }]

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_empty_credential_is_skipped(self, mock_list_tags, mock_gauge):
        # A dockerconfig entry with no auth and no user/pass carries nothing
        # to test — it must not produce a gauge sample at all.
        cred = self._cred(auth=None, username=None, password=None)
        _check_credential_validity([cred], self._pods())
        mock_gauge.labels.assert_not_called()
        mock_list_tags.assert_not_called()

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_duplicate_credential_tested_once(self, mock_list_tags, mock_gauge):
        # The same secret referenced by many pods resolves to identical creds;
        # each must be probed against the registry exactly once per cycle.
        mock_list_tags.return_value = (True, None, "")
        cred = self._cred()
        _check_credential_validity([cred, cred], self._pods())
        assert mock_list_tags.call_count == 1
        assert mock_gauge.labels.call_count == 1

    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_username_password_credential_supported(self, mock_list_tags, mock_gauge):
        # Secrets that carry username/password instead of a pre-encoded auth
        # blob (older tooling writes these) must still be testable.
        mock_list_tags.return_value = (True, None, "")
        cred = self._cred(auth=None, username="u", password="p")
        _check_credential_validity([cred], self._pods())
        mock_gauge.labels.return_value.set.assert_called_once_with(1)

    @patch("kubeic_checker.main.CREDENTIAL_TEST_IMAGE", "r.io/canary/probe")
    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_configured_test_image_auth_failure_marks_invalid(self, mock_list_tags, mock_gauge):
        mock_list_tags.return_value = (False, "unauthorized", "auth_failure")
        _check_credential_validity([self._cred()], self._pods())
        mock_list_tags.assert_called_once()
        assert mock_list_tags.call_args[0][0] == "r.io/canary/probe"
        mock_gauge.labels.return_value.set.assert_called_once_with(0)

    @patch("kubeic_checker.main.CREDENTIAL_TEST_IMAGE", "r.io/canary/probe")
    @patch("kubeic_checker.main.kube_image_credential_valid")
    @patch("kubeic_checker.availability._run_skopeo_inspect")
    @patch("kubeic_checker.availability._run_skopeo_list_tags")
    def test_configured_test_image_network_error_falls_back_to_inspect(
        self, mock_list_tags, mock_inspect, mock_gauge,
    ):
        # A registry that rejects list-tags (network/unknown) must not mark
        # the credential invalid if a plain inspect works — the syd-1 registry
        # rate-limit shape.
        mock_list_tags.return_value = (False, "timeout", "network")
        mock_inspect.return_value = (True, None, {}, "")
        _check_credential_validity([self._cred()], self._pods())
        mock_gauge.labels.return_value.set.assert_called_once_with(1)


class TestCycleLogging:
    """The unavailable/digest-mismatch WARNING lines are the checker's Loki
    interface — production triage greps exactly these strings."""

    def setup_method(self):
        from kubeic_operator import metrics
        metrics.kube_image_available.clear()
        metrics.kube_image_credential_valid.clear()

    teardown_method = setup_method

    def _cycle(self, pods_items, registry):
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page(pods_items)
        with patch("kubeic_checker.main.config"), \
             patch("kubeic_checker.main.client") as mock_client, \
             patch("kubeic_checker.main.NAMESPACE", "testns"), \
             patch("kubeic_checker.availability.time.sleep"), \
             patch("kubeic_checker.availability.subprocess.run", side_effect=registry):
            mock_client.CoreV1Api.return_value = mock_v1
            _run_one_cycle()

    def test_unavailable_image_logged_once_across_pods(self, caplog):
        # Two pods running the same missing image must produce ONE
        # "Image unavailable" line, not one per pod.
        registry = _FakeRegistry({})  # knows no repos -> denied for all
        pods = [
            _make_mock_pod(name="a", containers=[{"name": "main", "image": "r.corp.io/org/gone:v1"}]),
            _make_mock_pod(name="b", containers=[{"name": "main", "image": "r.corp.io/org/gone:v1"}]),
        ]
        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            self._cycle(pods, registry)
        lines = [r.message for r in caplog.records if "Image unavailable" in r.message]
        assert len(lines) == 1
        assert "error_class=auth_failure" in lines[0]

    def test_no_pods_logs_and_continues(self, caplog):
        import logging as _logging
        with caplog.at_level(_logging.INFO):
            self._cycle([], _FakeRegistry({}))
        assert any("No pods found" in r.message for r in caplog.records)

    def test_all_available_logs_summary(self, caplog):
        registry = _FakeRegistry({}, public_repos={"docker://r.corp.io/org/ok:v1"})
        pods = [_make_mock_pod(name="ok", containers=[{"name": "main", "image": "r.corp.io/org/ok:v1"}])]
        import logging as _logging
        with caplog.at_level(_logging.INFO):
            self._cycle(pods, registry)
        assert any("All 1 images available" in r.message for r in caplog.records)


class TestTerminatedPodsExcluded:
    @patch("kubeic_checker.main.client")
    def test_get_pods_skips_succeeded_and_failed(self, mock_client_module):
        pods = [
            _make_mock_pod(name="running", phase="Running",
                           containers=[{"name": "c", "image": "img:1"}]),
            _make_mock_pod(name="pending", phase="Pending",
                           containers=[{"name": "c", "image": "img:1"}]),
            _make_mock_pod(name="done", phase="Succeeded",
                           containers=[{"name": "c", "image": "img:1"}]),
            _make_mock_pod(name="dead", phase="Failed",
                           containers=[{"name": "c", "image": "img:1"}]),
        ]
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = _pod_page(pods)
        mock_client_module.CoreV1Api.return_value = mock_v1

        result = _get_pods("ns")
        names = [p["metadata"]["name"] for p in result]
        assert names == ["running", "pending"]

    def test_failed_job_pod_with_dead_token_produces_no_false_alert(self):
        # The production shape behind JSM #64116 one layer deeper than the
        # merged-auth-file bug: a Failed CI job pod lingers with an ephemeral
        # pull secret whose job token expired. Auditing it yields a
        # guaranteed auth_failure for an image nothing will ever re-pull.
        from kubeic_operator import metrics
        metrics.kube_image_available.clear()
        metrics.kube_image_credential_valid.clear()
        try:
            registry = _FakeRegistry({}, public_repos={"docker://r.corp.io/org/live:v1"})
            dead = _make_mock_pod(
                name="runner-job-dead", phase="Failed",
                containers=[{"name": "build", "image": "r.corp.io/devops/ci-images:v1"}],
                image_pull_secrets=[{"name": "runner-ephemeral"}],
            )
            live = _make_mock_pod(
                name="live", phase="Running",
                containers=[{"name": "main", "image": "r.corp.io/org/live:v1"}],
            )
            mock_v1 = MagicMock()
            mock_v1.list_namespaced_pod.return_value = _pod_page([dead, live])
            # the ephemeral secret is gone/expired — reading it would fail
            mock_v1.read_namespaced_secret.side_effect = RuntimeError("secret deleted with job")
            with patch("kubeic_checker.main.config"), \
                 patch("kubeic_checker.main.client") as mock_client, \
                 patch("kubeic_checker.main.NAMESPACE", "testns"), \
                 patch("kubeic_checker.availability.subprocess.run", side_effect=registry):
                mock_client.CoreV1Api.return_value = mock_v1
                _run_one_cycle()

            refs = [r for r, _ in registry.inspect_calls]
            assert refs == ["docker://r.corp.io/org/live:v1"]
            # the dead pod's secret was never even read
            mock_v1.read_namespaced_secret.assert_not_called()
            from prometheus_client import REGISTRY
            # Positive control: the live pod's series must be present under
            # these exact labels, so the absence assertions below are looking
            # in a place where a sample really could have appeared.
            assert REGISTRY.get_sample_value("kube_image_available", {
                "image": "r.corp.io/org/live:v1", "registry": "r.corp.io",
                "image_name": "org/live", "namespace": "testns",
                "pod": "live", "container": "main", "error_class": "",
            }) == 1
            for ec in ("auth_failure", "not_found", "network", "unknown"):
                v = REGISTRY.get_sample_value("kube_image_available", {
                    "image": "r.corp.io/devops/ci-images:v1", "registry": "r.corp.io",
                    "image_name": "devops/ci-images", "namespace": "testns",
                    "pod": "runner-job-dead", "container": "build", "error_class": ec,
                })
                assert v is None
        finally:
            metrics.kube_image_available.clear()
            metrics.kube_image_credential_valid.clear()
