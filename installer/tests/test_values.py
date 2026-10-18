"""The chart's placeholders and the installer's values must agree in both directions."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from helpers import BASE_CHART, Base, FAKE_OUTPUTS, PLATFORM_CHART, RELEASE, ROOT, example, make_ctx, resolved
from dial_install import charts, config, values
from dial_install.errors import InstallerError
from dial_install.values import ConflictError, merge, placeholders, substitute

CHART_VALUES = yaml.safe_load((PLATFORM_CHART / "values.yaml").read_text())
HAVE_HELM = shutil.which("helm") is not None


class SubstituteTests(Base):
    def test_find(self):
        node = {"a": "x PLACEHOLDER_ONE y", "b": ["PLACEHOLDER_TWO", {"c": "PLACEHOLDER_ONE"}], "d": 5}
        self.assertEqual(substitute.find(node), {"ONE", "TWO"})

    def test_whole_and_embedded_replacement(self):
        out = substitute.overrides({"u": "https://PLACEHOLDER_HOST/x", "v": "PLACEHOLDER_HOST"}, {"HOST": "h.example"})
        self.assertEqual(out, {"u": "https://h.example/x", "v": "h.example"})

    def test_only_changed_entries_are_returned(self):
        out = substitute.overrides({"keep": {"a": 1}, "env": {"fixed": "x", "var": "PLACEHOLDER_V"}}, {"V": "1"})
        self.assertEqual(out, {"env": {"var": "1"}})

    def test_list_is_all_or_nothing(self):
        out = substitute.overrides({"hosts": ["PLACEHOLDER_H", "static"], "other": ["a", "b"]}, {"H": "h"})
        self.assertEqual(out, {"hosts": ["h", "static"]})

    def test_token_followed_by_punctuation(self):
        out = substitute.overrides({"x": "http://PLACEHOLDER_REL-core.dial:80/PLACEHOLDER_REL"}, {"REL": "dial"})
        self.assertEqual(out["x"], "http://dial-core.dial:80/dial")

    def test_unresolved_names_are_all_reported(self):
        with self.assertRaises(substitute.UnresolvedPlaceholders) as raised:
            substitute.overrides({"a": "PLACEHOLDER_A", "b": "PLACEHOLDER_B"}, {"A": "1"})
        self.assertEqual(raised.exception.names, ["B"])

    def test_merge_conflict(self):
        with self.assertRaises(ConflictError):
            merge({"a": {"b": 1}}, {"a": {"b": 2}})


class ContractTests(Base):
    """chart values.yaml  <->  chart placeholders.yaml  <->  installer resolver table."""

    def chart_names(self):
        return substitute.find(CHART_VALUES)

    def declared(self, chart):
        data = yaml.safe_load((chart / "placeholders.yaml").read_text())
        self.assertEqual(data["apiVersion"], "dial-placeholders/v1")
        return data["placeholders"]

    def test_every_placeholder_in_the_chart_is_documented_and_nothing_else(self):
        self.assertEqual(self.chart_names(), set(self.declared(PLATFORM_CHART)))

    def test_every_declaration_is_complete(self):
        for name, spec in self.declared(PLATFORM_CHART).items():
            self.assertIn(spec["source"], {"config", "stack-output", "secret", "derived", "runtime", "constant"}, name)
            self.assertTrue(spec["description"].strip(), name)

    def test_installer_can_fill_every_placeholder_and_has_no_dead_entries(self):
        table = set(placeholders.table(make_ctx()))
        self.assertEqual(self.chart_names() - table, set(), "chart placeholders the installer cannot fill")
        self.assertEqual(table - self.chart_names(), set(), "installer fills placeholders the chart no longer has")

    def test_secret_placeholders_match_the_secret_keys(self):
        from dial_install import secrets
        keys = {k.name for k in secrets.REQUIRED_KEYS}
        for name, spec in self.declared(PLATFORM_CHART).items():
            if spec["source"] == "secret":
                key = placeholders._SECRET_ALIASES.get(name, name.lower())
                self.assertIn(key, keys, name)
        used = {placeholders._SECRET_ALIASES.get(n, n.lower()) for n in placeholders.SECRET_PLACEHOLDERS}
        self.assertEqual(keys - used, set(), "secret keys that no placeholder uses")

    def test_no_placeholder_is_left_after_building(self):
        built = values.build(make_ctx(), CHART_VALUES)
        self.assertEqual(substitute.find(built), set())
        self.assertNotIn("PLACEHOLDER", json.dumps(built))

    def test_chart_has_no_concrete_secret_defaults(self):
        sensitive = re.compile(r"(password|secret|token|api_?key|salt|signing)", re.I)
        for path, value in flatten(CHART_VALUES):
            leaf = re.split(r"[.\[]", path)[-1]
            if sensitive.search(leaf) and not leaf.lower().endswith(("secretname", "existingsecret")) and isinstance(value, str) and value:
                self.assertTrue("PLACEHOLDER" in value or "{{" in value or value.lower() in ("true", "false"),
                                f"{path} holds a concrete value in the chart; use a placeholder")

    def test_base_chart_contract(self):
        base_values = yaml.safe_load((BASE_CHART / "values.yaml").read_text())
        self.assertEqual(substitute.find(base_values), set(self.declared(BASE_CHART)))
        filled = charts.filled_values(BASE_CHART, {"NAMESPACE": "dial", "STORAGE_CLASS_IS_DEFAULT": "true"})
        self.assertEqual(filled, {"namespace": "dial", "storageClass": {"makeDefault": "true"}})

    def test_unknown_placeholder_in_a_future_chart_is_a_clear_error(self):
        future = {**CHART_VALUES, "newThing": "PLACEHOLDER_SOMETHING_NEW"}
        with self.assertRaises(InstallerError) as raised:
            values.build(make_ctx(), future)
        self.assertIn("SOMETHING_NEW", raised.exception.message)
        self.assertIn("installer", raised.exception.hint.lower())

    def test_embedded_release_manifest_matches_the_bundled_one(self):
        embedded = yaml.safe_load((PLATFORM_CHART / "release.yaml").read_text())
        bundled = yaml.safe_load((ROOT / "releases" / f"{embedded['release']}.yaml").read_text())
        self.assertEqual(embedded, bundled)
        chart = yaml.safe_load((PLATFORM_CHART / "Chart.yaml").read_text())
        self.assertEqual(chart["version"], embedded["chart"]["version"])
        self.assertEqual(chart["name"], "naya-helm-deployment")


def flatten(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from flatten(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from flatten(value, f"{path}[{index}]")
    else:
        yield path, node


class BuiltValuesTests(Base):
    def setUp(self):
        self.ctx = make_ctx()
        self.built = values.build(self.ctx, CHART_VALUES)

    def test_every_public_host_has_an_ingress_on_the_shared_alb(self):
        hosts = self.ctx.cfg["domain"]["hosts"]
        ingresses = {
            "core": self.built["dial"]["core"]["ingress"], "chat": self.built["dial"]["chat"]["ingress"],
            "themes": self.built["dial"]["themes"]["ingress"], "admin": self.built["admin"]["frontend"]["ingress"],
            "grafana": self.built["grafana"]["ingress"], "superset": self.built["superset"]["ingress"],
        }
        for service, ingress in ingresses.items():
            self.assertEqual(ingress["hosts"], [hosts[service]], service)
            annotations = ingress["annotations"]
            self.assertEqual(annotations["alb.ingress.kubernetes.io/group.name"], "dial-acme")
            self.assertEqual(annotations["alb.ingress.kubernetes.io/certificate-arn"], self.ctx.cfg["domain"]["certificate_arn"])
            self.assertEqual(annotations["alb.ingress.kubernetes.io/scheme"], "internal")
            self.assertEqual(annotations["alb.ingress.kubernetes.io/subnets"], "subnet-prv1,subnet-prv2")
            self.assertEqual(annotations["alb.ingress.kubernetes.io/security-groups"], "sg-alb")

    def test_public_exposure(self):
        ctx = make_ctx(cfg=resolved(network={"exposure": "public"}))
        scheme = values.build(ctx, CHART_VALUES)["dial"]["chat"]["ingress"]["annotations"]["alb.ingress.kubernetes.io/scheme"]
        self.assertEqual(scheme, "internet-facing")

    def test_custom_hosts_flow_into_callbacks(self):
        raw = example()
        raw["domain"]["hosts"] = {"chat": "ai.other.org", "admin": "admin.other.org"}
        built = values.build(make_ctx(cfg=config.resolve(raw, RELEASE)), CHART_VALUES)
        self.assertEqual(built["dial"]["chat"]["env"]["APP_COGNITO_REDIRECT_URI"], "https://ai.other.org/api/auth/callback/cognito")
        self.assertEqual(built["admin"]["frontend"]["env"]["NEXTAUTH_URL"], "https://admin.other.org")

    def test_secrets_come_from_the_customer_secret(self):
        secret = self.ctx.secret
        self.assertEqual(self.built["dial"]["core"]["configuration"]["encryption"]["key"], secret["core_encryption_key"])
        self.assertEqual(self.built["admin"]["externalDatabase"]["password"], secret["db_master_password"])
        self.assertEqual(self.built["influxdb"]["adminUser"]["token"], secret["influxdb_token"])
        self.assertEqual(self.built["dial"]["chat"]["secrets"]["COGNITO_CLIENT_SECRET"], "cs")

    def test_images_are_redirected_to_the_customers_registry_and_nothing_else_changes(self):
        flat = dict(flatten(self.built))
        registries = {v for k, v in flat.items() if k.endswith(".image.registry")}
        self.assertEqual(registries, {"123456789012.dkr.ecr.us-east-1.amazonaws.com"})
        self.assertFalse([k for k in flat if k.endswith((".image.tag", ".image.pullPolicy"))], "tags stay in the chart")
        self.assertNotIn("709825985650", json.dumps(self.built), "no Marketplace registry reference may remain")

    def test_public_mode_with_the_default_source_changes_no_image(self):
        """The chart already names the source registry, so there is nothing to override."""
        built = values.build(make_ctx(cfg=resolved(registry={"provider": "public"})), CHART_VALUES)
        flat = dict(flatten(built))
        self.assertFalse([k for k in flat if k.endswith((".image.registry", ".image.repository"))])
        # the customer's registry still appears where the applications push images they build
        self.assertEqual(flat["app-controller.env.APP_DOCKER_REGISTRY"], "123456789012.dkr.ecr.us-east-1.amazonaws.com")

    def test_the_chart_defaults_are_concrete_and_use_one_registry(self):
        """The Marketplace validates the chart with its default values: every image must be a real reference."""
        declared = CHART_VALUES["installer"]["registry"]
        text = (PLATFORM_CHART / "values.yaml").read_text()
        self.assertNotIn("PLACEHOLDER_IMAGE", text)
        hosts = set(re.findall(r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com", text))
        self.assertEqual(hosts, {declared.split("/")[0]})
        for url in re.findall(r"(?:docker\.io|quay\.io|ghcr\.io|gcr\.io|registry\.k8s\.io)/\S+", text):
            self.fail(f"image outside the registry: {url}")

    def test_moving_to_another_account_moves_every_reference(self):
        from dial_install import releases
        source = "oci://999999999999.dkr.ecr.eu-west-1.amazonaws.com/acme-products"
        cfg = resolved(registry={"source": source})
        release = releases.offline_latest(source)
        for provider, host in (("public", "999999999999.dkr.ecr.eu-west-1.amazonaws.com"), ("ecr-mirror", "123456789012.dkr.ecr.us-east-1.amazonaws.com")):
            cfg["registry"]["provider"] = provider
            ctx = make_ctx(cfg=cfg, release=release)
            built = values.build(ctx, CHART_VALUES)
            text = json.dumps(built)
            self.assertNotIn("709825985650", text)
            self.assertNotIn("naya-technologies-by-epam", text)
            self.assertIn(f'"registry": "{host}"', text)
            self.assertIn('"repository": "acme-products/naya-dial-core"', text)
            installer_section = values.effective(ctx, CHART_VALUES)["installer"]
            self.assertTrue(all(r.startswith(host + "/acme-products/") for r in [installer_section["addons"]["clusterAutoscaler"], *installer_section["addons"]["knative"].values()]))
        self.assertEqual(release["chart"]["repository"], source + "/naya-helm-deployment")

    def test_chat_runs_the_naya_ui_image(self):
        self.assertIn("naya-dial-ui", CHART_VALUES["dial"]["chat"]["image"]["repository"])
        self.assertNotIn("naya-dial-chat", (PLATFORM_CHART / "values.yaml").read_text())

    def test_app_controller_can_build_and_run_python_applications(self):
        env = CHART_VALUES["app-controller"]["env"]
        self.assertIn("naya-dial-kaniko-executor", env["APP_BUILDER_IMAGE"])
        self.assertIn("naya-dial-app-builder-python", env["APP_TEMPLATE_IMAGE"])
        self.assertEqual(env["APP_DEFAULT_RUNTIME"], "python3.12")
        runtimes = json.loads(env["SPRING_APPLICATION_JSON"])["app"]["runtimes"]
        self.assertEqual(sorted(runtimes), [f"[python3.{v}]" for v in (10, 11, 12, 8, 9)])
        self.assertTrue(all("naya-dial-python:" in r["image"] and r["profile"] == "python-pip" for r in runtimes.values()))
        built = values.build(make_ctx(), CHART_VALUES)["app-controller"]["env"]
        self.assertEqual(built["APP_AWS_REGION"], "us-east-1")

    def models(self, ctx=None, models=None):
        ctx = ctx or self.ctx
        if models is not None:
            ctx.release = {**ctx.release, "models": models}       # a copy: the release is shared between tests
        built = values.build(ctx, CHART_VALUES)
        return json.loads(built["dial"]["core"]["secrets"]["aidial.config.json"])["models"]

    def test_core_config_registers_the_default_model_behind_a_stable_id(self):
        config_json = json.loads(self.built["dial"]["core"]["secrets"]["aidial.config.json"])
        self.assertEqual(list(config_json["models"]), ["default.model"])
        model = config_json["models"]["default.model"]
        self.assertEqual(model["displayName"], "AWS Private Bedrock")
        # the global inference profile is used even though the region is us-east-1
        self.assertIn("/openai/deployments/global.anthropic.claude-sonnet-5-5/chat/completions", model["endpoint"])
        self.assertEqual(list(config_json["keys"]), [self.ctx.secret["core_admin_api_key"]])

    def test_inference_profile_auto_follows_the_region(self):
        base = {"key": "m", "display_name": "M", "icon": "a.svg", "bedrock_model_id": "anthropic.x"}
        endpoint = self.models(models=[{**base, "inference_profile": "auto"}])["m"]["endpoint"]
        self.assertIn("/deployments/us.anthropic.x/", endpoint)
        raw = example()
        raw["aws"]["region"] = "il-central-1"
        raw["domain"]["certificate_arn"] = raw["domain"]["certificate_arn"].replace("us-east-1", "il-central-1")
        ctx = make_ctx(cfg=config.resolve(raw, RELEASE))
        endpoint = self.models(ctx, [{**base}])["m"]["endpoint"]                    # `auto` is the default
        self.assertIn("/deployments/global.anthropic.x/", endpoint)

    def test_inference_profile_none_uses_the_model_id_as_written(self):
        base = {"key": "m", "display_name": "M", "icon": "a.svg", "bedrock_model_id": "arn-or-id"}
        endpoint = self.models(models=[{**base, "inference_profile": "none"}])["m"]["endpoint"]
        self.assertIn("/deployments/arn-or-id/chat/completions", endpoint)

    def test_feature_flags(self):
        built = values.build(make_ctx(cfg=resolved(features={"analytics": False, "deployment_manager": False})), CHART_VALUES)
        self.assertFalse(built["superset"]["enabled"])
        self.assertFalse(built["deployment-manager"]["enabled"])
        self.assertEqual(built["dial"]["chat"]["env"]["APP_ANALYTICS_ENABLED"], "false")
        self.assertEqual(built["admin"]["frontend"]["env"]["DEPLOYMENTS_ENABLED"], "false")

    def test_deployment_manager_json_is_valid_json_after_substitution(self):
        raw = self.built["deployment-manager"]["env"]["SPRING_APPLICATION_JSON"]
        cognito = json.loads(raw)["providers"]["cognito"]
        self.assertEqual(cognito["audiences"], ["client1"])
        self.assertEqual(cognito["issuer"], FAKE_OUTPUTS["identity"]["CognitoIssuerUrl"])

    def test_a_string_with_placeholders_and_a_registry_host_gets_both_changes(self):
        """The Deployment Manager JSON names Cognito (placeholders) and the build-tool images (registry host)."""
        raw = self.built["deployment-manager"]["env"]["SPRING_APPLICATION_JSON"]
        document = json.loads(raw)
        self.assertEqual(document["providers"]["cognito"]["audiences"], ["client1"])
        self.assertNotIn("PLACEHOLDER", raw)
        for key in ("builder-root-image", "builder-rootless-image", "analyser-image", "copy-image"):
            self.assertTrue(document["app"][key].startswith("123456789012.dkr.ecr.us-east-1.amazonaws.com/naya-technologies-by-epam/naya-dial-"), key)

    def test_license_installer_gets_the_redis_host_only(self):
        self.assertEqual(self.built["lic-ins"]["env"]["REDIS_HOST"], "cache.example.use1.cache.amazonaws.com")

    def test_superset_database_uri_escapes_password(self):
        ctx = make_ctx()
        ctx.secret["db_master_password"] = "p@ss/w:rd"
        uri = values.build(ctx, CHART_VALUES)["superset"]["secrets"]["SUPERSET_METADATA_DATABASE_URI"]
        self.assertIn("postgres:p%40ss%2Fw%3Ard@db.cluster", uri)

    def test_registries_are_the_customers_account_registry(self):
        registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
        self.assertEqual(self.built["app-controller"]["env"]["APP_DOCKER_REGISTRY"], registry)
        self.assertEqual(self.built["deployment-manager"]["env"]["DOCKER_REGISTRY"], registry)


@unittest.skipUnless(HAVE_HELM, "helm is not installed")
class RealHelmTests(Base):
    """Render the real charts with the generated values; no cluster needed."""

    @classmethod
    def helm(cls, *args):
        return subprocess.run(["helm", *args], capture_output=True, text=True)

    def write_values(self, document):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(document, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def render_platform(self, ctx=None):
        ctx = ctx or make_ctx()
        path = self.write_values(values.build(ctx, CHART_VALUES))
        result = self.helm("template", "dial", str(PLATFORM_CHART), "--namespace", "dial", "-f", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, [d for d in yaml.safe_load_all(result.stdout) if d]

    def test_chat_runs_conversations_through_a_registered_managed_chat_application(self):
        """The chat UI sends every conversation with a model to the application named by APP_MANAGED_CHAT_APPLICATION_ID.
        Without that application in Core the UI shows "Chat is unavailable"."""
        _, docs = self.render_platform()
        chat = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "dial-chat")
        env = {e["name"]: e.get("value") for e in chat["spec"]["template"]["spec"]["containers"][0]["env"]}
        app_id = env["APP_MANAGED_CHAT_APPLICATION_ID"]
        self.assertTrue(app_id)
        config_map = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "dial-managed-model-chat")
        applications = json.loads(config_map["data"]["managed-model-chat.json"])["applications"]
        self.assertEqual(list(applications), [app_id])
        self.assertIn("-quickapps.", applications[app_id]["endpoint"])
        core = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "dial-core")
        spec = core["spec"]["template"]["spec"]
        core_env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        files = json.loads(core_env["aidial.config.files"])
        self.assertIn("/mnt/system-config/managed-model-chat.json", files)
        self.assertIn("/mnt/system-config", [m["mountPath"] for m in spec["containers"][0]["volumeMounts"]])
        self.assertIn("managed-model-chat", [v["name"] for v in spec["volumes"]])

    def test_platform_chart_lints_with_generated_values(self):
        path = self.write_values(values.build(make_ctx(), CHART_VALUES))
        result = self.helm("lint", str(PLATFORM_CHART), "-f", path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rendered_manifests_contain_no_placeholder(self):
        text, _ = self.render_platform()
        left = sorted(set(re.findall(r"PLACEHOLDER_[A-Z0-9_]+", text)))
        self.assertEqual(left, [])

    def test_rendered_ingresses(self):
        _, docs = self.render_platform()
        ingresses = [d for d in docs if d["kind"] == "Ingress"]
        hosts = sorted(r["host"] for i in ingresses for r in i["spec"]["rules"])
        self.assertEqual(hosts, sorted(make_ctx().cfg["domain"]["hosts"].values()))
        for ingress in ingresses:
            ann = ingress["metadata"]["annotations"]
            self.assertEqual(ann["alb.ingress.kubernetes.io/group.name"], "dial-acme")
            self.assertEqual(ann["alb.ingress.kubernetes.io/scheme"], "internal")

    def test_rendered_components_exist(self):
        _, docs = self.render_platform()
        names = {d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"}
        for expected in ("dial-core", "dial-chat", "dial-admin-backend", "dial-frontend", "dial-themes",
                         "dial-bedrock", "dial-quickapps", "dial-app-controller", "dial-lic-ins"):
            self.assertIn(expected, names)

    def test_disabling_features_removes_their_workloads(self):
        ctx = make_ctx(cfg=resolved(features={"analytics": False, "deployment_manager": False}))
        _, docs = self.render_platform(ctx)
        names = {d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"}
        self.assertNotIn("dial-superset", names)
        self.assertNotIn("dial-deployment-manager", names)

    def test_each_service_account_matches_the_iam_trust_policy(self):
        """A mismatch here means a pod cannot assume its role (or any pod can): check both directions."""
        from test_modules import template
        from dial_install import modules
        iam = template(next(m for m in modules.discover() if m.slot == "iam"))
        trusted = {}                                   # output key -> service account name allowed by the role
        for output, spec in iam["Outputs"].items():
            if output == "AdminBackendRDSRoleArn":
                continue                              # IAM database login is not enabled by the installer
            role = spec["Value"]["GetAtt"].split(".")[0] if isinstance(spec["Value"]["GetAtt"], str) else spec["Value"]["GetAtt"][0]
            document = iam["Resources"][role]["Properties"]["AssumeRolePolicyDocument"]
            text = document["Sub"][0] if isinstance(document, dict) and "Sub" in document else None
            if text is None:
                continue
            found = re.findall(r"system:serviceaccount:\$\{Namespace\}:([a-z0-9-]+)", text)
            self.assertEqual(len(found), 1, f"{role} must trust exactly one service account")
            self.assertIn('"${Issuer}:aud": "sts.amazonaws.com"', text)
            trusted[output] = found[0]
        _, docs = self.render_platform()
        rendered = {d["metadata"]["name"]: d["metadata"].get("annotations", {}).get("eks.amazonaws.com/role-arn", "")
                    for d in docs if d["kind"] == "ServiceAccount"}
        for output, account in trusted.items():
            if output == "AdminBackendRDSRoleArn":
                continue                              # IAM database login is not enabled by the installer
            self.assertIn(account, rendered, f"{output} trusts {account} but the chart creates no such service account")
            self.assertTrue(rendered[account].endswith(output), f"{account} must use the role {output}, uses {rendered[account]}")
        for account, arn in rendered.items():
            if arn:
                self.assertIn(arn.rsplit("/", 1)[-1], trusted, f"service account {account} uses a role that trusts nobody")

    def test_the_chart_is_region_independent(self):
        """The deployment region is a setting; nothing in the chart may be tied to one (the registry host is not a deployment region)."""
        registry_host = CHART_VALUES["installer"]["registry"].split("/")[0]
        region = re.compile(r"\b(?:us|eu|ap|il|me|sa|ca|af|mx)-(?:east|west|north|south|central|northeast|southeast|northwest|southwest)-\d\b")
        for path in list(PLATFORM_CHART.glob("*.yaml")) + list((PLATFORM_CHART / "templates").glob("*")):
            text = path.read_text().replace(registry_host, "<registry>")
            self.assertEqual(region.findall(text), [], f"{path.name} is tied to an AWS region")
        names = [p.name for p in (PLATFORM_CHART / "files").iterdir()]
        self.assertFalse([n for n in names if region.search(n)], f"region-specific file: {names}")

    def test_the_database_ca_bundle_covers_every_region(self):
        bundle = (PLATFORM_CHART / "files" / "aws-rds-global-bundle.pem").read_text()
        self.assertGreaterEqual(bundle.count("BEGIN CERTIFICATE"), 100)
        self.assertEqual(CHART_VALUES["adminRdsIam"]["caBundle"]["file"], "files/aws-rds-global-bundle.pem")

    def test_the_raw_chart_renders_with_only_registry_images(self):
        """What the Marketplace sees: the chart rendered with its own default values."""
        result = self.helm("template", "dial", str(PLATFORM_CHART), "--namespace", "dial")
        self.assertEqual(result.returncode, 0, result.stderr)
        registry = CHART_VALUES["installer"]["registry"]
        images = set()
        for item in yaml.safe_load_all(result.stdout):
            if item and item["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
                spec = item["spec"]["template"]["spec"]
                images |= {c["image"] for c in spec.get("containers", []) + spec.get("initContainers", [])}
        self.assertTrue(images)
        self.assertEqual([i for i in images if not i.startswith(registry + "/")], [])

    def test_the_raw_chart_lints(self):
        self.assertEqual(self.helm("lint", str(PLATFORM_CHART)).returncode, 0)

    def test_cluster_base_chart_renders(self):
        for default, expect in (("true", 1), ("false", 0)):
            path = self.write_values(charts.filled_values(BASE_CHART, {"NAMESPACE": "dial", "STORAGE_CLASS_IS_DEFAULT": default}))
            result = self.helm("template", "base", str(BASE_CHART), "-f", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count("is-default-class"), expect)
            docs = [d for d in yaml.safe_load_all(result.stdout) if d]
            self.assertEqual(sorted(d["kind"] for d in docs).count("CustomResourceDefinition"), 2)
            self.assertNotIn("PLACEHOLDER", result.stdout)

    def test_cluster_base_lints(self):
        self.assertEqual(self.helm("lint", str(BASE_CHART)).returncode, 0)

    def test_packaged_platform_chart_carries_the_release_manifest(self):
        with tempfile.TemporaryDirectory() as out:
            result = self.helm("package", str(PLATFORM_CHART), "-d", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            package = next(Path(out).glob("*.tgz"))
            import tarfile
            with tarfile.open(package) as archive:
                names = archive.getnames()
            self.assertIn("naya-helm-deployment/release.yaml", names)
            self.assertIn("naya-helm-deployment/placeholders.yaml", names)


if __name__ == "__main__":
    unittest.main()


class MarketplaceListTests(Base):
    """The Marketplace requires every image the chart refers to in its delivery option, including components that are off by default."""

    LIST = ROOT / "releases" / "2026.10.4-marketplace-uris.txt"

    def static_images(self):
        host = CHART_VALUES["installer"]["registry"].split("/")[0]
        found = set()

        def walk(node):
            if isinstance(node, dict):
                repository, tag = node.get("repository"), node.get("tag")
                if isinstance(repository, str) and tag and (host in repository or host in str(node.get("registry", ""))):
                    ref = repository if repository.startswith(host) else f"{node['registry']}/{repository}"
                    found.add(f"{ref}:{tag}")
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str):
                found.update(re.findall(re.escape(host) + r"/[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+", node))

        walk(CHART_VALUES)
        return found

    def listed(self):
        return [line.strip() for line in self.LIST.read_text().splitlines() if line.strip()]

    def test_every_image_in_values_is_in_the_list(self):
        self.assertEqual(sorted(self.static_images() - set(self.listed())), [])

    def test_components_that_are_off_by_default_are_covered_too(self):
        self.assertTrue(any("naya-dial-file-processing:" in ref for ref in self.listed()))
        self.assertFalse(CHART_VALUES["file-processing"]["enabled"])

    def test_the_list_holds_the_chart_and_nothing_that_is_unused(self):
        listed = self.listed()
        self.assertTrue(listed[0].endswith("/naya-helm-deployment:2026.10.4"))
        unused = set(listed[1:]) - self.static_images()
        self.assertEqual(unused, set(), "listed images that no values.yaml entry refers to")

    def test_every_listed_reference_exists_in_the_registry_inventory(self):
        inventory = json.loads((ROOT / "tests" / "fixtures" / "registry-inventory.json").read_text())["repositories"]
        for ref in self.listed()[1:]:
            repository, tag = ref.split("/naya-technologies-by-epam/")[1].rsplit(":", 1)
            self.assertIn(tag, inventory[repository], ref)

    def test_there_are_no_duplicates(self):
        self.assertEqual(len(self.listed()), len(set(self.listed())))
