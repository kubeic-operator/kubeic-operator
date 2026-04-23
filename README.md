# kubeic-operator
![kubeic operator logo](https://github.com/organizations/kubeic-operator/settings/profile)
Kubernetes operator that audits running pod images for availability, pre-release age, and version spread. Surfaces findings as Prometheus metrics for Grafana dashboards and alerting.

## What it checks

| Check | Component | Description |
| --- | --- | --- |
| Image availability | Per-namespace checker | Verifies images are still reachable in their registry using `skopeo inspect` |
| Credential validity | Per-namespace checker | Tests that imagePullSecrets can authenticate against their registry |
| Pre-release age | Operator (cluster-wide) | Detects alpha/beta/rc/dev/latest/etc. images running beyond a configurable threshold |
| Version spread | Operator (cluster-wide) | Alerts when too many distinct versions of the same image base are running simultaneously |

## Architecture

The operator deploys a checker pod into each audited namespace. Checkers handle image availability and credential checks locally (using namespace-scoped secrets access). The operator handles cluster-wide checks (pre-release age, version spread) that only need pod specs.

```text
Operator (cluster-scoped)          Per-namespace Checkers
 - watches namespaces               - reads pods in own namespace
 - deploys/tears down checkers      - reads imagePullSecrets (own ns only)
 - pre-release age checks           - skopeo inspect for availability
 - version spread checks            - credential validation
 - exposes /metrics                  - exposes /metrics
```

## Installation

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
    patterns: [alpha, beta, rc, dev, nightly, snapshot, canary, unstable, latest]
  versionSpread:
    threshold: 3
  availability:
    intervalMinutes: 30
  namespaceSelector:
    excludeLabels:
      audit: disabled
  credentialSource:
    type: pullSecret  # or workloadIdentity
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

## Prometheus metrics

### Operator metrics (cluster-wide, port 9090)

| Metric | Type | Labels |
| --- | --- | --- |
| `kube_image_is_prerelease` | Gauge | image, image_base, tag, namespace, pod, container |
| `kube_image_prerelease_age_days` | Gauge | image, image_base, tag, namespace, pod, container |
| `kube_image_version_count` | Gauge | image_base |
| `kube_image_version_pod_count` | Gauge | image_base, tag, namespace |
| `kube_image_version_spread_violation` | Gauge | image_base |
| `kube_image_total_prerelease_violations` | Gauge | - |
| `kube_image_total_spread_violations` | Gauge | - |

### Checker metrics (per-namespace, port 9090)

| Metric | Type | Labels |
| --- | --- | --- |
| `kube_image_available` | Gauge | image, image_base, namespace, pod, container |
| `kube_image_credential_valid` | Gauge | registry, namespace, secret_name |
| `kube_image_total_unavailable` | Gauge | namespace |

## Alert rules

The Helm chart deploys a `PrometheusRule` with four alerts:

| Alert | Severity | For | Condition |
| --- | --- | --- | --- |
| ImageUnavailableInRegistry | critical | 10m | `kube_image_available == 0` |
| PrereleaseImageRunningTooLong | warning | 1h | `kube_image_prerelease_age_days > maxAgeDays` |
| ImageVersionSpreadTooHigh | warning | 30m | `kube_image_version_spread_violation == 1` |
| RegistryCredentialInvalid | critical | 10m | `kube_image_credential_valid == 0` |

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

