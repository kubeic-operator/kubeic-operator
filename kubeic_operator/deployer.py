import json
import logging
import os
import threading
import time

from kubernetes import client
from kubernetes.client import ApiException

logger = logging.getLogger("kubeic-operator.deployer")

CHECKER_SERVICE_ACCOUNT = "kubeic-checker"
CHECKER_ROLE = "kubeic-checker"
CHECKER_ROLE_BINDING = "kubeic-checker"
CHECKER_DEPLOYMENT = "kubeic-checker"
CHECKER_SERVICE = os.environ.get("CHECKER_SERVICE", "kubeic-checker-metrics")
OPERATOR_NAME = "kubeic-operator"

CHECKER_IMAGE = os.environ.get("CHECKER_IMAGE", "kubeic-checker:latest")
RELEASE_NAME = os.environ.get("RELEASE_NAME", "kubeic-operator")
CHECKER_VERSION = os.environ.get("CHECKER_VERSION", "latest")
CHECKER_CPU_REQUEST = os.environ.get("CHECKER_CPU_REQUEST", "50m")
CHECKER_MEMORY_REQUEST = os.environ.get("CHECKER_MEMORY_REQUEST", "64Mi")
CHECKER_CPU_LIMIT = os.environ.get("CHECKER_CPU_LIMIT", "200m")
CHECKER_MEMORY_LIMIT = os.environ.get("CHECKER_MEMORY_LIMIT", "128Mi")
SKIP_ANNOTATION = os.environ.get("SKIP_ANNOTATION", "")


def _parse_json_env(key: str, default: str = "{}") -> dict:
    raw = os.environ.get(key, default)
    if raw == default:
        return json.loads(default)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse env %s as JSON, falling back to empty dict", key)
        return {}


CHECKER_POD_LABELS = _parse_json_env("CHECKER_POD_LABELS")
CHECKER_POD_ANNOTATIONS = _parse_json_env("CHECKER_POD_ANNOTATIONS")


def _parse_bool_env(key: str, default: bool = True) -> bool:
    """Parse a boolean env var, treating unset *and* empty as the default.

    Helm renders a missing value as an empty string rather than omitting the
    env var, so "" must fall back to the default instead of reading as false —
    otherwise a chart typo silently disables checkers fleet-wide.
    """
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


CHECKER_ENABLED = _parse_bool_env("CHECKER_ENABLED")


def _parse_int_env(key: str, default: int, minimum: int = 0) -> int:
    """Parse a non-negative integer env var, falling back to the default.

    Unset, empty, non-numeric and out-of-range values all fall back rather than
    raising: a bad value here would crash the operator at import time, which is
    a far worse outcome than one setting reverting to its default.
    """
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Failed to parse env %s as int, falling back to %d", key, default)
        return default
    if value < minimum:
        logger.warning("Env %s (%d) is below minimum %d, falling back to %d", key, value, minimum, default)
        return default
    return value


# Deployments default to keeping 10 old ReplicaSets. With one checker Deployment
# per namespace that is the dominant source of API objects this operator creates
# — 54 checkers on one production cluster had accumulated 521 ReplicaSets — and
# every version bump adds another per namespace. Two is enough to roll back one
# bad release.
CHECKER_REVISION_HISTORY_LIMIT = _parse_int_env("CHECKER_REVISION_HISTORY_LIMIT", 2)

# Rollout pacing. minimum=1 because a zero timeout would skip the readiness wait
# entirely and a zero poll interval would busy-loop against the API server.
CHECKER_READY_TIMEOUT = _parse_int_env("CHECKER_READY_TIMEOUT_SECONDS", 90, minimum=1)
CHECKER_READY_POLL_SECONDS = _parse_int_env("CHECKER_READY_POLL_SECONDS", 2, minimum=1)


def _parse_excluded_namespaces() -> set[str]:
    raw = os.environ.get("EXCLUDED_NAMESPACES", "")
    if not raw:
        return set()
    return {ns.strip() for ns in raw.split(",") if ns.strip()}


def _parse_no_secret_namespaces() -> set[str]:
    raw = os.environ.get("NO_SECRET_NAMESPACES", "")
    if not raw:
        return set()
    return {ns.strip() for ns in raw.split(",") if ns.strip()}


