"""Sleep / wake control: templates, the environment configuration, permissions, deployment with the worker's refresh protocol,
starting the state machines, and removal."""

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from unittest import mock

import yaml

from helpers import Base, ROOT, make_ctx, ok_json, resolved
from test_modules import _CfnLoader
from dial_install import power, uninstall
from dial_install.errors import InstallerError
from dial_install.runner import Result

ASSETS = ROOT / "assets" / "power"
GROUPS = {"acme-cluster-core": 2, "acme-cluster-dynamic": 1, "acme-cluster-system": 1, "acme-cluster-ai-rag-x": 1}


def template(name):
    return yaml.load((ASSETS / name).read_text(), Loader=_CfnLoader)


def pod(namespace, name, owner="ReplicaSet", phase="Running"):
    return {"metadata": {"namespace": namespace, "name": name, "ownerReferences": [{"kind": owner}]}, "status": {"phase": phase}}


def item(namespace, name, replicas=1):
    return {"metadata": {"namespace": namespace, "name": name}, "spec": {"replicas": replicas}}


class Env:
    """A scripted fake of the AWS calls and the cluster the sleep / wake control needs."""

    def __init__(self, groups=None, stacks=None, invoke=None):
        self.groups = dict(GROUPS if groups is None else groups)
        self.stacks = dict(stacks or {})              # name -> {"Parameters": [...], "Outputs": [...], "Tags": [...]}
        self.invoke_results = dict(invoke or {})
        self.events = []
        self.uploads = {}
        self.applied = ""

    def responses(self):
        return {
            ("aws", "eks", "list-nodegroups"): ok_json({"nodegroups": sorted(self.groups)}),
            ("aws", "eks", "describe-nodegroup"): self.describe_nodegroup,
            ("aws", "cloudformation", "describe-stacks"): self.describe_stack,
            ("aws", "cloudformation", "deploy"): self.deploy,
            ("aws", "s3", "cp"): self.upload,
            ("aws", "lambda", "invoke"): self.invoke,
            ("kubectl", "--context", "dial-acme", "get", "pods"): ok_json({"items": [
                pod("dial", "dial-core-1"), pod("kube-system", "coredns-1"), pod("kube-system", "aws-node-x", owner="DaemonSet"),
                pod("amazon-cloudwatch", "agent-1", owner="DaemonSet"), pod("knative-serving", "activator-1"),
                pod("istio-system", "istiod-1"), pod("dial", "job-1", owner="Job", phase="Succeeded")]}),
            ("kubectl", "--context", "dial-acme", "get", "deployments"): ok_json({"items": [
                item("dial", "dial-core"), item("dial", "dial-chat"), item("kube-system", "coredns", 2),
                item("kube-system", "cluster-autoscaler-aws-cluster-autoscaler"), item("istio-system", "istiod"),
                *[item("knative-serving", n) for n in ("activator", "autoscaler", "controller", "net-istio-controller", "net-istio-webhook", "webhook")]]}),
            ("kubectl", "--context", "dial-acme", "get", "statefulsets"): ok_json({"items": [item("dial", "dial-influxdb")]}),
            ("kubectl", "--context", "dial-acme", "-n", "kube-system", "get", "deployment"): ok_json({"spec": {"replicas": 1}}),
            ("kubectl", "--context", "dial-acme", "apply"): self.kubectl_apply,
        }

    def describe_nodegroup(self, argv):
        name = argv[argv.index("--nodegroup-name") + 1]
        return ok_json({"nodegroup": {"nodegroupArn": f"arn:aws:eks:us-east-1:123456789012:nodegroup/acme-cluster/{name}/id",
                                      "scalingConfig": {"minSize": self.groups[name], "maxSize": self.groups[name] + 2,
                                                        "desiredSize": self.groups[name]}}})

    def describe_stack(self, argv):
        name = argv[argv.index("--stack-name") + 1]
        if name not in self.stacks:
            return Result(255, "", "does not exist")
        return ok_json({"Stacks": [{"StackStatus": "CREATE_COMPLETE", **self.stacks[name]}]})

    def deploy(self, argv):
        name = argv[argv.index("--stack-name") + 1]
        params = dict(a.split("=", 1) for a in argv[argv.index("--parameter-overrides") + 1:] if "=" in a)
        outputs = [{"OutputKey": "ArtifactBucketName", "OutputValue": "acme-sleep-wake-artifacts-123456789012-us-east-1"}] \
            if name.endswith("-artifacts") else []
        self.stacks[name] = {"Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()], "Outputs": outputs,
                             "Tags": [{"Key": "dial-install:component", "Value": "sleep-wake"}]}
        self.events.append(f"deploy {name}")
        self.last_deploy = argv
        return Result(0)

    def upload(self, argv):
        self.uploads[argv[4].split("/", 3)[3]] = Path(argv[3]).read_bytes()
        self.events.append("upload " + argv[4].rsplit("/", 1)[-1])
        return Result(0)

    def invoke(self, argv):
        payload = json.loads(argv[argv.index("--payload") + 1])
        self.events.append("invoke " + payload["operation"])
        result = self.invoke_results.get(payload["operation"], {"status": "OWNER" if payload["operation"] == "prepareConfigRefresh" else "SUCCEEDED"})
        Path(argv[argv.index("--payload") + 2]).write_text(json.dumps(result))
        return Result(0)

    def kubectl_apply(self, argv):
        self.events.append("rbac")
        return Result(0)


