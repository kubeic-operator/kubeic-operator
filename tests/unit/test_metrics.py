from unittest.mock import MagicMock

from prometheus_client import REGISTRY

from kubeic_operator import metrics


def _make_prerelease_finding(
    image="myapp:1.0.0-alpha",
    registry="docker.io",
    image_name="library/myapp",
    tag="1.0.0-alpha",
    namespace="default",
    pod="pod-1",
    container="main",
    age_days=10,
):
    f = MagicMock()
    f.image = image
    f.registry = registry
    f.image_name = image_name
    f.tag = tag
    f.namespace = namespace
    f.pod = pod
    f.container = container
    f.age_days = age_days
    return f


def _make_spread_finding(
    registry="docker.io",
    image_name="library/nginx",
    version_count=4,
    violates_threshold=True,
    version_pod_counts=None,
):
    f = MagicMock()
    f.registry = registry
    f.image_name = image_name
    f.version_count = version_count
    f.violates_threshold = violates_threshold
    f.version_pod_counts = version_pod_counts or {
        "1.25": {"default": 2},
        "1.26": {"default": 1},
    }
    return f


def _make_availability_result(
    image="nginx:1.25",
    registry="docker.io",
    image_name="library/nginx",
    namespace="default",
    pod="pod-1",
    container="main",
    available=True,
    digest_match=None,
):
    r = MagicMock()
    r.image = image
    r.registry = registry
    r.image_name = image_name
    r.namespace = namespace
    r.pod = pod
    r.container = container
    r.available = available
    r.digest_match = digest_match
    return r


class TestUpdatePrereleaseMetrics:
    def setup_method(self):
        metrics.kube_image_is_prerelease.clear()
        metrics.kube_image_prerelease_age_days.clear()
        metrics.kube_image_total_prerelease_violations.set(0)

    def teardown_method(self):
        metrics.kube_image_is_prerelease.clear()
        metrics.kube_image_prerelease_age_days.clear()
        metrics.kube_image_total_prerelease_violations.set(0)

    def test_empty_findings_sets_violation_count_to_zero(self):
        metrics.update_prerelease_metrics([], violation_count=0)

        value = REGISTRY.get_sample_value("kube_image_total_prerelease_violations")
        assert value == 0

    def test_findings_set_prerelease_and_age_days(self):
        f = _make_prerelease_finding(age_days=15)
        metrics.update_prerelease_metrics([f], violation_count=2)

        labels = {
            "image": f.image,
            "registry": f.registry,
            "image_name": f.image_name,
            "tag": f.tag,
            "namespace": f.namespace,
            "pod": f.pod,
            "container": f.container,
        }
        is_pre = REGISTRY.get_sample_value("kube_image_is_prerelease", labels)
        age = REGISTRY.get_sample_value("kube_image_prerelease_age_days", labels)
        violations = REGISTRY.get_sample_value("kube_image_total_prerelease_violations")

        assert is_pre == 1
        assert age == 15
        assert violations == 2


