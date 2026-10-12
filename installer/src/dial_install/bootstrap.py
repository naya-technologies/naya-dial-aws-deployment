"""Cluster add-ons that must exist before the DIAL chart is installed.

Every step is idempotent: `apply` can be re-run, and `upgrade` skips this file.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Callable

import yaml

from . import ROOT, charts, cilium, deploy, mirror, tools
from . import values as values_pkg
from .values import placeholders
from .context import Context
from .errors import CommandError, InstallerError

ISTIO_NAMESPACE = "istio-system"
KNATIVE_NAMESPACE = "knative-serving"


class Bootstrap:
    def __init__(self, ctx: Context, say: Callable[[str], None]) -> None:
        self.ctx = ctx
        self.cloud = ctx.cloud
        self.say = say
        self.namespace = ctx.release["helm_release"]["namespace"]

    # ---- entry point ------------------------------------------------------
    def run(self) -> None:
        self.images = mirror.addons(values_pkg.effective(self.ctx, deploy.chart_defaults(self.ctx)))
        # The creator of the cluster is admin automatically. Whoever runs a later `apply` may be someone else
        # (another user, or an IAM Identity Center session in CloudShell), so that identity is granted too.
        caller = None if self.cloud.runner.dry_run else self.ctx.caller_principal()
        self.grant_access(list(dict.fromkeys([*self.ctx.cfg["kubernetes"]["admin_arns"], *([caller] if caller else [])])))
        self.connect()
        self.pin_coredns()
        self.cluster_autoscaler()
        self.cluster_base()
        self.knative_with_istio()

    # ---- steps ------------------------------------------------------------
    def connect(self) -> None:
        self.say("  Connecting kubectl to the cluster")
        self.cloud.aws("eks", "update-kubeconfig", "--name", self.ctx.cluster_name,
                       "--alias", self.cloud.kube_context, write=True)
        if self.cloud.runner.dry_run:
            return
        self.cloud.kubectl("wait", "--for=condition=Ready", "node", "--all", "--timeout=600s")

    def grant_access(self, principals: list[str]) -> None:
        """Give IAM users/roles cluster-admin through EKS access entries (idempotent)."""
        for arn in principals:
            self.say(f"  Granting cluster-admin to {arn}")
            created = self.cloud.aws("eks", "create-access-entry", "--cluster-name", self.ctx.cluster_name,
                                     "--principal-arn", arn, write=True, check=False)
            if created.returncode != 0 and "ResourceInUseException" not in created.stderr:
                raise InstallerError(f"Could not create an access entry for {arn}", created.stderr.strip())
            self.cloud.aws("eks", "associate-access-policy", "--cluster-name", self.ctx.cluster_name,
                           "--principal-arn", arn,
                           "--policy-arn", "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                           "--access-scope", "type=cluster", write=True)

    def pin_coredns(self) -> None:
        self.say("  Placing CoreDNS on the system node group")
        patch = {"spec": {"template": {"spec": {
            "nodeSelector": {"workload": "system"},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "system", "effect": "NoSchedule"}],
        }}}}
        self.cloud.kubectl("-n", "kube-system", "patch", "deployment", "coredns",
                           "--type", "merge", "-p", json.dumps(patch), write=True)
        self.cloud.kubectl("-n", "kube-system", "rollout", "status", "deployment/coredns",
                           "--timeout=180s", check=False)

    def cluster_autoscaler(self) -> None:
        addon = self.ctx.release["addons"]["cluster_autoscaler"]
        kube_version = self.ctx.cfg["kubernetes"]["version"]
        host, repository, tag = mirror.split_ref(self.images["clusterAutoscaler"])
        if addon["kubernetes"] != kube_version:
            raise InstallerError(
                f"The Cluster Autoscaler of this release is built for Kubernetes {addon['kubernetes']}, not {kube_version}.",
                f"Set kubernetes.version to {addon['kubernetes']} (the default), or use a release that supports {kube_version}.",
            )
        self.say("  Installing Cluster Autoscaler")
        bundled = ROOT / "assets" / "charts" / f"cluster-autoscaler-{addon['chart_version']}.tgz"
        source = [str(bundled)] if bundled.is_file() else [
            "cluster-autoscaler", "--repo", "https://kubernetes.github.io/autoscaler", "--version", addon["chart_version"]]
        self.cloud.helm(
            "upgrade", "--install", "cluster-autoscaler", *source,
            "--namespace", "kube-system",
            "--values", str(ROOT / "assets" / "cluster-autoscaler-values.yaml"),
            "--set-string", f"autoDiscovery.clusterName={self.ctx.cluster_name}",
            "--set-string", f"awsRegion={self.ctx.region}",
            "--set-string", f"image.repository={host}/{repository}",
            "--set-string", f"image.tag={tag}",
            write=True,
        )

    def cluster_base(self) -> None:
        """Namespace, gp3 storage class, App Controller permissions and load balancer CRDs (chart dial-cluster-base)."""
        self.say("  Installing cluster prerequisites (chart dial-cluster-base)")
        directory = charts.chart_dir("dial-cluster-base")
        has_default = False
        if not self.cloud.runner.dry_run:
            classes = self.cloud.kubectl_json("get", "storageclass", check=False) or {"items": []}
            has_default = any(
                item["metadata"].get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") == "true"
                for item in classes["items"] if item["metadata"]["name"] != "auto-ebs-gp3"
            )
        values = charts.filled_values(directory, {
            "NAMESPACE": self.namespace,
            "STORAGE_CLASS_IS_DEFAULT": str(not has_default).lower(),
        })
        handle, path = tempfile.mkstemp(prefix="dial-base-values-", suffix=".yaml")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                yaml.safe_dump(values, stream)
            self.cloud.helm("upgrade", "--install", "dial-cluster-base", str(directory),
                            "--namespace", "kube-system", "--values", path, write=True, stream=True)
        finally:
            os.unlink(path)

    def knative_with_istio(self) -> None:
        knative = self.ctx.release["addons"]["knative"]["version"]
        istio = self.ctx.release["addons"]["istio"]["version"]

        self.say(f"  Installing Knative Serving {knative}")
        self.cloud.kubectl("apply", "--server-side", "--force-conflicts", "-f", "-", write=True,
                           input_text=self._manifest(f"knative/{knative}/serving-crds.yaml", self._knative_url("serving", knative, "serving-crds.yaml")))
        self._apply_knative(knative, "serving-core.yaml", self._knative_url("serving", knative, "serving-core.yaml"))

        self.say(f"  Installing Istio {istio}")
        self.cloud.kubectl("apply", "-f", "-", write=True, input_text=f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {ISTIO_NAMESPACE}\n")
        istio_manifest = mirror.redirect_istio(self.images["istio"], self._manifest(f"istio/istio-{istio}.yaml", None))
        self.cloud.kubectl("apply", "--server-side", "--force-conflicts", "-f", "-", write=True, input_text=istio_manifest)

        self._apply_knative(knative, "net-istio.yaml", self._knative_url("net-istio", knative, "net-istio.yaml"))
        knative_deployments = ["controller", "webhook", "activator", "autoscaler",
                               "net-istio-controller", "net-istio-webhook"]
        self._schedule(KNATIVE_NAMESPACE, knative_deployments, "system")

        if self.ctx.cfg["kubernetes"]["cni"] == "cilium":
            # The API server validates Knative objects through the webhook below; with an overlay network it can
            # only reach it on the node network, so this must come before any ConfigMap is changed.
            cilium.host_network_webhooks(self.ctx, self.say, self.namespace, KNATIVE_NAMESPACE)

        self._merge_patch(KNATIVE_NAMESPACE, "configmap/config-autoscaler", {"data": {"allow-zero-initial-scale": "true"}})
        self._merge_patch(KNATIVE_NAMESPACE, "configmap/config-network",
                          {"data": {"ingress-class": "istio.ingress.networking.knative.dev"}})
        self._merge_patch(KNATIVE_NAMESPACE, "configmap/config-features", {"data": {
            "kubernetes.podspec-nodeselector": "enabled", "kubernetes.podspec-tolerations": "enabled"}})
        # Knative must not try to resolve tags of images in these registries (the product images and applications built here).
        skipped = sorted({placeholders.image_registry(self.ctx) if not self.cloud.runner.dry_run else "<image registry>",
                          mirror.target_host(self.ctx) if not self.cloud.runner.dry_run else "<account registry>"})
        self._merge_patch(KNATIVE_NAMESPACE, "configmap/config-deployment",
                          {"data": {"registries-skipping-tag-resolving": ",".join(skipped)}})

        self.cloud.kubectl("label", "namespace", KNATIVE_NAMESPACE, "istio-injection=enabled", "--overwrite", write=True)

        # Restart by stopping and starting. A rolling restart starts the new pod before the old one stops, which needs
        # room for both, and the small system node is mostly full. These two controllers can pause briefly here.
        for count in ("0", "1"):
            self.cloud.kubectl("-n", KNATIVE_NAMESPACE, "scale", "deployment", "autoscaler", "controller",
                               f"--replicas={count}", write=True)
        self.say("  Waiting for Istio and Knative")
        for deployment in ("istiod", "istio-ingressgateway"):
            self.cloud.kubectl("-n", ISTIO_NAMESPACE, "rollout", "status", f"deployment/{deployment}", "--timeout=300s", write=True)
        for deployment in knative_deployments:
            self.cloud.kubectl("-n", KNATIVE_NAMESPACE, "rollout", "status", f"deployment/{deployment}", "--timeout=300s", write=True)

    @staticmethod
    def _knative_url(project: str, version: str, name: str) -> str:
        owner = "knative" if project == "serving" else "knative-extensions"
        repository = "serving" if project == "serving" else "net-istio"
        return f"https://github.com/{owner}/{repository}/releases/download/{version}/{name}"

    def _manifest(self, relative: str, url: str | None) -> str:
        """A manifest shipped inside the installer package; downloaded only when this package does not carry it."""
        path = ROOT / "assets" / relative
        if path.is_file():
            return path.read_text(encoding="utf-8")
        if not url:
            raise InstallerError(f"This installer package does not include {relative}.",
                                 "Download the installer package built for this release.")
        self.say(f"    {relative} is not in the package; downloading {url}")
        return tools.download_text(url)

    def _apply_knative(self, version: str, name: str, url: str) -> None:
        """Apply a Knative manifest with its images replaced by the ones the chart pins."""
        text = mirror.redirect_knative(self.images["knative"], self._manifest(f"knative/{version}/{name}", url))
        self.cloud.kubectl("apply", "-f", "-", write=True, input_text=text)

    # ---- Knative private DNS ---------------------------------------------
    def knative_private_dns(self) -> None:
        """Route53 private zone so application routes resolve inside the VPC only."""
        ctx = self.ctx
        domain = f"knative.{ctx.name}.internal"
        zone_name = f"{self.namespace}.{domain}"
        vpc_id = ctx.outputs["network"]["VPCId"]
        self.say(f"  Configuring private DNS zone {zone_name}")
        if self.cloud.runner.dry_run:
            self.cloud.aws("route53", "create-hosted-zone", "--name", zone_name, write=True)
            return

        hostname = self._wait_for_ingress_hostname()
        zone_id = self._find_private_zone(zone_name, vpc_id) or self._create_private_zone(zone_name, vpc_id)
        batch = json.dumps({"Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": f"*.{zone_name}", "Type": "CNAME", "TTL": 30, "ResourceRecords": [{"Value": hostname}]}}]})
        self.cloud.aws("route53", "change-resource-record-sets", "--hosted-zone-id", zone_id,
                       "--change-batch", batch, write=True)
        self._merge_patch(KNATIVE_NAMESPACE, "configmap/config-domain", {"data": {domain: ""}})

    def _wait_for_ingress_hostname(self, timeout: int = 600) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.cloud.kubectl_json("-n", ISTIO_NAMESPACE, "get", "svc", "istio-ingressgateway", check=False)
            ingress = (data or {}).get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if ingress and ingress[0].get("hostname"):
                return ingress[0]["hostname"]
            time.sleep(10)
        raise InstallerError("Timed out waiting for the Istio load balancer.",
                             "Check `kubectl -n istio-system get svc istio-ingressgateway`.")

    def _find_private_zone(self, zone_name: str, vpc_id: str) -> str | None:
        data = self.cloud.aws_json("route53", "list-hosted-zones-by-name", "--dns-name", zone_name, check=False) or {}
        for zone in data.get("HostedZones", []):
            if zone["Name"].rstrip(".") != zone_name or not zone["Config"].get("PrivateZone"):
                continue
            zone_id = zone["Id"].rsplit("/", 1)[-1]
            detail = self.cloud.aws_json("route53", "get-hosted-zone", "--id", zone_id, check=False) or {}
            if any(v["VPCId"] == vpc_id for v in detail.get("VPCs", [])):
                return zone_id
        return None

    def _create_private_zone(self, zone_name: str, vpc_id: str) -> str:
        try:
            data = self.cloud.aws_json(
                "route53", "create-hosted-zone", "--name", zone_name,
                "--vpc", f"VPCRegion={self.ctx.region},VPCId={vpc_id}",
                "--caller-reference", f"dial-install-{self.ctx.name}-{int(time.time())}",
                "--hosted-zone-config", f"Comment=DIAL Knative private DNS for {self.ctx.name},PrivateZone=true",
            )
        except CommandError as error:
            raise InstallerError(
                f"Could not create the private DNS zone {zone_name} for VPC {vpc_id}.\n{error.stderr}",
                "If the VPC is shared from another AWS account, the VPC owner must allow this account to associate it "
                "with a private hosted zone (Route 53 create-vpc-association-authorization), or create the zone for you.",
            ) from error
        return data["HostedZone"]["Id"].rsplit("/", 1)[-1]

    # ---- small helpers ----------------------------------------------------
    def _schedule(self, namespace: str, deployments: list[str], pool: str) -> None:
        patch = json.dumps({"spec": {"template": {"spec": {
            "nodeSelector": {"workload": pool},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": pool, "effect": "NoSchedule"}],
        }}}})
        for deployment in deployments:
            self.cloud.kubectl("-n", namespace, "patch", "deployment", deployment, "--type", "merge", "-p", patch, write=True)

    def _merge_patch(self, namespace: str, resource: str, body: dict) -> None:
        self.cloud.kubectl("-n", namespace, "patch", resource, "--type", "merge", "-p", json.dumps(body), write=True)
