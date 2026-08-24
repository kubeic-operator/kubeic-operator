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

The operator deploys a checker pod into each audited namespace. Checkers handle image availability and credential checks locally (using namespace-scoped secrets access). The operator handles cluster-wide checks (pre-release age, version spread) that only need pod specs.

```text
Operator (cluster-scoped)          Per-namespace Checkers
 - watches namespaces               - reads pods in own namespace
 - deploys/tears down checkers      - reads imagePullSecrets (own ns only)
 - pre-release age checks           - skopeo inspect for availability
 - version spread checks            - skopeo list-tags for credential validation
 - exposes /metrics                  - digest verification
                                    - exposes /metrics
```

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

# Checker settings (per-namespace deployments)
checker:
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
| `checker.revisionHistoryLimit` | Old ReplicaSets kept per checker Deployment | `2` |
| `crds.install` | Install CRDs with the chart | `false` |

`checker.revisionHistoryLimit` defaults to 2 rather than the Kubernetes default of 10 because
this operator creates one Deployment per audited namespace, so retained ReplicaSets are its
largest class of API object and grow by one per namespace on every version bump. Lowering it
on an existing install is safe: the Deployment controller reaps the excess on its next sync,
and because `revisionHistoryLimit` lives on the Deployment spec rather than the pod template,
changing it does not restart any checker.

### Disabling checkers

`checker.enabled: false` stops the operator deploying checker pods and tears down any that
already exist on the next reconcile. The operator keeps running the cluster-wide checks that
need only pod specs.

This is a bigger reduction in coverage than it looks, because pre-release *age* is a joint
metric: the operator knows a tag is pre-release, but only the checker knows when the image was
published. Five of the six alerts are therefore omitted when checkers are disabled, and only
`ImageVersionSpreadTooHigh` remains:

| Alert | Survives `checker.enabled: false` |
| --- | --- |
| ImageVersionSpreadTooHigh | yes — operator metrics only |
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

### Checker metrics (per-namespace, port 9090)

| Metric | Type | Labels |
| --- | --- | --- |
| `kube_image_available` | Gauge | image, registry, image_name, namespace, pod, container, error_class |
| `kube_image_digest_match` | Gauge | image, registry, image_name, namespace, pod, container |
| `kube_image_created_timestamp_seconds` | Gauge | image, registry, image_name, namespace, pod, container |
| `kube_image_credential_valid` | Gauge | registry, namespace, secret_name |

## Alert rules

The Helm chart deploys a `PrometheusRule` with six alerts:

| Alert | Severity | For | Condition |
| --- | --- | --- | --- |
| ImageMissingFromRegistry | warning | interval + 10m | `kube_image_available{error_class="not_found"} == 0` |
| ImageAuditCheckerDegraded | warning | interval + 10m | `kube_image_available{error_class=~"auth_failure\|network"} == 0`, counted per namespace/registry |
| ImageDigestMismatch | warning | 30m | `kube_image_digest_match == 0` |
| PrereleaseImageRunningTooLong | warning | 1h | `kube_image_created_timestamp_seconds` age `> maxAgeDays`, joined to `kube_image_is_prerelease == 1` |
| ImageVersionSpreadTooHigh | warning | 30m | `kube_image_version_spread_violation == 1` |
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

# Run tests
pytest tests/ -v

# Render Helm chart locally
helm template test helm/kubeic-operator
```

## Licence

[MIT](LICENSE)


