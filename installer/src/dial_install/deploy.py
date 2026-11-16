"""The DIAL platform: Helm install, upgrade, verification and status.

Install and upgrade share one code path (`helm upgrade --install`), so the first
install and every later version upgrade render values the same way. Helm is never
run with --wait; readiness is verified separately so failures are readable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Callable

import yaml

from .cloud import Cloud
from .context import Context
from .errors import InstallerError
from . import values as values_pkg

# Paths whose values are never printed: credentials, keys, and URIs that embed credentials.
_SENSITIVE = re.compile(r"(secret|password|token|api_?key|signing|salt|database_uri|credential|config\.json|(^|[._])key$)", re.I)


# ---- inputs ---------------------------------------------------------------
def prepare(ctx: Context) -> None:
    """Resolve runtime Cognito client secrets."""
    identity = ctx.outputs["identity"]
    pool = identity["UserPoolId"]

    def client_secret(client_id: str) -> str:
        data = ctx.cloud.aws_json("cognito-idp", "describe-user-pool-client",
                                  "--user-pool-id", pool, "--client-id", client_id, check=False)
        if ctx.cloud.runner.dry_run and not data:
            return "<cognito-client-secret>"
        if not data:
            raise InstallerError(f"Cannot read Cognito client {client_id}",
                                 "Your AWS user needs cognito-idp:DescribeUserPoolClient.")
        return data["UserPoolClient"]["ClientSecret"]

    issuer = f"https://cognito-idp.{ctx.region}.amazonaws.com/{pool}"
    ctx.cache["cognito"] = {
        "issuer": issuer,
        "jwks_url": issuer + "/.well-known/jwks.json",
        "client_secret": client_secret(identity["ClientId"]),
        "admin_client_secret": client_secret(identity["AdminClientId"]),
    }


def chart_defaults(ctx: Context) -> dict:
    """The default values of the chart release being installed (its placeholders are still in place)."""
    repository, version = chart_ref(ctx)
    result = ctx.cloud.runner.run(["helm", "show", "values", repository, *(["--version", version] if version else [])])
    return yaml.safe_load(result.stdout) or {}


def render(ctx: Context) -> dict:
    return values_pkg.build(ctx, chart_defaults(ctx))


# ---- chart access ----------------------------------------------------------
def chart_ref(ctx: Context) -> tuple[str, str]:
    """(oci reference, version) of the chart to deploy."""
    """(chart reference, version). A local chart directory (vendor pre-publish check) has no version."""
    local = ctx.cache.get("local_chart")
    if local:
        return str(local), None
    return ctx.release["chart"]["repository"], ctx.release["chart"]["version"]


def registry_login(cloud: Cloud, repository: str) -> None:
    """Log Helm in to the registry that hosts `repository` (an oci:// reference)."""
    host = repository.removeprefix("oci://").split("/", 1)[0]
    match = re.search(r"\.ecr\.([a-z0-9-]+)\.amazonaws\.com$", host)
    region = match.group(1) if match else cloud.region        # ECR tokens are valid only in the registry's region
    argv = ["aws", "ecr", "get-login-password", "--region", region]
    if cloud.profile:
        argv += ["--profile", cloud.profile]
    token = cloud.runner.run(argv)
    cloud.runner.run(["helm", "registry", "login", "--username", "AWS", "--password-stdin", host],
                     input_text=token.stdout)


# ---- install / upgrade -----------------------------------------------------
def helm_upgrade(ctx: Context, say: Callable[[str], None]) -> None:
    repository, version = chart_ref(ctx)
    release = ctx.release["helm_release"]
    document = render(ctx)

    handle, path = tempfile.mkstemp(prefix="dial-values-", suffix=".yaml")
    try:
        os.fchmod(handle, 0o600)                       # the file contains secrets
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(document, stream, default_flow_style=False, allow_unicode=True)
        say(f"  helm upgrade --install {release['name']} (chart {version or 'local ' + repository})")
        ctx.cloud.helm(
            "upgrade", "--install", release["name"], repository,
            *(["--version", version] if version else []),             # a local chart directory has no version to pin
            "--namespace", release["namespace"], "--create-namespace",
            "--values", path,
            "--timeout", "15m",
            write=True, stream=True,
        )
    finally:
        os.unlink(path)


def installed(ctx: Context) -> dict | None:
    """Helm's view of the release: {'chart': ..., 'revision': ..., 'status': ...} or None."""
    release = ctx.release["helm_release"]
    result = ctx.cloud.helm("list", "--namespace", release["namespace"], "--filter", f"^{release['name']}$",
                            "--output", "json", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    rows = json.loads(result.stdout)
    if not rows:
        return None
    row = rows[0]
    chart = row["chart"]                                # e.g. naya-helm-deployment-2026.10.1
    return {"chart": chart, "revision": int(row["revision"]), "status": row["status"],
            "chart_version": chart.split("-", 3)[-1] if chart.count("-") >= 3 else chart}


def live_values(ctx: Context) -> dict:
    release = ctx.release["helm_release"]
    result = ctx.cloud.helm("get", "values", release["name"], "--namespace", release["namespace"], "--output", "json")
    return json.loads(result.stdout or "{}") or {}


# ---- diff between live and new values (secrets are never printed) ------------
def diff(old: Any, new: Any, path: str = "") -> list[str]:
    changes: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            here = f"{path}.{key}" if path else str(key)
            if key not in old:
                changes.append(f"+ {here}")
            elif key not in new:
                changes.append(f"- {here}")
            else:
                changes.extend(diff(old[key], new[key], here))
    elif old != new:
        label = "(value hidden)" if _SENSITIVE.search(path) else f"{_short(old)} -> {_short(new)}"
        changes.append(f"~ {path}: {label}")
    return changes


def _short(value: Any) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= 60 else text[:57] + "..."


# ---- verification -----------------------------------------------------------
def wait_until_ready(ctx: Context, say: Callable[[str], None], timeout_minutes: int = 20) -> list[str]:
    """Wait for every Deployment/StatefulSet of the release. Returns names that did not become ready."""
    if ctx.cloud.runner.dry_run:
        return []
    namespace = ctx.release["helm_release"]["namespace"]
    pending: list[str] = []
    deadline = time.monotonic() + timeout_minutes * 60
    for kind in ("deployment", "statefulset"):
        listing = ctx.cloud.kubectl_json("-n", namespace, "get", kind, check=False) or {"items": []}
        for item in listing["items"]:
            name = item["metadata"]["name"]
            remaining = max(int(deadline - time.monotonic()), 30)
            say(f"  waiting for {kind}/{name}")
            result = ctx.cloud.kubectl("-n", namespace, "rollout", "status", f"{kind}/{name}",
                                       f"--timeout={remaining}s", check=False)
            if result.returncode != 0:
                pending.append(f"{kind}/{name}")
    return pending


def ingress_hostnames(ctx: Context) -> dict[str, str]:
    """host name -> load balancer DNS name, for the DNS records the customer must create."""
    namespace = ctx.release["helm_release"]["namespace"]
    listing = ctx.cloud.kubectl_json("-n", namespace, "get", "ingress", check=False) or {"items": []}
    records: dict[str, str] = {}
    for item in listing["items"]:
        balancer = item.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        target = balancer[0].get("hostname", "") if balancer else ""
        for rule in item["spec"].get("rules", []):
            records[rule["host"]] = target or "(still provisioning)"
    return dict(sorted(records.items()))


def rollback_command(ctx: Context, revision: int) -> str:
    release = ctx.release["helm_release"]
    return (f"helm rollback {release['name']} {revision} --namespace {release['namespace']} "
            f"--kube-context {ctx.cloud.kube_context}")
