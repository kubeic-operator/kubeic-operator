# kubeic-operator
![kubeic operator logo](https://avatars.githubusercontent.com/u/278014922?s=400&u=d9efb33dc4a8928bb7b4fe46a78dc2aa66636da1&v=4)

![CI](https://github.com/kubeic-operator/kubeic-operator/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/kubeic-operator/kubeic-operator?color=brightgreen)
![Coverage](https://raw.githubusercontent.com/kubeic-operator/kubeic-operator/python-coverage-comment-action-data/badge.svg)

Kubernetes operator that audits running pod images for availability, pre-release age, and version spread. Surfaces findings as Prometheus metrics for Grafana dashboards and alerting.

## What it checks

| Check | Component | Description |
| --- | --- | --- |
| Image availability | Per-namespace checker | Verifies images are still reachable in their registry using `skopeo inspect` |
| Digest verification | Per-namespace checker | Confirms pinned SHA digests match the registry manifest |
| Credential validity | Per-namespace checker | Tests that imagePullSecrets can authenticate against their registry using `skopeo list-tags` |
| Pre-release age | Operator (cluster-wide) | Detects alpha/beta/rc/dev/latest/etc. images running beyond a configurable threshold |
| Version spread | Operator (cluster-wide) | Alerts when too many distinct versions of the same image base are running simultaneously |

## Architecture

The operator handles cluster-wide checks (pre-release age, version spread) that only need pod specs. Image availability and credential checks need registry access and pull secrets, so they run in a separate checker workload.

There are two layouts, chosen with `checker.mode`.

### `perNamespace` (default)

The operator deploys a checker pod into each audited namespace, each reading only its own namespace's pods and secrets.

```text
Operator (cluster-scoped)          Per-namespace Checkers
 - watches namespaces               - reads pods in own namespace
 - deploys/tears down checkers      - reads imagePullSecrets (own ns only)
 - pre-release age checks           - skopeo inspect for availability
 - version spread checks            - skopeo list-tags for credential validation
 - exposes /metrics                  - digest verification
                                    - exposes /metrics
```

### `central`

One checker for the whole cluster, owned by the chart rather than by the operator, and no per-namespace checkers at all.

```text
Operator (cluster-scoped)          Central Checker (one per cluster)
 - watches namespaces               - lists all pods, in pages
 - grants/revokes secret access     - reads imagePullSecrets per namespace,
   per namespace                       via the grants the operator creates
 - pre-release age checks           - applies namespace exclusions itself
 - version spread checks            - skopeo inspect / list-tags, paced
 - exposes /metrics                   across the interval
                                    - exposes /metrics
```

This trades detection latency for memory. Measured across a nine-cluster estate, 222 per-namespace checkers held 13.35 GiB resident, roughly 61 MiB of which is a Python interpreter floor paid once per pod rather than per unit of work. One checker per cluster is around 70 MiB.

Switching mode needs no migration step: every namespace stops wanting a checker of its own, and the operator's ordinary reconcile drains the existing ones one at a time, at the same pace as any other rollout.

**The central checker gets no cluster-wide `secrets` access.** That would hand every secret in the cluster to a pod that runs `skopeo` against arbitrary registries — strictly worse than the per-namespace checkers it replaces, each of which could only read its own namespace. Instead the operator creates a `kubeic-checker-central` Role and RoleBinding in each audited namespace, so the checker's reach is the union of the namespaces actually being audited. This is also what keeps `noSecretNamespaces` and `namespaceSecrets` working, since a ClusterRole's `resourceNames` are cluster-wide names and cannot express "these secret names, but only in this namespace".

Two things behave differently in central mode:

| | `perNamespace` | `central` |
|---|---|---|
| `availability.intervalMinutes`, `credentialSource.type` | per-namespace `ImageAuditPolicy` can override | Helm `policy` values only — there is no per-namespace checker to configure |
| `excludedNamespaces`, `namespaceSelector.excludeLabels` | enforced by not deploying a checker | applied by the checker itself, from Helm values. A namespace excluded *only* by its own namespace-scoped `ImageAuditPolicy` is still audited |
| `noSecretNamespaces`, `namespaceSecrets` | Role shape per namespace | unchanged — enforced by the per-namespace grants above |
| `ImageAuditPolicy` status `deployed` | this namespace's own checker | the single central Deployment, with reason `audited by central checker` |

## Installation

### CRD pre-install (required)

The `ImageAuditPolicy` CRD must exist in the cluster before installing or running `helm diff`. It is not managed by the chart by default.

```bash
kubectl apply -f config/crd/imageauditpolicy.yaml
```

To have the chart install the CRD automatically, set `crds.install: true`. This works for installs and upgrades but will cause `helm diff` to fail on a bare cluster (the CRD must already exist for Helm to validate the `cluster-defaults` policy instance).

### Helm

```bash
helm install kubeic-operator oci://ghcr.io/kubeic-operator/kubeic-operator \
  --namespace kubeic --create-namespace
```

### Configuration

The operator is configured via `ImageAuditPolicy` CRDs. A cluster-wide default is created from Helm values:

```yaml
# values.yaml overrides
policy:
  prerelease:
    maxAgeDays: 7
  versionSpread:
    threshold: 3
  availability:
    intervalMinutes: 30
  namespaceSelector:
    excludeLabels:
      audit: disabled
  credentialSource:
    type: pullSecret  # or workloadIdentity

# Operator settings
operator:
  image:
    repository: ghcr.io/kubeic-operator/kubeic-operator/operator
    tag: "0.0.1-alpha.8"
  podLabels: {}
  podAnnotations: {}

# Checker settings
checker:
  # perNamespace (one checker per audited namespace) or central (one per cluster)
  mode: perNamespace
  image:
    repository: ghcr.io/kubeic-operator/kubeic-operator/checker
    tag: "0.0.1-alpha.8"
  podLabels: {}
  podAnnotations: {}
  excludedNamespaces: [kube-public, kube-node-lease]
  noSecretNamespaces: [kube-system]
  namespaceSecrets: {}
  # namespaceSecrets:
  #   kube-system:
  #     - my-pull-secret
  # Only used when mode is central
  central:
    resources:
      requests: {cpu: 50m, memory: 256Mi}
      limits: {cpu: 500m, memory: 512Mi}
```

Per-namespace overrides:

```yaml
apiVersion: imageaudit.kubeic.io/v1alpha1
kind: ImageAuditPolicy
metadata:
  name: relaxed-policy
  namespace: dev
spec:
  prerelease:
    maxAgeDays: 30
  versionSpread:
    threshold: 10
```

### kube-prometheus-stack integration

To enable Prometheus rule and Grafana dashboard discovery:

```bash
helm install kubeic-operator oci://ghcr.io/kubeic-operator/kubeic-operator \
  --namespace kubeic --create-namespace \
  --set prometheusRule.labels.release=kube-prometheus-stack \
  --set grafanaDashboard.labels.release=kube-prometheus-stack
```

### Additional configuration

| Value | Description | Default |
| --- | --- | --- |
| `checker.enabled` | Deploy checker pods at all (see below) | `true` |
| `serviceMonitor.enabled` | Deploy a ServiceMonitor for checker pods | `true` |
| `serviceMonitor.interval` | Scrape interval | `30s` |
| `serviceMonitor.labels` | Labels for ServiceMonitor discovery | `{}` |
| `grafanaDashboard.enabled` | Deploy Grafana dashboard ConfigMap | `true` |
| `grafanaDashboard.labels` | Labels for Grafana sidecar discovery | `{}` |
| `prometheusRule.enabled` | Deploy Prometheus alert rules | `true` |
| `prometheusRule.labels` | Labels for PrometheusRule selection | `{}` |
| `networkPolicy.enabled` | Deploy network policy for checker pods | `true` |
| `checker.readyTimeoutSeconds` | Per-namespace wait during a serialised rollout | `90` |
| `checker.revisionHistoryLimit` | Old ReplicaSets kept per checker Deployment | `2` |
| `crds.install` | Install CRDs with the chart | `false` |

`checker.revisionHistoryLimit` defaults to 2 rather than the Kubernetes default of 10 because
this operator creates one Deployment per audited namespace, so retained ReplicaSets are its
largest class of API object and grow by one per namespace on every version bump. Lowering it
on an existing install is safe: the Deployment controller reaps the excess on its next sync,
and because `revisionHistoryLimit` lives on the Deployment spec rather than the pod template,
changing it does not restart any checker.

### Checker rollouts are serialised

Every checker pod template carries `app.kubernetes.io/version`, so bumping the checker image
changes all N templates and Kubernetes would roll every checker at once. On a cluster with many
audited namespaces that lands a large batch of pod sandbox creations on whichever nodes the
scheduler picks, which is enough to exhaust a CNI plugin's memory and strand unrelated
workloads.

The operator therefore deploys checkers **one namespace at a time**, waiting for each rollout to
complete before starting the next. The wait mirrors `kubectl rollout status` rather than just
checking `readyReplicas` — a single-replica Deployment surges to two pods, so the old pod is
still Ready immediately after the patch and a naive check would not serialise anything.

- A namespace that does not roll out within `checker.readyTimeoutSeconds` is logged and skipped,
  so one wedged namespace cannot stall the rest.
- Bootstrap runs on the operator's background thread, not the startup handler, so the operator
  keeps watching namespace events while a long rollout is in progress.
- Cluster-wide metrics (pre-release, version spread) are published before the rollout begins,
  since they need nothing from the checkers.
- Checker pods carry a `preferred` `podAntiAffinity` across `kubernetes.io/hostname` with an
  empty `namespaceSelector`, so the scheduler spreads them even when a burst comes from
  something the operator did not do, such as a node drain. `topologySpreadConstraints` cannot
  express this: a TSC `labelSelector` only counts pods in the same namespace, and each checker
  is `replicas: 1` alone in its own.

Teardown shares the same lock, so a mass removal — `checker.enabled: false` disables every
namespace at once — cannot interleave with a deployment still in flight. It deliberately does
not wait for pods to disappear: deletion is asynchronous and grace-period bound, so waiting
would stall reconciliation for roughly the grace period times the namespace count while
gaining little, as a burst of CNI teardowns has none of the retry amplification that makes a
burst of creations self-sustaining. Checker pods instead use a 5 second
`terminationGracePeriodSeconds` — the checker is a sleep loop with no SIGTERM handler and
nothing to flush, so the 30 second default was time spent holding a node slot for no reason.
### Disabling checkers

`checker.enabled: false` stops the operator deploying checker pods and tears down any that
already exist on the next reconcile. The operator keeps running the cluster-wide checks that
need only pod specs.

This is a bigger reduction in coverage than it looks, because pre-release *age* is a joint
metric: the operator knows a tag is pre-release, but only the checker knows when the image was
published. Five of the seven alerts are therefore omitted when checkers are disabled; the two
sourced from operator metrics remain:

| Alert | Survives `checker.enabled: false` |
| --- | --- |
| ImageVersionSpreadTooHigh | yes — operator metrics only |
| ImageAuditReconcileFailing | yes — reconcile still runs, and teardowns can still fail |
| ImageMissingFromRegistry | no |
| ImageAuditCheckerDegraded | no |
| ImageDigestMismatch | no |
| PrereleaseImageRunningTooLong | no — needs `kube_image_created_timestamp_seconds` |
| RegistryCredentialInvalid | no |

The rules are omitted from the `PrometheusRule` rather than left in place to fire never, so
there is no rule that silently cannot alert.

## Prometheus metrics

### Operator metrics (cluster-wide, port 9090)

| Metric | Type | Labels |
| --- | --- | --- |
| `kube_image_is_prerelease` | Gauge | image, registry, image_name, tag, namespace, pod, container |
| `kube_image_prerelease_age_days` | Gauge | image, registry, image_name, tag, namespace, pod, container |
| `kube_image_prerelease_violation` | Gauge | registry, image_name, namespace, pod, container |
| `kube_image_version_count` | Gauge | registry, image_name |
| `kube_image_version_pod_count` | Gauge | registry, image_name, tag, namespace |
| `kube_image_version_spread_violation` | Gauge | registry, image_name |
| `kube_image_checker_reconcile_failures_total` | Counter | namespace, operation, error_class |

### Checker metrics (per-namespace, port 9090)

| Metric | Type | Labels |
| --- | --- | --- |
| `kube_image_available` | Gauge | image, registry, image_name, namespace, pod, container, error_class |
| `kube_image_digest_match` | Gauge | image, registry, image_name, namespace, pod, container |
| `kube_image_created_timestamp_seconds` | Gauge | image, registry, image_name, namespace, pod, container |
| `kube_image_credential_valid` | Gauge | registry, namespace, secret_name |

### Reconciliation failures

`kube_image_checker_reconcile_failures_total` counts checker probe, deploy and teardown attempts
that raised. It is a Counter rather than a Gauge specifically because the gauges above are
cleared and repopulated every cycle, which would erase an intermittent failure before anyone
saw it.

`error_class` separates the two cases that want different responses. `api` is an API server
condition — a 403 from an admission webhook, a 409, a 503 — which may clear on its own and is
retried next pass. `internal` is a defect in the operator and will not clear by itself. Neither
is fatal to the other namespaces in the pass: a namespace that cannot be probed or deployed is
recorded and skipped, and the rest are still reconciled.

The per-namespace outcome is also written to the `cluster-defaults` ImageAuditPolicy status,
which records what actually happened rather than what was intended:

```bash
kubectl get iap cluster-defaults -n kubeic -o jsonpath='{.status.namespaces}'
```

A namespace whose deploy failed reports `deployed: false` with the reason; one whose teardown
failed reports `deployed: true`, because the checker is still running. The operator log carries
the full traceback for either.

## Alert rules

The Helm chart deploys a `PrometheusRule` with seven alerts:

| Alert | Severity | For | Condition |
| --- | --- | --- | --- |
| ImageMissingFromRegistry | warning | interval + 10m | `kube_image_available{error_class="not_found"} == 0` |
| ImageAuditCheckerDegraded | warning | interval + 10m | `kube_image_available{error_class=~"auth_failure\|network"} == 0`, counted per namespace/registry |
| ImageDigestMismatch | warning | 30m | `kube_image_digest_match == 0` |
| PrereleaseImageRunningTooLong | warning | 1h | `kube_image_created_timestamp_seconds` age `> maxAgeDays`, joined to `kube_image_is_prerelease == 1` |
| ImageVersionSpreadTooHigh | warning | 30m | `kube_image_version_spread_violation == 1` |
| ImageAuditReconcileFailing | warning | 15m | `increase(kube_image_checker_reconcile_failures_total[30m]) > 0` |
| RegistryCredentialInvalid | critical | interval + 10m | `kube_image_credential_valid == 0` |

Alerts driven by the availability sweep use `for: policy.availability.intervalMinutes + 10m`.
The metrics are step functions that only move once per sweep, so a shorter `for` would fire
on a single sample. `ImageMissingFromRegistry` is a warning rather than a page: the pod keeps
running on an already-pulled image, and only fails once it needs to be rescheduled. A pod that
genuinely cannot pull surfaces as `ImagePullBackOff`, which this chart does not alert on.

## Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Run unit tests
pytest tests/unit/ -v

# Run integration tests (needs a cluster with the chart installed)
pytest tests/integration/ -v -m "not central"

# Central-mode integration tests drain every per-namespace checker, so they
# need the release upgraded first and cannot share a cluster with the above
helm upgrade kubeic-operator ./helm/kubeic-operator --reuse-values \
  --set checker.mode=central --wait
pytest tests/integration/test_central_mode.py -v -m central

# Render Helm chart locally, in either mode
helm template test helm/kubeic-operator
helm template test helm/kubeic-operator --set checker.mode=central
```

## Licence

[MIT](LICENSE)