def new_ctx(env, cfg=None, dry_run=False):
    ctx = make_ctx(cfg=cfg, responses=env.responses(), dry_run=dry_run)
    ctx.cache["account_id"] = "123456789012"
    return ctx


class TemplateTests(Base):
    def test_the_control_template_is_the_environments_without_notifications(self):
        control = template("control.yaml")
        resources = set(control["Resources"])
        self.assertEqual(resources, {"StateTable", "WorkerRole", "WorkerLogGroup", "WorkerFunction", "WorkerAccessEntry", "StateMachineRole",
                                     "StateMachineLogGroup", "SleepStateMachine", "WakeStateMachine", "SchedulerDeadLetterQueue",
                                     "SchedulerRole", "SchedulerGroup", "SleepSchedule", "WakeSchedule"})
        self.assertNotIn("NotificationEmail", control["Parameters"])
        self.assertNotIn("SNS", json.dumps(control))

    def test_both_state_machines_come_from_one_definition_and_differ_only_in_the_desired_state(self):
        control = template("control.yaml")["Resources"]
        sleep, wake = control["SleepStateMachine"]["Properties"], control["WakeStateMachine"]["Properties"]
        self.assertEqual(sleep["DefinitionS3Location"], wake["DefinitionS3Location"])
        self.assertEqual((sleep["DefinitionSubstitutions"]["DesiredState"], wake["DefinitionSubstitutions"]["DesiredState"]), ("ASLEEP", "AWAKE"))
        asl = (ASSETS / "environment-control.asl.json").read_text()
        self.assertEqual(set(re.findall(r"\$\{([A-Za-z]+)\}", asl)), set(sleep["DefinitionSubstitutions"]))

    def test_schedules_exist_only_when_asked_for(self):
        control = template("control.yaml")
        self.assertEqual(control["Resources"]["SleepSchedule"]["Condition"], "HasSleepSchedule")
        self.assertEqual(control["Parameters"]["SleepScheduleExpression"]["Default"], "NONE")

    def test_the_parameters_the_installer_passes_are_exactly_the_templates(self):
        env = Env()
        ctx = new_ctx(env)
        params = power.control_parameters(ctx, "b", "artifacts/x", "k", "h", [{"arn": "a"}])
        self.assertEqual(set(params), set(template("control.yaml")["Parameters"]))

    def test_the_artifact_bucket_is_private_encrypted_and_versioned(self):
        bucket = template("artifacts.yaml")["Resources"]["ArtifactBucket"]["Properties"]
        self.assertEqual(bucket["VersioningConfiguration"]["Status"], "Enabled")
        self.assertTrue(all(bucket["PublicAccessBlockConfiguration"].values()))
        self.assertIn("ServerSideEncryptionConfiguration", bucket["BucketEncryption"])

    def test_the_worker_package_is_the_code_as_it_is_and_always_the_same_bytes(self):
        first, second = power.worker_zip(), power.worker_zip()
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(archive.namelist(), ["handler.py"])
            self.assertEqual(archive.read("handler.py"), (ASSETS / "handler.py").read_bytes())
        self.assertRegex(power.artifact_prefix(), r"^artifacts/[0-9a-f]{64}$")


class ScheduleTests(Base):
    def test_cron(self):
        self.assertEqual(power.cron("19:30", "SUN-THU"), "cron(30 19 ? * SUN-THU *)")
        self.assertEqual(power.cron("08:05", "MON,WED"), "cron(5 8 ? * MON,WED *)")

    def test_without_a_schedule_only_the_state_machines_exist(self):
        params = power.schedule_parameters(make_ctx())
        self.assertEqual((params["SleepScheduleExpression"], params["WakeScheduleExpression"]), ("NONE", "NONE"))

    def test_with_a_schedule(self):
        cfg = resolved(power={"schedule": {"sleep": "19:30", "wake": "08:40"}})
        params = power.schedule_parameters(make_ctx(cfg=cfg))
        self.assertEqual(params, {"SleepScheduleExpression": "cron(30 19 ? * SUN-THU *)", "WakeScheduleExpression": "cron(40 8 ? * SUN-THU *)",
                                  "SleepScheduleState": "ENABLED", "WakeScheduleState": "ENABLED", "ScheduleTimezone": "Asia/Jerusalem"})


