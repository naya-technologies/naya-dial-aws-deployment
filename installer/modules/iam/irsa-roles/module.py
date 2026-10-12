"""iam/irsa-roles."""


def parameters(ctx):
    compute, storage = ctx.outputs["compute"], ctx.outputs["storage"]
    cache, database = ctx.outputs["cache"], ctx.outputs["database"]
    oidc_url = compute["OIDCProviderUrl"]
    return {
        "DeploymentName": ctx.name,
        "OIDCProviderArn": compute["OIDCProviderArn"],
        "OIDCProviderUrl": oidc_url,
        "OIDCProviderIssuer": oidc_url.split("https://", 1)[-1],
        "DeploymentManagerRepositoryArn": storage["DeploymentManagerRepositoryArn"],
        "S3BucketName": storage["S3BucketName"],
        "RedisCacheArn": cache["RedisCacheArn"],
        "RedisUserArn": cache["RedisUserArn"],
        "AdminDatabaseClusterResourceId": database["ClusterResourceId"],
        "AdminDatabaseUser": ctx.cfg["database"]["iam_user"],
        "KubernetesNamespace": ctx.release["helm_release"]["namespace"],
        "AdminBackendServiceAccountName": "dial-admin-backend",
    }


def before_platform(ctx, say):
    """AWS License Manager refuses every call until its service-linked role exists, and DIAL Core then cannot
    find the Marketplace license. Accounts that never used License Manager do not have the role yet."""
    from dial_install.errors import InstallerError

    role = "AWSServiceRoleForAWSLicenseManagerRole"
    if ctx.cloud.aws("iam", "get-role", "--role-name", role, check=False).returncode == 0:
        return
    say("  Creating the AWS License Manager service-linked role")
    created = ctx.cloud.aws("iam", "create-service-linked-role", "--aws-service-name", "license-manager.amazonaws.com",
                            write=True, check=False)
    if created.returncode != 0 and "has been taken" not in created.stderr:
        raise InstallerError("Could not create the AWS License Manager service-linked role.\n" + created.stderr.strip(),
                             "Your AWS user needs iam:CreateServiceLinkedRole, or an administrator can run: "
                             "aws iam create-service-linked-role --aws-service-name license-manager.amazonaws.com")