def _parse_namespace_secrets() -> dict[str, list[str]]:
    raw = os.environ.get("NAMESPACE_SECRETS", "{}")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse NAMESPACE_SECRETS as JSON, falling back to empty dict")
        return {}

    if not isinstance(parsed, dict):
        logger.warning("NAMESPACE_SECRETS must be a JSON object, got %s", type(parsed).__name__)
        return {}

    validated: dict[str, list[str]] = {}
    for ns, names in parsed.items():
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            validated[ns] = names
        else:
            logger.warning("NAMESPACE_SECRETS[%s] must be a list of strings, skipping", ns)
    return validated


EXCLUDED_NAMESPACES = _parse_excluded_namespaces()
NO_SECRET_NAMESPACES = _parse_no_secret_namespaces()
NAMESPACE_SECRETS = _parse_namespace_secrets()


def _selector_labels() -> dict[str, str]:
    """Stable labels for Deployment.spec.selector and Service.spec.selector.

    Must not change after first creation — Kubernetes rejects selector mutations.
    """
    return {
        "app.kubernetes.io/name": "kubeic-operator",
        "app.kubernetes.io/component": "checker",
        "app.kubernetes.io/instance": RELEASE_NAME,
    }


def _common_labels() -> dict[str, str]:
    """Full label set for resource metadata, extending selector labels with mutable fields."""
    return {
        **_selector_labels(),
        "app.kubernetes.io/version": CHECKER_VERSION,
        "app.kubernetes.io/managed-by": OPERATOR_NAME,
        **CHECKER_POD_LABELS,
    }


def _build_service_account(namespace: str) -> client.V1ServiceAccount:
    return client.V1ServiceAccount(
        api_version="v1",
        kind="ServiceAccount",
        metadata=client.V1ObjectMeta(
            name=CHECKER_SERVICE_ACCOUNT,
            namespace=namespace,
            labels=_common_labels(),
        ),
    )


def _build_role(namespace: str, secret_names: list[str] | None = None) -> client.V1Role:
    rules = [
        client.V1PolicyRule(
            api_groups=[""],
            resources=["pods"],
            verbs=["get", "list"],
        ),
    ]

    if secret_names is None:
        rules.append(client.V1PolicyRule(
            api_groups=[""],
            resources=["secrets"],
            verbs=["get"],
        ))
    elif secret_names:
        rules.append(client.V1PolicyRule(
            api_groups=[""],
            resources=["secrets"],
            verbs=["get"],
            resource_names=secret_names,
        ))

    return client.V1Role(
        api_version="rbac.authorization.k8s.io/v1",
        kind="Role",
        metadata=client.V1ObjectMeta(
            name=CHECKER_ROLE,
            namespace=namespace,
            labels=_common_labels(),
        ),
        rules=rules,
    )


def _build_role_binding(namespace: str) -> client.V1RoleBinding:
    return client.V1RoleBinding(
        api_version="rbac.authorization.k8s.io/v1",
        kind="RoleBinding",
        metadata=client.V1ObjectMeta(
            name=CHECKER_ROLE_BINDING,
            namespace=namespace,
            labels=_common_labels(),
        ),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name=CHECKER_ROLE,
        ),
        subjects=[
            client.RbacV1Subject(
                kind="ServiceAccount",
                name=CHECKER_SERVICE_ACCOUNT,
                namespace=namespace,
            ),
        ],
    )


def _build_service(namespace: str) -> client.V1Service:
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=CHECKER_SERVICE,
            namespace=namespace,
            labels=_common_labels(),
        ),
        spec=client.V1ServiceSpec(
            selector=_selector_labels(),
            ports=[
                client.V1ServicePort(
                    name="metrics",
                    port=9090,
                    target_port=9090,
                    protocol="TCP",
                ),
            ],
        ),
    )


