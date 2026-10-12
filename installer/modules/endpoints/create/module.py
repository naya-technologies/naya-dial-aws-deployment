"""endpoints/create - interface and gateway VPC endpoints."""


def parameters(ctx):
    net = ctx.outputs["network"]
    return {
        "VPCId": net["VPCId"],
        "PrivateSubnet1Id": net["PrivateSubnet1Id"],
        "PrivateSubnet2Id": net["PrivateSubnet2Id"],
        "PrivateRouteTableId": net["PrivateRouteTableId"],
        "EndpointSecurityGroupId": net["VPCESecurityGroupId"],
    }