class ConfigTests(Base):
    def config(self, env=None, cfg=None):
        return power.build_config(new_ctx(env or Env(), cfg=cfg), "bucket")

    def test_every_key_the_worker_reads_is_there(self):
        config = self.config()
        code = (ASSETS / "handler.py").read_text()
        paths = set(re.findall(r'config(\[\"[a-zA-Z]+\"\](?:\[\"[a-zA-Z]+\"\])*)', code))
        for path in paths:
            keys = re.findall(r'"([a-zA-Z]+)"', path)
            if keys[0] in ("enabled", "namespace", "deployment"):          # read from a sub-object passed as `config`
                continue
            node = config
            for key in keys:
                self.assertIn(key, node, f"config{path}")
                node = node[key]
        for group in config["eks"]["nodeGroups"]:
            for key in ("name", "arn", "role", "awake", "sleepOrder", "wakeOrder"):
                self.assertIn(key, group)

    def test_every_node_group_with_its_role_and_order_including_an_add_ons(self):
        groups = {g["name"]: g for g in self.config()["eks"]["nodeGroups"]}
        self.assertEqual(set(groups), set(GROUPS))
        self.assertEqual((groups["acme-cluster-system"]["role"], groups["acme-cluster-system"]["wakeOrder"]), ("system", 10))
        self.assertEqual(groups["acme-cluster-core"]["awake"], {"desired": 2, "min": 2, "max": 4})
        self.assertEqual(groups["acme-cluster-ai-rag-x"]["role"], "addon")

    def test_an_installation_that_is_asleep_is_refused(self):
        with self.assertRaises(InstallerError) as raised:
            self.config(Env(groups={**GROUPS, "acme-cluster-core": 0}))
        self.assertIn("./dial-install wake", raised.exception.hint)

    def test_namespaces_and_workloads_come_from_the_cluster(self):
        config = self.config()
        self.assertEqual(config["kubernetes"]["managedNamespaces"], ["dial", "istio-system", "knative-serving", "kube-system"])
        self.assertIn({"name": "dial-core", "namespace": "dial"}, config["healthChecks"]["kubernetes"]["deployments"])
        self.assertEqual(config["healthChecks"]["kubernetes"]["statefulSets"], [{"name": "dial-influxdb", "namespace": "dial"}])

    def test_the_autoscaler_and_knative_are_found(self):
        kube = self.config()["kubernetes"]
        self.assertEqual(kube["autoscaler"], {"awakeReplicas": 1, "deployment": "cluster-autoscaler-aws-cluster-autoscaler", "enabled": True,
                                              "managedNodeGroups": ["acme-cluster-dynamic"], "namespace": "kube-system"})
        self.assertEqual(len(kube["deferredWake"]["deployments"]), 6)
        self.assertEqual(kube["deferredWake"]["afterNodeGroup"], "acme-cluster-core")

    def test_http_checks_go_through_the_load_balancer_and_follow_the_installation(self):
        endpoints = {e["name"]: e for e in self.config()["healthChecks"]["http"]["endpoints"]}
        self.assertEqual(endpoints["core"]["url"], "https://core.dial.acme.example/health")
        self.assertEqual(endpoints["flows-core"]["url"], "https://flows-core.dial.acme.example/auth/login")   # only /api and /auth are exposed
        self.assertIn("superset", endpoints)
        without = {e["name"] for e in self.config(cfg=resolved(features={"analytics": False}))["healthChecks"]["http"]["endpoints"]}
        self.assertNotIn("superset", without)
        self.assertNotIn("grafana", without)

    def test_database_and_environment(self):
        config = self.config()
        self.assertEqual(config["rds"]["clusterIdentifier"], "acme-db")
        self.assertEqual(config["environment"], "acme")
        self.assertEqual(config["eks"]["network"]["privateSubnetIds"], ["subnet-prv1", "subnet-prv2"])

    def test_the_hash_is_the_one_the_worker_checks(self):
        body = power.config_bytes(self.config())
        self.assertEqual(json.loads(body), self.config())
        self.assertEqual(len(hashlib.sha256(body).hexdigest()), 64)


