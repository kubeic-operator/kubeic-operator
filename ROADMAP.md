# kubeic-operator Roadmap

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
