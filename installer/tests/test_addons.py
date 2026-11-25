"""Add-ons: catalog, rendering, install / upgrade / remove against a scripted fake of AWS, Kubernetes and Helm."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from helpers import Base, HELM_DIR, RELEASE, ROOT, CHART_VALUES_TEXT, make_ctx, ok_json
from dial_install import addons, cli, deploy, tools, uninstall, workflow
from dial_install.errors import InstallerError
from dial_install.runner import FakeRunner, Result
from dial_install.values import placeholders

HAVE_HELM = subprocess.run(["which", "helm"], capture_output=True).returncode == 0
PLATFORM_CHART_NAME = f"naya-helm-deployment-{RELEASE['chart']['version']}"
SCHEMA_ID = "https://DialAIBrain"
REGISTRY_TAGS = ["2026.11.1", "sha-781d641", "2026.11.3", "2026.11.2", "sha-54a7ea9"]       # what `crane ls` shows (not in order)
LATEST = "2026.11.3"
APP = "dial-x-rag"


class Cluster:
    """A tiny stateful fake of the cluster: Helm releases and their values, ECR repositories, and an event log."""

    def __init__(self, platform_chart=PLATFORM_CHART_NAME, addons_installed=None, repos=None):
        self.platform_chart = platform_chart
        self.releases = {}                    # release name -> values (add-ons)
        self.platform_core_config = []        # core config JSON of every platform upgrade, in order
        self.repos = set(repos or [])
        self.copied = []
        self.events = []
        self.node_stacks = {}                 # stack name -> parameters of the last deploy
        self.tags = list(REGISTRY_TAGS)
        for name, values in (addons_installed or {}).items():
            self.releases[f"dial-addon-{name}"] = values

    def responses(self):
        return {
            ("aws", "ecr", "get-login-password"): Result(0, "token"),
            ("aws", "ec2", "describe-instance-type-offerings"): ok_json({"InstanceTypeOfferings": [{"InstanceType": "x"}]}),
            ("aws", "cloudformation", "deploy"): self.cfn_deploy,
            ("aws", "cloudformation", "describe-stacks"): self.cfn_describe,
            ("aws", "cloudformation", "delete-stack"): self.cfn_delete,
            ("/usr/bin/crane", "ls"): lambda argv: Result(0, "\n".join(self.tags)),
            ("aws", "cognito-idp", "describe-user-pool-client"): ok_json({"UserPoolClient": {"ClientSecret": "cognito-secret"}}),
            ("aws", "ecr", "describe-repositories"): self.describe_repositories,
            ("aws", "ecr", "list-tags-for-resource"): ok_json({"tags": [{"Key": "dial-install:installation", "Value": "acme"}]}),
            ("aws", "ecr", "create-repository"): self.create_repository,
            ("aws", "ecr", "delete-repository"): self.delete_repository,
            ("/usr/bin/crane", "digest"): lambda argv: Result(0, "sha256:" + "a" * 64) if argv[2].startswith("458490025663") or argv[2] in self.copied else Result(1, "", "MANIFEST_UNKNOWN"),
            ("/usr/bin/crane", "copy"): self.crane_copy,
            ("helm", "registry", "login"): Result(0),
            ("helm", "show", "values"): Result(0, CHART_VALUES_TEXT),
            ("helm", "list"): self.helm_list,
            ("helm", "get", "values"): self.helm_get_values,
            ("helm", "upgrade"): self.helm_upgrade,
            ("helm", "uninstall"): self.helm_uninstall,
            ("kubectl",): self.kubectl,
        }

    # ---- helpers -------------------------------------------------------------------------------------------
    def cfn_deploy(self, argv):
        stack = argv[argv.index("--stack-name") + 1]
        overrides = argv[argv.index("--parameter-overrides") + 1:]
        self.node_stacks[stack] = dict(item.split("=", 1) for item in overrides if "=" in item)
        self.events.append(f"node group {stack}")
        return Result(0)

    def cfn_describe(self, argv):
        stack = argv[argv.index("--stack-name") + 1]
        if stack not in self.node_stacks:
            return Result(255, "", "does not exist")
        return ok_json({"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": [{"Key": "dial-install:addon", "Value": "x"}]}]})

    def cfn_delete(self, argv):
        self.node_stacks.pop(argv[argv.index("--stack-name") + 1], None)
        self.events.append("delete node group")
        return Result(0)

    def describe_repositories(self, argv):
        if "--repository-names" not in argv:
            return Result(0, json.dumps({"repositories": []}))
        name = argv[argv.index("--repository-names") + 1]
        if name not in self.repos:
            return Result(255, "", "RepositoryNotFoundException")
        return ok_json({"repositories": [{"repositoryName": name, "repositoryArn": f"arn:aws:ecr:us-east-1:123456789012:repository/{name}"}]})

    def create_repository(self, argv):
        self.repos.add(argv[argv.index("--repository-name") + 1])
        self.events.append("create-repository")
        return Result(0)

    def delete_repository(self, argv):
        self.repos.discard(argv[argv.index("--repository-name") + 1])
        self.events.append("delete-repository")
        return Result(0)

    def crane_copy(self, argv):
        self.copied.append(argv[3])
        self.events.append(f"copy {argv[2]} -> {argv[3]}")
        return Result(0)

    def helm_list(self, argv):
        pattern = argv[argv.index("--filter") + 1]
        if pattern.startswith("^dial-addon-"):
            rows = [{"name": name, "chart": "dial-extension-2.1.0", "revision": "1", "status": "deployed"} for name in sorted(self.releases)]
        else:
            rows = [{"name": "dial", "chart": self.platform_chart, "revision": "3", "status": "deployed"}] if self.platform_chart else []
        return Result(0, json.dumps(rows))

    def helm_get_values(self, argv):
        return Result(0, json.dumps(self.releases.get(argv[3], {"global": {}})))

    def helm_upgrade(self, argv):
        values = yaml.safe_load(Path(argv[argv.index("--values") + 1]).read_text())      # the file exists only during the call
        release = argv[3]
        if release.startswith("dial-addon-"):
            self.releases[release] = values
            self.events.append(f"helm addon {release}")
        else:
            self.platform_core_config.append(values)
            self.events.append("helm platform")
        return Result(0)

    def helm_uninstall(self, argv):
        self.releases.pop(argv[2], None)
        self.events.append(f"helm uninstall {argv[2]}")
        return Result(0)

    def kubectl(self, argv):
        if "restart" in argv:
            self.events.append("restart core")
        return Result(0)


def addon_values(version="2026.11.1", schemas=None):
    return {"addon": {"id": "ai-brain", "version": version, "applicationTypeSchemas": schemas if schemas is not None else [{"$id": SCHEMA_ID, "x": 1}]}}


def tools_patch():
    return mock.patch.object(tools, "which", side_effect=lambda tool: f"/usr/bin/{tool}")


def new_ctx(cluster, **kw):
    ctx = make_ctx(responses=cluster.responses(), **kw)
    ctx.cache["license_helper_needed"] = False
    return ctx


class CatalogTests(Base):
    def test_the_catalog_is_valid_and_lists_ai_brain(self):
        catalog = addons.load_catalog()
        self.assertIn("ai-brain", catalog)
        entry = catalog["ai-brain"]
        self.assertEqual(entry["images"][0]["repository"], "naya-dial-ai-brain")
        self.assertEqual(entry["application_types"][0]["id"], SCHEMA_ID)

    def test_an_unknown_add_on_names_the_ones_that_exist(self):
        with self.assertRaises(InstallerError) as raised:
            addons.entry_for("nope")
        self.assertIn("ai-brain", raised.exception.hint)

    def test_every_catalog_entry_renders_without_unknown_tokens_and_leaves_none_behind(self):
        ctx = make_ctx()
        for name, entry in addons.load_catalog().items():
            document = addons.render_values(ctx, entry)
            self.assertNotIn("PLACEHOLDER_", json.dumps(document), name)

    def test_values_point_at_the_customers_registry_and_keep_the_service_private(self):
        ctx = new_ctx(Cluster())
        with tools_patch():
            entry = addons.pin(ctx, addons.entry_for("ai-brain"))
        document = addons.render_values(ctx, entry)
        self.assertEqual(document["image"]["registry"], "123456789012.dkr.ecr.us-east-1.amazonaws.com")
        self.assertEqual(document["image"]["tag"], "2026.11.3")
        self.assertEqual(document["fullnameOverride"], "dial-brain")
        self.assertFalse(document["ingress"]["enabled"])
        self.assertEqual(document["env"]["DIAL_CORE_URL"], "http://dial-core.dial.svc.cluster.local")
        self.assertNotIn("secrets", document)

    def test_with_the_public_registry_provider_the_cluster_pulls_from_naya(self):
        from helpers import resolved
        ctx = make_ctx(cfg=resolved(registry={"provider": "public"}))
        self.assertEqual(addons.render_values(ctx, addons.entry_for("ai-brain"))["image"]["registry"], "458490025663.dkr.ecr.us-east-1.amazonaws.com")

    def test_the_registered_schema_is_the_one_the_add_on_owner_specified(self):
        schema = addons.render_values(make_ctx(), addons.entry_for("ai-brain"))["addon"]["applicationTypeSchemas"][0]
        base = "http://dial-brain.dial.svc.cluster.local"
        self.assertEqual(schema, {
            "$schema": "https://dial.epam.com/application_type_schemas/schema#",
            "$id": SCHEMA_ID,
            "dial:applicationTypeDisplayName": "AI Brain",
            "dial:applicationTypeCompletionEndpoint": f"{base}/openai/deployments/brain/chat/completions",
            "dial:appendApplicationPropertiesHeader": False,
            "dial:applicationTypeInterceptors": [],
            "dial:applicationTypeAssistantAttachmentsInRequestSupported": False,
            "dial:applicationTypeSchemaEndpoint": f"{base}/v1/configuration-support/application-schema",
        })

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_the_generic_chart_accepts_the_values_and_renders_a_private_service(self):
        ctx = make_ctx()
        document = addons.render_values(ctx, addons.entry_for("ai-brain"))
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
            yaml.safe_dump(document, handle)
            handle.flush()
            chart = addons.catalog_dir() / addons.entry_for("ai-brain")["chart"]
            result = subprocess.run(["helm", "template", "dial-addon-ai-brain", str(chart), "--namespace", "dial", "-f", handle.name],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        self.assertEqual(sorted(d["kind"] for d in docs if d["kind"] != "NetworkPolicy"), ["Deployment", "Service", "ServiceAccount"])
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(deployment["metadata"]["name"], "dial-brain")
        self.assertEqual(container["image"], f"123456789012.dkr.ecr.us-east-1.amazonaws.com/naya-dial-ai-brain:{addons.entry_for('ai-brain')['version']}")
        self.assertEqual(container["ports"][0]["containerPort"], 5000)
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/health")

    def test_the_chart_in_the_catalog_is_the_one_the_platform_uses(self):
        self.assertEqual((addons.catalog_dir() / "charts" / "dial-extension-2.1.0.tgz").read_bytes(),
                         (HELM_DIR / "dial-platform" / "charts" / "dial-extension-2.1.0.tgz").read_bytes())


class CoreConfigTests(Base):
    def schemas(self, ctx):
        return [s["$id"] for s in json.loads(placeholders._core_config(ctx))["applicationTypeSchemas"]]

    def test_the_base_application_types_stay_and_the_add_ons_are_appended(self):
        ctx = make_ctx()
        base = self.schemas(ctx)
        ctx.cache["addon_schemas"] = [{"$id": SCHEMA_ID}]
        self.assertEqual(self.schemas(ctx), base + [SCHEMA_ID])

    def test_an_add_on_cannot_replace_a_base_application_type(self):
        ctx = make_ctx()
        base = self.schemas(ctx)
        ctx.cache["addon_schemas"] = [{"$id": base[0], "evil": True}]
        self.assertEqual(self.schemas(ctx), base)

    def test_prepare_reads_the_registrations_of_the_installed_add_ons(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values()})
        ctx = new_ctx(cluster)
        deploy.prepare(ctx)
        self.assertEqual([s["$id"] for s in ctx.cache["addon_schemas"]], [SCHEMA_ID])
        self.assertIn(SCHEMA_ID, self.schemas(ctx))

    def test_a_platform_upgrade_keeps_the_add_on_registered(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values()})
        ctx = new_ctx(cluster)
        deploy.prepare(ctx)
        self.assertIn(SCHEMA_ID, json.dumps(deploy.render(ctx)))

    def test_without_add_ons_the_core_config_is_what_it_was(self):
        cluster = Cluster()
        ctx = new_ctx(cluster)
        deploy.prepare(ctx)
        self.assertEqual(ctx.cache["addon_schemas"], [])
        self.assertNotIn(SCHEMA_ID, json.dumps(deploy.render(ctx)))


class InstallTests(Base):
    def run_install(self, cluster, **kw):
        ctx = new_ctx(cluster, **kw)
        lines = []
        with tools_patch(), mock.patch("time.sleep"):
            addons.install(ctx, "ai-brain", lines.append)
        return ctx, "\n".join(lines)

    def test_install_copies_deploys_registers_and_restarts_core_in_that_order(self):
        cluster = Cluster()
        ctx, text = self.run_install(cluster)
        version = LATEST
        self.assertEqual(cluster.events, [
            "create-repository",
            f"copy 458490025663.dkr.ecr.us-east-1.amazonaws.com/naya-dial-ai-brain:{version} -> 123456789012.dkr.ecr.us-east-1.amazonaws.com/naya-dial-ai-brain:{version}",
            "helm addon dial-addon-ai-brain", "helm platform", "restart core"])
        self.assertEqual(cluster.releases["dial-addon-ai-brain"]["addon"]["version"], version)
        self.assertIn(SCHEMA_ID, json.dumps(cluster.platform_core_config[-1]))      # Core's configuration now has the type
        self.assertIn("AI Brain", text)

    def test_the_source_registry_is_logged_in_with_the_customers_own_credentials(self):
        cluster = Cluster()
        ctx, _ = self.run_install(cluster)
        logins = ctx.cloud.runner.called("aws", "ecr", "get-login-password")
        self.assertTrue(logins and all("--region" in argv and argv[argv.index("--region") + 1] == "us-east-1" for argv in logins))

    def test_nothing_runs_helm_with_wait_and_everything_has_context_and_region(self):
        cluster = Cluster()
        ctx, _ = self.run_install(cluster)
        for argv in ctx.cloud.runner.calls:
            if argv[0] == "helm" and argv[1] not in ("registry", "show", "version", "template"):
                self.assertNotIn("--wait", argv)
                self.assertIn("--kube-context", argv)
            if argv[0] == "kubectl":
                self.assertEqual(argv[1], "--context")
            if argv[0] == "aws":
                self.assertIn("--region", argv)

    def test_the_values_file_holds_no_secret_and_is_not_left_behind(self):
        cluster = Cluster()
        ctx, _ = self.run_install(cluster)
        helm = next(a for a in ctx.cloud.runner.writes if a[:2] == ["helm", "upgrade"] and a[3] == "dial-addon-ai-brain")
        self.assertFalse(Path(helm[helm.index("--values") + 1]).exists())
        self.assertEqual(helm[4], str(addons.catalog_dir() / "charts" / "dial-extension-2.1.0.tgz"))

    def test_an_installed_add_on_is_not_installed_twice(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values()})
        with self.assertRaises(InstallerError) as raised:
            self.run_install(cluster)
        self.assertIn("add-ons upgrade ai-brain", raised.exception.hint)
        self.assertEqual(cluster.events, [])

    def test_it_never_moves_the_platform_to_another_release(self):
        cluster = Cluster(platform_chart="naya-helm-deployment-2026.10.3")
        with self.assertRaises(InstallerError) as raised:
            self.run_install(cluster)
        self.assertIn("upgrade", raised.exception.hint)
        self.assertEqual(cluster.events, [])

    def test_without_the_platform_it_asks_to_run_apply_first(self):
        cluster = Cluster(platform_chart=None)
        with self.assertRaises(InstallerError) as raised:
            self.run_install(cluster)
        self.assertIn("apply", raised.exception.hint)

    def test_a_missing_image_stops_before_anything_is_deployed(self):
        cluster = Cluster()
        responses = cluster.responses()
        responses[("/usr/bin/crane", "copy")] = Result(1, "", "denied: not authorized")
        ctx = make_ctx(responses=responses)
        with tools_patch(), mock.patch("time.sleep"), self.assertRaises(InstallerError) as raised:
            addons.install(ctx, "ai-brain", lambda _m: None)
        self.assertIn("458490025663", raised.exception.hint)
        self.assertEqual(cluster.releases, {})
        self.assertEqual(cluster.platform_core_config, [])

    def test_dry_run_changes_nothing(self):
        cluster = Cluster()
        ctx = new_ctx(cluster, dry_run=True)
        with tools_patch():
            addons.install(ctx, "ai-brain", lambda _m: None)
        self.assertEqual(cluster.events, [])
        self.assertEqual(cluster.releases, {})
        self.assertTrue(ctx.cloud.runner.writes)


class PublicSourceRegistryTests(Base):
    """Amazon ECR Public as the source: anyone may pull, so the installer logs in to nothing but the customer's own registry."""

    def public_entry(self):
        entry = copy.deepcopy(addons.entry_for("ai-brain"))
        entry["source"]["registry"] = "public.ecr.aws/naya"
        return entry

    def test_no_login_is_made_to_a_public_source_and_the_target_still_is(self):
        from dial_install import mirror
        ctx = new_ctx(Cluster())
        directory = mirror._docker_config(ctx, ["123456789012.dkr.ecr.us-east-1.amazonaws.com", "public.ecr.aws"])
        try:
            auths = json.loads((directory / "config.json").read_text())["auths"]
        finally:
            import shutil
            shutil.rmtree(directory)
        self.assertEqual(list(auths), ["123456789012.dkr.ecr.us-east-1.amazonaws.com"])
        self.assertEqual(len(ctx.cloud.runner.called("aws", "ecr", "get-login-password")), 1)

    def test_the_newest_version_and_the_copy_work_with_a_public_source(self):
        cluster = Cluster()
        ctx = new_ctx(cluster)
        entry = self.public_entry()
        with tools_patch():
            pinned = addons.pin(ctx, entry)
            addons.copy_images(ctx, pinned, lambda _m: None)
        self.assertEqual(pinned["version"], LATEST)
        self.assertEqual(ctx.cloud.runner.called("/usr/bin/crane", "ls")[0][2], "public.ecr.aws/naya/naya-dial-ai-brain")
        self.assertEqual(len(ctx.cloud.runner.called("aws", "ecr", "get-login-password")), 1)        # only the customer's registry, for the copy
        self.assertTrue(any(e.startswith(f"copy public.ecr.aws/naya/naya-dial-ai-brain:{LATEST} -> 123456789012") for e in cluster.events))