class RbacTests(Base):
    def documents(self):
        ctx = new_ctx(Env())
        return power.rbac(ctx, power.build_config(ctx, "b"))

    def test_bound_to_the_access_group_of_the_worker(self):
        for document in self.documents():
            if document["kind"].endswith("Binding"):
                self.assertEqual(document["subjects"], [{"kind": "Group", "name": "acme-sleep-wake-controller", "apiGroup": "rbac.authorization.k8s.io"}])

    def test_only_what_the_worker_needs(self):
        roles = {(d["metadata"].get("namespace"), d["metadata"]["name"]): d["rules"] for d in self.documents() if d["kind"] in ("Role", "ClusterRole")}
        knative = roles[("knative-serving", "acme-sleep-wake")]
        self.assertIn({"apiGroups": ["apps"], "resources": ["deployments/scale"], "verbs": ["patch"],
                       "resourceNames": ["activator", "autoscaler", "controller", "net-istio-controller", "net-istio-webhook", "webhook"]}, knative)
        self.assertIn({"apiGroups": [""], "resources": ["endpoints"], "resourceNames": ["istiod"], "verbs": ["get"]}, roles[("istio-system", "acme-sleep-wake")])
        self.assertEqual(roles[("kube-system", "acme-sleep-wake-autoscaler")][0]["resourceNames"], ["cluster-autoscaler-aws-cluster-autoscaler"])
        cluster = roles[(None, "acme-sleep-wake-cluster")]
        self.assertIn({"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch", "patch"]}, cluster)
        self.assertNotIn("secrets", json.dumps(list(roles.values())))


class DeployTests(Base):
    def test_a_new_installation_gets_everything_in_order(self):
        env = Env()
        ctx = new_ctx(env)
        power.deploy(ctx, lambda _m: None)
        self.assertEqual(env.events, ["deploy acme-sleep-wake-artifacts", "upload worker.zip", "upload environment-control.asl.json",
                                      "upload environment.json", "rbac", "deploy acme-sleep-wake"])
        self.assertIn("CAPABILITY_IAM", env.last_deploy)
        params = {p["ParameterKey"]: p["ParameterValue"] for p in env.stacks["acme-sleep-wake"]["Parameters"]}
        config = env.uploads[params["ConfigObjectKey"]]
        self.assertEqual(params["ConfigHash"], hashlib.sha256(config).hexdigest())
        self.assertEqual(len(params["NodegroupArns"].split(",")), len(GROUPS))
        self.assertEqual(params["SleepScheduleExpression"], "NONE")
        self.assertEqual(env.uploads[params["WorkerObjectKey"]], power.worker_zip())

    def test_the_same_environment_again_changes_no_configuration(self):
        env = Env()
        power.deploy(new_ctx(env), lambda _m: None)
        env.events.clear()
        power.deploy(new_ctx(env), lambda _m: None)
        self.assertNotIn("invoke prepareConfigRefresh", env.events)

    def test_a_changed_environment_uses_the_workers_refresh_protocol(self):
        env = Env()
        power.deploy(new_ctx(env), lambda _m: None)
        env.groups["acme-cluster-addon-new"] = 1                 # an add-on brought its own nodes
        env.events.clear()
        power.deploy(new_ctx(env), lambda _m: None)
        self.assertEqual([e for e in env.events if e.startswith(("invoke", "deploy acme-sleep-wake"))][1:],
                         ["invoke prepareConfigRefresh", "deploy acme-sleep-wake", "invoke validateConfigRefresh", "invoke completeConfigRefresh"])

    def test_a_refresh_the_worker_refuses_changes_nothing_and_says_why(self):
        env = Env()
        power.deploy(new_ctx(env), lambda _m: None)
        env.groups["acme-cluster-addon-new"] = 1
        env.invoke_results["prepareConfigRefresh"] = {"status": "BLOCKED", "message": "configuration refresh requires a stable awake environment",
                                                      "details": {"blockers": ["aurora:stopped"]}}
        env.events.clear()
        with self.assertRaises(InstallerError) as raised:
            power.deploy(new_ctx(env), lambda _m: None)
        self.assertIn("aurora:stopped", raised.exception.hint)
        self.assertNotIn("deploy acme-sleep-wake", env.events)

    def test_a_failed_validation_is_aborted(self):
        env = Env()
        power.deploy(new_ctx(env), lambda _m: None)
        env.groups["acme-cluster-addon-new"] = 1
        env.invoke_results["validateConfigRefresh"] = {"status": "BLOCKED", "details": {"blockers": ["x"]}}
        env.events.clear()
        with self.assertRaises(InstallerError):
            power.deploy(new_ctx(env), lambda _m: None)
        self.assertEqual(env.events[-1], "invoke abortConfigRefresh")

    def test_disabled_does_nothing(self):
        env = Env()
        power.deploy(new_ctx(env, cfg=resolved(power={"enabled": False})), lambda _m: None)
        self.assertEqual(env.events, [])

    def test_a_schedule_reaches_the_stack(self):
        env = Env()
        power.deploy(new_ctx(env, cfg=resolved(power={"schedule": {"sleep": "20:00", "wake": "07:00", "days": "MON-FRI", "timezone": "UTC"}})),
                     lambda _m: None)
        params = {p["ParameterKey"]: p["ParameterValue"] for p in env.stacks["acme-sleep-wake"]["Parameters"]}
        self.assertEqual((params["SleepScheduleExpression"], params["ScheduleTimezone"]), ("cron(0 20 ? * MON-FRI *)", "UTC"))


