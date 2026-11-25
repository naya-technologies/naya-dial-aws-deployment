"""Steps of the Bedrock Guardrail interceptor add-on that the catalog cannot express (see dial_install.addons.hooks).

- validate:          DRAFT is refused unless --allow-draft.
- preflight:         the guardrail must exist (bedrock get-guardrail) before anything is created.
- stack_parameters:  the add-on's own IAM role: trusted by its own service account only, ApplyGuardrail on this guardrail only.
- verify:            the pod runs as that role (sts get-caller-identity inside the pod), and a real chat request through DIAL Core
                     to an attached deployment returns the model's answer. Failures are explained with the troubleshooting map.

Security, kept visible: AWS access comes only from IRSA on the interceptor's own service account. Never give the container AWS_*
credentials (temporary SSO credentials included).
"""

from __future__ import annotations

import json
import re

from dial_install.errors import InstallerError

CHAT_POD = "dial-guardrail-check"
CHAT_PROMPT = "Reply with the single word OK."

# (text found in the answer or in the interceptor's log) -> what it means and what to do
TROUBLESHOOTING = [
    (r"POST /\?api-version", "DIAL Core calls the interceptor without a path (404 on \"POST /?api-version=...\"; chat says the model "
                             "could not be found). Its Completion endpoint must be <service>/openai/deployments/bedrock-guardrail/chat/completions. "
                             "If someone changed it in DIAL Admin, run `./dial-install add-ons upgrade bedrock-guardrail` to restore it."),
    (r"NoCredentialsError", "The interceptor has no AWS credentials: its service account is not annotated with its role, or the pod "
                            "started before the annotation. Run `./dial-install add-ons upgrade bedrock-guardrail` (it restarts the pod)."),
    (r"AccessDeniedException", "The role may not call ApplyGuardrail on this guardrail, or the guardrail is in another account or region "
                               "(--bedrock-region)."),
    (r"NoRegionError", "BEDROCK_REGION is missing in the interceptor's environment."),
    (r"ResourceNotFoundException|ValidationException", "Wrong guardrail id or version (--guardrail-id, --guardrail-version)."),
    (r"safety check is temporarily unavailable", "The interceptor could not reach Amazon Bedrock. See its log: "
                                                 "kubectl logs deployment/<release>-bedrock-guardrail."),
]


def diagnose(text: str) -> list[str]:
    return [meaning for pattern, meaning in TROUBLESHOOTING if re.search(pattern, text or "", re.I)]


def validate(ctx, entry, options):
    if options["guardrail_version"] == "DRAFT" and options["allow_draft"] != "true":
        raise InstallerError("The guardrail version DRAFT is refused: its rules can change at any time without a new version.",
                             "Publish a numbered version in the Amazon Bedrock console and pass --guardrail-version <number>, "
                             "or add --allow-draft (for testing only).")


def guardrail_arn(ctx, options) -> str:
    return f"arn:aws:bedrock:{options['bedrock_region']}:{ctx.account_id()}:guardrail/{options['guardrail_id']}"


def preflight(ctx, entry, options, say):
    guardrail, version, region = options["guardrail_id"], options["guardrail_version"], options["bedrock_region"]
    say("  Security: the interceptor gets AWS access only through its own IAM role (IRSA on its own service account), which may call "
        "ApplyGuardrail on this guardrail only. Never give it AWS_* credentials.")
    say(f"  Checking guardrail {guardrail} version {version} in {region}")
    if version == "DRAFT":
        say("  WARNING: the DRAFT version is used (--allow-draft). Do not use it in production.")
    result = ctx.cloud.aws("bedrock", "get-guardrail", "--guardrail-identifier", guardrail, "--guardrail-version", version,
                           region=region, check=False)
    if result.returncode == 0:
        return
    text = (result.stderr or result.stdout or "").strip()
    if re.search(r"ResourceNotFound|ValidationException", text):
        hint = "Check --guardrail-id, --guardrail-version and --bedrock-region (the guardrail must be in this account)."
    elif re.search(r"AccessDenied|UnauthorizedOperation", text):
        hint = "Your AWS user needs bedrock:GetGuardrail to check it."
    else:
        hint = "Check that Amazon Bedrock is available in that region, then run the command again."
    raise InstallerError(f"Guardrail {guardrail} version {version} was not found in {region}. Nothing was installed.\n{text[-400:]}", hint)


def stack_parameters(ctx, entry, options) -> dict:
    compute = ctx.outputs["compute"]
    helm = ctx.release["helm_release"]
    return {
        "OIDCProviderArn": compute["OIDCProviderArn"],
        "OIDCProviderIssuer": compute["OIDCProviderUrl"].split("https://", 1)[-1],
        "KubernetesNamespace": helm["namespace"],
        "ServiceAccountName": f"{helm['name']}-{entry['service']}",
        "GuardrailArn": guardrail_arn(ctx, options),
    }


def _role_name(arn: str) -> str:
    """'arn:aws:iam::1:role/NAME' or 'arn:aws:sts::1:assumed-role/NAME/session' -> NAME."""
    if ":assumed-role/" in arn:
        return arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
    return arn.rsplit("/", 1)[-1]