def _build_deployment(
    namespace: str,
    checker_image: str = CHECKER_IMAGE,
    check_interval_minutes: int = 30,
    credential_source: str = "pullSecret",
) -> client.V1Deployment:
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=CHECKER_DEPLOYMENT,
            namespace=namespace,
            labels=_common_labels(),
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            revision_history_limit=CHECKER_REVISION_HISTORY_LIMIT,
            selector=client.V1LabelSelector(
                match_labels=_selector_labels(),
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels=_common_labels(),
                    annotations=dict(CHECKER_POD_ANNOTATIONS),
                ),
                spec=client.V1PodSpec(
                    service_account_name=CHECKER_SERVICE_ACCOUNT,
                    # The checker is a sleep loop that installs no SIGTERM
                    # handler, so it never exits early and always burns the full
                    # grace period before SIGKILL. It holds nothing that needs
                    # flushing — metrics are in memory and authfiles live in an
                    # emptyDir that dies with the pod — so the default 30s is
                    # dead time during which N terminating pods hold their node
                    # slots and CNI state after a mass teardown.
                    termination_grace_period_seconds=5,
                    # Spread checkers across nodes. topologySpreadConstraints
                    # cannot do this: a TSC labelSelector only counts pods in
                    # the *same* namespace, and each checker is replicas:1 alone
                    # in its own namespace, so it would only ever match itself.
                    # podAntiAffinity with an empty namespaceSelector (= all
                    # namespaces) is the cross-namespace equivalent. Preferred
                    # rather than required so it degrades gracefully once
                    # checkers outnumber nodes, which they always will.
                    affinity=client.V1Affinity(
                        pod_anti_affinity=client.V1PodAntiAffinity(
                            preferred_during_scheduling_ignored_during_execution=[
                                client.V1WeightedPodAffinityTerm(
                                    weight=100,
                                    pod_affinity_term=client.V1PodAffinityTerm(
                                        topology_key="kubernetes.io/hostname",
                                        label_selector=client.V1LabelSelector(
                                            match_labels=_selector_labels(),
                                        ),
                                        namespace_selector=client.V1LabelSelector(),
                                    ),
                                ),
                            ],
                        ),
                    ),
                    security_context=client.V1PodSecurityContext(
                        run_as_non_root=True,
                        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        client.V1Container(
                            name="checker",
                            image=checker_image,
                            env=[
                                client.V1EnvVar(name="NAMESPACE", value=namespace),
                                client.V1EnvVar(name="CHECK_INTERVAL_MINUTES", value=str(check_interval_minutes)),
                                client.V1EnvVar(name="CREDENTIAL_SOURCE", value=credential_source),
                                client.V1EnvVar(name="SKIP_ANNOTATION", value=SKIP_ANNOTATION),
                            ],
                            ports=[
                                client.V1ContainerPort(container_port=9090, name="metrics"),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={
                                    "cpu": CHECKER_CPU_REQUEST,
                                    "memory": CHECKER_MEMORY_REQUEST,
                                },
                                limits={
                                    "cpu": CHECKER_CPU_LIMIT,
                                    "memory": CHECKER_MEMORY_LIMIT,
                                },
                            ),
                            security_context=client.V1SecurityContext(
                                run_as_non_root=True,
                                read_only_root_filesystem=True,
                                allow_privilege_escalation=False,
                                capabilities=client.V1Capabilities(drop=["ALL"]),
                            ),
                            volume_mounts=[
                                client.V1VolumeMount(name="tmp", mount_path="/tmp"),  # nosec B108
                            ],
                        ),
                    ],
                    volumes=[
                        client.V1Volume(
                            name="tmp",
                            empty_dir=client.V1EmptyDirVolumeSource(),
                        ),
                    ],
                ),
            ),
        ),
    )


def get_secret_names_for_namespace(namespace: str) -> list[str] | None:
    """Resolve the secret names config for a namespace.

    Returns:
        None  – full secret access (current default behavior)
        []    – no secret access at all
        [str] – restricted to specific secret names
    """
    if namespace in NAMESPACE_SECRETS:
        return list(NAMESPACE_SECRETS[namespace])
    if namespace in NO_SECRET_NAMESPACES:
        return []
    return None


