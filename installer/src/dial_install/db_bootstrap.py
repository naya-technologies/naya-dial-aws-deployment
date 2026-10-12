"""One-time creation of the PostgreSQL role DIAL Admin uses to sign in with its IAM role (database.auth: iam).

A short Job runs `psql` inside the cluster (the database is private) as the master user, over a verified TLS connection, and
makes the role: no password, member of rds_iam, owner of the Admin schema. Every statement is idempotent, so re-running it
(for example on `upgrade`) is harmless. The master password reaches the Job through a temporary Secret, never a command line.
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
SQL = r"""
SELECT format('CREATE ROLE %I LOGIN', :'admin_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'admin_user')
\gexec
SELECT format('ALTER ROLE %I PASSWORD NULL', :'admin_user')
\gexec
SELECT format('GRANT rds_iam TO %I', :'admin_user')
\gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', current_database(), :'admin_user')
\gexec
SELECT format('ALTER SCHEMA %I OWNER TO %I', :'admin_schema', :'admin_user')
\gexec
SELECT format('ALTER %s %I.%I OWNER TO %I',
  CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE' WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                 WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' END,
  n.nspname, c.relname, :'admin_user')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :'admin_schema' AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
  AND (c.relkind <> 'S' OR NOT EXISTS (
        SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
          AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')))
ORDER BY CASE c.relkind WHEN 'S' THEN 2 ELSE 1 END, c.relname
\gexec
SELECT format('ALTER %s %I.%I(%s) OWNER TO %I', CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
  n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), :'admin_user')
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = :'admin_schema' AND p.prokind IN ('f', 'p')
\gexec
SELECT format('ALTER %s %I.%I OWNER TO %I', CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
  n.nspname, t.typname, :'admin_user')
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = :'admin_schema' AND t.typtype IN ('d', 'e')
\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', :'admin_schema', :'admin_user')
\gexec
SELECT format('GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I TO %I', :'admin_schema', :'admin_user')
\gexec
SELECT format('GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I TO %I', :'admin_schema', :'admin_user')
\gexec
SELECT format('GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA %I TO %I', :'admin_schema', :'admin_user')
\gexec
SELECT 'iam_role=' || rolname || ',login=' || rolcanlogin::text || ',password_cleared=true' ||
       ',rds_iam=' || pg_has_role(rolname, 'rds_iam', 'member')::text
FROM pg_roles WHERE rolname = :'admin_user';
"""

SCRIPT = ("psql --set=ON_ERROR_STOP=1 --set=admin_user=\"${ADMIN_DATABASE_USER}\" "
          "--set=admin_schema=\"${ADMIN_DATABASE_SCHEMA}\" <<'SQL'\n" + SQL.strip() + "\nSQL\n")


def pinned_image(ctx: Context, host: str) -> str:
    """The release's bootstrap image in the registry `host`: <host>/<registry prefix>/<repository>:<tag>."""
    pin = ctx.release["addons"]["rds_iam_bootstrap_image"]
    prefix = ctx.release["registry"]["source"].removeprefix("oci://").split("/", 1)[1]
    return f"{host}/{prefix}/{pin['repository']}:{pin['tag']}"


def image(ctx: Context, effective_values: dict | None = None) -> str:
    """The image to run: database.bootstrap_image, else the release's pinned one in the registry the cluster pulls from."""
    from .values import placeholders
    return ctx.cfg["database"].get("bootstrap_image") or pinned_image(ctx, placeholders.image_registry(ctx))


def manifests(ctx: Context, bootstrap_image: str) -> dict[str, dict]:
    namespace = ctx.release["helm_release"]["namespace"]
    database = ctx.outputs["database"]
    user = ctx.cfg["database"]["iam_user"]
    if not USER_PATTERN.match(user):
        raise InstallerError(f"database.iam_user {user!r} is not a lowercase PostgreSQL identifier.")
    labels = {"app.kubernetes.io/name": NAME}
    ca = (ROOT / "assets" / "aws-rds-global-bundle.pem").read_text(encoding="utf-8")
    return {
        "secret": {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": f"{NAME}-db", "namespace": namespace, "labels": labels},
            "stringData": {"password": ctx.secret["db_master_password"]},
        },
        "configmap": {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-ca", "namespace": namespace, "labels": labels},
            "data": {"aws-rds-bundle.crt": ca},
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
                        "containers": [{
                            "name": "psql", "image": bootstrap_image, "imagePullPolicy": "IfNotPresent",
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}},
                            "env": [
                                {"name": "PGHOST", "value": database["DBEndpoint"]},
                                {"name": "PGPORT", "value": str(database.get("DBPort", "5432"))},
                                {"name": "PGDATABASE", "value": database["DBName"]},
                                {"name": "PGUSER", "value": "postgres"},
                                {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": f"{NAME}-db", "key": "password"}}},
                                {"name": "PGSSLMODE", "value": "verify-full"},
                                {"name": "PGSSLROOTCERT", "value": "/rds-ca/aws-rds-bundle.crt"},
                                {"name": "ADMIN_DATABASE_USER", "value": user},
                                {"name": "ADMIN_DATABASE_SCHEMA", "value": "public"},
                            ],
                            "volumeMounts": [{"name": "rds-ca", "mountPath": "/rds-ca", "readOnly": True},
                                             {"name": "tmp", "mountPath": "/tmp"}],
                            "command": ["/bin/sh", "-ec"],
                            "args": [SCRIPT],
                        }],
                        "volumes": [{"name": "rds-ca", "configMap": {"name": f"{NAME}-ca"}}, {"name": "tmp", "emptyDir": {}}],
                    },
                },
            },
        },
    }


def run(ctx: Context, effective_values: dict, say: Callable[[str], None]) -> None:
    kube = ctx.cloud.kubectl
    namespace = ctx.release["helm_release"]["namespace"]
    docs = manifests(ctx, image(ctx, effective_values))
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
                "Creating the database role for the IAM login failed.\n" + (logs.stdout or logs.stderr or waited.stderr).strip(),
                "Check that the database is reachable from the cluster and that the bootstrap image can be pulled "
                f"(kubectl --context {ctx.cloud.kube_context} -n {namespace} describe job/{NAME}).",
            )
        say("  " + next((line for line in logs.stdout.splitlines() if line.startswith("iam_role=")), "database role is ready"))
    except CommandError as error:
        raise InstallerError(f"Could not run the database bootstrap Job.\n{error.stderr}") from error
    finally:
        cleanup()
