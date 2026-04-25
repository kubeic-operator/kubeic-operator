from datetime import datetime, timezone, timedelta

from kubeic_operator.checks.prerelease import (
    _parse_image,
    _parse_registry,
    is_prerelease_tag,
    check_prerelease,
    filter_violations,
)


class TestParseRegistry:
    def test_bare_image(self):
        registry, name = _parse_registry("nginx")
        assert registry == "docker.io"
        assert name == "library/nginx"

    def test_docker_hub_user_repo(self):
        registry, name = _parse_registry("myuser/myapp")
        assert registry == "docker.io"
        assert name == "myuser/myapp"

    def test_explicit_registry(self):
        registry, name = _parse_registry("quay.io/myorg/myapp")
        assert registry == "quay.io"
        assert name == "myorg/myapp"

    def test_registry_with_port(self):
        registry, name = _parse_registry("myregistry.corp.com:5000/app")
        assert registry == "myregistry.corp.com:5000"
        assert name == "app"

    def test_deep_path(self):
        registry, name = _parse_registry("registry.k8s.io/ingress-nginx/controller")
        assert registry == "registry.k8s.io"
        assert name == "ingress-nginx/controller"


class TestParseImage:
    def test_simple_image_with_tag(self):
        registry, name, tag = _parse_image("nginx:1.25")
        assert registry == "docker.io"
        assert name == "library/nginx"
        assert tag == "1.25"

    def test_registry_with_port_and_tag(self):
        registry, name, tag = _parse_image("myregistry.corp.com:5000/app:v2")
        assert registry == "myregistry.corp.com:5000"
        assert name == "app"
        assert tag == "v2"

    def test_image_without_tag(self):
        registry, name, tag = _parse_image("nginx")
        assert registry == "docker.io"
        assert name == "library/nginx"
        assert tag == "latest"

    def test_image_with_digest_only(self):
        registry, name, tag = _parse_image("nginx@sha256:abc123")
        assert registry == "docker.io"
        assert name == "library/nginx"
        assert tag == "sha256:abc123"

    def test_image_with_tag_and_digest(self):
        registry, name, tag = _parse_image(
            "registry.k8s.io/ingress-nginx/controller:v1.15.1@sha256:594ceea76b01c592858f803f9ff4d2cb"
        )
        assert registry == "registry.k8s.io"
        assert name == "ingress-nginx/controller"
        assert tag == "v1.15.1"

    def test_image_with_tag_and_digest_registry_port(self):
        registry, name, tag = _parse_image(
            "myregistry.corp.com:5000/app:v2.0@sha256:abc123"
        )
        assert registry == "myregistry.corp.com:5000"
        assert name == "app"
        assert tag == "v2.0"

    def test_full_registry_path(self):
        registry, name, tag = _parse_image("quay.io/myorg/myapp:1.0.0-rc1")
        assert registry == "quay.io"
        assert name == "myorg/myapp"
        assert tag == "1.0.0-rc1"

    def test_registry_port_no_tag(self):
        registry, name, tag = _parse_image("localhost:5000/myimage")
        assert registry == "localhost:5000"
        assert name == "myimage"
        assert tag == "latest"