class AwakeTests(Base):
    def test_asleep_is_seen_and_named(self):
        env = Env(groups={**GROUPS, "acme-cluster-core": 0}, stacks={"acme-sleep-wake": {}})
        with self.assertRaises(InstallerError) as raised:
            power.require_awake(new_ctx(env))
        self.assertIn("./dial-install wake", raised.exception.hint)

    def test_without_the_control_nothing_is_asleep(self):
        power.require_awake(new_ctx(Env(groups={**GROUPS, "acme-cluster-core": 0})))

    def test_sleep_and_wake_start_the_state_machine_and_wait(self):
        env = Env(stacks={"acme-sleep-wake": {}})
        responses = env.responses()
        responses[("aws", "stepfunctions", "start-execution")] = ok_json({"executionArn": "arn:aws:states:us-east-1:1:execution:acme-wake:e1"})
        states = iter(["RUNNING", "SUCCEEDED"])
        responses[("aws", "stepfunctions", "describe-execution")] = lambda argv: ok_json({"status": next(states)})
        ctx = make_ctx(responses=responses)
        lines = []
        with mock.patch("time.sleep"):
            self.assertEqual(power.run(ctx, "wake", lines.append), "SUCCEEDED")
        started = ctx.cloud.runner.called("aws", "stepfunctions", "start-execution")[0]
        self.assertIn("arn:aws:states:us-east-1:123456789012:stateMachine:acme-wake", started)
        self.assertIn("The installation is awake.", lines)

    def test_a_failed_execution_is_an_error(self):
        env = Env(stacks={"acme-sleep-wake": {}})
        responses = env.responses()
        responses[("aws", "stepfunctions", "start-execution")] = ok_json({"executionArn": "arn:x:e1"})
        responses[("aws", "stepfunctions", "describe-execution")] = ok_json({"status": "FAILED"})
        with mock.patch("time.sleep"), self.assertRaises(InstallerError) as raised:
            power.run(make_ctx(responses=responses), "sleep", lambda _m: None)
        self.assertIn("FAILED", raised.exception.message)


class UninstallTests(Base):
    def test_a_stopped_database_is_started_before_it_is_deleted(self):
        responses = {("aws", "cloudformation", "describe-stack-resources"): ok_json({"StackResources": [
                         {"ResourceType": "AWS::RDS::DBCluster", "PhysicalResourceId": "acme-db"}]}),
                     ("aws", "rds", "describe-db-clusters"): ok_json({"DBClusters": [{"Status": "stopped"}]})}
        ctx = make_ctx(responses=responses)
        uninstall.Uninstaller(ctx, lambda _m: None).database_awake("acme-database")
        self.assertTrue(ctx.cloud.runner.called("aws", "rds", "start-db-cluster"))

    def test_the_control_and_its_bucket_are_removed(self):
        env = Env(stacks={"acme-sleep-wake": {"Tags": [{"Key": "dial-install:component", "Value": "sleep-wake"}]},
                          "acme-sleep-wake-artifacts": {"Tags": [{"Key": "dial-install:component", "Value": "sleep-wake"}]}})
        responses = env.responses()
        responses[("aws", "cloudformation", "describe-stack-resources")] = ok_json({"StackResources": [
            {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "acme-bucket"}]})
        ctx = make_ctx(responses=responses)
        uninstall.Uninstaller(ctx, lambda _m: None).sleep_wake_control()
        deleted = [c[c.index("--stack-name") + 1] for c in ctx.cloud.runner.writes if "delete-stack" in c]
        self.assertEqual(deleted, ["acme-sleep-wake", "acme-sleep-wake-artifacts"])
        self.assertTrue(ctx.cloud.runner.called("aws", "s3api", "list-object-versions"))
