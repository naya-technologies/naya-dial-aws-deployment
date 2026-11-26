"""The DIAL groups (permission levels) and the first administrator, in the Cognito user pool of the installation.

Three groups, one per permission level, with names the customer chooses at `init` (`identity.groups`, default `user`, `developer`,
`admin`). Members inherit the permissions of their group; the licensing and user management engine only manages who is allowed in.

The first administrator: when the admin group has no member yet, the installer creates the user `admin@<domain>` with the
initial password below and adds it to the admin group, so someone can sign in to DIAL Admin right after the installation and add
users (or set up federated sign-in). Cognito makes the user choose a new password at the first sign-in. The user name is an
e-mail address because the user pool signs users in by e-mail. Once an administrator exists the installer never creates or changes
one again, so deleting this user after real administrators exist is safe.
"""

from __future__ import annotations

import json
from typing import Callable

from .context import Context
from .errors import InstallerError

INITIAL_PASSWORD = "DialXWelcome123!"
LEVELS = ("user", "developer", "admin")
PRECEDENCE = {"admin": 1, "developer": 2, "user": 3}
DESCRIPTIONS = {"user": "DIAL users", "developer": "DIAL developers", "admin": "DIAL administrators (DIAL Admin, Flow Builder, Deployment Manager)"}
Say = Callable[[str], None]


def groups(cfg: dict) -> dict[str, str]:
    """permission level -> group name."""
    return {level: cfg["identity"]["groups"][level] for level in LEVELS}


def admin_username(ctx: Context) -> str:
    base = ctx.cfg["domain"].get("base") or ctx.cfg["domain"]["hosts"]["admin"].split(".", 1)[-1]
    return f"admin@{base}"


def _pool(ctx: Context) -> str:
    return ctx.outputs["identity"]["UserPoolId"]


def ensure_groups(ctx: Context, say: Say) -> None:
    """Create the groups that do not exist yet (a group created by hand with the same name is kept as it is)."""
    pool = _pool(ctx)
    existing = {g["GroupName"] for g in (ctx.cloud.aws_json("cognito-idp", "list-groups", "--user-pool-id", pool,
                                                             check=False) or {}).get("Groups", [])}
    for level, name in groups(ctx.cfg).items():
        if name in existing:
            continue
        say(f"  creating the Cognito group {name} ({level})")
        ctx.cloud.aws("cognito-idp", "create-group", "--user-pool-id", pool, "--group-name", name,
                      "--description", DESCRIPTIONS[level], "--precedence", str(PRECEDENCE[level]), write=True)


def ensure_admin(ctx: Context, say: Say) -> dict:
    """The first administrator. Returns {"username", "password" (None when unknown), "created"}."""
    pool = _pool(ctx)
    admin_group = groups(ctx.cfg)["admin"]
    username = admin_username(ctx)
    members = (ctx.cloud.aws_json("cognito-idp", "list-users-in-group", "--user-pool-id", pool, "--group-name", admin_group,
                                  check=False) or {}).get("Users", [])
    ours = next((m for m in members if _email(m) == username or m.get("Username") == username), None)
    if members and not ours:
        return {"username": None, "password": None, "created": False}          # the customer has administrators of its own
    if ours:
        waiting = ours.get("UserStatus") == "FORCE_CHANGE_PASSWORD"         # nobody signed in yet: the initial password still works
        return {"username": username, "password": INITIAL_PASSWORD if waiting else None, "created": False}
    found = ctx.cloud.aws("cognito-idp", "admin-get-user", "--user-pool-id", pool, "--username", username, check=False)
    created = found.returncode != 0
    if created:
        say(f"  creating the first administrator {username}")
        result = ctx.cloud.aws("cognito-idp", "admin-create-user", "--user-pool-id", pool, "--username", username,
                               "--temporary-password", INITIAL_PASSWORD, "--message-action", "SUPPRESS",
                               "--user-attributes", f"Name=email,Value={username}", "Name=email_verified,Value=true",
                               "Name=name,Value=Administrator", write=True, check=False)
        if result.returncode != 0:
            raise InstallerError(f"Could not create the first administrator {username} in Cognito.\n{(result.stderr or '').strip()[-400:]}",
                                 "Your AWS user needs cognito-idp:AdminCreateUser. Run `./dial-install apply` again; it continues.")
    ctx.cloud.aws("cognito-idp", "admin-add-user-to-group", "--user-pool-id", pool, "--username", username,
                  "--group-name", admin_group, write=True)
    status = "FORCE_CHANGE_PASSWORD" if created else json.loads(found.stdout or "{}").get("UserStatus")
    return {"username": username, "password": INITIAL_PASSWORD if status == "FORCE_CHANGE_PASSWORD" else None, "created": created}


def _email(user: dict) -> str | None:
    return next((a["Value"] for a in user.get("Attributes", []) if a["Name"] == "email"), None)


def prepare(ctx: Context, say: Say) -> dict | None:
    """Groups and the first administrator. None in a dry run."""
    if ctx.cloud.runner.dry_run:
        return None
    say("\nUsers and groups")
    ensure_groups(ctx, say)
    return ensure_admin(ctx, say)


def interfaces(ctx: Context) -> list[tuple[str, str]]:
    """(name, address) of every interface of the installation, in the order a person needs them."""
    hosts = ctx.cfg["domain"]["hosts"]
    rows = [("Chat", hosts["chat"]), ("Admin", hosts["admin"]), ("Flow Builder", hosts["flows"]), ("DIAL API (Core)", hosts["core"]),
            ("Themes", hosts["themes"])]
    if ctx.cfg["features"]["analytics"]:
        rows += [("Grafana", hosts["grafana"]), ("Superset", hosts["superset"])]
    return [(name, f"https://{host}") for name, host in rows]


def summary(ctx: Context, records: dict[str, str], account: dict | None) -> str:
    """The message at the end of an installation: where everything is, the DNS records to create, and how to sign in."""
    width = max(len(name) for name, _ in interfaces(ctx))
    lines = ["", "=" * 78, f"  DIAL is installed: {ctx.name} ({ctx.region})", "=" * 78, "", "  Interfaces"]
    lines += [f"    {name:<{width}}  {url}" for name, url in interfaces(ctx)]
    lines += ["", "  DNS records to create (CNAME, or ALIAS to the load balancer in Route 53)"]
    if records:
        host_width = max(len(host) for host in records)
        lines += [f"    {host:<{host_width}}  ->  {target}" for host, target in records.items()]
    else:
        lines.append("    (the load balancer is not ready yet: run ./dial-install status in a few minutes)")
    groups_line = ", ".join(f"{name} ({level})" for level, name in groups(ctx.cfg).items())
    lines += ["", "  First sign-in"]
    if account and account.get("username"):
        lines += [f"    Open        https://{ctx.cfg['domain']['hosts']['admin']}",
                  f"    User name   {account['username']}"]
        if account.get("password"):
            lines += [f"    Password    {account['password']}",
                      "    You choose a new password at the first sign-in (this one is valid for 7 days)."]
        else:
            lines.append("    Password    the one its owner set (the initial password has been changed)")
        lines += ["    Then add users in Cognito (or set up federated sign-in) and put each one in a group:"]
    else:
        lines += ["    The administrator group already has members; sign in with one of them.",
                  "    Users get their permissions from their group:"]
    lines += [f"    {groups_line}", f"    User pool: {_pool(ctx)}", "=" * 78]
    return "\n".join(lines)
