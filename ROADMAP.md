# kubeic-operator Roadmap

## Next: CI Quality Gates (partial)

The CI workflow (`.github/workflows/ci.yml`) runs on PRs to `main` and `alpha` and already includes:
- `ruff check` — linting
- `pytest tests/unit/` — unit tests
- `pytest tests/integration/` — kind-based integration tests

**Still missing:**
- `bandit -r kubeic_operator/ kubeic_checker/` — security-focused static analysis
- Optionally `mypy` or `pyright` for type checking
- pytest-cov coverage reporting (`pytest-cov` is a dev dependency but not wired into CI)
- Quality gates on the **release** workflow (`.github/workflows/release.yml`) — currently builds and publishes with no checks. A merge to `main` that bypasses PR review publishes untested code.

---

## Next: Expand Test Coverage

### Modules without direct unit tests
- `kubeic_operator/metrics.py` — only tested indirectly via integration `test_metrics.py`
- `kubeic_operator/cleanup.py` — **zero tests** (not unit, not integration)
- `kubeic_operator/handlers/policy.py` — **zero direct tests** (only tested implicitly via integration)
- `kubeic_checker/main.py` — **zero unit tests** (integration tests verify the deployed checker works, but the main loop logic and `_check_credential_validity` are not isolated)

### Expansion areas for existing tests
- `test_deployer.py` — env var validation warnings, annotation merge/cleanup logic
- `test_credentials.py` — edge cases in `_decode_docker_secret`, warning log verification
- `test_availability.py` — `write_auth_config` file permissions (verify `0o600`)

---

## Future: Kubernetes v1.33+ Forward-Compatibility

### Extend `credentialSource` CRD enum
Current enum: `pullSecret`, `workloadIdentity`. K8s v1.33+ introduces new credential paths.

**Add to enum:**
- `serviceAccountToken` — for KEP-4412 secret-less pulls (kubelet uses SA tokens)
- `imageVolume` — for KEP-4639 OCI Image Volume Source
- `auto` — detect cluster capability and pick the best method

### Detect ImageVolume usage in pods
K8s v1.33 allows pods to mount images as volumes (`spec.volumes[].image`) without `imagePullSecrets`. The checker needs to inspect this in addition to `imagePullSecrets`.

**Files:** `kubeic_checker/credentials.py`, `kubeic_checker/main.py`

### Handle mixed-credential pods
A single pod may use `imagePullSecrets` AND `image` volumes AND rely on SA token auth. The credential resolver must handle all three simultaneously and return structured results with the source type per-image.

### Kubernetes version detection
When `credentialSource.type` is `auto`, the operator needs to know the cluster's k8s version.

**Implementation:**
- Query the k8s API version endpoint at startup
- Cache major.minor version
- Map versions to features: <1.33 → `pullSecret` only, 1.33+ → add `serviceAccountToken`, 1.33+ with feature gate → add `imageVolume`

### Update per-namespace Role for new auth modes
When using `serviceAccountToken` auth, the checker may not need `secrets` `get` at all. `_build_role` should conditionally include/exclude the secrets rule based on credential source type.

**File:** `kubeic_operator/deployer.py`

---

## Future: Polish

- Audit daemon thread graceful shutdown via `threading.Event` (`kubeic_operator/main.py`) — currently uses `while True: time.sleep()` on a daemon thread with no interruption between cycles
- Implement Kubernetes event emission or remove unused `events` RBAC permissions (`helm/kubeic-operator/templates/operator-rbac.yaml`) — permissions exist but no code creates events
- Add `py.typed` markers to `kubeic_operator/` and `kubeic_checker/` packages

---

## Completed

- ~~IAP Status Subresource~~ — CRD has typed status schema (`lastReconcileTime`, `namespaces`), RBAC grants `update/patch` on status, operator writes via `patch_namespaced_custom_object_status()` from audit loop, startup, and policy handlers
- ~~Integration/E2E tests~~ — 5 integration test files using `kind` in CI covering CRD, operator deployment, metrics, checker deployment/RBAC, and reconciliation
- ~~Unit tests for core modules~~ — `deployer`, `credentials`, `availability`, `prerelease`, `spread`, `namespace_handler` all have dedicated test files
- ~~CI linting and testing~~ — `ruff check` + `pytest unit/integration` running on PRs via `.github/workflows/ci.yml`
