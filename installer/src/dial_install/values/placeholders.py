"""Values for the chart placeholders (see charts/dial-platform/placeholders.yaml).

One entry per placeholder name. The chart declares which names it needs; this table says how each one
is computed. `make test` checks both directions, so a chart change that adds or drops a placeholder
cannot ship with an installer that disagrees.
"""

from __future__ import annotations

import json
from typing import Callable
from urllib.parse import quote, urlencode

from ..context import Context

# Secrets whose placeholder name is not simply the upper-cased secret key.
_SECRET_ALIASES = {"ADMIN_DB_PASSWORD": "db_master_password"}

SECRET_PLACEHOLDERS = (
    "CORE_ENCRYPTION_SECRET", "CORE_ENCRYPTION_KEY", "COGNITO_LOGGING_SALT", "CORE_ADMIN_API_KEY", "DIAL_API_KEY",
    "ADMIN_NEXTAUTH_SECRET", "SUPERSET_ADMIN_PASSWORD", "SUPERSET_SECRET_KEY",
    "SUPERSET_DASHBOARD_ARTIFACT_SIGNING_SECRET", "DASHBOARDS_SIGNING_SECRET", "INFLUXDB_PASSWORD",
    "INFLUXDB_TOKEN", "GRAFANA_ADMIN_PASSWORD", "ADMIN_DB_PASSWORD",
)


def _inference_profile_prefix(region: str) -> str:
    for prefix, profile in (("us-", "us"), ("eu-", "eu"), ("ap-", "apac")):
        if region.startswith(prefix):
            return profile
    return "global"


def _service_url(ctx: Context, component: str) -> str:
    helm = ctx.release["helm_release"]
    return f"http://{helm['name']}-{component}.{helm['namespace']}.svc.cluster.local"


def _bedrock_deployment(model: dict, region: str) -> str:
    """Bedrock model id as the adapter receives it: `<inference profile>.<model id>`."""
    profile = model.get("inference_profile", "auto")
    if profile == "auto":
        profile = _inference_profile_prefix(region)
    return model["bedrock_model_id"] if profile == "none" else f"{profile}.{model['bedrock_model_id']}"


def _application_type_schemas(ctx: Context) -> list[dict]:
    """The application types Core offers (Agent Builder ...): one JSON schema each, pointing at the service that runs them."""
    schemas = []
    for kind in ctx.release.get("application_types", []):
        base = _service_url(ctx, kind["component"])
        deployment = f"{base}/openai/deployments/{kind['deployment']}"
        schema = {
            "$schema": "https://dial.epam.com/application_type_schemas/schema#",
            "$id": kind["id"],
            "dial:applicationTypeDisplayName": kind["display_name"],
            "dial:applicationTypeCompletionEndpoint": f"{deployment}/chat/completions",
            **({"dial:applicationTypeConfigurationEndpoint": f"{deployment}/configuration"} if kind.get("configuration_endpoint") else {}),
            "dial:applicationTypeSchemaEndpoint": f"{base}/v1/configuration-support/application-schema",
            "dial:appendApplicationPropertiesHeader": kind.get("append_properties_header", False),
            "dial:applicationTypeInterceptors": [],
            "dial:applicationTypeAssistantAttachmentsInRequestSupported": kind.get("assistant_attachments", False),
        }
        schemas.append(schema)
    return schemas


def _core_config(ctx: Context) -> str:
    themes = ctx.cfg["domain"]["hosts"]["themes"]
    models = {
        model["key"]: {
            "type": "chat",
            "displayName": model["display_name"],
            "iconUrl": f"https://{themes}/{model['icon']}",
            "endpoint": f"{_service_url(ctx, 'bedrock')}/openai/deployments/{_bedrock_deployment(model, ctx.region)}/chat/completions",
            **({"features": model["features"]} if model.get("features") else {}),
            **({"descriptionKeywords": model["topics"]} if model.get("topics") else {}),     # DIAL Admin calls them topics
        }
        for model in ctx.release["models"]
    }
    document = {
        "keys": {ctx.secret["core_admin_api_key"]: {"project": "DIAL Admin", "roles": ["admin"]}},
        "models": models,
        "applicationTypeSchemas": _application_type_schemas(ctx),
        "roles": {"admin": {"limits": {}}, "default": {"limits": {}}},
    }
    return json.dumps(document, indent=2)


def _superset_database_uri(ctx: Context) -> str:
    db = ctx.outputs["database"]
    return (
        "postgresql+psycopg2://postgres:" + quote(ctx.secret["db_master_password"], safe="")
        + f"@{db['DBEndpoint']}:{db.get('DBPort', '5432')}/{quote(db['DBName'], safe='')}"
        + "?" + urlencode({"sslmode": "require", "options": "-csearch_path=superset"})
    )


def _account_registry(ctx: Context) -> str:
    return f"{ctx.account_id()}.dkr.ecr.{ctx.region}.amazonaws.com"


def source_host(ctx: Context) -> str:
    return ctx.release["registry"]["source"].removeprefix("oci://").split("/", 1)[0]


def source_prefix(ctx: Context) -> str:
    return ctx.release["registry"]["source"].removeprefix("oci://").split("/", 1)[1]


def image_registry(ctx: Context) -> str:
    if ctx.cfg["registry"]["provider"] == "ecr-mirror":
        return _account_registry(ctx)
    return source_host(ctx)