def deploy_checker(
    namespace: str,
    checker_image: str = CHECKER_IMAGE,
    check_interval_minutes: int = 30,
    credential_source: str = "pullSecret",
    secret_names: list[str] | None = None,
) -> None:
    """Create SA, Role, RoleBinding, Service, and Deployment for the checker in a namespace."""
    v1 = client.CoreV1Api()
    rbac_v1 = client.RbacAuthorizationV1Api()
    apps_v1 = client.AppsV1Api()

    sa = _build_service_account(namespace)
    role = _build_role(namespace, secret_names=secret_names)
    rb = _build_role_binding(namespace)
    svc = _build_service(namespace)
    deploy = _build_deployment(namespace, checker_image, check_interval_minutes, credential_source)

    try:
        v1.read_namespaced_service_account(CHECKER_SERVICE_ACCOUNT, namespace)
        v1.patch_namespaced_service_account(CHECKER_SERVICE_ACCOUNT, namespace, sa)
        logger.info("Updated ServiceAccount in %s", namespace)
    except ApiException as e:
        if e.status == 404:
            v1.create_namespaced_service_account(namespace, sa)
            logger.info("Created ServiceAccount in %s", namespace)
        else:
            raise

    try:
        rbac_v1.read_namespaced_role(CHECKER_ROLE, namespace)
        rbac_v1.patch_namespaced_role(CHECKER_ROLE, namespace, role)
        logger.info("Updated Role in %s", namespace)
    except ApiException as e:
        if e.status == 404:
            rbac_v1.create_namespaced_role(namespace, role)
            logger.info("Created Role in %s", namespace)
        else:
            raise

    try:
        rbac_v1.read_namespaced_role_binding(CHECKER_ROLE_BINDING, namespace)
        rbac_v1.patch_namespaced_role_binding(CHECKER_ROLE_BINDING, namespace, rb)
        logger.info("Updated RoleBinding in %s", namespace)
    except ApiException as e:
        if e.status == 404:
            rbac_v1.create_namespaced_role_binding(namespace, rb)
            logger.info("Created RoleBinding in %s", namespace)
        else:
            raise

    try:
        v1.read_namespaced_service(CHECKER_SERVICE, namespace)
        v1.patch_namespaced_service(CHECKER_SERVICE, namespace, svc)
        logger.info("Updated metrics Service in %s", namespace)
    except ApiException as e:
        if e.status == 404:
            v1.create_namespaced_service(namespace, svc)
            logger.info("Created metrics Service in %s", namespace)
        else:
            raise

    try:
        existing = apps_v1.read_namespaced_deployment(CHECKER_DEPLOYMENT, namespace)
        existing_annotations = existing.spec.template.metadata.annotations or {}
        desired_annotations = deploy.spec.template.metadata.annotations or {}
        # Merge desired annotations with nulls for any stale keys so they get removed
        final_annotations = dict(desired_annotations)
        for key in existing_annotations:
            if key not in final_annotations:
                final_annotations[key] = None
        deploy.spec.template.metadata.annotations = final_annotations
        apps_v1.patch_namespaced_deployment(CHECKER_DEPLOYMENT, namespace, deploy)
        logger.info("Updated checker Deployment in %s", namespace)
    except ApiException as e:
        if e.status == 404:
            apps_v1.create_namespaced_deployment(namespace, deploy)
            logger.info("Created checker Deployment in %s", namespace)
        else:
            raise


# Every checker pod template carries app.kubernetes.io/version, so a version
# bump changes all N templates at once and Kubernetes rolls them simultaneously.
# On 2026-08-17 that put 34 new pods on one node in 29 seconds and drove its
# kube-multus into OOMKill, stranding 36 pods for ~50 minutes (#61).
#
# One lock, held across the patch *and* the readiness wait, makes checker
# rollouts strictly one-at-a-time however they are triggered. Both the paced
# bulk loops and the namespace handler go through it, so neither can burst on
# its own or race the other.
_rollout_lock = threading.Lock()


def _rollout_complete(deployment) -> bool:
    """Whether a Deployment has finished rolling out to its current generation.

    Mirrors `kubectl rollout status`. Checking readyReplicas alone is not
    enough: for a single-replica Deployment the default strategy surges to two
    pods, so immediately after a patch the *old* pod is still Ready and a naive
    check returns instantly — serialising nothing at all.
    """
    spec_replicas = deployment.spec.replicas if deployment.spec.replicas is not None else 1
    status = deployment.status
    generation = deployment.metadata.generation

    # The controller has not yet acted on the patch we just made.
    if generation is not None and (status.observed_generation or 0) < generation:
        return False
    # Not every replica has been recreated against the new template.
    if (status.updated_replicas or 0) < spec_replicas:
        return False
    # Old-template pods are still terminating.
    if (status.replicas or 0) > (status.updated_replicas or 0):
        return False
    if (status.available_replicas or 0) < spec_replicas:
        return False
    return True