class UpgradeTests(Base):
    def run_upgrade(self, cluster):
        ctx = new_ctx(cluster)
        lines = []
        with tools_patch(), mock.patch("time.sleep"):
            changed = addons.upgrade(ctx, "ai-brain", lines.append)
        return ctx, changed, "\n".join(lines)

    def test_same_version_is_a_noop(self):
        version = LATEST
        cluster = Cluster(addons_installed={"ai-brain": addon_values(version)})
        _, changed, text = self.run_upgrade(cluster)
        self.assertFalse(changed)
        self.assertIn("Nothing to do", text)
        self.assertEqual(cluster.events, [])

    def test_a_new_version_is_copied_and_deployed_without_touching_core_when_the_registration_is_the_same(self):
        ctx = make_ctx()
        same = addons.render_values(ctx, addons.entry_for("ai-brain"))["addon"]["applicationTypeSchemas"]
        cluster = Cluster(addons_installed={"ai-brain": addon_values("2026.11.1", same)}, repos={"naya-dial-ai-brain"})
        _, changed, _ = self.run_upgrade(cluster)
        self.assertTrue(changed)
        self.assertEqual([e.split()[0] for e in cluster.events], ["copy", "helm"])
        self.assertEqual(cluster.releases["dial-addon-ai-brain"]["addon"]["version"], LATEST)

    def test_a_changed_registration_updates_core_too(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values("2026.11.1", [{"$id": SCHEMA_ID, "old": True}])}, repos={"naya-dial-ai-brain"})
        _, _, _ = self.run_upgrade(cluster)
        self.assertIn("restart core", cluster.events)

    def test_not_installed_says_how_to_install(self):
        with self.assertRaises(InstallerError) as raised:
            self.run_upgrade(Cluster())
        self.assertIn("add-ons install ai-brain", raised.exception.hint)


