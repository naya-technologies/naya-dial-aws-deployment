"""compute/eks - the Kubernetes cluster."""

from dial_install import cilium
from dial_install.findings import fail, ok, warn


def check(ctx):
    findings = []
    stack = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name",
                               ctx.stack_name("compute"), check=False)
    cluster = ctx.cloud.aws_json("eks", "describe-cluster", "--name", ctx.cluster_name, check=False)
    if cluster and not stack:
        findings.append(fail("compute", f"an EKS cluster named {ctx.cluster_name} already exists outside this installer",
                             "Choose another `name` in dial.yaml."))
    else:
        findings.append(ok("compute", f"EKS cluster name {ctx.cluster_name} is available or owned by this installation"))

    if not ctx.cfg["kubernetes"]["public_endpoint"]:
        findings.append(warn("compute", "the cluster API will be private",
                             "kubectl and this installer can only reach it from inside the VPC (or a connected network). "
                             "Run `apply` and `upgrade` from there."))

    types = sorted({g["instance_type"] for g in ctx.cfg["sizing"]["node_groups"].values()})
    offered = ctx.cloud.aws_json("ec2", "describe-instance-type-offerings", "--location-type", "region",
                                 "--filters", f"Name=instance-type,Values={','.join(types)}", check=False)
    if offered is None:
        findings.append(warn("compute", "could not verify instance type availability"))
    else:
        found = {o["InstanceType"] for o in offered["InstanceTypeOfferings"]}
        missing = [t for t in types if t not in found]
        if missing:
            findings.append(fail("compute", f"instance type(s) not offered in {ctx.region}: {', '.join(missing)}",
                                 "Choose other types under sizing.node_groups.<group>.instance_type."))
        else:
            findings.append(ok("compute", f"instance types available: {', '.join(types)}"))
    return findings


def parameters(ctx):
    net = ctx.outputs["network"]
    groups = ctx.cfg["sizing"]["node_groups"]
    params = {
        "ClusterName": ctx.cluster_name,
        "KubernetesVersion": ctx.cfg["kubernetes"]["version"],
        "PrivateSubnet1Id": net["PrivateSubnet1Id"],
        "PrivateSubnet2Id": net["PrivateSubnet2Id"],
        "CoreNodeDiskSize": groups["core"]["disk_gb"],
        "PublicEndpoint": str(ctx.cfg["kubernetes"]["public_endpoint"]).lower(),
        "EnableContainerLogs": str(ctx.cfg["features"]["container_logs"]).lower(),
        "Cni": ctx.cfg["kubernetes"]["cni"],
    }
    for group, prefix in (("core", "Core"), ("dynamic", "Dynamic"), ("system", "System")):
        params[f"{prefix}NodeInstanceType"] = groups[group]["instance_type"]
        params[f"{prefix}NodeMinSize"] = groups[group]["min"]
        params[f"{prefix}NodeMaxSize"] = groups[group]["max"]
        params[f"{prefix}NodeDesiredSize"] = groups[group]["desired"]
    return params


def _deployed_phase(ctx):
    """'bootstrap' or 'final' for an existing compute stack of a Cilium installation, None when there is none."""
    data = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", ctx.stack_name("compute"), check=False)
    if not data or not data.get("Stacks"):
        return None
    for parameter in data["Stacks"][0].get("Parameters", []):
        if parameter["ParameterKey"] == "Phase":
            return parameter["ParameterValue"]
    return "final"


def deploy_phases(ctx):
    """One phase normally. With Cilium: control plane, then Cilium, then nodes. A cluster whose nodes already
    exist is never sent back to the bootstrap phase (that would delete them)."""
    if ctx.cfg["kubernetes"]["cni"] != "cilium":
        return [{"parameters": {"Phase": "final"}}]
    final = {"parameters": {"Phase": "final"}}
    if _deployed_phase(ctx) == "final":
        return [final]
    return [{"parameters": {"Phase": "bootstrap"}, "after": cilium.install}, final]

