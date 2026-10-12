"""network/existing-vpc - bring your own VPC (including RAM-shared VPCs).

Contract: returns exactly the same outputs as network/create, so nothing
downstream knows or cares which one was used.
"""

from dial_install.findings import fail, ok, warn

MIN_FREE_IPS = 64


def _subnets(ctx):
    """Describe every subnet named in the config (memoized)."""
    if "subnets" not in ctx.cache:
        ids = ctx.cfg["network"]["subnets"]
        wanted = [*ids["private"], *ids.get("public", [])]
        data = ctx.cloud.aws_json("ec2", "describe-subnets", "--subnet-ids", *wanted, check=False)
        ctx.cache["subnets"] = {s["SubnetId"]: s for s in (data or {}).get("Subnets", [])}
    return ctx.cache["subnets"]


def pick_two(ctx, kind):
    """First two configured subnets of `kind` that sit in different Availability Zones."""
    described = _subnets(ctx)
    chosen = []
    zones = set()
    for subnet_id in ctx.cfg["network"]["subnets"].get(kind, []):
        subnet = described.get(subnet_id)
        if subnet and subnet["AvailabilityZone"] not in zones:
            chosen.append(subnet_id)
            zones.add(subnet["AvailabilityZone"])
        if len(chosen) == 2:
            break
    return chosen


def _route_table_for(ctx, subnet_id):
    """Route table associated with the subnet, else the VPC main route table."""
    vpc_id = ctx.cfg["network"]["vpc_id"]
    for flt in (f"Name=association.subnet-id,Values={subnet_id}",
                f"Name=vpc-id,Values={vpc_id} Name=association.main,Values=true"):
        data = ctx.cloud.aws_json("ec2", "describe-route-tables", "--filters", *flt.split(" "), check=False)
        tables = (data or {}).get("RouteTables", [])
        if tables:
            return tables[0]
    return None


def check(ctx):
    network = ctx.cfg["network"]
    vpc_id = network["vpc_id"]
    findings = []

    vpcs = ctx.cloud.aws_json("ec2", "describe-vpcs", "--vpc-ids", vpc_id, check=False)
    if not vpcs or not vpcs.get("Vpcs"):
        return [fail("network", f"VPC {vpc_id} was not found in {ctx.region}",
                     "Check the VPC id and region. For a shared VPC, make sure the AWS RAM share is accepted.")]
    findings.append(ok("network", f"VPC {vpc_id} exists"))

    for attribute in ("enableDnsSupport", "enableDnsHostnames"):
        data = ctx.cloud.aws_json("ec2", "describe-vpc-attribute", "--vpc-id", vpc_id,
                                  "--attribute", attribute, check=False)
        key = attribute[0].upper() + attribute[1:]
        if not data or not data.get(key, {}).get("Value"):
            findings.append(fail("network", f"VPC attribute {attribute} is off",
                                 "EKS needs DNS support and DNS hostnames. Enable both on the VPC."))

    described = _subnets(ctx)
    for kind in ("private", "public"):
        configured = network["subnets"].get(kind, [])
        missing = [s for s in configured if s not in described]
        if missing:
            findings.append(fail("network", f"{kind} subnet(s) not found: {', '.join(missing)}",
                                 "Check the subnet ids, the region, and the RAM share."))
            continue
        wrong_vpc = [s for s in configured if described[s]["VpcId"] != vpc_id]
        if wrong_vpc:
            findings.append(fail("network", f"{kind} subnet(s) are not in {vpc_id}: {', '.join(wrong_vpc)}"))
        if configured and len(pick_two(ctx, kind)) < 2:
            findings.append(fail("network", f"{kind} subnets must cover two different Availability Zones"))
    if any(f.level == "fail" for f in findings):
        return findings

    for subnet_id in pick_two(ctx, "private"):
        free = described[subnet_id]["AvailableIpAddressCount"]
        if free < MIN_FREE_IPS:
            findings.append(warn("network", f"private subnet {subnet_id} has only {free} free IP addresses",
                                 f"Pods use VPC IPs. Aim for at least {MIN_FREE_IPS} free per subnet."))
    findings.append(ok("network", "subnets are valid and span two Availability Zones"))

    table = _route_table_for(ctx, pick_two(ctx, "private")[0])
    routes = (table or {}).get("Routes", [])
    has_default = any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in routes)
    if not has_default and not network["create_vpc_endpoints"]:
        findings.append(warn("network", "private subnets have no default route (no NAT / proxy / transit gateway)",
                             "Nodes then need VPC endpoints for ECR, S3, STS, EC2, EKS and ELB. "
                             "Set network.create_vpc_endpoints: true, or provide them yourself."))
    if network["exposure"] == "public":
        public_table = _route_table_for(ctx, pick_two(ctx, "public")[0]) if pick_two(ctx, "public") else None
        igw = any(str(r.get("GatewayId", "")).startswith("igw-") for r in (public_table or {}).get("Routes", []))
        if not igw:
            findings.append(fail("network", "exposure is `public` but the public subnets have no internet gateway route"))
    return findings


def parameters(ctx):
    network = ctx.cfg["network"]
    cidrs = list(network["allowed_cidrs"]) + [""] * 5
    allowed = {"AllowedCidr": cidrs[0], **{f"AllowedCidr{n}": cidrs[n - 1] for n in range(2, 6)}}
    return {"VPCId": network["vpc_id"], **allowed}


def finalize_outputs(ctx, raw):
    network = ctx.cfg["network"]
    private = pick_two(ctx, "private")
    public = pick_two(ctx, "public")
    table = _route_table_for(ctx, private[0])
    outputs = {
        **raw,
        "VPCId": network["vpc_id"],
        "PrivateSubnet1Id": private[0],
        "PrivateSubnet2Id": private[1],
        "PublicSubnet1Id": public[0] if public else "",
        "PublicSubnet2Id": public[1] if len(public) > 1 else "",
        "PrivateRouteTableId": table["RouteTableId"] if table else "",
    }
    placement = public if network["exposure"] == "public" else private
    outputs["AlbSubnetIds"] = ",".join(placement[:2])
    return outputs
