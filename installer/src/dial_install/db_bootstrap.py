"""Bootstrap the Core licensing database and, when enabled, the Admin IAM database role.

A short Job runs inside the cluster as the master user over a verified TLS connection. Core can use its own
JDBC/Flyway bootstrap command. The legacy mode continues to support older Core images.
The Job reads the master password from a temporary Secret.
"""

from __future__ import annotations

import re
from typing import Callable

import yaml

from . import ROOT
from .context import Context
from .errors import CommandError, InstallerError

NAME = "dial-admin-rds-iam-bootstrap"
TIMEOUT_SECONDS = 180
USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Statements verified against Aurora PostgreSQL.
ADMIN_SQL_PATH = ROOT / "assets" / "admin-iam-bootstrap.sql"


def admin_script() -> str:
    schema = ADMIN_SQL_PATH.read_text(encoding="utf-8")
    return (
        "psql --set=ON_ERROR_STOP=1 --set=admin_user=\"${ADMIN_DATABASE_USER}\" "
        "--set=admin_schema=\"${ADMIN_DATABASE_SCHEMA}\" <<'SQL'\n"
        "SELECT set_config('dial_bootstrap.admin_user', :'admin_user', false);\n"
        "SELECT set_config('dial_bootstrap.admin_schema', :'admin_schema', false);\n"
        + schema + "\nSQL\n"
    )


def uses_postgres_licensing(values: dict) -> bool:
    return values.get("dial", {}).get("core", {}).get("env", {}).get("aidial.licensing.store") == "postgres"


def uses_jdbc_bootstrap(ctx: Context, effective_values: dict) -> bool:
    chart_mode = effective_values.get("installer", {}).get("coreDatabaseBootstrap", "legacy")
    mode = ctx.cfg["database"].get("core_bootstrap", chart_mode)
    return uses_postgres_licensing(effective_values) and mode == "jdbc"


def requires_psql(ctx: Context, effective_values: dict) -> bool:
    needed = uses_postgres_licensing(effective_values) or ctx.cfg["database"]["auth"] == "iam"
    return needed and not uses_jdbc_bootstrap(ctx, effective_values)


def core_script() -> str:
    schema = (ROOT / "assets" / "core-licensing.sql").read_text(encoding="utf-8")
    return r"""psql --set=ON_ERROR_STOP=1 <<'SQL'
SELECT 'CREATE DATABASE dialcore' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'dialcore')
\gexec
SELECT 'CREATE ROLE dial_core_licensing_iam LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dial_core_licensing_iam')
\gexec
GRANT rds_iam TO dial_core_licensing_iam;
GRANT CONNECT ON DATABASE dialcore TO dial_core_licensing_iam;
\connect dialcore
BEGIN;
DO $guard$
BEGIN
    IF to_regclass('dial_licensing.scopes') IS NOT NULL THEN
        RAISE EXCEPTION 'Legacy Core licensing schema; upgrade it before deploying this chart';
    END IF;
END;
$guard$;
""" + schema + """
GRANT USAGE ON SCHEMA dial_licensing TO dial_core_licensing_iam;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dial_licensing TO dial_core_licensing_iam;
COMMIT;
SQL
"""


def pinned_image(ctx: Context, host: str) -> str:
    """The release's bootstrap image in the registry `host`: <host>/<registry prefix>/<repository>:<tag>."""
    pin = ctx.release["addons"]["rds_iam_bootstrap_image"]
    prefix = ctx.release["registry"]["source"].removeprefix("oci://").split("/", 1)[1]
    return f"{host}/{prefix}/{pin['repository']}:{pin['tag']}"


def image(ctx: Context, effective_values: dict | None = None) -> str:
    """The image to run: database.bootstrap_image, else the release's pinned one in the registry the cluster pulls from."""
    from .values import placeholders
    return ctx.cfg["database"].get("bootstrap_image") or pinned_image(ctx, placeholders.image_registry(ctx))


