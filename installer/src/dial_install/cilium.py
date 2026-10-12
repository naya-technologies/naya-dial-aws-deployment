"""Cilium as the network plugin (`kubernetes.cni: cilium`).

The cluster is created in two CloudFormation phases (see modules/compute/eks):

  1. control plane only, with no AWS VPC CNI and no nodes      -> `install()` puts Cilium on the empty cluster
  2. node groups and add-ons                                  -> nodes become Ready as soon as their agent starts

Cilium here is a VXLAN overlay with its own pod range. Two things follow from that, and both are handled here:
the VPC cannot route to pod addresses, so load balancers must target nodes (NodePort services), and every
webhook the control plane calls must run with host networking (see `platform_values` and `host_network_webhooks`).
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Callable

import yaml

from . import ROOT, mirror
from .context import Context
from .findings import Finding, fail, ok

NAMESPACE = "kube-system"
RELEASE = "cilium"
TARGET_TYPE = "alb.ingress.kubernetes.io/target-type"
COLLISION_LABEL = "dial-install/host-network-webhook"
Say = Callable[[str], None]


# ---- configuration -----------------------------------------------------------------------------------
def settings(ctx: Context) -> dict:
    return ctx.release["cilium"]


def chart_path(ctx: Context):
    return ROOT / "assets" / "charts" / f"cilium-{settings(ctx)['chart_version']}.tgz"


def image_pairs(ctx: Context) -> dict[str, tuple[str, str]]:
    """name -> (public source reference, destination `path:tag` in the account's ECR)."""
    cfg = settings(ctx)
    prefix = mirror.source_prefix(ctx)
    return {name: (f"{spec['source']}:{cfg['version']}", f"{prefix}/{spec['repository']}:{cfg['version']}")
            for name, spec in cfg["images"].items()}


def image_refs(ctx: Context) -> dict[str, str]:
    host = mirror.target_host(ctx)
    return {name: f"{host}/{dest}" for name, (_, dest) in image_pairs(ctx).items()}


def helm_values(ctx: Context) -> dict:
    """Cilium chart values. Pods get an overlay address; kube-proxy stays (kubeProxyReplacement is off)."""
    refs = image_refs(ctx)
    agent_repo, _, agent_tag = refs["agent"].rpartition(":")
    kube = ctx.cfg["kubernetes"]
    endpoint = ctx.outputs["compute"]["ClusterEndpoint"].removeprefix("https://").rstrip("/")
    return {
        "image": {"repository": agent_repo, "tag": agent_tag, "pullPolicy": "IfNotPresent", "useDigest": False},
        # Cilium must resolve the API server name without the cluster DNS: CoreDNS can only start once Cilium works.
        "dnsPolicy": "Default",
        "operator": {
            "replicas": 1,
            "hostNetwork": True,
            "dnsPolicy": "Default",
            "tolerations": [{"operator": "Exists"}],         # it must start on nodes that are not Ready yet
            "image": {"override": refs["operator"], "pullPolicy": "IfNotPresent", "useDigest": False},
            "removeNodeTaints": True,
        },
        "ipam": {"mode": "cluster-pool", "operator": {
            "clusterPoolIPv4PodCIDRList": [kube["pod_cidr"]],
            "clusterPoolIPv4MaskSize": settings(ctx)["node_cidr_mask"]}},
        "routingMode": "tunnel",
        "tunnelProtocol": "vxlan",
        "kubeProxyReplacement": False,
        # The agents reach the API server directly: the kube-proxy add-on only exists after the nodes do.
        "k8sServiceHost": endpoint,
        "k8sServicePort": 443,
        "cni": {"install": True, "exclusive": True, "chainingMode": "none"},
        "enableIPv4Masquerade": True,
        "hubble": {"enabled": False},
        "envoy": {"enabled": False},
        "prometheus": {"enabled": False},
    }


# ---- checks ------------------------------------------------------------------------------------------
def findings(ctx: Context) -> list[Finding]:
    cfg = settings(ctx)
    result = [ok("cni", f"Cilium {cfg['version']} overlay, pod range {ctx.cfg['kubernetes']['pod_cidr']}, "
                        f"one /{cfg['node_cidr_mask']} per node")]
    if not chart_path(ctx).is_file():
        result.append(fail("cni", f"the Cilium chart {chart_path(ctx).name} is not in this package",
                           "Download the installer package built for this release."))
    crane = mirror.crane_path(ctx) if not ctx.cloud.runner.dry_run else None
    if crane:
        for name, (source, _) in image_pairs(ctx).items():
            digest = ctx.cloud.runner.run([crane, "digest", source], check=False).stdout.strip()
            if not digest:
                result.append(fail("cni", f"cannot read the Cilium {name} image {source}",
                                   "The installer copies it into your ECR. Allow access to the source registry, or set "
                                   "installer.cilium.images.<name>.source to a mirror you control."))
    sources = ", ".join(sorted({spec["source"].split("/")[0] for spec in cfg["images"].values()}))
    result.append(ok("cni", f"the Cilium images are copied from {sources} into your ECR (not part of the Marketplace listing); "
                            "this machine needs access to that registry"))
    return result


# ---- installation between the two compute phases ------------------------------------------------------
def install(ctx: Context, say: Say) -> None:
    from .bootstrap import Bootstrap          # imported here: bootstrap imports this module

    cloud = ctx.cloud
    say("  Installing Cilium on the empty cluster")
    principals = list(ctx.cfg["kubernetes"]["admin_arns"])
    caller = None if cloud.runner.dry_run else ctx.caller_principal()
    Bootstrap(ctx, say).grant_access(list(dict.fromkeys([*principals, *([caller] if caller else [])])))
    cloud.aws("eks", "update-kubeconfig", "--name", ctx.cluster_name, "--alias", cloud.kube_context, write=True)

    pairs = list(image_pairs(ctx).values())
    copied, skipped = mirror.copy_pairs(ctx, pairs, say)
    say(f"    Cilium images: {copied} copied, {skipped} already present")

    handle, path = tempfile.mkstemp(prefix="dial-cilium-", suffix=".yaml")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(helm_values(ctx), stream, default_flow_style=False)
        cloud.helm("upgrade", "--install", RELEASE, str(chart_path(ctx)), "--namespace", NAMESPACE,
                   "--values", path, "--timeout", "10m", write=True, stream=True)
    finally:
        os.unlink(path)


# ---- platform changes ---------------------------------------------------------------------------------
def platform_values(chart_values: dict) -> dict:
    """Helm values for DIAL on an overlay network, derived from the chart's own structure:
    every ingress that targets pod addresses (`target-type: ip`) targets nodes instead, and its service becomes
    a NodePort. The load balancer controller itself uses host networking (the control plane calls its webhook)."""
    overrides: dict = {"albcontroller": {"hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet", "replicaCount": 1}}

    def walk(node, path: list[str]) -> None:
        if not isinstance(node, dict):
            return
        annotations = (node.get("ingress") or {}).get("annotations") if isinstance(node.get("ingress"), dict) else None
        if isinstance(annotations, dict) and annotations.get(TARGET_TYPE) == "ip":
            target = overrides
            for key in path:
                target = target.setdefault(key, {})
            target.setdefault("ingress", {}).setdefault("annotations", {})[TARGET_TYPE] = "instance"
            target.setdefault("service", {})["type"] = "NodePort"
        for key, value in node.items():
            walk(value, [*path, key])

    walk(chart_values, [])
    return overrides


def host_network_webhooks(ctx: Context, say: Say, namespace: str, knative_namespace: str) -> None:
    """Run the control plane's webhook backends on the node network (called after Istio and Knative exist)."""
    kubectl = ctx.cloud.kubectl
    host = {"hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet"}
    say("    Moving webhooks to the node network (the control plane cannot reach overlay pod addresses)")
    istiod = {"spec": {"replicas": 1, "template": {"metadata": {"annotations": {"sidecar.istio.io/inject": "false"}},
                                                    "spec": host}}}
    kubectl("-n", "istio-system", "patch", "deployment", "istiod", "--type", "merge", "-p", json.dumps(istiod),
            write=True)
    for name in ("webhook", "net-istio-webhook"):
        patch = {"spec": {"replicas": 1, "template": {
            "metadata": {"annotations": {"sidecar.istio.io/inject": "false"}, "labels": {COLLISION_LABEL: "true"}},
            "spec": {**host, "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
                "labelSelector": {"matchLabels": {COLLISION_LABEL: "true"}}, "topologyKey": "kubernetes.io/hostname"}]}}},
        }}}
        kubectl("-n", knative_namespace, "patch", "deployment", name, "--type", "merge", "-p", json.dumps(patch),
                write=True)
    # Wait for the new pods: until they answer on the node network, changes to Knative objects are refused.
    for namespace_, name in (("istio-system", "istiod"), (knative_namespace, "webhook"), (knative_namespace, "net-istio-webhook")):
        kubectl("-n", namespace_, "rollout", "status", f"deployment/{name}", "--timeout=300s", write=True)
