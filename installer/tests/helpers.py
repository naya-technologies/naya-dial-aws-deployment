"""Shared fixtures for the test-suite (no AWS access is ever needed)."""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dial_install import config, releases  # noqa: E402
from dial_install.cloud import Cloud  # noqa: E402
from dial_install.context import Context  # noqa: E402
from dial_install.runner import FakeRunner, Result  # noqa: E402
from dial_install import secrets as secrets_mod  # noqa: E402

RELEASE = releases.offline_latest()

HELM_DIR = ROOT.parent / "charts"
PLATFORM_CHART = HELM_DIR / "dial-platform"
BASE_CHART = HELM_DIR / "dial-cluster-base"
CHART_VALUES_TEXT = (PLATFORM_CHART / "values.yaml").read_text()

FAKE_OUTPUTS = {
    "network": {
        "VPCId": "vpc-0a1", "PublicSubnet1Id": "subnet-pub1", "PublicSubnet2Id": "subnet-pub2",
        "PrivateSubnet1Id": "subnet-prv1", "PrivateSubnet2Id": "subnet-prv2", "PrivateRouteTableId": "rtb-1",
        "ALBSecurityGroupId": "sg-alb", "EKSSecurityGroupId": "sg-eks", "RDSSecurityGroupId": "sg-rds",
        "RedisSecurityGroupId": "sg-redis", "VPCESecurityGroupId": "sg-vpce", "AlbSubnetIds": "subnet-prv1,subnet-prv2",
    },
    "storage": {
        "S3BucketName": "bucket", "S3BucketArn": "arn:aws:s3:::bucket", "CodeAppRepositoryUri": "1.dkr.ecr.r.amazonaws.com/code",
        "DeploymentManagerRepositoryArn": "arn:aws:ecr:r:1:repository/dm",
        "DeploymentManagerRepositoryUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/dm",
    },
    "compute": {
        "ClusterName": "acme-cluster", "ClusterArn": "arn:eks", "ClusterEndpoint": "https://eks",
        "OIDCProviderArn": "arn:aws:iam::1:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC",
        "OIDCProviderUrl": "https://oidc.eks.us-east-1.amazonaws.com/id/ABC", "ClusterSecurityGroupId": "sg-cluster",
        "NodeRoleArn": "arn:role", "ClusterAutoscalerRoleArn": "arn:ca",
    },
    "cache": {
        "RedisEndpoint": "rediss://cache.example.use1.cache.amazonaws.com:6379", "RedisClusterName": "acme-cache",
        "RedisUserId": "acme-redis", "RedisCacheArn": "arn:cache", "RedisUserArn": "arn:user",
    },
    "database": {"DBEndpoint": "db.cluster.rds.amazonaws.com", "DBPort": "5432", "DBName": "dialadmin", "ClusterResourceId": "cluster-XYZ"},
    "iam": {k: f"arn:aws:iam::123456789012:role/{k}" for k in (
        "CoreRoleArn", "BedrockRoleArn", "AppControllerRoleArn", "DeploymentManagerRoleArn",
        "ALBControllerRoleArn", "AdminBackendRDSRoleArn")},
    "identity": {
        "UserPoolId": "us-east-1_pool", "ClientId": "client1", "AdminClientId": "client1",
        "CognitoIssuerUrl": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
        "CognitoJWKSUrl": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool/.well-known/jwks.json",
        "CognitoHostedUIUrl": "https://acme.auth.us-east-1.amazoncognito.com",
    },
}

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:dial/acme-AbCdEf"


def example(name="minimal"):
    return config.load_yaml(ROOT / "examples" / f"dial.{name}.yaml")


def resolved(raw=None, **sections):
    raw = copy.deepcopy(raw if raw is not None else example())
    for key, value in sections.items():
        raw[key] = {**raw.get(key, {}), **value}
    return config.resolve(raw, RELEASE)


def make_ctx(cfg=None, responses=None, dry_run=False, with_outputs=True, release=None):
    answers = {("helm", "show", "values"): Result(0, CHART_VALUES_TEXT), **(responses or {})}
    runner = FakeRunner(answers, dry_run=dry_run)
    cloud = Cloud(runner, "us-east-1", None, "dial-acme")
    from dial_install import modules as modules_mod
    chosen_cfg = cfg or resolved()
    ctx = Context(cfg=chosen_cfg, release=release or RELEASE, cloud=cloud, modules=modules_mod.select(chosen_cfg))
    ctx.secret = secrets_mod.generate()
    ctx.cache["account_id"] = "123456789012"
    ctx.cache["cognito"] = {
        "issuer": FAKE_OUTPUTS["identity"]["CognitoIssuerUrl"], "jwks_url": FAKE_OUTPUTS["identity"]["CognitoJWKSUrl"],
        "client_secret": "cs", "admin_client_secret": "acs",
    }
    if with_outputs:
        ctx.outputs = copy.deepcopy(FAKE_OUTPUTS)
    return ctx


def ok_json(payload):
    return Result(0, json.dumps(payload))


class Base(unittest.TestCase):
    maxDiff = None