class RemoveTests(Base):
    def run_remove(self, cluster, **kw):
        ctx = new_ctx(cluster)
        lines = []
        with tools_patch(), mock.patch("time.sleep"):
            left = addons.remove(ctx, "ai-brain", lines.append, **kw)
        return ctx, left, "\n".join(lines)

    def test_remove_unregisters_first_then_removes_the_release_then_the_images(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values()}, repos={"naya-dial-ai-brain"})
        ctx, left, _ = self.run_remove(cluster)
        self.assertEqual(cluster.events, ["helm platform", "restart core", "helm uninstall dial-addon-ai-brain", "delete-repository"])
        self.assertNotIn(SCHEMA_ID, json.dumps(cluster.platform_core_config[-1]))       # Core no longer knows the type
        self.assertEqual((left, cluster.releases, cluster.repos), ([], {}, set()))

    def test_keep_images_leaves_the_repository(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values()}, repos={"naya-dial-ai-brain"})
        _, left, _ = self.run_remove(cluster, keep_images=True)
        self.assertEqual((left, cluster.repos), ([], {"naya-dial-ai-brain"}))

    def test_other_add_ons_stay_registered(self):
        other = {"addon": {"id": "other", "version": "1", "applicationTypeSchemas": [{"$id": "https://Other"}]}}
        cluster = Cluster(addons_installed={"ai-brain": addon_values(), "other": other}, repos={"naya-dial-ai-brain"})
        self.run_remove(cluster)
        self.assertIn("https://Other", json.dumps(cluster.platform_core_config[-1]))
        self.assertIn("dial-addon-other", cluster.releases)

    def test_removing_twice_is_harmless(self):
        cluster = Cluster()
        _, left, text = self.run_remove(cluster)
        self.assertEqual(left, [])
        self.assertEqual(cluster.events, [])
        self.assertIn("not installed", text)

    def test_an_interrupted_remove_can_be_run_again(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values()}, repos={"naya-dial-ai-brain"})
        cluster.releases["dial-addon-ai-brain"] = addon_values(schemas=[])      # already unregistered, release still there
        _, left, _ = self.run_remove(cluster)
        self.assertEqual((left, cluster.releases, cluster.repos), ([], {}, set()))

    def test_a_repository_of_another_installation_is_not_deleted(self):
        cluster = Cluster(repos={"naya-dial-ai-brain"})
        responses = cluster.responses()
        responses[("aws", "ecr", "list-tags-for-resource")] = ok_json({"tags": [{"Key": "dial-install:installation", "Value": "someone-else"}]})
        ctx = make_ctx(responses=responses)
        with tools_patch():
            addons.remove(ctx, "ai-brain", lambda _m: None)
        self.assertEqual(cluster.repos, {"naya-dial-ai-brain"})

    def test_uninstall_of_the_platform_removes_the_add_on_releases_first(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values()})
        ctx = new_ctx(cluster)
        uninstall.Uninstaller(ctx, lambda _m: None).cluster_workloads()
        self.assertEqual(cluster.events[0], "helm uninstall dial-addon-ai-brain")
        calls = [c for c in ctx.cloud.runner.calls if c[:2] == ["helm", "uninstall"]]
        self.assertEqual(calls[0][2], "dial-addon-ai-brain")
        self.assertEqual(calls[1][2], "dial")


