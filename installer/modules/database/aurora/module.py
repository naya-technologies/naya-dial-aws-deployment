"""database/aurora-create."""

from dial_install.findings import fail, ok, warn


def check(ctx):
    version = ctx.cfg["database"]["engine_version"]
    data = ctx.cloud.aws_json("rds", "describe-db-engine-versions", "--engine", "aurora-postgresql",
                              "--engine-version", version, check=False)
    if data is None:
        return [warn("database", f"could not verify Aurora PostgreSQL {version}",
                     "Your AWS user may lack rds:DescribeDBEngineVersions.")]
    found = data.get("DBEngineVersions", [])
    if not found or found[0].get("Status", "available") != "available":
        return [fail("database", f"Aurora PostgreSQL {version} is not available for new clusters in {ctx.region}",
                     "List the available versions:\n"
                     f"  aws rds describe-db-engine-versions --engine aurora-postgresql --region {ctx.region} "
                     "--query 'DBEngineVersions[].EngineVersion' --output text\n"
                     "Then set database.engine_version in dial.yaml to one of them.")]
    findings = [ok("database", f"Aurora PostgreSQL {version} is available")]
    if ctx.cfg["database"]["auth"] == "iam":
        findings.append(ok("database", f"DIAL Admin will sign in with its IAM role (PostgreSQL role '{ctx.cfg['database']['iam_user']}', no password)"))
    return findings


def before_helm(ctx, say):
    """With database.auth: iam, create the PostgreSQL role DIAL Admin signs in with (idempotent)."""
    if ctx.cfg["database"]["auth"] != "iam":
        return
    from dial_install import db_bootstrap, deploy, values
    effective = values.effective(ctx, deploy.chart_defaults(ctx))
    db_bootstrap.run(ctx, effective, say)


def parameters(ctx):
    net = ctx.outputs["network"]
    db = ctx.cfg["database"]
    return {
        "DeploymentName": ctx.name,
        "PrivateSubnet1Id": net["PrivateSubnet1Id"],
        "PrivateSubnet2Id": net["PrivateSubnet2Id"],
        "RDSSecurityGroupId": net["RDSSecurityGroupId"],
        "EKSClusterSecurityGroupId": ctx.outputs["compute"]["ClusterSecurityGroupId"],
        "DBMasterUsername": "postgres",
        "DBSecretArn": ctx.secret_arn,
        "DBName": "dialadmin",
        "EngineVersion": db["engine_version"],
        "BackupRetentionDays": db["backup_retention_days"],
        "DeletionProtection": str(db["deletion_protection"]).lower(),
        "MinCapacity": db["min_capacity"],
        "MaxCapacity": db["max_capacity"],
    }
