"""links/security-links - rules that need both the network and the cluster."""


def parameters(ctx):
    return {
        "ALBSecurityGroupId": ctx.outputs["network"]["ALBSecurityGroupId"],
        "VPCESecurityGroupId": ctx.outputs["network"]["VPCESecurityGroupId"],
        "ClusterSecurityGroupId": ctx.outputs["compute"]["ClusterSecurityGroupId"],
    }
