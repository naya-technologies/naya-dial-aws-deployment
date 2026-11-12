"""The connection file for the DIAL X Desktop application (features.desktop_app).

At the end of `apply` and `upgrade` (and in `status`) the installer writes `<name>.dialx.json` from values it already knows, checks it,
and prints it. Users import this file in the application and sign in; they type no address or id.

The file holds no secret: only the addresses of the environment and the identifiers of its sign-in. It is still not published anywhere,
because it lists where the environment is.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .context import Context
from .errors import InstallerError

MAX_BYTES = 64 * 1024
REDIRECT_URI = "nayadial://auth/callback"
Say = Callable[[str], None]


class NotResolvable(Exception):
    """The host name has no DNS record (yet)."""


class Unreachable(Exception):
    """The host did not answer (refused, timed out, bad certificate...)."""


Fetch = Callable[[str], "tuple[int, str]"]


def fetch(url: str, timeout: int = 15) -> tuple[int, str]:
    """(HTTP status, start of the body). Redirects are followed; an HTTP error status is an answer, not an exception."""
    request = urllib.request.Request(url, headers={"User-Agent": "dial-install"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:  # noqa: S310 (https only)
            return response.status, response.read(300_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except urllib.error.URLError as error:
        if isinstance(error.reason, socket.gaierror):
            raise NotResolvable(urllib.parse.urlparse(url).hostname) from error
        raise Unreachable(str(error.reason)) from error
    except (TimeoutError, OSError) as error:
        raise Unreachable(str(error)) from error


@dataclass
class Check:
    name: str
    status: str          # "ok" | "fail" | "pending" (cannot be checked yet: the DNS record does not exist)
    detail: str = ""


def document(ctx: Context, managed_chat_application_id: str) -> dict:
    """The content of the file, from what the installation already knows."""
    hosts = ctx.cfg["domain"]["hosts"]
    identity = ctx.outputs["identity"]
    return {
        "name": ctx.name,
        "coreUrl": f"https://{hosts['core']}",
        "chatUrl": f"https://{hosts['chat']}",
        "adminUrl": f"https://{hosts['admin']}",
        "flowsUrl": f"https://{hosts['flows']}",
        "cognito": {
            "region": ctx.region,
            "userPoolId": identity["UserPoolId"],
            "domain": identity["CognitoHostedUIUrl"],
            "clientId": identity["DesktopClientId"],
        },
        "managedChatApplicationId": managed_chat_application_id,
    }


def _strings(node) -> list[str]:
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    return [node] if isinstance(node, str) else []


def _all_text(node) -> bool:
    if isinstance(node, dict):
        return all(_all_text(v) for v in node.values())
    return isinstance(node, str)


def _secrets_of(ctx: Context) -> list[str]:
    """Every secret value this installation holds (the customer's secret, the Cognito client secrets)."""
    found = [value for value in (ctx.secret or {}).values() if isinstance(value, str)]
    found += [value for key, value in (ctx.cache.get("cognito") or {}).items() if "secret" in key and isinstance(value, str)]
    return [value for value in found if len(value) >= 8]


def verify(ctx: Context, doc: dict, text: str, get: Fetch | None = None) -> list[Check]:
    """The checks made before the file is handed over. Each has a name; see the module docstring of the call site."""
    get = get or fetch
    checks: list[Check] = []

    try:
        parsed = json.loads(text)
        problems = []
        if len(text.encode()) >= MAX_BYTES:
            problems.append(f"{len(text.encode())} bytes, the limit is {MAX_BYTES}")
        if not _all_text(parsed):
            problems.append("a value is not text")
    except ValueError as error:
        problems = [f"not valid JSON ({error})"]
    checks.append(Check("file-format", "fail" if problems else "ok", "; ".join(problems) or "valid JSON, under 64 KB, every value is text"))

    urls = {key: value for key, value in doc.items() if key.endswith("Url")}
    not_https = [key for key, value in urls.items() if not value.startswith("https://")]
    checks.append(Check("urls-https", "fail" if not_https else "ok", ("not https: " + ", ".join(not_https)) if not_https else "all addresses start with https://"))

    leaked = [value for value in _secrets_of(ctx) if value in text]
    suspicious = re.search(r"eyJ[A-Za-z0-9_-]{10,}\.|AKIA[0-9A-Z]{12,}|\"[A-Za-z]*(secret|token|password|apiKey)[A-Za-z]*\"", text, re.I)
    checks.append(Check("no-secrets", "fail" if leaked or suspicious else "ok",
                        "the file contains a secret, token or key" if leaked or suspicious else "no secret, token or key in the file"))

    answers, pending = [], []
    for key, url in urls.items():
        try:
            status, _ = get(url)
            answers.append(f"{key} answered ({status})")
        except NotResolvable as error:
            pending.append(f"{error} has no DNS record yet")
        except Unreachable as error:
            checks.append(Check("servers-answer", "fail", f"{url} does not answer: {error}"))
            break
    else:
        if pending:
            checks.append(Check("servers-answer", "pending", "; ".join(pending) + ". Create the DNS records, then run ./dial-install status"))
        else:
            checks.append(Check("servers-answer", "ok", "; ".join(answers)))

    client = doc["cognito"]["clientId"]
    try:
        status, body = get(doc["chatUrl"] + "/config.js")
        has_client = re.search(r'"desktop"\s*:\s*\{\s*"cognitoClientId"\s*:\s*"' + re.escape(client) + '"', body) is not None
        if status == 200 and has_client:
            checks.append(Check("chat-config-has-client", "ok", "config.js of the chat contains the Desktop client id"))
        else:
            checks.append(Check("chat-config-has-client", "fail",
                                f"{doc['chatUrl']}/config.js (HTTP {status}) does not contain the Desktop client id. "
                                "The chat has not picked up the Helm setting; check that its image supports DIAL Desktop and that its pods restarted"))
    except NotResolvable as error:
        checks.append(Check("chat-config-has-client", "pending", f"{error} has no DNS record yet"))
    except Unreachable as error:
        checks.append(Check("chat-config-has-client", "fail", f"the chat does not answer: {error}"))

    query = urllib.parse.urlencode({"client_id": client, "response_type": "code", "scope": "openid email profile", "redirect_uri": REDIRECT_URI})
    try:
        status, _ = get(f"{doc['cognito']['domain']}/oauth2/authorize?{query}")
        checks.append(Check("cognito-authorize", "ok" if status == 200 else "fail",
                            "Cognito accepts the Desktop client" if status == 200 else
                            f"Cognito answered HTTP {status} for the Desktop client (expected 200). 403 \"Login pages unavailable\" "
                            "means a managed-login style is missing for this client"))
    except (NotResolvable, Unreachable) as error:
        checks.append(Check("cognito-authorize", "fail", f"the Cognito sign-in page cannot be reached: {error}"))
    return checks


def file_name(ctx: Context) -> str:
    return f"{ctx.name}.dialx.json"


def deliver(ctx: Context, say: Say, directory: Path, managed_chat_application_id: str, get: Fetch | None = None) -> Path | None:
    """Write, check and print the connection file. Does nothing when DIAL Desktop is not supported or in a dry run.
    A failed check removes the file again and stops with the name of the check."""
    if not ctx.cfg["features"]["desktop_app"] or ctx.cloud.runner.dry_run:
        return None
    doc = document(ctx, managed_chat_application_id)
    text = json.dumps(doc, indent=2) + "\n"
    path = (directory / file_name(ctx)).resolve()
    path.write_text(text, encoding="utf-8")            # a new run replaces the old file with the current values
    checks = verify(ctx, doc, text, get)
    say("\nDIAL X Desktop connection file checks:")
    for check in checks:
        say(f"  [{ {'ok': ' OK ', 'fail': 'FAIL', 'pending': 'WAIT'}[check.status] }] {check.name}: {check.detail}")
    failed = [check for check in checks if check.status == "fail"]
    if failed:
        path.unlink(missing_ok=True)
        raise InstallerError("The DIAL X Desktop connection file failed the check: " + ", ".join(check.name for check in failed) + ".",
                             "\n".join(f"{check.name}: {check.detail}" for check in failed) +
                             "\nFix it and run `./dial-install apply` (or `status`) again; the file is created again each time.")
    say(f"\nEnvironment {ctx.name} is ready.")
    say(f"Desktop connection file: {path}")
    say("Share it with your users. In DIAL X Desktop: New connection > Import file > choose the file > Save, then click the connection to sign in.")
    say("It contains no secret, but it lists where your environment is: do not publish it.")
    say("DIAL Core: nothing to add. This installation does not restrict the audience of the tokens Core accepts, so the Desktop client is accepted.")
    if any(check.status == "pending" for check in checks):
        say("Some checks could not be made yet because DNS records are missing. After you create them, run `./dial-install status` "
            "to check again and to create the file again.")
    say("\nFile content:\n" + text.rstrip())
    return path