class TestIsPrereleaseTag:
    # --- Stable: numeric version with no suffix ---

    def test_full_semver_stable(self):
        assert is_prerelease_tag("1.25.3") is False

    def test_v_prefix_stable(self):
        assert is_prerelease_tag("v1.25.3") is False

    def test_major_minor_only(self):
        assert is_prerelease_tag("1.25") is False

    def test_major_only(self):
        assert is_prerelease_tag("1") is False

    def test_v_prefix_major_minor(self):
        assert is_prerelease_tag("v2") is False

    def test_date_tag_stable(self):
        assert is_prerelease_tag("20240101") is False

    def test_digest_pinned_stable(self):
        assert is_prerelease_tag("sha256:abc123def456") is False

    # --- Stable: numeric version with platform suffix only ---

    def test_alpine_suffix(self):
        assert is_prerelease_tag("1.2.3-alpine") is False

    def test_alpine_versioned_suffix(self):
        assert is_prerelease_tag("1.2.3-alpine3.18") is False

    def test_ubuntu_suffix(self):
        assert is_prerelease_tag("1.2.3-ubuntu22.04") is False

    def test_slim_suffix(self):
        assert is_prerelease_tag("1.2.3-slim") is False

    def test_debian_codename(self):
        assert is_prerelease_tag("1.2.3-bookworm") is False

    def test_slim_bookworm(self):
        assert is_prerelease_tag("1.2.3-slim-bookworm") is False

    def test_major_minor_with_platform(self):
        assert is_prerelease_tag("1.25-alpine") is False

    # --- Pre-release: numeric version with non-platform suffix ---

    def test_alpha_tag(self):
        assert is_prerelease_tag("1.0.0-alpha.1") is True

    def test_beta_tag(self):
        assert is_prerelease_tag("2.0-beta") is True

    def test_rc_tag(self):
        assert is_prerelease_tag("v3.0.0-rc.1") is True

    def test_custom_suffix(self):
        assert is_prerelease_tag("1.2.3-custom-build") is True

    def test_preview_suffix(self):
        assert is_prerelease_tag("1.0-preview") is True

    def test_prerelease_with_platform_suffix(self):
        # alpha.1 remains after stripping slim
        assert is_prerelease_tag("1.0.0-alpha.1-slim") is True

    def test_rc_with_alpine_suffix(self):
        # rc.1 remains after stripping alpine
        assert is_prerelease_tag("1.0.0-rc.1-alpine") is True

    def test_nightly_with_date_version(self):
        # Version is 20240101, suffix is nightly (not a platform)
        assert is_prerelease_tag("20240101-nightly") is True

    # --- Pre-release: non-numeric tags ---

    def test_latest(self):
        assert is_prerelease_tag("latest") is True

    def test_dev(self):
        assert is_prerelease_tag("dev") is True

    def test_canary(self):
        assert is_prerelease_tag("canary") is True

    def test_unstable(self):
        assert is_prerelease_tag("unstable") is True

    def test_snapshot(self):
        assert is_prerelease_tag("snapshot-20240101") is True

    def test_bookword_non_versioned(self):
        assert is_prerelease_tag("bookworm") is True

    def test_sha_tag(self):
        assert is_prerelease_tag("sha-abc123") is True

    def test_branch_name(self):
        assert is_prerelease_tag("main") is True

    # --- Platform prefix pattern (<platform>-v<version>) ---

    def test_platform_prefix_with_v(self):
        assert is_prerelease_tag("alpine-v18.3.1") is False

    def test_platform_prefix_without_v(self):
        assert is_prerelease_tag("alpine-18.3.1") is False

    def test_platform_prefix_ubuntu(self):
        assert is_prerelease_tag("ubuntu-22.04") is False

    def test_platform_prefix_slim(self):
        assert is_prerelease_tag("slim-v2.1.0") is False

    def test_platform_prefix_unknown_still_prerelease(self):
        # "custom" is not a known platform suffix → still prerelease
        assert is_prerelease_tag("custom-v1.0.0") is True

    # --- Component prefix ---

    def test_component_stable(self):
        assert is_prerelease_tag("server/v1.2.3") is False

    def test_component_with_platform(self):
        assert is_prerelease_tag("server/v1.2.3-alpine") is False

    def test_component_prerelease(self):
        assert is_prerelease_tag("client/v1.2.3-alpha") is True

    def test_component_prerelease_with_platform(self):
        assert is_prerelease_tag("agent/v2.0.0-rc.1-slim") is True

    # --- Custom stable suffixes ---

    def test_custom_suffix_is_prerelease_by_default(self):
        # "thick" is not in the default list → prerelease
        assert is_prerelease_tag("v4.2.1-thick") is True

    def test_custom_suffix_stable_when_configured(self):
        assert is_prerelease_tag("v4.2.1-thick", stable_suffixes=["alpine", "slim", "thick"]) is False

    def test_custom_suffix_per_namespace_via_check_prerelease(self):
        pods = [
            {
                "metadata": {"name": "pod", "namespace": "kube-system", "creationTimestamp": "2024-01-01T00:00:00Z"},
                "status": {"startTime": "2024-01-01T00:00:00Z"},
                "spec": {"containers": [{"name": "main", "image": "multus:v4.2.1-thick"}]},
            },
        ]
        # Without custom suffix → prerelease
        assert len(check_prerelease(pods)) == 1
        # With custom suffix → stable
        assert len(check_prerelease(pods, stable_suffixes=["alpine", "slim", "thick"])) == 0


class TestCheckPrerelease:
    def test_finds_prerelease_images(self):
        pods = [
            {
                "metadata": {"name": "pod-a", "namespace": "default", "creationTimestamp": "2024-01-01T00:00:00Z"},
                "status": {"startTime": "2024-01-01T00:00:00Z"},
                "spec": {"containers": [{"name": "main", "image": "myapp:1.0.0-alpha"}]},
            },
        ]
        findings = check_prerelease(pods)
        assert len(findings) == 1
        assert findings[0].is_prerelease is True
        assert findings[0].tag == "1.0.0-alpha"
        assert findings[0].registry == "docker.io"
        assert findings[0].image_name == "library/myapp"

    def test_ignores_stable_images(self):
        pods = [
            {
                "metadata": {"name": "pod-b", "namespace": "default", "creationTimestamp": "2024-01-01T00:00:00Z"},
                "status": {"startTime": "2024-01-01T00:00:00Z"},
                "spec": {"containers": [{"name": "main", "image": "nginx:1.25"}]},
            },
        ]
        findings = check_prerelease(pods)
        assert len(findings) == 0

    def test_checks_init_containers(self):
        pods = [
            {
                "metadata": {"name": "pod-c", "namespace": "default", "creationTimestamp": "2024-01-01T00:00:00Z"},
                "status": {"startTime": "2024-01-01T00:00:00Z"},
                "spec": {
                    "containers": [{"name": "main", "image": "nginx:1.25"}],
                    "initContainers": [{"name": "init", "image": "busybox:latest"}],
                },
            },
        ]
        findings = check_prerelease(pods)
        assert len(findings) == 1
        assert findings[0].container == "init"

    def test_suffix_based_detection(self):
        # 1.0-preview has a non-platform suffix → prerelease
        pods = [
            {
                "metadata": {"name": "pod-d", "namespace": "default", "creationTimestamp": "2024-01-01T00:00:00Z"},
                "status": {"startTime": "2024-01-01T00:00:00Z"},
                "spec": {"containers": [{"name": "main", "image": "myapp:1.0-preview"}]},
            },
        ]
        findings = check_prerelease(pods)
        assert len(findings) == 1


class TestFilterViolations:
    def test_filters_by_max_age(self):
        from kubeic_operator.checks.prerelease import PrereleaseFinding
        findings = [
            PrereleaseFinding("img:alpha", "docker.io", "library/img", "alpha", "ns", "pod", "c", True, 10),
            PrereleaseFinding("img2:beta", "docker.io", "library/img2", "beta", "ns", "pod2", "c", True, 3),
        ]
        violations = filter_violations(findings, max_age_days=7)
        assert len(violations) == 1
        assert violations[0].age_days == 10
