"""cache/serverless-redis."""


def parameters(ctx):
    net = ctx.outputs["network"]
    return {
        "DeploymentName": ctx.name,
        "PrivateSubnet1Id": net["PrivateSubnet1Id"],
        "PrivateSubnet2Id": net["PrivateSubnet2Id"],
        "RedisSecurityGroupId": net["RedisSecurityGroupId"],
        "EKSClusterSecurityGroupId": ctx.outputs["compute"]["ClusterSecurityGroupId"],
    }
