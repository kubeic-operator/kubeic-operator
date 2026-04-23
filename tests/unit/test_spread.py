from kubeic_operator.checks.spread import aggregate_version_spread


def _make_pod(name, namespace, image):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"containers": [{"name": "main", "image": image}]},
    }


class TestAggregateVersionSpread:
    def test_no_spread_single_version(self):
        pods = [
            _make_pod("pod-1", "default", "nginx:1.25"),
            _make_pod("pod-2", "default", "nginx:1.25"),
        ]
        findings = aggregate_version_spread(pods)
        assert len(findings) == 0

    def test_detects_version_spread(self):
        pods = [
            _make_pod("pod-1", "default", "nginx:1.25"),
            _make_pod("pod-2", "default", "nginx:1.26"),
            _make_pod("pod-3", "default", "nginx:1.27"),
            _make_pod("pod-4", "default", "nginx:1.28"),
        ]
        findings = aggregate_version_spread(pods)
        assert len(findings) == 1
        assert findings[0].registry == "docker.io"
        assert findings[0].image_name == "library/nginx"
        assert findings[0].version_count == 4
        assert findings[0].violates_threshold is True

    def test_respects_custom_threshold(self):
        pods = [
            _make_pod("pod-1", "default", "nginx:1.25"),
            _make_pod("pod-2", "default", "nginx:1.26"),
        ]
        findings = aggregate_version_spread(pods, threshold=1)
        assert len(findings) == 1
        assert findings[0].violates_threshold is True

    def test_below_threshold_not_violation(self):
        pods = [
            _make_pod("pod-1", "default", "nginx:1.25"),
            _make_pod("pod-2", "default", "nginx:1.26"),
        ]
        findings = aggregate_version_spread(pods, threshold=5)
        assert len(findings) == 1
        assert findings[0].violates_threshold is False

    def test_cross_namespace_spread(self):
        pods = [
            _make_pod("pod-1", "ns-a", "myapp:v1"),
            _make_pod("pod-2", "ns-b", "myapp:v2"),
            _make_pod("pod-3", "ns-a", "myapp:v3"),
        ]
        findings = aggregate_version_spread(pods)
        assert len(findings) == 1
        f = findings[0]
        assert f.version_count == 3
        assert f.version_pod_counts["v1"]["ns-a"] == 1
        assert f.version_pod_counts["v2"]["ns-b"] == 1
        assert f.version_pod_counts["v3"]["ns-a"] == 1

    def test_multiple_image_bases(self):
        pods = [
            _make_pod("pod-1", "default", "nginx:1.25"),
            _make_pod("pod-2", "default", "nginx:1.26"),
            _make_pod("pod-3", "default", "redis:7.0"),
            _make_pod("pod-4", "default", "redis:7.1"),
        ]
        findings = aggregate_version_spread(pods)
        assert len(findings) == 2
        names = {f.image_name for f in findings}
        assert names == {"library/nginx", "library/redis"}

    def test_includes_init_containers(self):
        pods = [{
            "metadata": {"name": "pod-1", "namespace": "default"},
            "spec": {
                "containers": [{"name": "main", "image": "nginx:1.25"}],
                "initContainers": [{"name": "init", "image": "nginx:1.26"}],
            },
        }]
        findings = aggregate_version_spread(pods)
        assert len(findings) == 1
        assert findings[0].version_count == 2

    def test_registry_with_port(self):
        pods = [
            _make_pod("pod-1", "default", "myregistry.corp.com:5000/app:v1"),
            _make_pod("pod-2", "default", "myregistry.corp.com:5000/app:v2"),
        ]
        findings = aggregate_version_spread(pods)
        assert len(findings) == 1
        assert findings[0].registry == "myregistry.corp.com:5000"
        assert findings[0].image_name == "app"
