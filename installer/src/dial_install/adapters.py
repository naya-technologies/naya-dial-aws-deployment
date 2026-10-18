"""Display names of the model adapters in DIAL Admin.

DIAL Core has no notion of an adapter: it only knows each model's endpoint. DIAL Admin derives one adapter per base
endpoint and names it with a random UUID. This step gives those adapters the display name from `installer.yaml`
(`adapters:`), so that the Admin portal shows "AWS Private Bedrock" instead of a UUID.

It runs inside the cluster, where Admin is reachable, as a short-lived pod that reads the API key of the DIAL Admin
project from Core's own configuration Secret; no secret value is passed on a command line or printed.
Cosmetic by design: a failure is reported as a warning and never stops an installation.
"""

from __future__ import annotations

import json
from typing import Callable

from . import values as values_pkg
from .context import Context

POD = "dial-adapter-names"
Say = Callable[[str], None]

_SCRIPT = r'''
import json, os, time, urllib.parse, urllib.request
base = os.environ["ADMIN_URL"]
want = json.loads(os.environ["WANT"])
config = json.load(open("/mnt/core/aidial.config.json"))
key = next(k for k, v in config["keys"].items() if v.get("project") == "DIAL Admin")

def call(method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Api-Key": key, "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.build_opener().open(request, timeout=30) as response:
            raw = response.read()
            return (json.loads(raw) if raw else None), response.headers
    except urllib.error.HTTPError as error:
        raise SystemExit(f"{method} {path} -> HTTP {error.code}: {error.read().decode(errors='replace')[:400]}")

deadline = time.time() + 180
while True:                      # Admin imports the models shortly after it starts
    found = [a for a in call("GET", "/api/v1/adapters")[0] if a.get("baseEndpoint") in want]
    if found or time.time() > deadline:
        break
    time.sleep(5)
for adapter in found:
    wanted = want[adapter["baseEndpoint"]]
    if adapter.get("displayName") == wanted:
        continue
    path = "/api/v1/adapters/" + urllib.parse.quote(adapter["name"], safe="")
    current, headers = call("GET", path, headers={"If-None-Match": "0"})   # Admin uses optimistic locking: PUT needs the current ETag
    etag = headers.get("ETag")
    current["displayName"] = wanted
    current.pop("createdAt", None)
    current.pop("updatedAt", None)
    call("PUT", path, current, {"If-Match": etag} if etag else None)
    print("renamed", adapter["name"])
print("adapters:", len(found), "checked")
'''


def wanted(ctx: Context) -> dict[str, str]:
    """base endpoint -> display name, for the adapters named in installer.yaml."""
    helm = ctx.release["helm_release"]
    names = ctx.release.get("adapters", {})
    result = {}
    for component, spec in names.items():
        base = f"http://{helm['name']}-{component}.{helm['namespace']}.svc.cluster.local/openai/deployments/"
        result[base] = spec["display_name"]
    return result


def name_adapters(ctx: Context, say: Say, chart_defaults: dict) -> None:
    targets = wanted(ctx)
    if not targets or ctx.cloud.runner.dry_run:
        return
    helm = ctx.release["helm_release"]
    namespace = helm["namespace"]
    effective = values_pkg.effective(ctx, chart_defaults)
    image = effective["quickapps"]["image"]                      # any component image that ships Python
    reference = f"{image['registry']}/{image['repository']}:{image['tag']}"
    overrides = {"spec": {
        "nodeSelector": {"workload": "core"},
        "volumes": [{"name": "core", "secret": {"secretName": f"{helm['name']}-core",
                                                "items": [{"key": "aidial.config.json", "path": "aidial.config.json"}]}}],
        "containers": [{
            "name": POD, "image": reference, "imagePullPolicy": "IfNotPresent",
            "command": ["python", "-c", _SCRIPT],
            "env": [{"name": "ADMIN_URL", "value": f"http://{helm['name']}-admin-backend.{namespace}.svc.cluster.local"},
                    {"name": "WANT", "value": json.dumps(targets)}],
            "volumeMounts": [{"name": "core", "mountPath": "/mnt/core", "readOnly": True}],
        }],
    }}
    kube = ctx.cloud.kubectl
    say("  naming the model adapters in DIAL Admin")
    kube("-n", namespace, "delete", "pod", POD, "--ignore-not-found", "--wait=true", write=True, check=False)
    started = kube("-n", namespace, "run", POD, "--image", reference, "--restart=Never", "--overrides",
                   json.dumps(overrides), write=True, check=False)
    finished = started if started.returncode != 0 else kube(
        "-n", namespace, "wait", f"pod/{POD}", "--for=jsonpath={.status.phase}=Succeeded", "--timeout=240s",
        write=True, check=False)
    logs = kube("-n", namespace, "logs", POD, check=False).stdout.strip()
    kube("-n", namespace, "delete", "pod", POD, "--ignore-not-found", "--wait=false", write=True, check=False)
    if finished.returncode != 0:
        say("  warning: could not set the adapter display names (the installation is not affected): "
            + (finished.stderr or finished.stdout or logs).strip().splitlines()[-1][:200])