def _bool(value: bool) -> str:
    return str(bool(value)).lower()


def table(ctx: Context) -> dict[str, Callable[[], str]]:
    """name -> function computing its value (evaluated only for names the chart uses)."""
    cfg, out = ctx.cfg, ctx.outputs
    hosts = cfg["domain"]["hosts"]
    cognito = lambda: ctx.cache["cognito"]                        # noqa: E731 (set by deploy.prepare)
    network = lambda: out["network"]                              # noqa: E731
    values: dict[str, Callable[[], str]] = {
        # constants and configuration
        "DIAL_RELEASE_NAME": lambda: ctx.release["helm_release"]["name"],
        "AWS_REGION": lambda: ctx.region,
        "EKS_CLUSTER_NAME": lambda: ctx.cluster_name,
        "ACM_CERTIFICATE_ARN": lambda: cfg["domain"]["certificate_arn"],
        "ALB_SCHEME": lambda: "internet-facing" if cfg["network"]["exposure"] == "public" else "internal",
        "ALB_GROUP_NAME": lambda: f"dial-{ctx.name}",
        "ANALYTICS_ENABLED": lambda: _bool(cfg["features"]["analytics"]),
        "DEPLOYMENT_MANAGER_ENABLED": lambda: _bool(cfg["features"]["deployment_manager"]),
        "ADMIN_DB_USER": lambda: "postgres",
        "ADMIN_DB_IAM_USER": lambda: cfg["database"]["iam_user"],
        "ADMIN_BACKEND_RDS_ROLE_ARN": lambda: out["iam"]["AdminBackendRDSRoleArn"],
        # host names
        "DIAL_PUBLIC_HOST": lambda: hosts["core"],
        "CHAT_PUBLIC_HOST": lambda: hosts["chat"],
        "ADMIN_PUBLIC_HOST": lambda: hosts["admin"],
        "THEMES_PUBLIC_HOST": lambda: hosts["themes"],
        "GRAFANA_PUBLIC_HOST": lambda: hosts["grafana"],
        "SUPERSET_PUBLIC_HOST": lambda: hosts["superset"],
        "ADMIN_PUBLIC_URL": lambda: "https://" + hosts["admin"],
        "THEMES_PUBLIC_URL": lambda: "https://" + hosts["themes"],
        # network
        "VPC_ID": lambda: network()["VPCId"],
        "ALB_SECURITY_GROUP_ID": lambda: network()["ALBSecurityGroupId"],
        "ALB_SUBNETS": lambda: network()["AlbSubnetIds"],
        # storage, cache, database
        "S3_BUCKET_NAME": lambda: out["storage"]["S3BucketName"],
        "REDIS_ENDPOINT": lambda: out["cache"]["RedisEndpoint"],
        "REDIS_HOSTNAME": lambda: out["cache"]["RedisEndpoint"].split("://")[-1].split(":")[0],
        "REDIS_USER_ID": lambda: out["cache"]["RedisUserId"],
        "REDIS_CLUSTER_NAME": lambda: out["cache"]["RedisClusterName"],
        "SUPERSET_CACHE_REDIS_URL": lambda: out["cache"]["RedisEndpoint"].rstrip("/") + "/0",
        "ADMIN_DB_HOST": lambda: out["database"]["DBEndpoint"],
        "ADMIN_DB_NAME": lambda: out["database"]["DBName"],
        "SUPERSET_METADATA_DATABASE_URI": lambda: _superset_database_uri(ctx),
        # identity
        "COGNITO_USER_POOL_ID": lambda: out["identity"]["UserPoolId"],
        "COGNITO_CLIENT_ID": lambda: out["identity"]["ClientId"],
        "COGNITO_HOSTED_UI_URL": lambda: out["identity"]["CognitoHostedUIUrl"],
        "COGNITO_CLIENT_SECRET": lambda: cognito()["client_secret"],
        "COGNITO_HOST": lambda: cognito()["issuer"],
        "COGNITO_JWKS_URL": lambda: cognito()["jwks_url"],
        # IAM roles
        "CORE_SERVICE_ROLE_ARN": lambda: out["iam"]["CoreRoleArn"],
        "BEDROCK_SERVICE_ROLE_ARN": lambda: out["iam"]["BedrockRoleArn"],
        "APP_CONTROLLER_ROLE_ARN": lambda: out["iam"]["AppControllerRoleArn"],
        "DEPLOYMENT_MANAGER_ROLE_ARN": lambda: out["iam"]["DeploymentManagerRoleArn"],
        "ALB_CONTROLLER_ROLE_ARN": lambda: out["iam"]["ALBControllerRoleArn"],
        # registries and documents
        "APP_DOCKER_REGISTRY": lambda: _account_registry(ctx),
        "DEPLOYMENT_MANAGER_DOCKER_REGISTRY": lambda: out["storage"]["DeploymentManagerRepositoryUri"].split("/")[0],
        "CORE_CONFIG_JSON": lambda: _core_config(ctx),
    }
    for name in SECRET_PLACEHOLDERS:
        key = _SECRET_ALIASES.get(name, name.lower())
        values[name] = (lambda k=key: ctx.secret[k])
    return values


def resolve(ctx: Context, names: set[str]) -> dict[str, str]:
    """Values for exactly `names`. Names without an entry are reported together by the caller."""
    known = table(ctx)
    return {name: str(known[name]()) for name in names if name in known}
