"""network/create - a brand-new VPC."""

from dial_install.findings import ok, warn

# AWS default quotas; used only when Service Quotas cannot be read.
_QUOTAS = (
    ("ec2", "L-F678F1CE", "VPCs per Region", "describe-vpcs", "Vpcs"),
    ("ec2", "L-0263D0A3", "Elastic IPs per Region", "describe-addresses", "Addresses"),
)


def check(ctx):
    findings = []
    network = ctx.cfg["network"]
    findings.append(ok("network", f"will create a VPC {network['cidr']} (NAT gateway, 2 public + 2 private subnets)"))
    for service, code, label, describe, key in _QUOTAS:
        quota = ctx.cloud.aws_json("service-quotas", "get-service-quota",
                                   "--service-code", service, "--quota-code", code, check=False)
        used = ctx.cloud.aws_json(service, describe, check=False)
        if not quota or used is None:
            findings.append(warn("network", f"could not verify the quota for {label}",
                                 "Make sure you have at least one free before applying."))
            continue
        limit, count = quota["Quota"]["Value"], len(used[key])
        if count >= limit:
            findings.append(warn("network", f"{label}: {count} of {limit} already used",
                                 "Request a quota increase in AWS Service Quotas, or free one."))
    return findings


def parameters(ctx):
    network = ctx.cfg["network"]
    subnets = network["subnet_cidrs"]
    cidrs = list(network["allowed_cidrs"]) + [""] * 5
    allowed = {"AllowedCidr": cidrs[0], **{f"AllowedCidr{n}": cidrs[n - 1] for n in range(2, 6)}}
    return {
        "VPCCidr": network["cidr"],
        "PublicSubnet1Cidr": subnets["public"][0],
        "PublicSubnet2Cidr": subnets["public"][1],
        "PrivateSubnet1Cidr": subnets["private"][0],
        "PrivateSubnet2Cidr": subnets["private"][1],
        **allowed,
    }


def finalize_outputs(ctx, raw):
    placement = "PublicSubnet" if ctx.cfg["network"]["exposure"] == "public" else "PrivateSubnet"
    return {**raw, "AlbSubnetIds": f"{raw[placement + '1Id']},{raw[placement + '2Id']}"}