class ListTests(Base):
    def test_status_tells_installed_and_available_upgrades(self):
        cluster = Cluster(addons_installed={"ai-brain": addon_values("2026.11.1")})
        with tools_patch():
            rows = addons.rows(new_ctx(cluster))
            self.assertIn(f"installed 2026.11.1 (upgrade to {LATEST} available)", rows[0]["status"])
            self.assertEqual(rows[0]["version"], LATEST)
            self.assertEqual(addons.rows(new_ctx(Cluster()))[0]["status"], "not installed")

    def test_without_a_cluster_it_lists_the_catalog_only(self):
        self.assertIsNone(addons.rows(None)[0]["status"])


class CommandTests(Base):
    def options(self, addon=None, **kw):
        return workflow.Options(config_path=ROOT / "examples" / "dial.minimal.yaml", addon=addon, **kw)

    def test_the_command_line_accepts_the_examples_of_the_task(self):
        parser = cli._parser()
        self.assertEqual(parser.parse_args(["add-ons", "list"]).addon_action, "list")
        args = parser.parse_args(["add-ons", "install", "ai-brain", "-y"])
        self.assertEqual((args.addon, args.yes), ("ai-brain", True))
        self.assertEqual(parser.parse_args(["addons", "upgrade", "ai-brain"]).addon_action, "upgrade")
        self.assertTrue(parser.parse_args(["add-ons", "remove", "ai-brain", "--keep-images"]).keep_images)
        self.assertEqual(parser.parse_args(["add-ons", "install", "ai-rag", "--model", "m"]).model, "m")
        self.assertEqual(parser.parse_args(["add-ons", "upgrade", "ai-rag", "--model", "m"]).model, "m")
        args = parser.parse_args(["add-ons", "install", "ai-rag", "--version", "2026.11.1", "--instance-type", "c6i.2xlarge", "--nodes", "2", "--no-dedicated-nodes"])
        self.assertEqual((args.addon_version, args.instance_type, args.nodes, args.no_dedicated_nodes), ("2026.11.1", "c6i.2xlarge", "2", True))

    def test_list_works_without_any_configuration(self):
        lines = []
        code = workflow.cmd_addons_list(workflow.Options(config_path=Path("/nonexistent/dial.yaml")), FakeRunner(), lines.append)
        self.assertEqual(code, 0)
        self.assertIn("ai-brain", "\n".join(lines))

    def test_an_unknown_add_on_fails_before_touching_anything(self):
        runner = FakeRunner()
        with self.assertRaises(InstallerError):
            workflow.cmd_addons_install(self.options("nope"), runner, lambda _m: None)
        self.assertEqual(runner.calls, [])


