"""The files the installer applies are shipped in the package, and they fit the chart that pins their images."""

import re
import unittest
from pathlib import Path

import yaml

from helpers import Base, CHART_VALUES_TEXT, RELEASE, ROOT, make_ctx
from dial_install import mirror, tools, values

CHART_VALUES = yaml.safe_load(CHART_VALUES_TEXT)


class PackagedAssetsTests(Base):
    def test_every_asset_named_by_the_release_exists(self):
        addons = RELEASE["addons"]
        knative = ROOT / "assets" / "knative" / addons["knative"]["version"]
        for name in ("serving-crds.yaml", "serving-core.yaml", "net-istio.yaml"):
            self.assertTrue((knative / name).is_file(), name)
        self.assertTrue((ROOT / "assets" / "istio" / f"istio-{addons['istio']['version']}.yaml").is_file())
        self.assertTrue((ROOT / "assets" / "charts" / f"cluster-autoscaler-{addons['cluster_autoscaler']['chart_version']}.tgz").is_file())

    def test_istio_gateway_load_balancer_is_internal_and_left_to_the_aws_controller(self):
        """Without `type: external` Kubernetes tries to build a Classic load balancer itself and rejects the
        controller-only annotations, so the gateway never gets an address."""
        path = ROOT / "assets" / "istio" / f"istio-{RELEASE['addons']['istio']['version']}.yaml"
        service = next(d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                       if d and d["kind"] == "Service" and d["metadata"]["name"] == "istio-ingressgateway")
        prefix = "service.beta.kubernetes.io/aws-load-balancer-"
        annotations = service["metadata"]["annotations"]
        self.assertEqual(annotations[prefix + "type"], "external")
        self.assertEqual(annotations[prefix + "scheme"], "internal")
        self.assertEqual(annotations[prefix + "nlb-target-type"], "instance")

    def test_istio_gateway_exposes_only_the_ports_something_serves(self):
        path = ROOT / "assets" / "istio" / f"istio-{RELEASE['addons']['istio']['version']}.yaml"
        service = next(d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                       if d and d["kind"] == "Service" and d["metadata"]["name"] == "istio-ingressgateway")
        self.assertEqual(sorted(p["port"] for p in service["spec"]["ports"]), [80, 15021])
        source = yaml.safe_load((ROOT / "assets" / "istio" / "operator.yaml").read_text(encoding="utf-8"))
        declared = source["spec"]["components"]["ingressGateways"][0]["k8s"]["service"]["ports"]
        self.assertEqual(sorted(p["port"] for p in declared), [80, 15021])

    def test_istio_manifest_matches_its_source_annotations(self):
        source = yaml.safe_load((ROOT / "assets" / "istio" / "operator.yaml").read_text(encoding="utf-8"))
        wanted = source["spec"]["components"]["ingressGateways"][0]["k8s"]["serviceAnnotations"]
        path = ROOT / "assets" / "istio" / f"istio-{RELEASE['addons']['istio']['version']}.yaml"
        service = next(d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                       if d and d["kind"] == "Service" and d["metadata"]["name"] == "istio-ingressgateway")
        self.assertEqual(service["metadata"]["annotations"], wanted)

    def test_knative_manifests_leave_no_upstream_image_after_redirect(self):
        images = mirror.addons(values.effective(make_ctx(), CHART_VALUES))["knative"]
        directory = ROOT / "assets" / "knative" / RELEASE["addons"]["knative"]["version"]
        for name in ("serving-core.yaml", "net-istio.yaml"):
            out = mirror.redirect_knative(images, (directory / name).read_text())
            self.assertNotIn("gcr.io", out, name)
            self.assertNotIn("@sha256", out.replace("@sha256:" + "0" * 64, ""), name)

    def test_every_knative_image_the_chart_pins_is_used_by_the_manifests(self):
        images = mirror.addons(values.effective(make_ctx(), CHART_VALUES))["knative"]
        directory = ROOT / "assets" / "knative" / RELEASE["addons"]["knative"]["version"]
        used = set()
        for name in ("serving-core.yaml", "net-istio.yaml"):
            for ref in images.values():
                if ref in mirror.redirect_knative(images, (directory / name).read_text()):
                    used.add(ref)
        self.assertEqual(used, set(images.values()))

    def test_istio_asset_has_exactly_the_tokens_the_installer_replaces(self):
        text = (ROOT / "assets" / "istio" / f"istio-{RELEASE['addons']['istio']['version']}.yaml").read_text()
        self.assertEqual(set(re.findall(r"registry\.invalid/[a-z-]+", text)), {"registry.invalid/dial-hub"})
        for token in ("dial-image-tag", "dial-pilot-image", "dial-proxy-image"):
            self.assertIn(token, text)
        kinds = {d["kind"] for d in yaml.safe_load_all(text) if d}
        self.assertIn("CustomResourceDefinition", kinds)
        self.assertIn("Deployment", kinds)

    def test_istio_workloads_are_scheduled_on_the_dynamic_node_group(self):
        text = (ROOT / "assets" / "istio" / f"istio-{RELEASE['addons']['istio']['version']}.yaml").read_text()
        for item in yaml.safe_load_all(text):
            if item and item["kind"] == "Deployment":
                self.assertEqual(item["spec"]["template"]["spec"]["nodeSelector"], {"workload": "dynamic"}, item["metadata"]["name"])

    def test_the_installer_never_downloads_when_the_package_carries_the_file(self):
        from dial_install.bootstrap import Bootstrap
        from unittest import mock
        with mock.patch.object(tools, "download_text", side_effect=AssertionError("download")):
            bootstrap = Bootstrap(make_ctx(), lambda _m: None)
            text = bootstrap._manifest("knative/knative-v1.21.0/serving-core.yaml", "https://example.invalid/x")
        self.assertIn("knative-serving", text)

    def test_an_asset_missing_from_the_package_is_a_clear_error(self):
        from dial_install.bootstrap import Bootstrap
        from dial_install.errors import InstallerError
        with self.assertRaises(InstallerError):
            Bootstrap(make_ctx(), lambda _m: None)._manifest("istio/istio-0.0.0.yaml", None)

    def test_packaged_tools_are_preferred(self):
        from unittest import mock
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "helm").write_text("#!/bin/sh\n")
            (Path(directory) / "helm").chmod(0o755)
            with mock.patch.object(tools, "PACKAGED_BIN", Path(directory)):
                self.assertEqual(tools.which("helm"), str(Path(directory) / "helm"))


class OfflineHygieneTests(Base):
    def test_downloads_exist_only_as_a_fallback_in_tools_and_bootstrap(self):
        callers = []
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text()
            if re.search(r"urlopen|download_text|_fetch\(", text):
                callers.append(path.name)
        self.assertEqual(sorted(set(callers)), ["bootstrap.py", "tools.py"])


if __name__ == "__main__":
    unittest.main()