class TestUpdateSpreadMetrics:
    def setup_method(self):
        metrics.kube_image_version_count.clear()
        metrics.kube_image_version_pod_count.clear()
        metrics.kube_image_version_spread_violation.clear()
        metrics.kube_image_total_spread_violations.set(0)

    def teardown_method(self):
        metrics.kube_image_version_count.clear()
        metrics.kube_image_version_pod_count.clear()
        metrics.kube_image_version_spread_violation.clear()
        metrics.kube_image_total_spread_violations.set(0)

    def test_findings_set_version_count_violation_and_pod_counts(self):
        pod_counts = {
            "1.25": {"default": 3},
            "1.26": {"kube-system": 1},
        }
        f = _make_spread_finding(
            version_count=2,
            violates_threshold=True,
            version_pod_counts=pod_counts,
        )
        metrics.update_spread_metrics([f])

        vc = REGISTRY.get_sample_value(
            "kube_image_version_count",
            {"registry": f.registry, "image_name": f.image_name},
        )
        assert vc == 2

        violation = REGISTRY.get_sample_value(
            "kube_image_version_spread_violation",
            {"registry": f.registry, "image_name": f.image_name},
        )
        assert violation == 1

        pc1 = REGISTRY.get_sample_value(
            "kube_image_version_pod_count",
            {"registry": f.registry, "image_name": f.image_name, "tag": "1.25", "namespace": "default"},
        )
        assert pc1 == 3

        pc2 = REGISTRY.get_sample_value(
            "kube_image_version_pod_count",
            {"registry": f.registry, "image_name": f.image_name, "tag": "1.26", "namespace": "kube-system"},
        )
        assert pc2 == 1

        total = REGISTRY.get_sample_value("kube_image_total_spread_violations")
        assert total == 1

    def test_no_violations_sets_violation_gauge_to_zero(self):
        f = _make_spread_finding(
            version_count=2,
            violates_threshold=False,
        )
        metrics.update_spread_metrics([f])

        violation = REGISTRY.get_sample_value(
            "kube_image_version_spread_violation",
            {"registry": f.registry, "image_name": f.image_name},
        )
        assert violation == 0

        total = REGISTRY.get_sample_value("kube_image_total_spread_violations")
        assert total == 0


class TestUpdateAvailabilityMetrics:
    def setup_method(self):
        metrics.kube_image_available.clear()
        metrics.kube_image_digest_match.clear()
        metrics.kube_image_total_unavailable.clear()

    def teardown_method(self):
        metrics.kube_image_available.clear()
        metrics.kube_image_digest_match.clear()
        metrics.kube_image_total_unavailable.clear()

    def test_available_images_set_available_to_one(self):
        r = _make_availability_result(available=True)
        metrics.update_availability_metrics([r], namespace="default")

        labels = {
            "image": r.image,
            "registry": r.registry,
            "image_name": r.image_name,
            "namespace": r.namespace,
            "pod": r.pod,
            "container": r.container,
        }
        avail = REGISTRY.get_sample_value("kube_image_available", labels)
        assert avail == 1

        total = REGISTRY.get_sample_value(
            "kube_image_total_unavailable", {"namespace": "default"},
        )
        assert total == 0

    def test_unavailable_images_set_available_to_zero(self):
        r = _make_availability_result(available=False)
        metrics.update_availability_metrics([r], namespace="monitoring")

        labels = {
            "image": r.image,
            "registry": r.registry,
            "image_name": r.image_name,
            "namespace": r.namespace,
            "pod": r.pod,
            "container": r.container,
        }
        avail = REGISTRY.get_sample_value("kube_image_available", labels)
        assert avail == 0

        total = REGISTRY.get_sample_value(
            "kube_image_total_unavailable", {"namespace": "monitoring"},
        )
        assert total == 1

    def test_digest_match_true(self):
        r = _make_availability_result(available=True, digest_match=True)
        metrics.update_availability_metrics([r], namespace="default")

        labels = {
            "image": r.image,
            "registry": r.registry,
            "image_name": r.image_name,
            "namespace": r.namespace,
            "pod": r.pod,
            "container": r.container,
        }
        digest = REGISTRY.get_sample_value("kube_image_digest_match", labels)
        assert digest == 1

    def test_digest_match_false(self):
        r = _make_availability_result(available=True, digest_match=False)
        metrics.update_availability_metrics([r], namespace="default")

        labels = {
            "image": r.image,
            "registry": r.registry,
            "image_name": r.image_name,
            "namespace": r.namespace,
            "pod": r.pod,
            "container": r.container,
        }
        digest = REGISTRY.get_sample_value("kube_image_digest_match", labels)
        assert digest == 0

    def test_digest_match_none_omits_gauge(self):
        r = _make_availability_result(available=True, digest_match=None)
        metrics.update_availability_metrics([r], namespace="default")

        labels = {
            "image": r.image,
            "registry": r.registry,
            "image_name": r.image_name,
            "namespace": r.namespace,
            "pod": r.pod,
            "container": r.container,
        }
        digest = REGISTRY.get_sample_value("kube_image_digest_match", labels)
        assert digest is None
