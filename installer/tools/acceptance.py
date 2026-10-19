#!/usr/bin/env python3
"""End-to-end acceptance check of a DIAL installation made by dial-install (vendor tool, for test environments).

It uses only what a customer would have: the AWS account, the load balancer, a Cognito user. Run it after `apply`:

    python3 tools/acceptance.py -c dial.yaml --profile <aws-profile> [--create-user] [--since 2026-10-19T09:00:00Z]

Without --create-user it expects ADMIN_EMAIL / ADMIN_PASSWORD in the environment (an existing user of the group `admin`).
`--create-user` creates admin@<base domain> with a random password that it prints (a throw-away test user; never use it in production).
It prints PASS or FAIL for every check and exits with 1 if any failed. It changes nothing except the optional test user.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dial_install import config  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return ok


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


class Env:
    def __init__(self, args):
        raw = config.load(Path(args.config))
        self.name = raw["name"]
        self.region = raw["aws"]["region"]
        self.profile = args.profile
        base = raw["domain"].get("base")
        self.hosts = {s: raw["domain"].get("hosts", {}).get(s) or f"{s}.{base}" for s in config.SERVICES}
        self.base = base
        self.context = f"dial-{self.name}"
        self.secret_arn = raw["secrets"]["arn"]

    def aws(self, *a):
        return run(["aws", *a, "--profile", self.profile, "--region", self.region, "--output", "json"])

    def aws_json(self, *a):
        out = self.aws(*a)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None

    def kubectl(self, *a, **kw):
        return run(["kubectl", "--context", self.context, "-n", "dial", *a], **kw)


def stack_outputs(env: Env, stack: str) -> dict:
    data = env.aws_json("cloudformation", "describe-stacks", "--stack-name", f"{env.name}-{stack}")
    return {o["OutputKey"]: o["OutputValue"] for o in data["Stacks"][0]["Outputs"]} if data else {}


def load_balancer_ip(env: Env) -> tuple[str, str]:
    listing = json.loads(env.kubectl("get", "ingress", "-o", "json").stdout or '{"items":[]}')
    for item in listing["items"]:
        balancer = item.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        if balancer:
            name = balancer[0]["hostname"]
            return name, socket.gethostbyname(name)
    raise SystemExit("no load balancer yet")


def http(env: Env, ip: str, host: str, path: str, *, method="GET", body=None, headers=None, timeout=60):
    """Call https://host/path through the load balancer IP (no DNS needed); returns (status, text)."""
    context = ssl.create_default_context()
    connection_target = (ip, 443)

    class Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            import http.client

            class Conn(http.client.HTTPSConnection):
                def connect(self_inner):
                    sock = socket.create_connection(connection_target, timeout=timeout)
                    self_inner.sock = context.wrap_socket(sock, server_hostname=host)
            return self.do_open(Conn, req)

    request = urllib.request.Request(f"https://{host}{path}", method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(Handler, _NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except Exception as error:  # noqa: BLE001
        return 0, str(error)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get_token(env: Env, args) -> str | None:
    identity = stack_outputs(env, "identity")
    pool, client = identity["UserPoolId"], identity["ClientId"]
    email = os.environ.get("ADMIN_EMAIL") or f"admin@{env.base}"
    password = os.environ.get("ADMIN_PASSWORD") or "Aa1!" + secrets.token_urlsafe(12)
    if args.create_user:
        print(f"      test user {email} / password {password}")
    if args.create_user:
        env.aws("cognito-idp", "admin-create-user", "--user-pool-id", pool, "--username", email, "--user-attributes",
                f"Name=email,Value={email}", "Name=email_verified,Value=true", "--message-action", "SUPPRESS")
        env.aws("cognito-idp", "admin-set-user-password", "--user-pool-id", pool, "--username", email, "--password", password, "--permanent")
        env.aws("cognito-idp", "create-group", "--user-pool-id", pool, "--group-name", "admin")
        env.aws("cognito-idp", "admin-add-user-to-group", "--user-pool-id", pool, "--username", email, "--group-name", "admin")
    secret = env.aws_json("cognito-idp", "describe-user-pool-client", "--user-pool-id", pool, "--client-id", client)["UserPoolClient"]["ClientSecret"]
    digest = base64.b64encode(hmac.new(secret.encode(), (email + client).encode(), hashlib.sha256).digest()).decode()
    out = env.aws_json("cognito-idp", "initiate-auth", "--auth-flow", "USER_PASSWORD_AUTH", "--client-id", client, "--auth-parameters",
                       f"USERNAME={email},PASSWORD={password},SECRET_HASH={digest}")
    return out["AuthenticationResult"]["IdToken"] if out else None


def exec_in_cluster(env: Env, script: str, *argv: str) -> str:
    done = env.kubectl("exec", "deploy/dial-quickapps", "--", "python", "-c", script, *argv)
    return done.stdout.strip() if done.returncode == 0 else f"ERROR {done.stderr.strip()[-200:]}"


def cloudtrail_checkouts(env: Env, since: str) -> list[str]:
    data = env.aws_json("cloudtrail", "lookup-events", "--lookup-attributes", "AttributeKey=EventName,AttributeValue=CheckoutLicense",
                        "--start-time", since, "--max-items", "200")
    return sorted(str(e["EventTime"])[:19] for e in (data or {}).get("Events", []))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default="dial.yaml")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--create-user", action="store_true")
    parser.add_argument("--since", help="UTC time of the installation start, to count license checkouts (default: last 6 hours)")
    args = parser.parse_args()
    env = Env(args)

    # 1. every workload is ready
    pods = json.loads(env.kubectl("get", "deploy,statefulset", "-o", "json").stdout or '{"items":[]}')["items"]
    unready = [p["metadata"]["name"] for p in pods if (p.get("status", {}).get("readyReplicas") or 0) < (p["spec"].get("replicas") or 0)]
    check(bool(pods) and not unready, "all workloads ready", f"{len(pods)} workloads" + (f", not ready: {unready}" if unready else ""))
    for needed in ("dial-core", "dial-chat", "dial-quickapps", "dial-flows-core", "dial-flows", "dial-admin-backend"):
        check(any(p["metadata"]["name"] == needed for p in pods), f"workload {needed} exists")
    check(not any(p["metadata"]["name"].endswith("lic-ins") for p in pods), "dial-lic-ins is gone after its first run")

    name, ip = load_balancer_ip(env)
    print(f"      load balancer {name} ({ip})")
    token = get_token(env, args)
    check(bool(token), "sign in with a Cognito user")
    if not token:
        return 1
    auth = {"Authorization": f"Bearer {token}"}
    core = env.hosts["core"]

    # 2. DIAL Core API
    status, body = http(env, ip, core, "/openai/models", headers=auth)
    models = json.loads(body)["data"] if status == 200 else []
    model = next((m for m in models if m["id"] == "default.model"), {})
    check(status == 200 and bool(model), "default.model is registered", f"HTTP {status}")
    check(model.get("features", {}).get("temperature") is False, "default.model does not accept temperature")
    check("hidden" in (model.get("description_keywords") or []), "default.model is hidden")
    check(http(env, ip, core, "/v1/bucket", headers=auth)[0] == 200, "licensed API call /v1/bucket (200)")
    check(http(env, ip, core, "/v1/user/info", headers=auth)[0] == 200, "/v1/user/info (200)")
    status, body = http(env, ip, core, "/openai/applications", headers=auth)
    check(status == 200 and "managed-model-chat" in body, "application managed-model-chat is registered")
    status, body = http(env, ip, core, "/v1/application_type_schemas/schemas?api-version=2025-01-01-preview", headers=auth)
    names = [s.get("dial:applicationTypeDisplayName") for s in (json.loads(body) if status == 200 else [])]
    check("Agent Builder" in names and "Flow Builder" in names, "application types Agent Builder and Flow Builder", f"found {names}")

    # 3. a real answer through the path the chat UI uses
    status, body = http(env, ip, core, "/openai/deployments/managed-model-chat/chat/completions?api-version=2025-01-01-preview", method="POST",
                        headers=auth, timeout=180, body={"model": "managed-model-chat", "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                                                         "stream": False, "custom_fields": {"configuration": {"model_id": "default.model"}}})
    text = ""
    try:
        text = json.loads(body)["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        text = body[:150]
    check(status == 200 and "pong" in text.lower(), "chat answers through managed-model-chat", f"HTTP {status}: {text.strip()[:60]}")

    # 4. what is reachable from outside
    for service, expected in (("chat", (200,)), ("themes", (200,)), ("admin", (200, 307)), ("grafana", (200, 302)), ("superset", (200, 302)), ("flows", (200,))):
        code = http(env, ip, env.hosts[service], "/")[0]
        check(code in expected, f"https://{env.hosts[service]}/ answers", f"HTTP {code}")
    flows_core = env.hosts["flows-core"]
    check(http(env, ip, flows_core, "/auth/login")[0] in (200, 302, 307), "Flow Builder sign-in endpoint /auth/login is reachable")
    check(http(env, ip, flows_core, "/openai/deployments/flow-builder/chat/completions", method="POST", body={})[0] in (403, 404, 503),
          "Flow Builder runner /openai is NOT reachable from outside")
    check(http(env, ip, flows_core, "/")[0] in (403, 404, 503), "Flow Builder backend root / is NOT reachable from outside")

    # 5. inside the cluster: the Flow Builder runner and the catalog the chat server reads
    probe = ("import sys,urllib.request,json\nu,h=sys.argv[1],json.loads(sys.argv[2])\n"
             "r=urllib.request.Request(u,headers=h)\n"
             "try:\n  x=urllib.request.build_opener().open(r,timeout=30); print(x.status)\n"
             "except Exception as e:\n  print(getattr(e,'code',e))\n")
    base = "http://dial-flows-core.dial.svc.cluster.local"
    check(exec_in_cluster(env, probe, base + "/v1/configuration-support/application-schema", "{}") == "200", "Flow Builder schema endpoint answers inside the cluster")
    check(exec_in_cluster(env, probe, base + "/api/flows?source=published", json.dumps(auth)) == "200", "published-flow catalog answers with a user token")

    # 6. DIAL Admin: sync and adapter name
    key_json = env.aws_json("secretsmanager", "get-secret-value", "--secret-id", env.secret_arn)
    admin_key = json.loads(key_json["SecretString"])["core_admin_api_key"] if key_json else ""
    admin_probe = ("import sys,json,urllib.request as u\nb='http://dial-admin-backend.dial.svc.cluster.local'\nk=sys.argv[1]\n"
                   "def c(p,h=None):\n  r=u.Request(b+p,headers={'Api-Key':k,**(h or {})}); x=u.build_opener().open(r,timeout=30); return x.headers,json.loads(x.read() or 'null')\n"
                   "h,_=c('/api/v1/models/default.model',{'If-None-Match':'0'})\n"
                   "print(json.dumps({'sync':c('/api/v1/models/default.model/sync-state',{'If-Match':h['ETag']})[1].get('status'),"
                   "'adapters':[a['displayName'] for a in c('/api/v1/adapters')[1]]}))\n")
    out = exec_in_cluster(env, admin_probe, admin_key)
    try:
        info = json.loads(out)
    except Exception:  # noqa: BLE001
        info = {}
    check(info.get("sync") == "fully_synced", "Admin: default.model is fully_synced", out[:80] if not info else "")
    check("AWS Private Bedrock" in info.get("adapters", []), "Admin: the adapter is named AWS Private Bedrock")

    # 7. license
    since = args.since or run(["date", "-u", "-d", "6 hours ago", "+%Y-%m-%dT%H:%M:%SZ"]).stdout.strip()
    checkouts = cloudtrail_checkouts(env, since)
    print(f"      CheckoutLicense events since {since}: {checkouts}")
    check(len(checkouts) == 1, "exactly one license unit consumed so far", f"{len(checkouts)} checkout(s) (CloudTrail can lag 5-15 minutes)")

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