def wait_for_checker_ready(namespace: str, timeout: int = CHECKER_READY_TIMEOUT) -> bool:
    """Block until the checker Deployment in a namespace finishes rolling out.

    Returns False on timeout or API error rather than raising, so one wedged
    namespace cannot stall the rollout for every namespace behind it.
    """
    apps_v1 = client.AppsV1Api()
    deadline = time.monotonic() + timeout
    while True:
        try:
            deployment = apps_v1.read_namespaced_deployment(CHECKER_DEPLOYMENT, namespace)
        except ApiException as exc:
            logger.warning("Cannot read checker Deployment in %s while waiting: %s", namespace, exc)
            return False

        if _rollout_complete(deployment):
            return True

        if time.monotonic() >= deadline:
            logger.warning(
                "Checker in %s did not roll out within %ds; continuing to next namespace",
                namespace, timeout,
            )
            return False

        time.sleep(CHECKER_READY_POLL_SECONDS)


def deploy_checker_serialised(namespace: str, *, blocking: bool = True, **kwargs) -> bool:
    """Deploy a checker, serialised cluster-wide against other checker rollouts.

    With blocking=False the call is skipped entirely when another rollout holds
    the lock, and the caller relies on the reconcile loop to pick the namespace
    up on its next pass. That is what keeps the namespace event handler from
    fanning out: on startup kopf fires it for every existing namespace at once,
    and all but one of those return immediately.
    """
    if not _rollout_lock.acquire(blocking=blocking):
        logger.info("Checker rollout in progress; deferring %s to reconcile", namespace)
        return False
    try:
        deploy_checker(namespace=namespace, **kwargs)
        wait_for_checker_ready(namespace)
        return True
    finally:
        _rollout_lock.release()


def teardown_checker_serialised(namespace: str, *, blocking: bool = True) -> bool:
    """Tear down a checker under the same lock that serialises deployments.

    Teardown is already sequential per namespace, so this is not about rate —
    it is about not interleaving with a deployment. `checker.enabled: false`
    makes every namespace fail _should_audit at once, so reconcile walks the
    whole cluster tearing down while the namespace handler may still be
    deploying; without a shared lock those two can race the same namespace and
    leave a half-removed checker behind.

    Deliberately does not wait for the pods to disappear. Pod deletion is
    asynchronous and grace-period bound, so waiting would stall the audit thread
    for roughly grace x N with little gained: a CNI DEL burst has none of the
    retry amplification that makes an ADD burst self-sustaining (#61).
    """
    if not _rollout_lock.acquire(blocking=blocking):
        logger.info("Checker rollout in progress; deferring teardown of %s to reconcile", namespace)
        return False
    try:
        teardown_checker(namespace)
        return True
    finally:
        _rollout_lock.release()


def teardown_checker(namespace: str) -> None:
    """Delete checker Deployment, RoleBinding, Role, and ServiceAccount from a namespace."""
    v1 = client.CoreV1Api()
    rbac_v1 = client.RbacAuthorizationV1Api()
    apps_v1 = client.AppsV1Api()

    for delete_fn in [
        lambda: apps_v1.delete_namespaced_deployment(CHECKER_DEPLOYMENT, namespace),
        lambda: v1.delete_namespaced_service(CHECKER_SERVICE, namespace),
        lambda: rbac_v1.delete_namespaced_role_binding(CHECKER_ROLE_BINDING, namespace),
        lambda: rbac_v1.delete_namespaced_role(CHECKER_ROLE, namespace),
        lambda: v1.delete_namespaced_service_account(CHECKER_SERVICE_ACCOUNT, namespace),
    ]:
        try:
            delete_fn()
        except ApiException as e:
            if e.status != 404:
                raise

    logger.info("Tore down checker in %s", namespace)
