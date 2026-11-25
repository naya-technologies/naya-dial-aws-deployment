"""Sleep / wake control of an installation: two Step Functions state machines that put the environment to sleep and wake it up.

Created with every installation (`apply`, `upgrade`), from two CloudFormation stacks:

    <name>-sleep-wake-artifacts   an S3 bucket with the worker code, the state machine definition and the environment configuration
    <name>-sleep-wake             the worker Lambda (in the VPC), the state machines <name>-sleep and <name>-wake, their lock table
                                  (DynamoDB), the EKS access entry of the worker, and, when `power.schedule` is set, the EventBridge
                                  Scheduler schedules that start them

Sleeping stops Knative and the Cluster Autoscaler, empties and scales every node group to zero and stops the Aurora cluster. Waking
reverses it and waits until every workload and every public endpoint answers. The worker (`assets/power/handler.py`) and the
definition (`assets/power/environment-control.asl.json`) are used as they are; this module only builds their configuration from the
installation and deploys them.

The configuration lists every node group, namespace and workload of the environment; the worker refuses to sleep while something
it does not know runs. It is therefore built again from the live cluster at the end of every `apply` and `upgrade` and after every
add-on change, and handed over with the worker's own configuration-refresh protocol (prepare, deploy, validate, complete).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Callable

import yaml

from . import ROOT, __version__
from .context import Context
from .errors import CommandError, InstallerError

ASSETS = ROOT / "assets" / "power"
Say = Callable[[str], None]

DAYS = re.compile(r"^(SUN|MON|TUE|WED|THU|FRI|SAT)(-(SUN|MON|TUE|WED|THU|FRI|SAT))?(,(SUN|MON|TUE|WED|THU|FRI|SAT)(-(SUN|MON|TUE|WED|THU|FRI|SAT))?)*$")
TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
KNATIVE = ("activator", "autoscaler", "controller", "net-istio-controller", "net-istio-webhook", "webhook")
CONTROLLER = {"lambda": {"memoryMb": 256, "timeoutSeconds": 90}, "lockLeaseSeconds": 300, "logRetentionDays": 14,
              "manualWakeHoldSeconds": 0, "maxTransitionSeconds": 3600, "retrySeconds": 30}
# node group role -> (sleepOrder, wakeOrder): the platform's system nodes go last and come back first.
ORDERS = {"system": (30, 10), "core": (20, 20), "dynamic": (10, 30), "addon": (15, 25)}


# ---- names -----------------------------------------------------------------------------------------------------------------
def enabled(ctx: Context) -> bool:
    return bool(ctx.cfg.get("power", {}).get("enabled", True))


def control_stack(ctx: Context) -> str:
    return f"{ctx.name}-sleep-wake"


def artifacts_stack(ctx: Context) -> str:
    return f"{ctx.name}-sleep-wake-artifacts"


def access_group(ctx: Context) -> str:
    return f"{ctx.name}-sleep-wake-controller"


def worker_function(ctx: Context) -> str:
    return f"{ctx.name}-sleep-wake-worker"


def state_machine_arn(ctx: Context, action: str) -> str:
    return f"arn:aws:states:{ctx.region}:{ctx.account_id()}:stateMachine:{ctx.name}-{action}"


# ---- schedule ----------------------------------------------------------------------------------------------------------------
def cron(clock: str, days: str) -> str:
    hour, minute = (int(part) for part in clock.split(":"))
    return f"cron({minute} {hour} ? * {days} *)"


def schedule_parameters(ctx: Context) -> dict[str, str]:
    """No schedule: the state machines exist and are started by hand (`./dial-install sleep|wake`)."""
    schedule = ctx.cfg.get("power", {}).get("schedule")
    if not schedule:
        return {"SleepScheduleExpression": "NONE", "WakeScheduleExpression": "NONE", "SleepScheduleState": "DISABLED",
                "WakeScheduleState": "DISABLED", "ScheduleTimezone": "UTC"}
    return {"SleepScheduleExpression": cron(schedule["sleep"], schedule["days"]),
            "WakeScheduleExpression": cron(schedule["wake"], schedule["days"]),
            "SleepScheduleState": "ENABLED", "WakeScheduleState": "ENABLED", "ScheduleTimezone": schedule["timezone"]}


# ---- artifacts ---------------------------------------------------------------------------------------------------------------
def worker_zip() -> bytes:
    """The worker package, byte for byte the same for the same code (fixed timestamp and mode), so its hash names it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("handler.py", date_time=(2026, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, (ASSETS / "handler.py").read_bytes())
    return buffer.getvalue()


def definition() -> bytes:
    return (ASSETS / "environment-control.asl.json").read_bytes()


def artifact_prefix() -> str:
    return "artifacts/" + hashlib.sha256(worker_zip() + definition()).hexdigest()


def config_bytes(config: dict) -> bytes:
    return (json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n").encode()


# ---- the environment configuration (built from the live installation) ------------------------------------------------------
def node_group_role(cluster: str, group: str) -> str:
    for role in ("system", "core", "dynamic"):
        if group == f"{cluster}-{role}":
            return role
    return "addon"


def node_groups(ctx: Context) -> list[dict]:
    """Every node group of the cluster (the platform's and the add-ons'), with the size it has now as its awake size."""
    cluster = ctx.cluster_name
    names = (ctx.cloud.aws_json("eks", "list-nodegroups", "--cluster-name", cluster) or {}).get("nodegroups", [])
    groups = []
    for name in sorted(names):
        group = ctx.cloud.aws_json("eks", "describe-nodegroup", "--cluster-name", cluster, "--nodegroup-name", name)["nodegroup"]
        scaling = group["scalingConfig"]
        if int(scaling.get("desiredSize", 0)) < 1:
            raise InstallerError(f"The node group {name} has no nodes: the installation is asleep (or waking up).",
                                 "Wake it first: ./dial-install wake")
        role = node_group_role(cluster, name)
        sleep_order, wake_order = ORDERS[role]
        groups.append({"arn": group["nodegroupArn"], "name": name, "role": role, "sleepOrder": sleep_order, "wakeOrder": wake_order,
                       "awake": {"desired": int(scaling["desiredSize"]), "min": int(scaling["minSize"]), "max": int(scaling["maxSize"])}})
    if not groups:
        raise InstallerError(f"The cluster {cluster} has no node groups.", "Run `./dial-install apply`.")
    return groups


def _is_daemon_or_mirror(pod: dict) -> bool:
    metadata = pod.get("metadata", {})
    if "kubernetes.io/config.mirror" in metadata.get("annotations", {}):
        return True
    return any(owner.get("kind") == "DaemonSet" for owner in metadata.get("ownerReferences") or [])


def workloads(ctx: Context) -> tuple[list[str], list[dict], list[dict]]:
    """(namespaces where something of the environment runs, its Deployments, its StatefulSets) from the live cluster."""
    pods = (ctx.cloud.kubectl_json("get", "pods", "--all-namespaces") or {}).get("items", [])
    deployments = (ctx.cloud.kubectl_json("get", "deployments", "--all-namespaces") or {}).get("items", [])
    statefulsets = (ctx.cloud.kubectl_json("get", "statefulsets", "--all-namespaces") or {}).get("items", [])
    namespaces = {p["metadata"]["namespace"] for p in pods
                  if not _is_daemon_or_mirror(p) and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")}
    namespaces |= {d["metadata"]["namespace"] for d in deployments + statefulsets}

    def identity(item):
        return {"name": item["metadata"]["name"], "namespace": item["metadata"]["namespace"]}
    return (sorted(namespaces), sorted((identity(d) for d in deployments), key=lambda i: (i["namespace"], i["name"])),
            sorted((identity(s) for s in statefulsets), key=lambda i: (i["namespace"], i["name"])))


def http_endpoints(ctx: Context) -> list[dict]:
    """The public endpoints of the installation, through its load balancer; waking is complete only when they answer."""
    hosts = ctx.cfg["domain"]["hosts"]
    checks = [("core", "/health", [200]), ("chat", "/bff/health", [200]), ("admin", "/api/health", [200]),
              ("themes", "/health", [200]), ("flows", "/health", [200]),
              ("flows-core", "/auth/login", [200, 302, 303, 307])]          # the load balancer exposes only /api and /auth of flows-core
    if ctx.cfg["features"]["analytics"]:
        checks += [("superset", "/health", [200]), ("grafana", "/api/health", [200])]
    return [{"name": name, "url": f"https://{hosts[name]}{path}", "acceptedStatusCodes": codes, "required": True}
            for name, path, codes in checks]


def build_config(ctx: Context, bucket: str) -> dict:
    compute = ctx.outputs["compute"]
    groups = node_groups(ctx)
    namespaces, deployments, statefulsets = workloads(ctx)
    live = {(d["namespace"], d["name"]) for d in deployments}
    autoscaler = next((d["name"] for d in deployments if d["namespace"] == "kube-system" and "cluster-autoscaler" in d["name"]), None)
    autoscaler_replicas = 1
    if autoscaler:
        spec = ctx.cloud.kubectl_json("-n", "kube-system", "get", "deployment", autoscaler) or {}
        autoscaler_replicas = max(1, int(spec.get("spec", {}).get("replicas", 1) or 1))
    core = next(g["name"] for g in groups if g["role"] == "core")
    dynamic = [g["name"] for g in groups if g["role"] == "dynamic"]
    network = ctx.outputs["network"]
    database = f"{ctx.name}-db"
    schedule = ctx.cfg.get("power", {}).get("schedule")
    return {
        "schemaVersion": 1,
        "environment": ctx.name,
        "aws": {"accountId": ctx.account_id(), "region": ctx.region},
        "controller": dict(CONTROLLER),
        "eks": {
            "authenticationMode": "API",                     # the installer creates the cluster with access entries only
            "clusterArn": compute["ClusterArn"], "clusterName": ctx.cluster_name,
            "network": {"clusterSecurityGroupId": compute["ClusterSecurityGroupId"], "mode": "vpc",
                        "privateSubnetIds": [network["PrivateSubnet1Id"], network["PrivateSubnet2Id"]],
                        "securityGroupIds": [compute["ClusterSecurityGroupId"]], "vpcId": network["VPCId"]},
            "nodeGroups": groups,
        },
        "healthChecks": {
            "http": {"endpoints": http_endpoints(ctx), "requestTimeoutSeconds": 15},
            "kubernetes": {"deployments": deployments, "statefulSets": statefulsets},
        },
        "kubernetes": {
            "autoscaler": {"awakeReplicas": autoscaler_replicas, "deployment": autoscaler or "cluster-autoscaler",
                           "enabled": bool(autoscaler and dynamic), "managedNodeGroups": dynamic, "namespace": "kube-system"},
            "deferredWake": {"afterNodeGroup": core,
                             "deployments": [{"name": n, "namespace": "knative-serving"} for n in KNATIVE if ("knative-serving", n) in live],
                             "prerequisite": {"deployment": "istiod", "namespace": "istio-system", "service": "istiod"}},
            "managedNamespaces": namespaces,
            "quiesce": {"forceDeleteAfterSeconds": 600, "gracePeriodSeconds": 120},
        },
        "rds": {"clusterArn": f"arn:aws:rds:{ctx.region}:{ctx.account_id()}:cluster:{database}", "clusterIdentifier": database},
        "schedules": {"timezone": (schedule or {}).get("timezone", "UTC"),
                      "sleep": {"enabled": bool(schedule), "expression": cron(schedule["sleep"], schedule["days"]) if schedule else "NONE"},
                      "wake": {"enabled": bool(schedule), "expression": cron(schedule["wake"], schedule["days"]) if schedule else "NONE"}},
        "stack": {"artifactBucket": bucket, "artifactName": artifacts_stack(ctx), "controlName": control_stack(ctx),
                  "resourcePrefix": ctx.name},
    }


# ---- Kubernetes permissions of the worker ------------------------------------------------------------------------------------
def rbac(ctx: Context, config: dict) -> list[dict]:
    """What the worker may do in the cluster: read and cordon nodes, read workloads, delete pods when it empties a node, scale
    the Cluster Autoscaler and Knative, read the istiod endpoints. Bound to its EKS access entry group."""
    group = access_group(ctx)
    labels = {"app.kubernetes.io/managed-by": "dial-install", "app.kubernetes.io/name": "sleep-wake"}
    subject = [{"kind": "Group", "name": group, "apiGroup": "rbac.authorization.k8s.io"}]
    name = f"{ctx.name}-sleep-wake"
    base = [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch", "delete"]},
        {"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["apps"], "resources": ["deployments", "statefulsets", "replicasets", "daemonsets"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["policy"], "resources": ["poddisruptionbudgets"], "verbs": ["get", "list", "watch"]},
    ]
    documents = [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": f"{name}-cluster", "labels": labels},
         "rules": [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch", "patch"]},
                   {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                   {"apiGroups": ["storage.k8s.io"], "resources": ["volumeattachments"], "verbs": ["get", "list", "watch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": f"{name}-cluster", "labels": labels},
         "subjects": subject, "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": f"{name}-cluster"}},
    ]
    deferred = [d["name"] for d in config["kubernetes"]["deferredWake"]["deployments"]]
    for namespace in config["kubernetes"]["managedNamespaces"]:
        rules = list(base)
        if namespace == "knative-serving" and deferred:
            rules.append({"apiGroups": ["apps"], "resources": ["deployments/scale"], "resourceNames": deferred, "verbs": ["patch"]})
        if namespace == "istio-system":
            rules.append({"apiGroups": [""], "resources": ["endpoints"], "resourceNames": ["istiod"], "verbs": ["get"]})
        documents += [
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": name, "namespace": namespace, "labels": labels},
             "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": name, "namespace": namespace, "labels": labels},
             "subjects": subject, "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name}},
        ]
    autoscaler = config["kubernetes"]["autoscaler"]
    if autoscaler["enabled"]:
        documents += [
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
             "metadata": {"name": f"{name}-autoscaler", "namespace": autoscaler["namespace"], "labels": labels},
             "rules": [{"apiGroups": ["apps"], "resources": ["deployments", "deployments/scale"], "resourceNames": [autoscaler["deployment"]],
                        "verbs": ["get", "patch", "update"]}]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
             "metadata": {"name": f"{name}-autoscaler", "namespace": autoscaler["namespace"], "labels": labels},
             "subjects": subject, "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": f"{name}-autoscaler"}},
        ]
    return documents


# ---- AWS helpers -------------------------------------------------------------------------------------------------------------
def _stack(ctx: Context, name: str) -> dict | None:
    data = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", name, check=False)
    return (data or {}).get("Stacks", [None])[0] if data else None


def _deploy_stack(ctx: Context, stack: str, template: Path, params: dict, capabilities: tuple[str, ...] = ()) -> None:
    try:
        ctx.cloud.aws("cloudformation", "deploy", "--stack-name", stack, "--template-file", str(template),
                      "--no-fail-on-empty-changeset", *(["--capabilities", *capabilities] if capabilities else []),
                      "--tags", f"dial-install:installation={ctx.name}", f"dial-install:version={__version__}",
                      "dial-install:component=sleep-wake", *[f"{k}={v}" for k, v in ctx.cfg["tags"].items()],
                      "--parameter-overrides", *[f"{k}={v}" for k, v in params.items()], write=True, stream=True)
    except CommandError as error:
        raise InstallerError(
            f"CloudFormation stack {stack} failed.\n{error.stderr}",
            f"See what failed:\n  aws cloudformation describe-stack-events --stack-name {stack} --region {ctx.region} --max-items 15 "
            "--query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' --output table\n"
            "Fix the cause, then run the same command again.") from error


def _upload(ctx: Context, bucket: str, key: str, body: bytes) -> None:
    handle, path = tempfile.mkstemp(prefix="dial-sleep-wake-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
        ctx.cloud.aws("s3", "cp", path, f"s3://{bucket}/{key}", "--only-show-errors", write=True)
    finally:
        os.unlink(path)


def invoke(ctx: Context, payload: dict) -> dict:
    """Call the worker directly (the configuration-refresh operations are not part of the state machines)."""
    handle, path = tempfile.mkstemp(prefix="dial-sleep-wake-result-", suffix=".json")
    os.close(handle)
    try:
        ctx.cloud.aws("lambda", "invoke", "--function-name", worker_function(ctx), "--cli-binary-format", "raw-in-base64-out",
                      "--payload", json.dumps(payload), path, write=True)
        if ctx.cloud.runner.dry_run:
            return {"status": "OWNER" if payload["operation"] == "prepareConfigRefresh" else "SUCCEEDED"}
        text = Path(path).read_text() or "{}"
        return json.loads(text)
    finally:
        os.unlink(path)


def control_parameters(ctx: Context, bucket: str, prefix: str, config_key: str, config_hash: str, groups: list[dict]) -> dict:
    network = ctx.outputs["network"]
    database = f"{ctx.name}-db"
    return {
        "EnvironmentName": ctx.name, "ResourcePrefix": ctx.name, "ArtifactBucketName": bucket,
        "WorkerObjectKey": f"{prefix}/worker.zip", "StateMachineObjectKey": f"{prefix}/environment-control.asl.json",
        "ConfigObjectKey": config_key, "ConfigHash": config_hash,
        "ClusterName": ctx.cluster_name, "ClusterArn": ctx.outputs["compute"]["ClusterArn"],
        "PrivateSubnetIds": f"{network['PrivateSubnet1Id']},{network['PrivateSubnet2Id']}",
        "SecurityGroupIds": ctx.outputs["compute"]["ClusterSecurityGroupId"],
        "NodegroupArns": ",".join(g["arn"] for g in groups),
        "RdsClusterArn": f"arn:aws:rds:{ctx.region}:{ctx.account_id()}:cluster:{database}", "RdsClusterIdentifier": database,
        "KubernetesAccessGroup": access_group(ctx),
        "LambdaMemoryMb": CONTROLLER["lambda"]["memoryMb"], "LambdaTimeoutSeconds": CONTROLLER["lambda"]["timeoutSeconds"],
        "LockLeaseSeconds": CONTROLLER["lockLeaseSeconds"], "RetrySeconds": CONTROLLER["retrySeconds"],
        "LogRetentionDays": CONTROLLER["logRetentionDays"],
        **schedule_parameters(ctx),
    }


# ---- deploy ------------------------------------------------------------------------------------------------------------------
def deploy(ctx: Context, say: Say) -> None:
    """Create or update the sleep / wake control with the current state of the installation. Safe to repeat."""
    if not enabled(ctx):
        return
    say("\nSleep / wake control")
    _deploy_stack(ctx, artifacts_stack(ctx), ASSETS / "artifacts.yaml", {"ResourcePrefix": ctx.name})
    stack = _stack(ctx, artifacts_stack(ctx)) or {}
    bucket = next((o["OutputValue"] for o in stack.get("Outputs", []) if o["OutputKey"] == "ArtifactBucketName"),
                  f"{ctx.name}-sleep-wake-artifacts-{ctx.account_id()}-{ctx.region}")
    if ctx.cloud.runner.dry_run and not stack:
        say("  (dry run: the configuration is built from the live cluster when it exists)")
        return
    config = build_config(ctx, bucket)
    body = config_bytes(config)
    config_hash = hashlib.sha256(body).hexdigest()
    config_key = f"configurations/{ctx.name}/{config_hash}/environment.json"
    prefix = artifact_prefix()
    _upload(ctx, bucket, f"{prefix}/worker.zip", worker_zip())
    _upload(ctx, bucket, f"{prefix}/environment-control.asl.json", definition())
    _upload(ctx, bucket, config_key, body)
    ctx.cloud.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(rbac(ctx, config)), write=True)
    params = control_parameters(ctx, bucket, prefix, config_key, config_hash, config["eks"]["nodeGroups"])

    current = _stack(ctx, control_stack(ctx))
    active = next((p["ParameterValue"] for p in (current or {}).get("Parameters", []) if p["ParameterKey"] == "ConfigHash"), None)
    if current and active and active != config_hash:
        _refresh(ctx, params, active, config_hash, say)
    else:
        _deploy_stack(ctx, control_stack(ctx), ASSETS / "control.yaml", params, ("CAPABILITY_IAM",))
    schedule = ctx.cfg.get("power", {}).get("schedule")
    say(f"  state machines {ctx.name}-sleep and {ctx.name}-wake are ready; "
        + (f"schedule: sleep {schedule['sleep']}, wake {schedule['wake']} ({schedule['days']}, {schedule['timezone']})" if schedule
           else "no schedule (start them with ./dial-install sleep | wake)"))


def _refresh(ctx: Context, params: dict, from_hash: str, to_hash: str, say: Say) -> None:
    """The worker's configuration-refresh protocol: it holds the environment lock while the new configuration is deployed and
    accepts the new configuration only if the awake environment passes it."""
    refresh = {"environment": ctx.name, "refreshId": str(uuid.uuid4()), "fromConfigHash": from_hash, "toConfigHash": to_hash}
    say("  the environment changed: handing the new configuration to the sleep / wake worker")
    prepared = invoke(ctx, {"operation": "prepareConfigRefresh", **refresh})
    if prepared.get("status") == "SUCCEEDED":
        _deploy_stack(ctx, control_stack(ctx), ASSETS / "control.yaml", params, ("CAPABILITY_IAM",))
        return
    if prepared.get("status") != "OWNER":
        raise InstallerError("The sleep / wake control was not updated: " + (prepared.get("message") or "the worker refused the change") + ".",
                             _blockers(prepared) + "The environment must be awake and stable (and not sleeping or waking). "
                             "Run the same command again later.")
    try:
        _deploy_stack(ctx, control_stack(ctx), ASSETS / "control.yaml", params, ("CAPABILITY_IAM",))
        validated = invoke(ctx, {"operation": "validateConfigRefresh", **refresh})
        if validated.get("status") != "SUCCEEDED":
            raise InstallerError("The new sleep / wake configuration did not pass the worker's check.",
                                 _blockers(validated) + "Run the same command again once the environment is stable.")
        invoke(ctx, {"operation": "completeConfigRefresh", **refresh})
    except InstallerError:
        invoke(ctx, {"operation": "abortConfigRefresh", **refresh})
        raise


def _blockers(result: dict) -> str:
    details = result.get("details") or {}
    blockers = details.get("blockers") or []
    return ("Blocked by: " + ", ".join(blockers[:10]) + ".\n") if blockers else ""


def refresh_if_installed(ctx: Context, say: Say) -> None:
    """After an add-on change: the configuration must know its node group and its workloads."""
    if enabled(ctx) and _stack(ctx, control_stack(ctx)):
        deploy(ctx, say)


# ---- asleep? -----------------------------------------------------------------------------------------------------------------
def asleep(ctx: Context) -> bool:
    """True when the cluster's node groups have been scaled to zero by the sleep state machine (or are being)."""
    if not _stack(ctx, control_stack(ctx)):
        return False
    names = (ctx.cloud.aws_json("eks", "list-nodegroups", "--cluster-name", ctx.cluster_name, check=False) or {}).get("nodegroups", [])
    for name in names:
        group = (ctx.cloud.aws_json("eks", "describe-nodegroup", "--cluster-name", ctx.cluster_name, "--nodegroup-name", name,
                                    check=False) or {}).get("nodegroup", {})
        if int(group.get("scalingConfig", {}).get("desiredSize", 1)) < 1:
            return True
    return False


def require_awake(ctx: Context) -> None:
    if asleep(ctx):
        raise InstallerError("The installation is asleep: its nodes and database are stopped.", "Wake it first: ./dial-install wake")


# ---- sleep / wake by hand ----------------------------------------------------------------------------------------------------
def run(ctx: Context, action: str, say: Say, timeout_minutes: int = 70) -> str:
    """Start <name>-sleep or <name>-wake and wait for it. Returns the final status."""
    arn = state_machine_arn(ctx, action)
    if not _stack(ctx, control_stack(ctx)):
        raise InstallerError("The sleep / wake control does not exist yet.", "Run `./dial-install apply` (or `upgrade`) first.")
    if ctx.cloud.runner.dry_run:
        ctx.cloud.aws("stepfunctions", "start-execution", "--state-machine-arn", arn, "--input", json.dumps({"source": "dial-install"}),
                      write=True)
        return "DRY_RUN"
    result = ctx.cloud.aws("stepfunctions", "start-execution", "--state-machine-arn", arn, "--input", json.dumps({"source": "dial-install"}),
                           "--output", "json", write=True)
    started = json.loads(result.stdout)
    execution = started["executionArn"]
    say(f"Started {action} ({execution.rsplit(':', 1)[-1]}). This can take up to an hour; waiting.")
    deadline = time.monotonic() + timeout_minutes * 60
    status = "RUNNING"
    while time.monotonic() < deadline:
        described = ctx.cloud.aws_json("stepfunctions", "describe-execution", "--execution-arn", execution) or {}
        status = described.get("status", "RUNNING")
        if status != "RUNNING":
            break
        time.sleep(30)
    if status != "SUCCEEDED":
        raise InstallerError(f"The {action} execution ended with {status}.",
                             f"See its steps in the AWS console: Step Functions > {ctx.name}-{action} > the latest execution, "
                             f"and the worker's log /aws/lambda/{worker_function(ctx)}.")
    say(f"The installation is {'asleep' if action == 'sleep' else 'awake'}.")
    return status