RAG_APPLICATION = {
    "displayName": "DIAL X RAG",
    "endpoint": "http://dial-rag.dial.svc.cluster.local/openai/deployments/dial-rag/chat/completions",
    "iconUrl": "https://themes.dial.acme.example/RAG_files_search.svg",
    "userRoles": [],
    "inputAttachmentTypes": ["*/*"],
    "maxRetryAttempts": 1,
    "features": {
        "system_prompt_supported": True, "tools_supported": True, "seed_supported": False, "url_attachments_supported": True,
        "folder_attachments_supported": True, "allow_resume": True, "accessible_by_per_request_key": True,
        "content_parts_supported": True, "temperature_supported": True, "parallel_tool_calls_supported": True,
        "assistant_attachments_in_request_supported": True, "max_tokens_supported": True, "custom_temperature_supported": True,
    },
}


class RagTests(Base):
    """RAG is an add-on, not part of the base platform."""

    def install_rag(self, cluster, **given):
        ctx = new_ctx(cluster)
        with tools_patch(), mock.patch("time.sleep"):
            addons.install(ctx, "ai-rag", lambda _m: None, given or None)
        return ctx

    def cluster_after(self, cluster, **given):
        self.install_rag(cluster, **given)
        return cluster

    def values_of(self, cluster):
        return cluster.releases["dial-addon-ai-rag"]

    def models(self, values):
        env = values["env"]
        return {env["CHAT_MODEL"], env["DIAL_RAG__REQUEST__QA_CHAIN__QUERY_CHAIN__LLM__DEPLOYMENT_NAME"],
                env["DIAL_RAG__REQUEST__QA_CHAIN__CHAT_CHAIN__LLM__DEPLOYMENT_NAME"]}

    def test_the_default_is_the_system_default_model_in_all_three_places(self):
        values = self.values_of(self.cluster_after(Cluster()))
        self.assertEqual(self.models(values), {"default.model"})
        self.assertEqual(values["addon"]["options"]["model"], "default.model")

    def test_the_default_model_is_one_that_core_registers(self):
        registered = {m["key"] for m in RELEASE["models"]}
        self.assertIn(addons.entry_for("ai-rag")["options"]["model"]["default"], registered)

    def test_the_model_flag_changes_all_three(self):
        self.assertEqual(self.models(self.values_of(self.cluster_after(Cluster(), model="my.model"))), {"my.model"})

    def test_a_bad_value_is_refused_before_anything_runs(self):
        for bad in ({"model": "bad value; rm -rf"}, {"instance_type": "c6i xlarge"}, {"nodes": "0"}, {"nodes": "99"}):
            cluster = Cluster()
            with self.assertRaises(InstallerError, msg=str(bad)):
                self.install_rag(cluster, **bad)
            self.assertEqual(cluster.events, [])

    def test_a_setting_the_add_on_does_not_have_is_refused(self):
        with self.assertRaises(InstallerError) as raised:
            addons.resolve_options(addons.entry_for("ai-brain"), None, {"model": "x"})
        self.assertIn("no setting 'model'", raised.exception.message)
        with self.assertRaises(InstallerError):
            addons.resolve_options(addons.entry_for("ai-brain"), None, {"instance_type": "c6i.large"})

    def test_it_uses_the_internal_api_key_of_the_installation_and_the_naya_registry(self):
        cluster = Cluster()
        ctx = self.install_rag(cluster)
        self.assertEqual(self.values_of(cluster)["env"]["DIAL_API_KEY"], ctx.secret["dial_api_key"])
        copy = next(e for e in cluster.events if e.startswith("copy"))
        self.assertIn("458490025663.dkr.ecr.us-east-1.amazonaws.com/naya-dial-ai-rag:" + LATEST, copy)

    def test_upgrade_keeps_the_chosen_settings_unless_new_ones_are_given(self):
        cluster = self.cluster_after(Cluster(), model="my.model", instance_type="c6i.2xlarge")
        state = addons.installed(new_ctx(cluster))["ai-rag"]
        entry = addons.entry_for("ai-rag")
        kept = addons.resolve_options(entry, state, None)
        self.assertEqual((kept["model"], kept["instance_type"]), ("my.model", "c6i.2xlarge"))
        self.assertEqual(addons.resolve_options(entry, state, {"model": "other"})["model"], "other")

    def test_changing_only_the_model_is_an_upgrade_and_the_same_settings_are_not(self):
        cluster = self.cluster_after(Cluster(), model="my.model")
        ctx = new_ctx(cluster)
        with tools_patch():
            entry = addons.pin(ctx, addons.entry_for("ai-rag"))
        state = addons.installed(ctx)["ai-rag"]
        self.assertTrue(addons.is_current(entry, state, None))
        self.assertTrue(addons.is_current(entry, state, {"model": "my.model"}))
        self.assertFalse(addons.is_current(entry, state, {"model": "other"}))
        with tools_patch(), mock.patch("time.sleep"):
            self.assertTrue(addons.upgrade(ctx, "ai-rag", lambda _m: None, {"model": "other"}))
        self.assertEqual(cluster.releases["dial-addon-ai-rag"]["env"]["CHAT_MODEL"], "other")

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_the_generic_chart_renders_it_like_the_old_subchart_did(self):
        ctx = new_ctx(Cluster())
        with tools_patch():
            entry = addons.pin(ctx, addons.entry_for("ai-rag"))
        document = addons.render_values(ctx, entry)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
            yaml.safe_dump(document, handle)
            handle.flush()
            result = subprocess.run(["helm", "template", "dial-addon-ai-rag", str(addons.catalog_dir() / entry["chart"]),
                                     "--namespace", "dial", "-f", handle.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(deployment["metadata"]["name"], "dial-rag")
        self.assertEqual(pod["nodeSelector"], {"workload": "rag"})
        self.assertEqual(pod["tolerations"], [{"key": "dedicated", "operator": "Equal", "value": "rag", "effect": "NoSchedule"}])
        self.assertEqual(container["ports"][0]["containerPort"], 5000)
        self.assertEqual(container["resources"]["requests"], {"cpu": "1000m", "memory": "4Gi"})
        self.assertEqual(deployment["spec"]["strategy"]["rollingUpdate"], {"maxSurge": 0, "maxUnavailable": 1})
        self.assertNotIn("Ingress", [d["kind"] for d in docs])


class NodeGroupTests(Base):
    def install(self, cluster, **given):
        ctx = new_ctx(cluster)
        with tools_patch(), mock.patch("time.sleep"):
            addons.install(ctx, "ai-rag", lambda _m: None, given or None)
        return ctx

    def test_the_node_group_exists_before_the_pod_is_deployed_and_has_the_defaults(self):
        cluster = Cluster()
        self.install(cluster)
        order = [e.split()[0] + " " + e.split()[1] for e in cluster.events if not e.startswith(("create-repo", "copy"))]
        self.assertEqual(order[:2], ["node group", "helm addon"])
        params = cluster.node_stacks["acme-addon-ai-rag"]
        self.assertEqual((params["InstanceType"], params["Nodes"], params["WorkloadLabel"]), ("c6i.xlarge", "1", "rag"))
        self.assertEqual(params["ClusterName"], "acme-cluster")
        self.assertEqual(params["PrivateSubnet1Id"], "subnet-prv1")

    def test_the_flags_override_the_defaults(self):
        cluster = Cluster()
        self.install(cluster, instance_type="c6i.2xlarge", nodes="2")
        params = cluster.node_stacks["acme-addon-ai-rag"]
        self.assertEqual((params["InstanceType"], params["Nodes"]), ("c6i.2xlarge", "2"))

    def test_only_the_add_on_runs_there_and_the_stack_is_marked_as_its_own(self):
        cluster = Cluster()
        ctx = self.install(cluster)
        values = cluster.releases["dial-addon-ai-rag"]
        self.assertEqual(values["nodeSelector"], {"workload": "rag"})
        self.assertEqual(values["tolerations"][0]["value"], "rag")
        deploy = next(c for c in ctx.cloud.runner.writes if c[:3] == ["aws", "cloudformation", "deploy"])
        self.assertIn("dial-install:addon=ai-rag", deploy)
        self.assertIn("dial-install:installation=acme", deploy)

    def test_without_dedicated_nodes_it_uses_the_shared_ones_and_creates_nothing(self):
        cluster = Cluster()
        self.install(cluster, dedicated_nodes="false")
        self.assertEqual(cluster.node_stacks, {})
        values = cluster.releases["dial-addon-ai-rag"]
        self.assertEqual(values["nodeSelector"], {"workload": "core"})
        self.assertNotIn("tolerations", values)

    def test_a_missing_instance_type_stops_before_anything_is_created(self):
        cluster = Cluster()
        responses = cluster.responses()
        responses[("aws", "ec2", "describe-instance-type-offerings")] = ok_json({"InstanceTypeOfferings": []})
        ctx = make_ctx(responses=responses)
        with tools_patch(), self.assertRaises(InstallerError) as raised:
            addons.install(ctx, "ai-rag", lambda _m: None, {"instance_type": "c9z.huge"})
        self.assertIn("--instance-type", raised.exception.hint)
        self.assertEqual(cluster.node_stacks, {})
        self.assertEqual(cluster.releases, {})

    def test_an_add_on_without_nodes_creates_no_stack(self):
        cluster = Cluster()
        ctx = new_ctx(cluster)
        with tools_patch(), mock.patch("time.sleep"):
            addons.install(ctx, "ai-brain", lambda _m: None)
        self.assertEqual(cluster.node_stacks, {})

    def test_switching_to_the_shared_nodes_removes_the_node_group_after_the_pod_moved(self):
        cluster = Cluster()
        self.install(cluster)
        ctx = new_ctx(cluster)
        with tools_patch(), mock.patch("time.sleep"):
            addons.upgrade(ctx, "ai-rag", lambda _m: None, {"dedicated_nodes": "false"})
        self.assertEqual(cluster.node_stacks, {})
        self.assertLess(cluster.events.index("helm addon dial-addon-ai-rag", 5), len(cluster.events) - 1)
        self.assertEqual(cluster.events[-1], "delete node group")

    def test_remove_deletes_the_node_group_and_the_check_at_the_end_is_clean(self):
        cluster = Cluster()
        self.install(cluster)
        ctx = new_ctx(cluster)
        with tools_patch():
            left = addons.remove(ctx, "ai-rag", lambda _m: None)
        self.assertEqual((left, cluster.node_stacks, cluster.releases, cluster.repos), ([], {}, {}, set()))
        self.assertLess(cluster.events.index("helm uninstall dial-addon-ai-rag"), cluster.events.index("delete node group"))

    def test_uninstalling_the_platform_deletes_the_node_group_before_the_cluster_stacks(self):
        cluster = Cluster()
        self.install(cluster)
        ctx = new_ctx(cluster)
        runner = ctx.cloud.runner
        runner.responses[("aws", "cloudformation", "describe-stacks")] = cluster.cfn_describe
        uninstaller = uninstall.Uninstaller(ctx, lambda _m: None)
        self.assertEqual(uninstaller.addon_node_groups(), ["ai-rag"])
        self.assertIn("node group of add-on ai-rag", "\n".join(uninstaller.plan()))
        uninstaller.addon_stacks()
        self.assertEqual(cluster.node_stacks, {})

    def test_the_template_is_valid_and_isolates_the_nodes(self):
        template = (addons.catalog_dir() / "stacks" / "node-group.yaml").read_text()
        for needed in ("NO_SCHEDULE", "dedicated", "workload", "MinSize: !Ref Nodes", "MaxSize: !Ref Nodes"):
            self.assertIn(needed, template)
        self.assertNotIn("NodegroupName", template)       # a generated name lets a changed instance type replace the group


class VersionTests(Base):
    def pin(self, tags, requested=None):
        cluster = Cluster()
        cluster.tags = tags
        ctx = new_ctx(cluster)
        with tools_patch():
            return addons.pin(ctx, addons.entry_for("ai-brain"), requested)["version"]

    def test_the_newest_version_is_looked_up_in_the_registry_now(self):
        self.assertEqual(self.pin(REGISTRY_TAGS), "2026.11.3")
        self.assertEqual(self.pin(REGISTRY_TAGS + ["2026.11.4"]), "2026.11.4")

    def test_versions_compare_as_numbers_not_as_text(self):
        self.assertEqual(self.pin(["2026.9.1", "2026.11.2", "2026.11.10", "2026.11.9"]), "2026.11.10")
        self.assertEqual(self.pin(["2026.12.1", "2027.1.1", "2026.9.30"]), "2027.1.1")

    def test_tags_that_are_not_versions_are_ignored(self):
        self.assertEqual(self.pin(["sha-abc1234", "latest", "2026.11.1-rc1", "2026.11.2"]), "2026.11.2")

    def test_no_version_at_all_is_a_clear_error(self):
        with self.assertRaises(InstallerError) as raised:
            self.pin(["sha-abc1234"])
        self.assertIn("--version", raised.exception.hint)

    def test_an_unreadable_registry_is_a_clear_error_and_never_an_old_version(self):
        cluster = Cluster()
        responses = cluster.responses()
        responses[("/usr/bin/crane", "ls")] = Result(1, "", "denied: not authorized")
        ctx = make_ctx(responses=responses)
        with tools_patch(), self.assertRaises(InstallerError) as raised:
            addons.pin(ctx, addons.entry_for("ai-brain"))
        self.assertIn("denied", raised.exception.message)

    def test_a_version_flag_pins_exactly_that_tag_without_asking_the_registry(self):
        cluster = Cluster()
        ctx = new_ctx(cluster)
        with tools_patch():
            self.assertEqual(addons.pin(ctx, addons.entry_for("ai-brain"), "2026.11.1")["version"], "2026.11.1")
        self.assertEqual(ctx.cloud.runner.called("/usr/bin/crane", "ls"), [])
        with self.assertRaises(InstallerError):
            addons.pin(ctx, addons.entry_for("ai-brain"), "bad tag!")

    def test_install_and_upgrade_use_the_version_flag(self):
        cluster = Cluster()
        ctx = new_ctx(cluster)
        with tools_patch(), mock.patch("time.sleep"):
            addons.install(ctx, "ai-brain", lambda _m: None, {"version": "2026.11.2"})
        self.assertEqual(cluster.releases["dial-addon-ai-brain"]["addon"]["version"], "2026.11.2")
        with tools_patch(), mock.patch("time.sleep"):
            addons.upgrade(ctx, "ai-brain", lambda _m: None)           # no flag: the newest
        self.assertEqual(cluster.releases["dial-addon-ai-brain"]["addon"]["version"], LATEST)

    def test_nothing_is_remembered_between_runs(self):
        first, second = Cluster(), Cluster()
        second.tags = ["2026.11.9"]
        with tools_patch():
            self.assertEqual(addons.pin(new_ctx(first), addons.entry_for("ai-brain"))["version"], LATEST)
            self.assertEqual(addons.pin(new_ctx(second), addons.entry_for("ai-brain"))["version"], "2026.11.9")


class RagRegistrationTests(Base):
    def install(self, cluster, **given):
        ctx = new_ctx(cluster)
        with tools_patch(), mock.patch("time.sleep"):
            addons.install(ctx, "ai-rag", lambda _m: None, given or None)
        return ctx

    def core_applications(self, cluster):
        values = cluster.platform_core_config[-1]
        return json.loads(values["dial"]["core"]["secrets"]["aidial.config.json"]).get("applications", {})

    def test_the_application_is_registered_in_cores_configuration_as_naya_defines_it(self):
        cluster = Cluster()
        self.install(cluster)
        self.assertEqual(self.core_applications(cluster), {APP: RAG_APPLICATION})

    def test_core_is_restarted_after_the_configuration_changed(self):
        cluster = Cluster()
        self.install(cluster)
        self.assertEqual(cluster.events[-2:], ["helm platform", "restart core"])

    def test_it_is_registered_without_access_for_anyone_until_an_administrator_adds_roles(self):
        cluster = Cluster()
        self.install(cluster)
        application = self.core_applications(cluster)[APP]
        self.assertEqual(application["userRoles"], [])         # present and empty, as DIAL Admin writes it; absent would mean public

    def test_only_feature_names_that_core_writes_itself_are_used(self):
        known = {"system_prompt_supported", "tools_supported", "seed_supported", "url_attachments_supported",
                 "folder_attachments_supported", "allow_resume", "accessible_by_per_request_key", "content_parts_supported",
                 "temperature_supported", "cache_supported", "auto_caching_supported", "parallel_tool_calls_supported",
                 "assistant_attachments_in_request_supported", "max_tokens_supported", "custom_temperature_supported"}
        features = addons.entry_for("ai-rag")["core"]["applications"][APP]["features"]
        self.assertFalse(set(features) - known, "a feature name that a real Core configuration does not contain")

    def test_the_endpoint_is_the_service_of_the_add_on(self):
        cluster = Cluster()
        self.install(cluster)
        self.assertEqual(self.core_applications(cluster)[APP]["endpoint"], "http://dial-rag.dial.svc.cluster.local/openai/deployments/dial-rag/chat/completions")

    def test_the_registration_survives_a_platform_upgrade(self):
        cluster = Cluster()
        self.install(cluster)
        ctx = new_ctx(cluster)
        deploy.prepare(ctx)
        built = json.loads(deploy.render(ctx)["dial"]["core"]["secrets"]["aidial.config.json"])
        self.assertEqual(built["applications"], {APP: RAG_APPLICATION})

    def test_without_add_ons_core_has_no_applications_section(self):
        ctx = new_ctx(Cluster())
        deploy.prepare(ctx)
        self.assertNotIn("applications", json.loads(placeholders._core_config(ctx)))

    def test_remove_unregisters_it_first(self):
        cluster = Cluster()
        self.install(cluster)
        ctx = new_ctx(cluster)
        with tools_patch():
            addons.remove(ctx, "ai-rag", lambda _m: None)
        self.assertEqual(self.core_applications(cluster), {})
        self.assertLess(len(cluster.platform_core_config), 3)
        platform = [i for i, e in enumerate(cluster.events) if e == "helm platform"]
        self.assertEqual(len(platform), 2)                      # one to register, one to unregister
        self.assertLess(platform[-1], cluster.events.index("helm uninstall dial-addon-ai-rag"))

    def test_the_other_add_ons_stay_registered_when_one_is_removed(self):
        cluster = Cluster()
        self.install(cluster)
        ctx = new_ctx(cluster)
        with tools_patch(), mock.patch("time.sleep"):
            addons.install(ctx, "ai-brain", lambda _m: None)
        ctx = new_ctx(cluster)
        with tools_patch():
            addons.remove(ctx, "ai-brain", lambda _m: None)
        self.assertEqual(self.core_applications(cluster), {APP: RAG_APPLICATION})

    def test_a_core_that_does_not_start_leaves_a_way_back(self):
        cluster = Cluster()
        responses = cluster.responses()
        responses[("kubectl",)] = lambda argv: Result(1, "", "timed out") if "status" in argv and "deployment/dial-core" in argv else cluster.kubectl(argv)
        ctx = make_ctx(responses=responses)
        ctx.cache["license_helper_needed"] = False
        with tools_patch(), mock.patch("time.sleep"), self.assertRaises(InstallerError) as raised:
            addons.install(ctx, "ai-rag", lambda _m: None)
        self.assertIn("helm rollback dial 3", raised.exception.hint)


class RagIsNotInTheBasePlatformTests(Base):
    def test_the_platform_chart_has_no_rag(self):
        chart = HELM_DIR / "dial-platform"
        values = yaml.safe_load((chart / "values.yaml").read_text())
        self.assertNotIn("rag", values)
        self.assertNotIn("rag", [d.get("alias", d["name"]) for d in yaml.safe_load((chart / "Chart.yaml").read_text())["dependencies"]])
        text = (chart / "values.yaml").read_text()
        for forbidden in ("naya-dial-rag", "naya-dial-ai-rag", "DIAL_RAG__", "workload: rag"):
            self.assertNotIn(forbidden, text)

    def test_the_cluster_has_no_rag_node_group(self):
        from dial_install import config
        self.assertNotIn("rag", config.NODE_GROUPS)
        self.assertNotIn("rag", yaml.safe_load((ROOT / "installer.yaml").read_text())["node_groups"])
        self.assertNotIn("Rag", (ROOT / "modules" / "compute" / "eks" / "stack.yaml").read_text())

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_no_rendered_platform_workload_is_rag(self):
        from helpers import PLATFORM_CHART
        from dial_install import values
        ctx = make_ctx()
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
            yaml.safe_dump(values.build(ctx, yaml.safe_load(CHART_VALUES_TEXT)), handle)
            handle.flush()
            rendered = subprocess.run(["helm", "template", "dial", str(PLATFORM_CHART), "-n", "dial", "-f", handle.name],
                                      capture_output=True, text=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertNotIn("rag", {d["metadata"]["name"].removeprefix("dial-") for d in yaml.safe_load_all(rendered.stdout)
                                 if d and d["kind"] == "Deployment"})
        self.assertNotIn("naya-dial-rag", rendered.stdout)


if __name__ == "__main__":
    unittest.main()