def manifests(
    ctx: Context, bootstrap_image: str, *, core: bool = False, admin: bool = True, core_image: str | None = None
) -> dict[str, dict]:
    namespace = ctx.release["helm_release"]["namespace"]
    database = ctx.outputs["database"]
    user = ctx.cfg["database"]["iam_user"]
    if admin and not USER_PATTERN.match(user):
        raise InstallerError(f"database.iam_user {user!r} is not a lowercase PostgreSQL identifier.")
    labels = {"app.kubernetes.io/name": NAME}
    ca = (ROOT / "assets" / "aws-rds-global-bundle.pem").read_text(encoding="utf-8")
    environment = [
        {"name": "PGUSER", "value": "postgres"},
        {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": f"{NAME}-db", "key": "password"}}},
        {"name": "PGSSLMODE", "value": "verify-full"},
        {"name": "PGSSLROOTCERT", "value": "/rds-ca/aws-rds-bundle.crt"},
    ]
    security = {
        "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]},
    }
    mounts = [
        {"name": "rds-ca", "mountPath": "/rds-ca", "readOnly": True},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    admin_environment = [
        {"name": "ADMIN_DATABASE_USER", "value": user},
        {"name": "ADMIN_DATABASE_SCHEMA", "value": "public"},
    ]
    ca_files = {"aws-rds-bundle.crt": ca}
    containers = []
    if core_image:
        port = database.get("DBPort", "5432")
        url = f"jdbc:postgresql://{database['DBEndpoint']}:{port}/postgres"
        core_environment = [*environment, {"name": "CORE_BOOTSTRAP_URL", "value": url}]
        if admin:
            core_environment.extend([*admin_environment, {"name": "ADMIN_BOOTSTRAP_DATABASE", "value": database["DBName"]}])
            ca_files["admin-iam-bootstrap.sql"] = ADMIN_SQL_PATH.read_text(encoding="utf-8")
        containers.append({
            "name": "core-licensing", "image": core_image, "imagePullPolicy": "IfNotPresent",
            "securityContext": {**security, "runAsUser": 1001, "runAsGroup": 1001},
            "env": core_environment,
            "volumeMounts": mounts,
            "command": ["java", "-cp", "/app/lib/*"],
            "args": ["com.epam.aidial.core.server.security.LicensingDatabaseBootstrap"],
        })
    elif core or admin:
        containers.append({
            "name": "psql", "image": bootstrap_image, "imagePullPolicy": "IfNotPresent",
            "securityContext": security,
            "env": [
                *environment,
                {"name": "PGHOST", "value": database["DBEndpoint"]},
                {"name": "PGPORT", "value": str(database.get("DBPort", "5432"))},
                {"name": "PGDATABASE", "value": database["DBName"]},
                *admin_environment,
            ],
            "volumeMounts": mounts,
            "command": ["/bin/sh", "-ec"],
            "args": [(core_script() if core else "") + (admin_script() if admin else "")],
        })
    return {
        "secret": {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": f"{NAME}-db", "namespace": namespace, "labels": labels},
            "stringData": {"password": ctx.secret["db_master_password"]},
        },
        "configmap": {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-ca", "namespace": namespace, "labels": labels},
            "data": ca_files,
        },
        "job": {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": NAME, "namespace": namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0, "activeDeadlineSeconds": TIMEOUT_SECONDS + 30, "ttlSecondsAfterFinished": 600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False, "restartPolicy": "Never",
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 70, "runAsGroup": 70,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": containers,
                        "volumes": [{"name": "rds-ca", "configMap": {"name": f"{NAME}-ca"}}, {"name": "tmp", "emptyDir": {}}],
                    },
                },
            },
        },
    }


def run(ctx: Context, effective_values: dict, say: Callable[[str], None]) -> None:
    kube = ctx.cloud.kubectl
    namespace = ctx.release["helm_release"]["namespace"]
    core = uses_postgres_licensing(effective_values)
    admin = ctx.cfg["database"]["auth"] == "iam"
    core_image = None
    if uses_jdbc_bootstrap(ctx, effective_values):
        core_image_values = effective_values["dial"]["core"]["image"]
        core_image = f"{core_image_values['registry']}/{core_image_values['repository']}:{core_image_values['tag']}"
    bootstrap_image = image(ctx, effective_values) if requires_psql(ctx, effective_values) else ""
    docs = manifests(ctx, bootstrap_image, core=core, admin=admin, core_image=core_image)
    if core:
        say("  Provisioning dialcore and its IAM database role")
    if admin:
        say(f"  Creating the PostgreSQL role '{ctx.cfg['database']['iam_user']}' for the IAM login")

    def cleanup() -> None:
        for kind in ("job", "secret", "configmap"):
            kube("-n", namespace, "delete", kind, "-l", f"app.kubernetes.io/name={NAME}", "--ignore-not-found", "--wait=false",
                 write=True, check=False)

    cleanup()                                      # leftovers of an interrupted run
    try:
        for kind in ("secret", "configmap", "job"):
            kube("apply", "-f", "-", write=True, input_text=yaml.safe_dump(docs[kind]))
        waited = kube("-n", namespace, "wait", "--for=condition=complete", f"job/{NAME}", f"--timeout={TIMEOUT_SECONDS}s",
                      write=True, check=False)
        logs = kube("-n", namespace, "logs", f"job/{NAME}", "--all-containers=true", "--tail=200", write=True, check=False)
        if waited.returncode != 0:
            raise InstallerError(
                "Preparing the PostgreSQL databases failed.\n" + (logs.stdout or logs.stderr or waited.stderr).strip(),
                "Check that the database is reachable from the cluster and that the bootstrap image can be pulled "
                f"(kubectl --context {ctx.cloud.kube_context} -n {namespace} describe job/{NAME}).",
            )
        say("  " + next((line for line in logs.stdout.splitlines() if line.startswith("iam_role=")), "database role is ready"))
    except CommandError as error:
        raise InstallerError(f"Could not run the database bootstrap Job.\n{error.stderr}") from error
    finally:
        cleanup()
