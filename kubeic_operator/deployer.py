import json
import logging
import os

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
                                client.V1VolumeMount(name="tmp", mount_path="/tmp"),
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
