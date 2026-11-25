"""The Bedrock Guardrail interceptor add-on: catalog, role, options, install / upgrade / remove, checks and the troubleshooting map."""

import json
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

from helpers import Base, ROOT, make_ctx, resolved
from test_addons import Cluster, HAVE_HELM, tools_patch
from test_modules import _CfnLoader
from dial_install import addons, cli, deploy, uninstall
from dial_install.errors import InstallerError
from dial_install.runner import Result
from dial_install.values import placeholders

ID = "bedrock-guardrail"
GUARDRAIL = {"guardrail_id": "14k3pse9k3ui", "guardrail_version": "1"}
ROLE = "arn:aws:iam::123456789012:role/acme-addon-bedrock-guardrail-role"
DIGEST = "sha256:7f573ce0ff7151edc71664d180ddad5b7016dc4be033fdd12f800e701274ef3f"
SOURCE = "public.ecr.aws/e8r1u6x9/naya-dial-x-bedrock-guardrail-interceptor"
ENDPOINT = "http://dial-bedrock-guardrail.dial.svc.cluster.local/openai/deployments/bedrock-guardrail/chat/completions"
hooks = addons.hooks(addons.entry_for(ID))


class GuardCluster(Cluster):
    """The fake cluster, plus Bedrock, the role stack's output, the pod's identity and the chat check."""

    def __init__(self, *args, guardrail=Result(0, "{}"), identity=None, chat="STATUS 200\nANSWER OK", logs="", digest=DIGEST, **kw):
        super().__init__(*args, **kw)
        self.guardrail, self.chat, self.logs, self.digest = guardrail, chat, logs, digest
        self.identity = identity if identity is not None else "arn:aws:sts::123456789012:assumed-role/acme-addon-bedrock-guardrail-role/botocore-session-1"
        self.tags = ["1.0.0", "1.0.1", "latest"]

    def responses(self):
        answers = super().responses()
        answers[("aws", "bedrock", "get-guardrail")] = self.get_guardrail
        answers[("/usr/bin/crane", "digest")] = self.crane_digest
        return answers

    def get_guardrail(self, argv):
        self.events.append("get-guardrail")
        return self.guardrail

    def crane_digest(self, argv):
        if argv[2].startswith("public.ecr.aws/"):
            return Result(0, self.digest)
        return Result(0, self.digest) if argv[2] in self.copied else Result(1, "", "MANIFEST_UNKNOWN")

    def cfn_describe(self, argv):
        stack = argv[argv.index("--stack-name") + 1]
        if stack not in self.node_stacks:
            return Result(255, "", "does not exist")
        return Result(0, json.dumps({"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": [{"Key": "dial-install:addon", "Value": ID}],
                                                 "Outputs": [{"OutputKey": "RoleArn", "OutputValue": ROLE}]}]}))

    def kubectl(self, argv):
        if "exec" in argv:
            self.events.append("identity check")
            return Result(0, self.identity + "\n")
        if "logs" in argv and "dial-guardrail-check" in argv:
            self.events.append("chat check")
            return Result(0, self.chat)
        if "logs" in argv:
            return Result(0, self.logs)
        return super().kubectl(argv)


def run(cluster, action="install", **given):
    ctx = make_ctx(responses=cluster.responses())
    ctx.cache["license_helper_needed"] = False
    lines = []
    with tools_patch(), mock.patch("time.sleep"):
        result = getattr(addons, action)(ctx, ID, lines.append, {**GUARDRAIL, **given} if action != "remove" else None) \
            if action != "remove" else addons.remove(ctx, ID, lines.append)
    return ctx, "\n".join(lines), result


def core_config(cluster, index=-1):
    return json.loads(cluster.platform_core_config[index]["dial"]["core"]["secrets"]["aidial.config.json"])


