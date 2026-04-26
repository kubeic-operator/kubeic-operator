# kubeic-operator
![kubeic operator logo](https://avatars.githubusercontent.com/u/278014922?s=400&u=d9efb33dc4a8928bb7b4fe46a78dc2aa66636da1&v=4)

![CI](https://github.com/kubeic-operator/kubeic-operator/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/kubeic-operator/kubeic-operator)
![Coverage](https://raw.githubusercontent.com/kubeic-operator/kubeic-operator/python-coverage-comment-action/badge.svg)

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
| `serviceMonitor.enabled` | Deploy a ServiceMonitor for checker pods | `true` |
| `serviceMonitor.interval` | Scrape interval | `30s` |
| `serviceMonitor.labels` | Labels for ServiceMonitor discovery | `{}` |
| `grafanaDashboard.enabled` | Deploy Grafana dashboard ConfigMap | `true` |
| `grafanaDashboard.labels` | Labels for Grafana sidecar discovery | `{}` |
| `prometheusRule.enabled` | Deploy Prometheus alert rules | `true` |
| `prometheusRule.labels` | Labels for PrometheusRule selection | `{}` |
| `networkPolicy.enabled` | Deploy network policy for checker pods | `true` |
| `crds.install` | Install CRDs with the chart | `false` |

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
| `kube_image_available` | Gauge | image, registry, image_name, namespace, pod, container |
| `kube_image_digest_match` | Gauge | image, registry, image_name, namespace, pod, container |
| `kube_image_credential_valid` | Gauge | registry, namespace, secret_name |

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

