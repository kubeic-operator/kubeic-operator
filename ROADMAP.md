# kubeic-operator Roadmap

## Reassess: IAP Status Subresource

The `status` subresource was removed from the `ImageAuditPolicy` CRD because it was declared but never written to. Re-adding it is worth reconsidering — writing `lastAuditTime`, `namespacesChecked`, and audit summaries to `.status` would give users visibility into the operator's activity directly via `kubectl get iap`.

The original concern was around RBAC: the operator already uses `_NoWriteProgressStorage` and disables finalizers to avoid needing namespace-level patch permissions. However, patching an IAP object's status subresource is a different operation — the operator controls the IAP CRD and already has cluster-scope permissions to manage it. The per-namespace checker pods don't interact with IAP objects at all (they check images and expose metrics), so there's no delegation issue.

This is unlike the secrets RBAC situation where the operator's ClusterRole grants `get secrets` cluster-wide. With secrets, the operator isn't reading them directly — it creates per-namespace Roles that grant checker SAs access. The operator holding that permission is for future use, not current need. With IAP status, the operator itself would be the writer, which is a clean fit.

If re-added:
- Add `subresources: status: {}` back to the CRD schema
- Add `status` field with typed properties (no `x-kubernetes-preserve-unknown-fields`)
- Add `update`/`patch` on `imageauditpolicies/status` to operator RBAC
- Write status updates in the policy/namespace handlers after each audit cycle

---

## Next: Test Coverage

### Integration/E2E tests
No tests verify actual API interactions, CRD application, or checker pod deployment.

**Next steps:**
- Add `tests/integration/` with tests using `kind` in CI
- Cover: CRD install, policy creation, checker pod deployment, RBAC verification
- Add a CI workflow step to run integration tests before build/publish

### Unit tests for core modules
`deployer.py`, `credentials.py`, and `availability.py` now have dedicated test files but coverage could be expanded:
- `test_deployer.py` — env var validation warnings, annotation merge/cleanup logic
- `test_credentials.py` — edge cases in `_decode_docker_secret`, warning log verification
- `test_availability.py` — `write_auth_config` file permissions (verify `0o600`)

---

## Next: CI Quality Gates

The release workflow (`.github/workflows/release.yml`) builds and publishes without any automated quality checks.

**Recommended additions:**
- `ruff check` — linting
- `pytest tests/unit/` — unit tests
- `bandit -r kubeic_operator/ kubeic_checker/` — security-focused static analysis
- Optionally `mypy` or `pyright` for type checking
- Add `[tool.ruff]` config to `pyproject.toml`

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

- Audit daemon thread graceful shutdown via `threading.Event` (`kubeic_operator/main.py`)
- Implement Kubernetes event emission or remove `events` RBAC permissions (`helm/kubeic-operator/templates/operator-rbac.yaml`)
- Add `py.typed` markers to `kubeic_operator/` and `kubeic_checker/` packages