class CatalogTests(Base):
    def setUp(self):
        self.entry = addons.entry_for(ID)
        self.values = yaml.safe_load((self.entry["_dir"] / "values.yaml").read_text())

    def test_the_image_is_pinned_to_the_tested_tag_and_digest(self):
        self.assertEqual(self.entry["version"], "1.0.1")
        self.assertEqual(self.entry["images"][0]["digest"], DIGEST)
        self.assertEqual(f"{self.entry['source']['registry']}/{self.entry['images'][0]['repository']}", SOURCE)

    def test_the_port_the_chart_exposes_is_the_port_the_image_listens_on(self):
        self.assertEqual(str(self.values["containerPorts"]["http"]), self.values["env"]["PORT"])
        for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
            self.assertEqual(self.values[probe]["httpGet"], {"path": "/health", "port": "http"})

    def test_no_aws_credentials_in_the_environment_and_its_own_service_account_with_the_role(self):
        self.assertFalse([key for key in self.values["env"] if key.startswith("AWS_")])
        self.assertEqual(self.values["serviceAccount"]["annotations"], {"eks.amazonaws.com/role-arn": "PLACEHOLDER_STACK_ROLE_ARN"})
        self.assertEqual(self.values["serviceAccount"]["name"], "PLACEHOLDER_DIAL_RELEASE_NAME-bedrock-guardrail")

    def test_resources_and_the_user_of_the_image(self):
        self.assertEqual(self.values["resources"], {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}})
        self.assertEqual(self.values["containerSecurityContext"]["runAsUser"], 10001)
        self.assertFalse(self.values["ingress"]["enabled"])

    def test_the_interceptor_definition(self):
        definition = self.entry["core"]["interceptors"][ID]
        self.assertTrue(definition["endpoint"].endswith("/openai/deployments/bedrock-guardrail/chat/completions"))
        self.assertIs(definition["forwardAuthToken"], False)
        self.assertNotIn("configurationEndpoint", definition)

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_the_generic_chart_renders_it(self):
        ctx = make_ctx()
        ctx.cache["addon_stack_outputs"] = {ID: {"RoleArn": ROLE}}
        entry = {**self.entry, "version": "1.0.1"}
        document = addons.render_values(ctx, entry, addons.resolve_options(entry, None, GUARDRAIL, ctx.region))
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
            yaml.safe_dump(document, handle)
            handle.flush()
            result = subprocess.run(["helm", "template", "dial-addon-bedrock-guardrail", str(addons.catalog_dir() / entry["chart"]),
                                     "-n", "dial", "-f", handle.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(result.stdout) if d}
        deployment = docs[("Deployment", "dial-bedrock-guardrail")]
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        self.assertEqual(container["ports"][0]["containerPort"], int(env["PORT"]))
        self.assertEqual(container["image"], "123456789012.dkr.ecr.us-east-1.amazonaws.com/naya-dial-x-bedrock-guardrail-interceptor:1.0.1")
        self.assertEqual({k: env[k] for k in ("DIAL_URL", "GUARDRAIL_ID", "GUARDRAIL_VERSION", "BEDROCK_REGION")},
                         {"DIAL_URL": "http://dial-core.dial.svc.cluster.local", "GUARDRAIL_ID": "14k3pse9k3ui",
                          "GUARDRAIL_VERSION": "1", "BEDROCK_REGION": "us-east-1"})
        self.assertEqual(pod["serviceAccountName"], "dial-bedrock-guardrail")
        account = docs[("ServiceAccount", "dial-bedrock-guardrail")]
        self.assertEqual(account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"], ROLE)
        self.assertEqual(docs[("Service", "dial-bedrock-guardrail")]["spec"]["ports"][0]["targetPort"], "http")
        self.assertNotIn(("Ingress", "dial-bedrock-guardrail"), docs)


class RoleTests(Base):
    def setUp(self):
        self.template = yaml.load((addons.catalog_dir() / "stacks" / "bedrock-guardrail-role.yaml").read_text(), Loader=_CfnLoader)

    def test_only_its_own_service_account_may_assume_it(self):
        trust = self.template["Resources"]["InterceptorRole"]["Properties"]["AssumeRolePolicyDocument"]["Sub"][0]
        self.assertIn('"${Issuer}:sub": "system:serviceaccount:${Namespace}:${ServiceAccount}"', trust)
        self.assertIn('"${Issuer}:aud": "sts.amazonaws.com"', trust)

    def test_it_may_only_apply_this_guardrail(self):
        policies = self.template["Resources"]["InterceptorRole"]["Properties"]["Policies"]
        self.assertEqual(policies[0]["PolicyDocument"]["Statement"],
                         [{"Effect": "Allow", "Action": "bedrock:ApplyGuardrail", "Resource": {"Ref": "GuardrailArn"}}])
        self.assertNotIn("ManagedPolicyArns", self.template["Resources"]["InterceptorRole"]["Properties"])

    def test_the_parameters(self):
        ctx = make_ctx()
        entry = addons.entry_for(ID)
        options = addons.resolve_options(entry, None, GUARDRAIL, ctx.region)
        params = hooks.stack_parameters(ctx, entry, options)
        self.assertEqual(params["OIDCProviderIssuer"], "oidc.eks.us-east-1.amazonaws.com/id/ABC")
        self.assertEqual(params["ServiceAccountName"], "dial-bedrock-guardrail")
        self.assertEqual(params["KubernetesNamespace"], "dial")
        self.assertEqual(params["GuardrailArn"], "arn:aws:bedrock:us-east-1:123456789012:guardrail/14k3pse9k3ui")
        self.assertEqual(set(params), set(self.template["Parameters"]))


class OptionTests(Base):
    def resolve(self, **given):
        return addons.resolve_options(addons.entry_for(ID), None, given, "eu-west-1")

    def test_id_and_version_are_required(self):
        for missing in ("guardrail_id", "guardrail_version"):
            given = {k: v for k, v in GUARDRAIL.items() if k != missing}
            with self.assertRaises(InstallerError) as raised:
                self.resolve(**given)
            self.assertIn("--" + missing.replace("_", "-"), raised.exception.message)

    def test_defaults(self):
        options = self.resolve(**GUARDRAIL)
        self.assertEqual(options["bedrock_region"], "eu-west-1")              # the region of the installation
        self.assertEqual(options["guardrail_deployments"], "default.model")
        self.assertEqual((options["guardrail_global"], options["allow_draft"]), ("false", "false"))

    def test_the_region_can_be_changed(self):
        self.assertEqual(self.resolve(**GUARDRAIL, bedrock_region="us-west-2")["bedrock_region"], "us-west-2")

    def test_draft_is_refused_without_the_flag(self):
        entry = addons.entry_for(ID)
        draft = self.resolve(guardrail_id="abc", guardrail_version="DRAFT")
        with self.assertRaises(InstallerError) as raised:
            hooks.validate(make_ctx(), entry, draft)
        self.assertIn("--allow-draft", raised.exception.hint)
        hooks.validate(make_ctx(), entry, self.resolve(guardrail_id="abc", guardrail_version="DRAFT", allow_draft="true"))

    def test_bad_values_are_refused(self):
        for bad in ({"guardrail_id": "arn:aws:bedrock:x"}, {"guardrail_version": "v1"}, {"bedrock_region": "nowhere"},
                    {"guardrail_deployments": "a,,b"}):
            with self.assertRaises(InstallerError, msg=str(bad)):
                self.resolve(**{**GUARDRAIL, **bad})

    def test_the_flags(self):
        args = cli._parser().parse_args(["add-ons", "install", ID, "--guardrail-id", "x1", "--guardrail-version", "2", "--allow-draft",
                                         "--bedrock-region", "us-west-2", "--guardrail-deployments", "a,b", "--guardrail-global"])
        self.assertEqual(cli._addon_settings(args), {"guardrail_id": "x1", "guardrail_version": "2", "allow_draft": "true",
                                                     "bedrock_region": "us-west-2", "guardrail_deployments": "a,b", "guardrail_global": "true"})


class InstallTests(Base):
    def test_everything_in_order_and_each_step_once(self):
        cluster = GuardCluster()
        ctx, text, _ = run(cluster)
        order = [e.split(" ->")[0] for e in cluster.events]
        self.assertEqual(order, ["get-guardrail", "create-repository", f"copy {SOURCE}:1.0.1", "node group acme-addon-bedrock-guardrail",
                                 "helm addon dial-addon-bedrock-guardrail", "helm platform", "restart core", "identity check", "chat check"])
        self.assertIn("is installed and registered in DIAL Core", text)

    def test_the_role_stack_is_created_with_iam_capability_and_the_service_account_gets_the_role(self):
        cluster = GuardCluster()
        ctx, _, _ = run(cluster)
        deploy_call = next(c for c in ctx.cloud.runner.writes if c[:3] == ["aws", "cloudformation", "deploy"])
        self.assertIn("CAPABILITY_NAMED_IAM", deploy_call)
        self.assertIn("dial-install:addon=bedrock-guardrail", deploy_call)
        self.assertEqual(cluster.node_stacks["acme-addon-bedrock-guardrail"]["GuardrailArn"],
                         "arn:aws:bedrock:us-east-1:123456789012:guardrail/14k3pse9k3ui")
        values = cluster.releases["dial-addon-bedrock-guardrail"]
        self.assertEqual(values["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"], ROLE)
        self.assertFalse(any(c[:3] == ["kubectl", "--context", "dial-acme"] and "annotate" in c for c in ctx.cloud.runner.calls))

    def test_core_registers_it_on_default_model_only(self):
        cluster = GuardCluster()
        run(cluster)
        config = core_config(cluster)
        self.assertEqual(config["interceptors"], {ID: {"displayName": "Bedrock Guardrail", "endpoint": ENDPOINT, "forwardAuthToken": False,
                                                        "description": "Checks messages and answers with Amazon Bedrock Guardrails."}})
        self.assertEqual(config["models"]["default.model"]["interceptors"], [ID])
        self.assertNotIn("globalInterceptors", config)

    def test_global_is_an_explicit_choice(self):
        cluster = GuardCluster()
        run(cluster, guardrail_global="true")
        config = core_config(cluster)
        self.assertEqual(config["globalInterceptors"], [ID])
        self.assertNotIn("interceptors", config["models"]["default.model"])

    def test_it_can_be_attached_to_an_add_on_application(self):
        cluster = GuardCluster()
        run(cluster)
        cluster2 = GuardCluster(addons_installed={"ai-rag": {"addon": {"id": "ai-rag", "version": "1", "applications": {"dial-x-rag": {"endpoint": "x"}}}}})
        run(cluster2, guardrail_deployments="default.model,dial-x-rag")
        config = core_config(cluster2)
        self.assertEqual(config["applications"]["dial-x-rag"]["interceptors"], [ID])

    def test_an_unknown_deployment_is_refused_before_anything_is_changed(self):
        cluster = GuardCluster()
        with self.assertRaises(InstallerError) as raised:
            run(cluster, guardrail_deployments="no-such-model")
        self.assertIn("default.model", raised.exception.hint)
        self.assertEqual(cluster.events, [])

    def test_a_missing_guardrail_stops_before_anything_is_changed(self):
        cluster = GuardCluster(guardrail=Result(254, "", "An error occurred (ResourceNotFoundException) when calling the GetGuardrail operation"))
        with self.assertRaises(InstallerError) as raised:
            run(cluster)
        self.assertIn("--guardrail-id", raised.exception.hint)
        self.assertEqual(cluster.events, ["get-guardrail"])

    def test_the_guardrail_is_looked_up_in_the_region_of_the_guardrail(self):
        cluster = GuardCluster()
        ctx, _, _ = run(cluster, bedrock_region="us-west-2")
        call = ctx.cloud.runner.called("aws", "bedrock", "get-guardrail")[0]
        self.assertEqual(call[call.index("--region") + 1], "us-west-2")
        self.assertEqual(call[call.index("--guardrail-version") + 1], "1")

    def test_a_tag_that_moved_is_refused(self):
        cluster = GuardCluster(digest="sha256:" + "0" * 64)
        with self.assertRaises(InstallerError) as raised:
            run(cluster)
        self.assertIn("not the image this installer was tested with", raised.exception.message)
        self.assertNotIn("create-repository", cluster.events)

    def test_draft_needs_allow_draft_and_then_warns(self):
        with self.assertRaises(InstallerError):
            run(GuardCluster(), guardrail_version="DRAFT")
        _, text, _ = run(GuardCluster(), guardrail_version="DRAFT", allow_draft="true")
        self.assertIn("WARNING: the DRAFT version", text)


class VerifyTests(Base):
    def failure(self, cluster):
        with self.assertRaises(InstallerError) as raised:
            run(cluster)
        return raised.exception

    def test_a_pod_with_another_identity_fails(self):
        error = self.failure(GuardCluster(identity="arn:aws:sts::123456789012:assumed-role/some-node-role/i-123"))
        self.assertIn("does not run as the role acme-addon-bedrock-guardrail-role", error.message)

    def test_no_credentials(self):
        error = self.failure(GuardCluster(chat="STATUS 200\nANSWER The safety check is temporarily unavailable.",
                                          logs="botocore.exceptions.NoCredentialsError: Unable to locate credentials"))
        self.assertIn("did not return the model's answer", error.message)
        self.assertIn("not annotated", error.hint)

    def test_access_denied(self):
        error = self.failure(GuardCluster(chat="STATUS 500\nBODY error", logs="AccessDeniedException: not authorized to perform bedrock:ApplyGuardrail"))
        self.assertIn("ApplyGuardrail", error.hint)

    def test_the_troubleshooting_map(self):
        cases = {
            '"POST /?api-version=2024-02-01 HTTP/1.1" 404': "without a path",
            "NoCredentialsError": "not annotated",
            "AccessDeniedException": "ApplyGuardrail",
            "NoRegionError": "BEDROCK_REGION",
            "ResourceNotFoundException": "Wrong guardrail id or version",
            "ValidationException": "Wrong guardrail id or version",
        }
        for text, expected in cases.items():
            self.assertTrue(any(expected in reason for reason in hooks.diagnose(text)), text)
        self.assertEqual(hooks.diagnose("all good"), [])

    def test_the_chat_check_uses_an_attached_deployment_and_cores_own_key_inside_the_cluster(self):
        cluster = GuardCluster()
        ctx, _, _ = run(cluster, guardrail_deployments="default.model")
        started = next(c for c in ctx.cloud.runner.calls if "run" in c and "dial-guardrail-check" in c)
        overrides = json.loads(started[started.index("--overrides") + 1])
        env = {e["name"]: e["value"] for e in overrides["spec"]["containers"][0]["env"]}
        self.assertEqual(env["DEPLOYMENT"], "default.model")
        self.assertEqual(env["CORE"], "http://dial-core.dial.svc.cluster.local")
        self.assertNotIn(ctx.secret["core_admin_api_key"], json.dumps(started))           # the key is read inside the pod


class UpgradeAndRemoveTests(Base):
    def installed_cluster(self, **given):
        cluster = GuardCluster()
        run(cluster, **given)
        cluster.events.clear()
        return cluster

    def test_same_settings_only_checks_again(self):
        cluster = self.installed_cluster()
        _, text, changed = run(cluster, action="upgrade")
        self.assertFalse(changed)
        self.assertEqual(cluster.events, ["identity check", "chat check"])

    def test_changing_where_it_is_attached_updates_core(self):
        cluster = self.installed_cluster()
        run(cluster, action="upgrade", guardrail_global="true")
        self.assertEqual(core_config(cluster)["globalInterceptors"], [ID])
        self.assertIn("restart core", cluster.events)

    def test_the_registration_survives_a_platform_upgrade(self):
        cluster = self.installed_cluster()
        ctx = make_ctx(responses=cluster.responses())
        deploy.prepare(ctx)
        config = json.loads(placeholders._core_config(ctx))
        self.assertEqual(config["models"]["default.model"]["interceptors"], [ID])
        self.assertEqual(config["interceptors"][ID]["endpoint"], ENDPOINT)

    def test_remove_detaches_it_from_core_first_then_deletes_everything(self):
        cluster = self.installed_cluster()
        cluster.repos.add("naya-dial-x-bedrock-guardrail-interceptor")
        _, _, left = run(cluster, action="remove")
        self.assertEqual(cluster.events[:3], ["helm platform", "restart core", "helm uninstall dial-addon-bedrock-guardrail"])
        config = core_config(cluster)
        self.assertNotIn("interceptors", config)
        self.assertNotIn("interceptors", config["models"]["default.model"])
        self.assertEqual((left, cluster.node_stacks, cluster.releases), ([], {}, {}))
        self.assertNotIn("naya-dial-x-bedrock-guardrail-interceptor", cluster.repos)

    def test_uninstall_of_the_platform_deletes_its_role_stack(self):
        cluster = self.installed_cluster()
        ctx = make_ctx(responses=cluster.responses())
        uninstaller = uninstall.Uninstaller(ctx, lambda _m: None)
        self.assertIn(ID, uninstaller.addon_node_groups())
        uninstaller.addon_stacks()
        self.assertEqual(cluster.node_stacks, {})


class CommandTests(Base):
    def test_the_template_passes_cfn_lint_when_it_is_installed(self):
        import shutil
        if not shutil.which("cfn-lint"):
            self.skipTest("cfn-lint is not installed")
        result = subprocess.run(["cfn-lint", str(addons.catalog_dir() / "stacks" / "bedrock-guardrail-role.yaml"), "--regions", "us-east-1"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
