"""Image mirroring: Marketplace registry -> the customer's ECR."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from helpers import Base, CHART_VALUES_TEXT, PLATFORM_CHART, ROOT, make_ctx, resolved
from dial_install import mirror, tools, values
from dial_install.errors import InstallerError
from dial_install.runner import FakeRunner, Result
from test_workflow import SOURCE_HOST, crane_copy, crane_digest, MIRRORED, real_helm_template

CHART_VALUES = yaml.safe_load(CHART_VALUES_TEXT)
TARGET = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
HAVE_HELM = shutil.which("helm") is not None
WORKLOADS, IN_VALUES, ADDONS = 18, 15, 10      # pod images / images named in values (build tools, ...) / add-on images
TOTAL = WORKLOADS + IN_VALUES + ADDONS


# The repositories published in the Marketplace listing (provided by the vendor).
MARKETPLACE_REPOSITORIES = """
naya-technologies-by-epam/naya-dial-core naya-technologies-by-epam/naya-dial-chat naya-technologies-by-epam/naya-dial-vertexai
naya-technologies-by-epam/naya-dial-themes naya-technologies-by-epam/naya-dial-realtime-analytics naya-technologies-by-epam/naya-dial-bedrock
naya-technologies-by-epam/naya-dial-admin-frontend naya-technologies-by-epam/naya-dial-admin-backend naya-technologies-by-epam/naya-dial-openai
naya-technologies-by-epam/naya-helm-deployment naya-technologies-by-epam/naya-dial-realtime-analytics-vector
naya-technologies-by-epam/naya-dial-alb-controllers naya-technologies-by-epam/naya-dial-app-controller
naya-technologies-by-epam/naya-dial-quickapps-backend naya-technologies-by-epam/naya-dial-lic-ins naya-technologies-by-epam/naya-dial-rag
naya-technologies-by-epam/naya-dial-grafana naya-technologies-by-epam/naya-dial-influxdb naya-technologies-by-epam/naya-dial-code-interpreter
naya-technologies-by-epam/naya-dial-busybox naya-technologies-by-epam/naya-dial-ui naya-technologies-by-epam/naya-dial-flows-backend
naya-technologies-by-epam/naya-dial-flows-frontend naya-technologies-by-epam/naya-dial-file-processing naya-technologies-by-epam/naya-dial-superset
naya-technologies-by-epam/naya-dial-deployment-manager naya-technologies-by-epam/naya-dial-interceptors
naya-technologies-by-epam/naya-dial-istio-pilot naya-technologies-by-epam/naya-dial-istio-proxyv2
naya-technologies-by-epam/naya-dial-knative-activator naya-technologies-by-epam/naya-dial-knative-autoscaler
naya-technologies-by-epam/naya-dial-knative-controller naya-technologies-by-epam/naya-dial-knative-webhook
naya-technologies-by-epam/naya-dial-knative-queue-proxy naya-technologies-by-epam/naya-dial-net-istio-controller
naya-technologies-by-epam/naya-dial-net-istio-webhook naya-technologies-by-epam/naya-dial-python
naya-technologies-by-epam/naya-dial-app-builder-python naya-technologies-by-epam/naya-dial-kaniko-executor
naya-technologies-by-epam/naya-dial-buildkit naya-technologies-by-epam/naya-dial-syft naya-technologies-by-epam/naya-dial-skopeo
naya-technologies-by-epam/naya-dial-git naya-technologies-by-epam/naya-dial-mcp-proxy naya-technologies-by-epam/naya-dial-cluster-autoscaler
"""


def runner_for(extra=None):
    MIRRORED.clear()
    answers = {
        ("helm", "template"): real_helm_template,
        ("helm", "show", "values"): Result(0, CHART_VALUES_TEXT),
        ("/usr/bin/crane", "digest"): crane_digest,
        ("/usr/bin/crane", "copy"): crane_copy,
        ("aws", "ecr", "get-login-password"): Result(0, "ecr-token\n"),
        ("aws", "ecr", "describe-repositories"): lambda argv: Result(255, "", "RepositoryNotFoundException") if "--repository-names" in argv else Result(0, "{}"),
        **(extra or {}),
    }
    return FakeRunner(answers)


def context(extra=None):
    ctx = make_ctx()
    ctx.cloud.runner = runner_for(extra)
    return ctx


class Say(list):
    def __call__(self, message):
        self.append(message)


@unittest.skipUnless(HAVE_HELM, "helm is not installed")
class DiscoveryTests(Base):
    def test_every_image_of_the_chart_is_found(self):
        images = mirror.discover(context(), CHART_VALUES)
        self.assertEqual(len(images), TOTAL)
        self.assertTrue(all(i.startswith(SOURCE_HOST + "/") for i in images))
        names = " ".join(images)
        for expected in ("naya-dial-core:", "naya-dial-ui:", "naya-dial-admin-backend:", "naya-dial-superset:",
                         "naya-dial-deployment-manager:", "naya-dial-code-interpreter:", "naya-dial-alb-controllers:",
                         "naya-dial-lic-ins:", "naya-dial-influxdb:", "naya-dial-buildkit:2026.10.1-rootless", "naya-dial-syft:",
                         "naya-dial-skopeo:", "naya-dial-git:", "naya-dial-mcp-proxy:2026.10.1-alpine", "naya-dial-app-builder-python:",
                         "naya-dial-cluster-autoscaler:2026.10.1", "naya-dial-istio-pilot:2026.10.1", "naya-dial-istio-proxyv2:2026.10.1",
                         "naya-dial-ui:", "naya-dial-kaniko-executor:", "naya-dial-python:",
                         "naya-dial-knative-queue-proxy:2026.10.1", "naya-dial-net-istio-webhook:2026.10.1"):
            self.assertIn(expected, names)

    def test_disabled_features_do_not_start_pods_but_their_images_are_still_listed_when_named_in_values(self):
        ctx = make_ctx(cfg=resolved(features={"analytics": False, "deployment_manager": False}))
        ctx.cloud.runner = runner_for()
        images = mirror.discover(ctx, CHART_VALUES)
        rendered = mirror._render_images(ctx, mirror._filled(ctx, CHART_VALUES, SOURCE_HOST))
        self.assertFalse([i for i in rendered if "naya-dial-superset" in i or "naya-dial-deployment-manager" in i])
        self.assertTrue(images)

    def test_chart_references_only_repositories_that_exist_in_the_marketplace_registry(self):
        listed = set(MARKETPLACE_REPOSITORIES.split())
        images = mirror.discover(context(), CHART_VALUES)
        for image in images:
            repository = image.removeprefix(SOURCE_HOST + "/").rsplit(":", 1)[0]
            self.assertIn(repository, listed, f"{image}: no such repository in the Marketplace registry")

    def test_every_workload_image_comes_from_the_configured_registry(self):
        """A chart or sub-chart that names another registry directly would bypass the mirror; this must be empty."""
        self.assertEqual(mirror.outside_registry(context(), CHART_VALUES), [])

    def test_discovery_needs_no_infrastructure(self):
        ctx = make_ctx(with_outputs=False)
        ctx.cloud.runner = runner_for()
        self.assertEqual(len(mirror.discover(ctx, CHART_VALUES)), TOTAL)


@unittest.skipUnless(HAVE_HELM, "helm is not installed")
class CopyTests(Base):
    def copy(self, ctx=None):
        ctx = ctx or context()
        images = mirror.discover(ctx, CHART_VALUES)
        say = Say()
        with mock.patch.object(tools, "which", return_value="/usr/bin/crane"):
            result = mirror.copy_images(ctx, images, say)
        return ctx, images, result, say

    def test_copies_each_image_to_the_same_path_in_the_customers_registry(self):
        ctx, images, (copied, skipped), _ = self.copy()
        self.assertEqual((copied, skipped), (TOTAL, 0))
        copies = ctx.cloud.runner.called("/usr/bin/crane", "copy")
        self.assertEqual({c[3] for c in copies}, {i.replace(SOURCE_HOST, TARGET) for i in images})
        self.assertTrue(all(c[2].startswith(SOURCE_HOST) for c in copies))

    def test_repositories_are_created_before_their_images_with_scanning_and_tags(self):
        ctx, images, _, _ = self.copy()
        creates = [w for w in ctx.cloud.runner.writes if w[:3] == ["aws", "ecr", "create-repository"]]
        self.assertEqual(len(creates), TOTAL)
        first = creates[0]
        self.assertIn("scanOnPush=true", first)
        self.assertIn("Key=dial-install:installation,Value=acme", first)
        order = [w[1] if w[0] == "aws" else "copy" for w in ctx.cloud.runner.writes if w[0] == "aws" or w[0].endswith("crane")]
        self.assertEqual(order[:2], ["ecr", "copy"])                       # create, then copy, per image

    def test_existing_images_with_the_same_digest_are_skipped(self):
        ctx = context()
        images = mirror.discover(ctx, CHART_VALUES)
        MIRRORED.update(i.replace(SOURCE_HOST, TARGET) for i in images)
        say = Say()
        with mock.patch.object(tools, "which", return_value="/usr/bin/crane"):
            copied, skipped = mirror.copy_images(ctx, images, say)
        self.assertEqual((copied, skipped), (0, TOTAL))
        self.assertEqual(ctx.cloud.runner.called("/usr/bin/crane", "copy"), [])
        self.assertEqual([w for w in ctx.cloud.runner.writes if w[:3] == ["aws", "ecr", "create-repository"]], [])

    def test_a_changed_tag_copies_only_that_image(self):
        ctx = context()
        images = mirror.discover(ctx, CHART_VALUES)
        MIRRORED.update(i.replace(SOURCE_HOST, TARGET) for i in images[1:])
        with mock.patch.object(tools, "which", return_value="/usr/bin/crane"):
            copied, skipped = mirror.copy_images(ctx, images, Say())
        self.assertEqual((copied, skipped), (1, TOTAL - 1))

    def test_credentials_travel_in_a_private_docker_config_not_on_a_command_line(self):
        ctx, _, _, _ = self.copy()
        runner = ctx.cloud.runner
        config_dirs = {e["DOCKER_CONFIG"] for e in runner.envs if "DOCKER_CONFIG" in e}
        self.assertEqual(len(config_dirs), 1)
        self.assertFalse(Path(next(iter(config_dirs))).exists(), "the credentials directory must be removed afterwards")
        self.assertFalse(any("ecr-token" in " ".join(c) or "QVdTOm" in " ".join(c) for c in runner.calls))

    def test_docker_config_contains_both_registries(self):
        ctx = context()
        directory = mirror._docker_config(ctx, [SOURCE_HOST, TARGET])
        try:
            data = json.loads((directory / "config.json").read_text())
            self.assertEqual(set(data["auths"]), {SOURCE_HOST, TARGET})
            import base64
            self.assertEqual(base64.b64decode(data["auths"][TARGET]["auth"]), b"AWS:ecr-token")
            self.assertEqual(oct(directory.stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct((directory / "config.json").stat().st_mode & 0o777), "0o600")
        finally:
            shutil.rmtree(directory)

    def test_marketplace_login_uses_the_registrys_own_region(self):
        ctx = context()
        ctx.cloud.region = "il-central-1"
        directory = mirror._docker_config(ctx, [SOURCE_HOST, "123456789012.dkr.ecr.il-central-1.amazonaws.com"])
        shutil.rmtree(directory)
        logins = [c for c in ctx.cloud.runner.calls if c[:3] == ["aws", "ecr", "get-login-password"]]
        self.assertEqual([c[c.index("--region") + 1] for c in logins], ["us-east-1", "il-central-1"])

    def test_copy_failure_explains_what_to_check(self):
        ctx = context({("/usr/bin/crane", "copy"): Result(1, "", "UNAUTHORIZED: not entitled")})
        images = mirror.discover(ctx, CHART_VALUES)
        with mock.patch.object(tools, "which", return_value="/usr/bin/crane"), self.assertRaises(InstallerError) as raised:
            mirror.copy_images(ctx, images, Say())
        self.assertIn("Could not copy " + SOURCE_HOST, raised.exception.message)
        self.assertIn("Marketplace subscription", raised.exception.hint)

    def test_dry_run_copies_nothing(self):
        ctx = context()
        ctx.cloud.runner.dry_run = True
        images = mirror.discover(ctx, CHART_VALUES)
        with mock.patch.object(tools, "which", return_value="/usr/bin/crane"):
            mirror.copy_images(ctx, images, Say())
        self.assertEqual(MIRRORED, set())


class ToolTests(Base):
    def test_tarball_member_must_match_exactly(self):
        import io, tarfile
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, body in (("gcrane", b"wrong"), ("krane", b"wrong"), ("crane", b"right")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        self.assertEqual(tools._from_tarball(buffer.getvalue(), "crane"), b"right")

    def test_install_crane_verifies_the_checksum(self):
        import hashlib, io, tarfile
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("crane")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bin"))
        data = buffer.getvalue()
        name = "go-containerregistry_Linux_x86_64.tar.gz"
        good = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
        bad = f"{'0' * 64}  {name}\n".encode()
        with mock.patch.object(tools, "machine_arch", return_value="amd64"), \
                mock.patch.object(tools, "BIN_DIR", Path(__import__("tempfile").mkdtemp())):
            with mock.patch.object(tools, "_fetch", side_effect=[data, bad]), self.assertRaises(InstallerError):
                tools.install_crane("v0.22.1")
            with mock.patch.object(tools, "_fetch", side_effect=[data, good]):
                self.assertEqual(tools.install_crane("v0.22.1").read_bytes(), b"bin")


class ModuleSelectionTests(Base):
    def test_default_is_mirror_and_public_is_available(self):
        from dial_install import modules
        pick = lambda cfg: next(m for m in modules.select(cfg) if m.slot == "registry").directory.name  # noqa: E731
        self.assertEqual(pick(resolved()), "ecr-mirror")
        self.assertEqual(pick(resolved(registry={"provider": "public"})), "public")

    def test_plan_marks_the_mirror_as_a_step_to_run(self):
        from dial_install import infra, modules
        ctx = make_ctx()
        mirror_module = next(m for m in modules.select(ctx.cfg) if m.slot == "registry")
        self.assertEqual(infra.planned_action(ctx, mirror_module), "run")


if __name__ == "__main__":
    unittest.main()


class AddonTests(Base):
    def images(self, redirected=True):
        ctx = make_ctx()
        effective = values.effective(ctx, CHART_VALUES) if redirected else CHART_VALUES
        return mirror.addons(effective)

    def test_add_on_images_are_pinned_by_the_chart(self):
        refs = mirror.addons(values.effective(make_ctx(cfg=resolved(registry={"provider": "public"})), CHART_VALUES))
        self.assertEqual(refs["clusterAutoscaler"], f"{SOURCE_HOST}/naya-technologies-by-epam/naya-dial-cluster-autoscaler:2026.10.1")
        self.assertEqual(set(refs["knative"]), {"serving-activator", "serving-autoscaler", "serving-controller", "serving-webhook",
                                                "serving-queue", "net-istio-controller", "net-istio-webhook"})
        self.assertEqual(sum(len(v) if isinstance(v, dict) else 1 for v in refs.values()), ADDONS)

    def test_a_chart_without_the_section_is_a_clear_error(self):
        with self.assertRaises(InstallerError):
            mirror.addons({"dial": {}})

    def test_mirror_mode_points_every_add_on_image_at_the_customers_registry(self):
        refs = self.images()
        flat = [refs["clusterAutoscaler"], *refs["istio"].values(), *refs["knative"].values()]
        self.assertTrue(all(r.startswith(TARGET + "/naya-technologies-by-epam/naya-dial-") for r in flat), flat)

    def test_public_mode_keeps_the_marketplace_references(self):
        ctx = make_ctx(cfg=resolved(registry={"provider": "public"}))
        refs = mirror.addons(values.effective(ctx, CHART_VALUES))
        self.assertTrue(refs["clusterAutoscaler"].startswith(SOURCE_HOST))

    def test_installer_section_never_reaches_helm(self):
        built = values.build(make_ctx(), CHART_VALUES)
        self.assertNotIn("installer", built)
        self.assertNotIn("installer", values.build(make_ctx(cfg=resolved(registry={"provider": "public"})), CHART_VALUES))

    def test_knative_manifest_images_are_replaced_by_the_pinned_ones(self):
        text = (Path(__file__).parent / "fixtures" / "knative-sample.yaml").read_text()
        images = self.images()["knative"]
        out = mirror.redirect_knative(images, text)
        self.assertNotIn("gcr.io", out)
        self.assertNotIn("@sha256", out)
        self.assertEqual(out.count(images["serving-queue"]), 2)                      # container + config map
        self.assertIn(images["net-istio-controller"], out)
        self.assertIn(images["serving-activator"], out)

    def test_unknown_upstream_image_is_a_clear_error(self):
        with self.assertRaises(InstallerError) as raised:
            mirror.redirect_knative(self.images()["knative"], "image: gcr.io/knative-releases/knative.dev/serving/cmd/newthing@sha256:" + "a" * 64)
        self.assertIn("serving-newthing", raised.exception.hint)

    def test_istio_manifest_gets_the_pinned_images(self):
        images = self.images()["istio"]
        text = (ROOT / "assets" / "istio" / "istio-1.29.1.yaml").read_text()
        out = mirror.redirect_istio(images, text)
        for token in ("registry.invalid", "dial-image-tag", "dial-pilot-image", "dial-proxy-image"):
            self.assertNotIn(token, out)
        found = set()
        for item in yaml.safe_load_all(out):
            if item and item.get("kind") == "Deployment":
                found |= {c["image"] for c in item["spec"]["template"]["spec"]["containers"]}
        self.assertEqual(found, {images["pilot"], images["proxy"]})
        # the sidecar injector builds proxy images from the same values
        self.assertIn(f'"hub": "{TARGET}/naya-technologies-by-epam"', out)
        self.assertIn('"tag": "2026.10.1"', out)

    def test_mismatched_istio_images_are_rejected(self):
        with self.assertRaises(InstallerError):
            mirror.redirect_istio({"pilot": f"{TARGET}/a/pilot:1.0", "proxy": f"{TARGET}/a/proxy:2.0"}, "x")


class BootstrapRedirectTests(Base):
    def run_bootstrap(self, provider):
        from dial_install.bootstrap import Bootstrap
        ctx = make_ctx(cfg=resolved(registry={"provider": provider}))
        ctx.cloud.runner = FakeRunner({("helm", "show", "values"): Result(0, CHART_VALUES_TEXT), ("kubectl",): Result(0, "{}")})
        with mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"), \
                mock.patch.object(tools, "download_text", side_effect=AssertionError("the installer must not download anything")), \
                mock.patch.object(Bootstrap, "knative_private_dns", lambda self: None), \
                mock.patch("time.sleep"):
            bootstrap = Bootstrap(ctx, lambda _m: None)
            bootstrap.images = mirror.addons(values.effective(ctx, CHART_VALUES))
            bootstrap.cluster_autoscaler()
            bootstrap.knative_with_istio()
        return ctx.cloud.runner

    def applied_documents(self, runner):
        """Every manifest sent to `kubectl apply -f -`, parsed."""
        documents = []
        for call, env in zip(runner.calls, runner.envs):
            pass
        return [c for c in runner.writes if c[-2:] == ["-f", "-"]]

    def test_nothing_is_downloaded_and_everything_comes_from_the_package(self):
        runner = self.run_bootstrap("ecr-mirror")
        applies = self.applied_documents(runner)
        self.assertEqual(len(applies), 5)       # Knative CRDs, Knative core, Istio namespace, Istio, net-istio
        autoscaler = next(c for c in runner.writes if "cluster-autoscaler" in " ".join(c) and c[0] == "helm")
        self.assertTrue(any(a.endswith("cluster-autoscaler-9.59.0.tgz") for a in autoscaler))
        self.assertNotIn("--repo", autoscaler)
        self.assertFalse([c for c in runner.calls if "istioctl" in c[0]])

    def test_mirror_mode_installs_the_copied_images(self):
        runner = self.run_bootstrap("ecr-mirror")
        autoscaler = next(c for c in runner.writes if "cluster-autoscaler" in " ".join(c) and c[0] == "helm")
        self.assertIn(f"image.repository={TARGET}/naya-technologies-by-epam/naya-dial-cluster-autoscaler", autoscaler)
        self.assertIn("image.tag=2026.10.1", autoscaler)

    def test_public_mode_uses_the_source_registry_directly(self):
        runner = self.run_bootstrap("public")
        autoscaler = next(c for c in runner.writes if "cluster-autoscaler" in " ".join(c) and c[0] == "helm")
        self.assertIn(f"image.repository={SOURCE_HOST}/naya-technologies-by-epam/naya-dial-cluster-autoscaler", autoscaler)


class VerifyReleaseTests(Base):
    def test_reports_missing_images(self):
        from dial_install import workflow
        from test_workflow import aws_fakes, options, Capture, patched_tools
        absent = "naya-dial-knative-webhook"
        runner = runner_for({**aws_fakes(deployed=False),
                             ("/usr/bin/crane", "digest"): lambda argv: Result(1, "", "MANIFEST_UNKNOWN") if absent in argv[2] else crane_digest(argv)})
        say = Capture()
        with patched_tools(), mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"):
            code = workflow.cmd_verify_release(options(), runner, say)
        self.assertEqual(code, 1)
        self.assertIn("MISSING " + SOURCE_HOST, say.text)
        self.assertEqual(say.text.count("MISSING"), 1)
        self.assertIn(f"needs {TOTAL} images", say.text)

    def test_all_present(self):
        from dial_install import workflow
        from test_workflow import aws_fakes, options, Capture, patched_tools
        runner = runner_for(aws_fakes(deployed=False))
        say = Capture()
        with patched_tools(), mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"):
            code = workflow.cmd_verify_release(options(), runner, say)
        self.assertEqual(code, 0)
        self.assertIn("All images exist", say.text)


class VerifyLocalChartTests(Base):
    """Vendor flow: check a chart directory before pushing it to the Marketplace."""

    def run_verify(self, absent=None):
        from dial_install import workflow
        from test_workflow import aws_fakes, Capture, patched_tools
        import subprocess

        def helm_show(argv):
            proc = subprocess.run(["helm", *argv[1:]], capture_output=True, text=True)
            return Result(proc.returncode, proc.stdout, proc.stderr)

        runner = runner_for({**aws_fakes(deployed=False), ("helm", "show", "values"): helm_show,
                             ("/usr/bin/crane", "digest"): lambda argv: Result(1, "", "MANIFEST_UNKNOWN") if absent and absent in argv[2] else crane_digest(argv)})
        say = Capture()
        opts = workflow.Options(config_path=ROOT / "examples" / "dial.minimal.yaml", chart_path=PLATFORM_CHART)
        with patched_tools(), mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"):
            code = workflow.cmd_verify_release(opts, runner, say)
        return code, say, runner

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_a_complete_chart_passes(self):
        code, say, runner = self.run_verify()
        self.assertEqual(code, 0, say.text)
        self.assertIn(f"needs {TOTAL} images", say.text)
        self.assertIn("(from local chart)", say.text)

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_it_never_asks_the_registry_for_the_unpublished_chart(self):
        _, _, runner = self.run_verify()
        for call in runner.calls:
            if call[:2] in (["helm", "template"], ["helm", "show"]):
                self.assertNotIn("--version", call)
        self.assertEqual(runner.called("helm", "registry"), [])

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_a_missing_tag_fails_the_check(self):
        code, say, _ = self.run_verify(absent="naya-dial-istio-pilot")
        self.assertEqual(code, 1)
        self.assertIn("MISSING", say.text)

    def test_a_directory_without_release_yaml_is_rejected(self):
        from dial_install import releases
        with self.assertRaises(InstallerError):
            releases.from_local_chart(Path(__file__).parent)


@unittest.skipUnless(HAVE_HELM, "helm is not installed")
class RelocationTests(Base):
    def test_discovery_and_copy_use_the_configured_source_registry(self):
        from dial_install import releases
        source = "oci://999999999999.dkr.ecr.eu-west-1.amazonaws.com/acme-products"
        ctx = make_ctx(cfg=resolved(registry={"source": source}), release=releases.offline_latest(source))
        ctx.cloud.runner = runner_for()
        images = mirror.discover(ctx, CHART_VALUES)
        self.assertEqual(len(images), TOTAL)
        self.assertTrue(all(i.startswith("999999999999.dkr.ecr.eu-west-1.amazonaws.com/acme-products/") for i in images))
        with mock.patch.object(tools, "which", return_value="/usr/bin/crane"):
            mirror.copy_images(ctx, images, Say())
        copies = ctx.cloud.runner.called("/usr/bin/crane", "copy")
        self.assertEqual({c[3] for c in copies}, {i.replace("999999999999.dkr.ecr.eu-west-1.amazonaws.com", TARGET) for i in images})
        logins = [c[c.index("--region") + 1] for c in ctx.cloud.runner.calls if c[:3] == ["aws", "ecr", "get-login-password"]]
        self.assertEqual(logins, ["eu-west-1", "us-east-1"])          # each registry gets a token from its own region


INVENTORY = json.loads((Path(__file__).parent / "fixtures" / "registry-inventory.json").read_text())["repositories"]


@unittest.skipUnless(HAVE_HELM, "helm is not installed")
class RegistryInventoryTests(Base):
    """Every image the chart uses exists, under that exact name and tag, in the registry (inventory captured 2026-10-08)."""

    def images(self, **database):
        ctx = make_ctx(cfg=resolved(database=database) if database else resolved())
        ctx.cloud.runner = runner_for()
        return mirror.discover(ctx, CHART_VALUES)

    def missing(self, images):
        absent = []
        for image in images:
            repository, tag = image.removeprefix(SOURCE_HOST + "/naya-technologies-by-epam/").rsplit(":", 1)
            if tag not in INVENTORY.get(repository, []):
                absent.append(image)
        return absent

    def test_every_image_exists_with_the_exact_name_and_tag(self):
        self.assertEqual(self.missing(self.images()), [])

    def test_every_image_of_the_release_is_tagged_with_the_release_version(self):
        for image in self.images():
            tag = image.rsplit(":", 1)[1]
            self.assertTrue(tag == "2026.10.1" or tag.startswith("2026.10.1-"), image)

    def test_the_variants_are_the_ones_the_registry_has(self):
        text = " ".join(self.images())
        for variant in ("naya-dial-buildkit:2026.10.1-rootless", "naya-dial-mcp-proxy:2026.10.1-alpine", "naya-dial-mcp-proxy:2026.10.1-debian"):
            self.assertIn(variant, text)
        for minor in (8, 9, 10, 11, 12):
            self.assertIn(f"naya-dial-python:2026.10.1-3.{minor}", text)

    def test_the_inventory_is_complete_for_the_listing(self):
        self.assertEqual(set(INVENTORY), set(MARKETPLACE_REPOSITORIES.split()) and {r.split("/")[-1] for r in MARKETPLACE_REPOSITORIES.split()})

    def test_the_only_gap_is_the_optional_database_bootstrap_image(self):
        gap = self.missing(self.images(auth="iam"))
        self.assertEqual([i.rsplit("/", 1)[1] for i in gap], ["naya-dial-postgres:15-alpine"])

    def test_the_chart_tag_is_free_in_the_chart_repository(self):
        self.assertNotIn("2026.10.1", INVENTORY["naya-helm-deployment"])


class KnativeRolloutTests(Base):
    def test_control_plane_restarts_by_scaling_so_a_full_system_node_does_not_block_it(self):
        from dial_install.bootstrap import Bootstrap
        ctx = make_ctx()
        ctx.cloud.runner = FakeRunner({("helm", "show", "values"): Result(0, CHART_VALUES_TEXT), ("kubectl",): Result(0, "{}")})
        with mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"), \
                mock.patch.object(Bootstrap, "knative_private_dns", lambda self: None), \
                mock.patch("time.sleep"):
            bootstrap = Bootstrap(ctx, lambda _m: None)
            bootstrap.images = mirror.addons(values.effective(ctx, CHART_VALUES))
            bootstrap.knative_with_istio()
        writes = [" ".join(w) for w in ctx.cloud.runner.writes]
        self.assertEqual([w.split("--replicas=")[1] for w in writes if "scale deployment autoscaler controller" in w], ["0", "1"])
        self.assertFalse(any("rollout restart" in w for w in writes))
        self.assertFalse(any("strategy" in w for w in writes))


class MissingImageToleranceTests(Base):
    """A few failed copies may be accepted explicitly; by default any failure stops the installation."""

    def run_copy(self, allowed):
        from dial_install import mirror
        from dial_install.errors import CommandError
        from dial_install.runner import Result
        ctx = make_ctx()
        ctx.cache["account_id"] = "123456789012"
        ctx.cache["allow_missing_images"] = allowed
        images = [f"{mirror.source_host(ctx)}/{mirror.source_prefix(ctx)}/img{i}:1" for i in range(4)]

        class Runner(FakeRunner):
            def run(self, argv, **kw):
                if argv[1:2] == ["digest"]:
                    return Result(0, "sha256:aaa" if "123456789012" not in argv[2] else "")
                if argv[1:2] == ["copy"] and argv[2].endswith("img2:1"):
                    raise CommandError(argv, 1, "denied")
                return Result(0, "")

        ctx.cloud.runner = Runner({})
        lines = []
        with mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"), \
                mock.patch.object(mirror, "_docker_config", return_value=Path(tempfile.mkdtemp())), \
                mock.patch.object(mirror, "_ensure_repository"):
            result = mirror.copy_images(ctx, images, lines.append)
        return result, "\n".join(lines)

    def test_default_stops_on_the_first_failure(self):
        from dial_install.errors import InstallerError
        with self.assertRaises(InstallerError):
            self.run_copy(0)

    def test_allowed_failures_are_reported_and_the_rest_is_copied(self):
        (copied, skipped), text = self.run_copy(1)
        self.assertEqual((copied, skipped), (3, 0))
        self.assertIn("img2:1", text)
        self.assertIn("will not start", text)