def _logs(ctx, deployment: str) -> str:
    namespace = ctx.release["helm_release"]["namespace"]
    return ctx.cloud.kubectl("-n", namespace, "logs", f"deployment/{deployment}", "--tail=200", check=False).stdout or ""


def _fail(ctx, deployment: str, what: str, seen: str) -> None:
    reasons = diagnose(seen + "\n" + _logs(ctx, deployment))
    raise InstallerError(f"The Bedrock Guardrail interceptor check failed: {what}.",
                         "\n".join(reasons) if reasons else
                         f"See its log: kubectl --context {ctx.cloud.kube_context} -n {ctx.release['helm_release']['namespace']} "
                         f"logs deployment/{deployment}")


def check_identity(ctx, entry, outputs) -> None:
    """The pod must run as the add-on's role (IRSA), not as anything else."""
    helm = ctx.release["helm_release"]
    deployment = f"{helm['name']}-{entry['service']}"
    code = f"import boto3;print(boto3.client('sts', region_name='{ctx.region}').get_caller_identity()['Arn'])"
    result = ctx.cloud.kubectl("-n", helm["namespace"], "exec", f"deployment/{deployment}", "-c", "application", "--",
                               "python", "-c", code, check=False)
    seen = (result.stdout or "").strip()
    expected = _role_name(outputs["RoleArn"])
    if result.returncode != 0 or _role_name(seen.splitlines()[-1] if seen else "") != expected:
        _fail(ctx, deployment, f"the pod does not run as the role {expected} (it reports: {seen or result.stderr.strip()[-300:]})",
              (result.stderr or "") + seen)


_CHAT_SCRIPT = r'''
import json, os, urllib.error, urllib.request
config = json.load(open("/mnt/core/aidial.config.json"))
key = next(k for k, v in config["keys"].items() if v.get("project") == "DIAL Admin")
url = os.environ["CORE"] + "/openai/deployments/" + os.environ["DEPLOYMENT"] + "/chat/completions?api-version=2024-02-01"
body = json.dumps({"messages": [{"role": "user", "content": os.environ["PROMPT"]}]}).encode()
request = urllib.request.Request(url, data=body, method="POST", headers={"Api-Key": key, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=240) as response:
        answer = json.load(response)["choices"][0]["message"].get("content") or ""
        print("STATUS 200")
        print("ANSWER " + answer[:300].replace("\n", " "))
except urllib.error.HTTPError as error:
    print("STATUS %d" % error.code)
    print("BODY " + error.read().decode(errors="replace")[:2000])
'''


def check_chat(ctx, entry, options) -> None:
    """A normal chat request through DIAL Core to an attached deployment must return the model's answer."""
    helm = ctx.release["helm_release"]
    namespace = helm["namespace"]
    deployment = f"{helm['name']}-{entry['service']}"
    attached = [name for name in options["guardrail_deployments"].split(",") if name]
    target = attached[0] if attached and options["guardrail_global"] != "true" else ctx.release["models"][0]["key"]
    from dial_install import addons
    image = f"{addons.image_registry(ctx, entry)}/{entry['images'][0]['repository']}:{entry['version']}"   # it has Python
    overrides = {"spec": {
        "nodeSelector": {"workload": "core"},
        "volumes": [{"name": "core", "secret": {"secretName": f"{helm['name']}-core",
                                                "items": [{"key": "aidial.config.json", "path": "aidial.config.json"}]}}],
        "containers": [{
            "name": CHAT_POD, "image": image, "imagePullPolicy": "IfNotPresent", "command": ["python", "-c", _CHAT_SCRIPT],
            "env": [{"name": "CORE", "value": f"http://{helm['name']}-core.{namespace}.svc.cluster.local"},
                    {"name": "DEPLOYMENT", "value": target}, {"name": "PROMPT", "value": CHAT_PROMPT}],
            "volumeMounts": [{"name": "core", "mountPath": "/mnt/core", "readOnly": True}],
        }],
    }}
    kube = ctx.cloud.kubectl
    kube("-n", namespace, "delete", "pod", CHAT_POD, "--ignore-not-found", "--wait=true", write=True, check=False)
    kube("-n", namespace, "run", CHAT_POD, "--image", image, "--restart=Never", "--overrides", json.dumps(overrides),
         write=True, check=False)
    kube("-n", namespace, "wait", f"pod/{CHAT_POD}", "--for=jsonpath={.status.phase}=Succeeded", "--timeout=300s",
         write=True, check=False)
    output = kube("-n", namespace, "logs", CHAT_POD, check=False).stdout or ""
    kube("-n", namespace, "delete", "pod", CHAT_POD, "--ignore-not-found", "--wait=false", write=True, check=False)
    answer = next((line[7:] for line in output.splitlines() if line.startswith("ANSWER ")), "")
    if "STATUS 200" not in output or not answer.strip() or re.search(r"safety check is temporarily unavailable", answer, re.I):
        _fail(ctx, deployment, f"a chat request to {target} through DIAL Core did not return the model's answer", output)


def verify(ctx, entry, options, outputs, say):
    check_identity(ctx, entry, outputs)
    say("  the interceptor runs as its own role")
    check_chat(ctx, entry, options)
    say("  a chat request through DIAL Core returned the model's answer (answers arrive all at once: they are checked first)")
