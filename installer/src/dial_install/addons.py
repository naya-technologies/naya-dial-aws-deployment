"""Add-ons: optional products (AI Brain, ...) installed next to the base platform from Naya's own registry.

An add-on is described by a catalog entry, `addons/<id>/addon.yaml` (see docs/ADDONS.md). Installing one:

1. copies its images from Naya's registry into the customer's private ECR (the Marketplace is not involved);
2. installs it as its own Helm release, `<platform release>-addon-<id>`, in the platform namespace;
3. registers its application types in DIAL Core, by upgrading the platform release with the add-on's schemas added
   to Core's configuration, and restarts Core so that it reads them.

Nothing about an installed add-on is kept on the machine that runs the installer. Its Helm release is the state: the
release exists (installed), its values say which version it is and which application types it registered. A later
`apply` or `upgrade` of the platform reads those values (deploy.prepare) and keeps the registrations in Core's
configuration; without that, upgrading the platform would silently unregister every add-on.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

import yaml

from . import ROOT, __version__, deploy, mirror
from .context import Context
from .errors import CommandError, InstallerError
from .values import layer_over, placeholders, substitute

Say = Callable[[str], None]
API_VERSION = "dial-addon/v1"
_ID = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_REQUIRED = ("id", "display_name", "description", "version", "source", "images", "chart", "values")


# ---- the catalog -----------------------------------------------------------------------------------------
def catalog_dir() -> Path:
    return ROOT / "addons"


def load_catalog() -> dict[str, dict]:
    """id -> catalog entry, for every add-on in this package (sorted by id)."""
    entries: dict[str, dict] = {}
    for path in sorted(catalog_dir().glob("*/addon.yaml")):
        entry = yaml.safe_load(path.read_text()) or {}
        entry["_dir"] = path.parent
        problem = _problem(entry, path)
        if problem:
            raise InstallerError(f"Add-on catalog entry {path} is not valid: {problem}",
                                 "Download the installer package again.")
        entries[entry["id"]] = entry
    return entries


def _problem(entry: dict, path: Path) -> str:
    if entry.get("apiVersion") != API_VERSION:
        return f"apiVersion must be {API_VERSION}"
    missing = [key for key in _REQUIRED if not entry.get(key)]
    if missing:
        return "missing " + ", ".join(missing)
    if not _ID.match(str(entry["id"])) or entry["id"] != path.parent.name:
        return "`id` must be lower-case letters, digits and dashes, and equal to the folder name"
    if not entry["source"].get("registry") or not all(i.get("repository") for i in entry["images"]):
        return "`source.registry` and a `repository` for every image are required"
    for key in ("chart", "values"):
        base = path.parent if key == "values" else catalog_dir()
        if not (base / entry[key]).is_file():
            return f"{key} file {entry[key]} does not exist"
    group = entry.get("node_group")
    if group and not all(group.get(key) for key in ("instance_type", "nodes", "workload")):
        return "`node_group` needs instance_type, nodes and workload"
    if group and not (catalog_dir() / "stacks" / "node-group.yaml").is_file():
        return "the node group template addons/stacks/node-group.yaml is missing"
    stack = entry.get("stack")
    if stack and group:
        return "an add-on has either `node_group` or `stack`, not both (one stack per add-on)"
    if stack and not (catalog_dir() / stack.get("template", "")).is_file():
        return f"stack template {stack.get('template')} does not exist"
    for name, option in (entry.get("options") or {}).items():
        if not option.get("required") and "default" not in option:
            return f"option {name} needs a `default` or `required: true`"
    attach = (entry.get("core") or {}).get("attach")
    if attach and attach.get("interceptor") not in ((entry.get("core") or {}).get("interceptors") or {}):
        return "`core.attach.interceptor` must name one of `core.interceptors`"
    for kind in entry.get("application_types", []):
        if not all(kind.get(key) for key in ("id", "display_name", "component", "deployment")):
            return "every application type needs id, display_name, component and deployment"
    return ""


def entry_for(name: str) -> dict:
    catalog = load_catalog()
    if name not in catalog:
        raise InstallerError(f"Unknown add-on '{name}'.", "Available add-ons: " + (", ".join(catalog) or "(none)") +
                             ". List them with: ./dial-install add-ons list")
    return catalog[name]


def hooks(entry: dict):
    """The add-on's own steps (`addons/<id>/hooks.py`), or None. Optional functions: validate(ctx, entry, options),
    preflight(ctx, entry, options, say), stack_parameters(ctx, entry, options), verify(ctx, entry, options, outputs, say)."""
    path = entry["_dir"] / "hooks.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"dial_addon_{entry['id'].replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hook(entry: dict, name: str):
    module = hooks(entry)
    return getattr(module, name, None) if module else None


_VERSION = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")


def latest_version(ctx: Context, entry: dict) -> str:
    """The highest `YYYY.M.N` tag of the add-on's first image in its registry, looked up now (never remembered between runs).
    Tags such as `sha-1234567` and untagged builds are not versions."""
    host, repository = entry["source"]["registry"], entry["images"][0]["repository"]
    config_dir = mirror._docker_config(ctx, [host])
    try:
        listing = ctx.cloud.runner.run([mirror.crane_path(ctx), "ls", f"{host}/{repository}"], check=False,
                                       env={"DOCKER_CONFIG": str(config_dir)})
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)
    versions = sorted((tuple(int(part) for part in m.groups()), tag) for tag in listing.stdout.split()
                      if (m := _VERSION.match(tag)))
    if listing.returncode != 0 or not versions:
        if ctx.cloud.runner.dry_run and listing.returncode == 0:
            return "<latest>"
        raise InstallerError(
            f"Could not find the latest version of {entry['display_name']} in {host}/{repository}.\n{(listing.stderr or '').strip()[-400:]}",
            "Check that this machine can reach Naya's registry (for a private one, that your AWS account is allowed to read it: ask Naya), "
            "or name the version: --version <tag>.")
    return versions[-1][1]


def pin(ctx: Context, entry: dict, requested: str | None = None) -> dict:
    """The entry with its version fixed: the one asked for, else the catalog's, and `latest` is looked up in the registry now."""
    memo = ctx.cache.setdefault("addon_versions", {})
    key = (entry["id"], requested)
    if key not in memo:
        version = str(requested or entry["version"])
        if not _TAG.match(version):
            raise InstallerError(f"'{version}' is not a valid version.", "Use a tag of the registry, for example 2026.11.1.")
        memo[key] = latest_version(ctx, entry) if version == "latest" else version
    return {**entry, "version": memo[key]}


def split_settings(given: dict | None) -> tuple[str | None, dict]:
    """(version asked for, the other settings) from the flags."""
    given = dict(given or {})
    return given.pop("version", None), given


def image_tag(entry: dict, image: dict) -> str:
    return str(image.get("tag") or entry["version"])


def source_images(entry: dict) -> list[tuple[str, str]]:
    """[(source reference, `repository:tag`)] for every image of the add-on."""
    host = entry["source"]["registry"]
    return [(f"{host}/{i['repository']}:{image_tag(entry, i)}", f"{i['repository']}:{image_tag(entry, i)}") for i in entry["images"]]


# ---- what is installed ------------------------------------------------------------------------------------------
def release_prefix(ctx: Context) -> str:
    return f"{ctx.release['helm_release']['name']}-addon-"


def release_name(ctx: Context, name: str) -> str:
    return release_prefix(ctx) + name


def installed(ctx: Context) -> dict[str, dict]:
    """id -> {"release", "version", "schemas"} for the add-ons installed in the cluster (read only; {} when unreachable)."""
    namespace = ctx.release["helm_release"]["namespace"]
    prefix = release_prefix(ctx)
    listing = ctx.cloud.helm("list", "--namespace", namespace, "--filter", f"^{prefix}", "--output", "json", check=False)
    if listing.returncode != 0 or not listing.stdout.strip():
        return {}
    found: dict[str, dict] = {}
    for row in json.loads(listing.stdout):
        release = row.get("name", "")
        if not release.startswith(prefix):
            continue
        values = ctx.cloud.helm("get", "values", release, "--namespace", namespace, "--output", "json", check=False)
        marker = (json.loads(values.stdout or "{}") or {}).get("addon", {}) if values.returncode == 0 else {}
        found[release.removeprefix(prefix)] = {
            "release": release, "version": marker.get("version", "?"), "schemas": marker.get("applicationTypeSchemas", []),
            "options": marker.get("options", {}), "applications": marker.get("applications", {}),
            "interceptors": marker.get("interceptors", {}), "attachments": marker.get("interceptorAttachments", []),
            "stackOutputs": marker.get("stackOutputs", {}),
        }
    return found


def registered_schemas(ctx: Context, states: dict | None = None) -> list[dict]:
    """The application type schemas of the installed add-ons, which the platform's Core configuration must keep.
    `ctx.cache["addon_exclude"]` names add-ons that are being removed."""
    skip = set(ctx.cache.get("addon_exclude", ()))
    return [schema for name, state in sorted((states if states is not None else installed(ctx)).items())
            if name not in skip for schema in state["schemas"]]


def registered_applications(ctx: Context, states: dict | None = None) -> dict[str, dict]:
    """The applications of the installed add-ons (Core's configuration format), kept in Core's configuration like the schemas."""
    skip = set(ctx.cache.get("addon_exclude", ()))
    found: dict[str, dict] = {}
    for name, state in sorted((states if states is not None else installed(ctx)).items()):
        if name not in skip:
            found.update(state.get("applications") or {})
    return found


def remember_registrations(ctx: Context) -> None:
    """Read the registrations of the installed add-ons once; the platform's Core configuration is built from them."""
    states = installed(ctx)
    skip = set(ctx.cache.get("addon_exclude", ()))
    ctx.cache["addon_schemas"] = registered_schemas(ctx, states)
    ctx.cache["addon_applications"] = registered_applications(ctx, states)
    kept = {name: state for name, state in sorted(states.items()) if name not in skip}
    ctx.cache["addon_interceptors"] = {k: v for state in kept.values() for k, v in (state.get("interceptors") or {}).items()}
    ctx.cache["addon_interceptor_attachments"] = [a for state in kept.values() for a in state.get("attachments") or []]


def registers_in_core(entry: dict) -> bool:
    core = entry.get("core") or {}
    return bool(entry.get("application_types") or core.get("applications") or core.get("interceptors"))


def attachable_deployments(ctx: Context, states: dict, entry: dict | None = None) -> list[str]:
    """Deployments an interceptor can be attached to: the models the installer registers and the applications of add-ons
    (in DIAL Core's configuration). Deployments created in DIAL Admin are not part of that file."""
    names = [model["key"] for model in ctx.release.get("models", [])]
    for state in states.values():
        names += list((state.get("applications") or {}).keys())
    if entry:
        names += list(((entry.get("core") or {}).get("applications") or {}).keys())
    return sorted(set(names))


def check_attachments(ctx: Context, entry: dict, options: dict[str, str], states: dict) -> None:
    known = attachable_deployments(ctx, states, entry)
    for attach in attachments(entry, options):
        unknown = [name for name in attach["deployments"] if name not in known]
        if unknown:
            raise InstallerError(f"{entry['display_name']} cannot be attached to: {', '.join(unknown)}.",
                                 "It can be attached to: " + ", ".join(known) + ". Or use --guardrail-global for every deployment.")


# ---- values ----------------------------------------------------------------------------------------------------
def image_registry(ctx: Context, entry: dict) -> str:
    """Where the cluster pulls the add-on's images from: the customer's ECR when images are mirrored, else Naya's registry."""
    return mirror.target_host(ctx) if ctx.cfg["registry"]["provider"] == "ecr-mirror" else entry["source"]["registry"]


_PATTERNS = {                       # how each built-in setting must look (catalog options may give their own `pattern`)
    "model": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}$"),
    "instance_type": re.compile(r"^[a-z][a-z0-9]*\.[a-z0-9]+$"),
    "nodes": re.compile(r"^([1-9]|10)$"),
    "dedicated_nodes": re.compile(r"^(true|false)$"),
}
_FLAGS = {"instance_type": "--instance-type", "nodes": "--nodes", "dedicated_nodes": "--no-dedicated-nodes"}


def declared_options(entry: dict) -> dict[str, dict]:
    """name -> {"default"|"required", "pattern", "flag"}: the settings of the catalog (`options`) and, for an add-on with its own
    nodes, the node settings."""
    options = {name: dict(option) for name, option in (entry.get("options") or {}).items()}
    group = entry.get("node_group")
    if group:
        options["instance_type"] = {"default": group["instance_type"]}
        options["nodes"] = {"default": str(group["nodes"])}
        options["dedicated_nodes"] = {"default": "true"}
    return options


def flag_of(name: str, option: dict | None = None) -> str:
    return (option or {}).get("flag") or _FLAGS.get(name, "--" + name.replace("_", "-"))


def resolve_options(entry: dict, state: dict | None, given: dict[str, str] | None, region: str | None = None) -> dict[str, str]:
    """The settings of an add-on: what the customer gave now, else what is installed, else the default. `{region}` in a default
    is the region of the installation."""
    declared = declared_options(entry)
    given = {k: str(v) for k, v in (given or {}).items() if v is not None}
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise InstallerError(f"{entry['display_name']} has no setting '{unknown[0]}'.",
                             "Its settings: " + (", ".join(flag_of(name, option) for name, option in declared.items()) or "(none)"))
    chosen = {}
    for name, option in declared.items():
        value = given.get(name) or (state or {}).get("options", {}).get(name)
        if not value:
            if option.get("required"):
                raise InstallerError(f"{entry['display_name']} needs {flag_of(name, option)}.",
                                     f"{flag_of(name, option)}: {option.get('description', '')}".rstrip(": "))
            value = str(option["default"])
        value = str(value)
        if "{region}" in value:
            value = value.replace("{region}", region or "")
        pattern = re.compile(option["pattern"]) if option.get("pattern") else _PATTERNS.get(name, _PATTERNS["model"])
        if not pattern.match(value):
            raise InstallerError(f"'{value}' is not a valid value for {flag_of(name, option)}.",
                                 option.get("description") or ("Use letters, digits and . _ : / - only." if name == "model" else
                                 {"instance_type": "For example c6i.xlarge.", "nodes": "A number from 1 to 10."}.get(name, "")))
        chosen[name] = value
    return chosen


def check_given(entry: dict, given: dict[str, str] | None) -> None:
    """Fail early (before anything is read from AWS) on an unknown setting or a value of the wrong form."""
    declared = declared_options(entry)
    given = {k: str(v) for k, v in (given or {}).items() if v is not None}
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise InstallerError(f"{entry['display_name']} has no setting '{unknown[0]}'.",
                             "Its settings: " + (", ".join(flag_of(name, option) for name, option in declared.items()) or "(none)"))
    for name, value in given.items():
        option = declared[name]
        pattern = re.compile(option["pattern"]) if option.get("pattern") else _PATTERNS.get(name, _PATTERNS["model"])
        if not pattern.match(value):
            raise InstallerError(f"'{value}' is not a valid value for {flag_of(name, option)}.", option.get("description", ""))


def is_current(entry: dict, state: dict | None, given: dict[str, str] | None = None, region: str | None = None) -> bool:
    """True when the installed add-on already has this entry's version and the settings that would be installed.
    `entry` has its version fixed (see `pin`)."""
    return (bool(state) and state["version"] == str(entry["version"])
            and resolve_options(entry, state, given, region) == (state.get("options") or {}))


def dedicated(entry: dict, options: dict[str, str]) -> bool:
    return bool(entry.get("node_group")) and options.get("dedicated_nodes") == "true"


def render_values(ctx: Context, entry: dict, options: dict[str, str] | None = None) -> dict:
    """Helm values of the add-on's release, including the `addon` marker that records what it registered in Core."""
    helm = ctx.release["helm_release"]
    options = resolve_options(entry, None, None) if options is None else options
    mapping = {
        "DIAL_RELEASE_NAME": helm["name"],
        "DIAL_NAMESPACE": helm["namespace"],
        "DIAL_CORE_URL": placeholders._service_url(ctx, "core"),
        "SERVICE_URL": placeholders._service_url(ctx, entry.get("service") or _component(entry)),
        "THEMES_HOST": ctx.cfg["domain"]["hosts"]["themes"],
        "IMAGE_REGISTRY": image_registry(ctx, entry),
        "ADDON_VERSION": str(entry["version"]),
        "AWS_REGION": ctx.region,
        **{name.upper(): value for name, value in options.items()},
    }
    if entry.get("stack"):                           # outputs of the add-on's own stack, as STACK_<OUTPUT_NAME>
        mapping.update({"STACK_" + re.sub(r"(?<!^)(?=[A-Z])", "_", key).upper(): value
                        for key, value in stack_outputs(ctx, entry["id"]).items()})
    if "PLACEHOLDER_DIAL_API_KEY" in (entry["_dir"] / entry["values"]).read_text():           # the internal API key of the platform
        if not ctx.secret:
            raise InstallerError("The secret is not loaded.", "Run `./dial-install check` and fix what it reports.")
        mapping["DIAL_API_KEY"] = ctx.secret["dial_api_key"]
    chart_values = yaml.safe_load((entry["_dir"] / entry["values"]).read_text()) or {}
    try:
        document = substitute.fill(chart_values, mapping)
        applications = substitute.fill(copy.deepcopy((entry.get("core") or {}).get("applications") or {}), mapping)
        interceptors = substitute.fill(copy.deepcopy((entry.get("core") or {}).get("interceptors") or {}), mapping)
    except substitute.UnresolvedPlaceholders as error:
        raise InstallerError(f"Add-on {entry['id']} uses values this installer cannot provide: {', '.join(error.names)}",
                             "Download the latest installer package.") from error
    if dedicated(entry, options):                    # only this add-on runs on its own nodes, and it tolerates exactly their taint
        label = entry["node_group"]["workload"]
        layer_over(document, {"nodeSelector": {"workload": label},
                              "tolerations": [{"key": "dedicated", "operator": "Equal", "value": label, "effect": "NoSchedule"}]})
    document["addon"] = {
        "id": entry["id"], "version": str(entry["version"]), "options": options,
        "applicationTypeSchemas": [placeholders.application_type_schema(ctx, kind) for kind in entry.get("application_types", [])],
        "applications": applications,
        "interceptors": interceptors,
        "interceptorAttachments": attachments(entry, options),
        "stackOutputs": stack_outputs(ctx, entry["id"]) if entry.get("stack") else {},
    }
    return document


def attachments(entry: dict, options: dict[str, str]) -> list[dict]:
    """Where the add-on's interceptor is attached in DIAL Core: [{"interceptor", "deployments", "global"}]."""
    attach = (entry.get("core") or {}).get("attach")
    if not attach:
        return []
    deployments = [name for name in options.get(attach.get("deployments_option", ""), "").split(",") if name]
    return [{"interceptor": attach["interceptor"], "deployments": deployments,
             "global": options.get(attach.get("global_option", ""), "false") == "true"}]


def _component(entry: dict) -> str:
    return entry.get("service") or (entry.get("application_types") or [{"component": entry["id"]}])[0]["component"]


def _service(ctx: Context, entry: dict) -> str:
    return f"{ctx.release['helm_release']['name']}-{_component(entry)}"


# ---- steps -------------------------------------------------------------------------------------------------------
def copy_images(ctx: Context, entry: dict, say: Say) -> None:
    if ctx.cfg["registry"]["provider"] != "ecr-mirror":
        say("  registry.provider is `public`: the cluster pulls the images directly, nothing to copy.")
        return
    check_digests(ctx, entry)
    pairs = source_images(entry)
    say(f"  Copying {len(pairs)} image(s) from {entry['source']['registry']} into your registry")
    copied, skipped = mirror.copy_pairs(
        ctx, pairs, lambda text: say("  " + text.strip()), source_hosts=(entry["source"]["registry"],),
        hint=f"The images of {entry['display_name']} {entry['version']} are in Naya's registry "
             f"({entry['source']['registry']}). Check that this machine can reach it (for a private registry, that your AWS account is allowed to pull "
             "from it: ask Naya) and that this version has been published; then run the command again.")
    say(f"  {copied} copied, {skipped} already there")


def check_digests(ctx: Context, entry: dict) -> None:
    """Images pinned by digest in the catalog: the tag must still point at exactly that image."""
    pinned = [image for image in entry["images"] if image.get("digest")]
    if not pinned or ctx.cloud.runner.dry_run:
        return
    host = entry["source"]["registry"]
    config_dir = mirror._docker_config(ctx, [host])
    try:
        for image in pinned:
            reference = f"{host}/{image['repository']}:{image_tag(entry, image)}"
            found = ctx.cloud.runner.run([mirror.crane_path(ctx), "digest", reference], check=False,
                                         env={"DOCKER_CONFIG": str(config_dir)}).stdout.strip()
            if found != image["digest"]:
                raise InstallerError(f"{reference} is not the image this installer was tested with.",
                                     f"Expected {image['digest']}, the registry has {found or 'nothing'}. Nothing was installed.")
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


# ---- the add-on's own node group ---------------------------------------------------------------------------------
def node_stack(ctx: Context, name: str) -> str:
    return f"{ctx.name}-addon-{name}"


def has_stack(entry: dict) -> bool:
    """The add-on owns a CloudFormation stack <name>-addon-<id>: a node group, or its own `stack` (an IAM role ...)."""
    return bool(entry.get("node_group") or entry.get("stack"))


def stack_outputs(ctx: Context, name: str) -> dict[str, str]:
    """Outputs of the add-on's stack (read from CloudFormation, the only state). Placeholders in a dry run without the stack."""
    memo = ctx.cache.setdefault("addon_stack_outputs", {})
    if name not in memo:
        data = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", node_stack(ctx, name), check=False)
        outputs = {o["OutputKey"]: o["OutputValue"] for o in ((data or {}).get("Stacks") or [{}])[0].get("Outputs", [])}
        if not outputs and ctx.cloud.runner.dry_run:
            return {"RoleArn": "<role-arn>"}
        memo[name] = outputs
    return memo[name]


def deploy_addon_stack(ctx: Context, entry: dict, options: dict[str, str], say: Say) -> None:
    """Create or update the add-on's own stack (`stack:` in the catalog); its parameters come from the add-on's hooks."""
    parameters = _hook(entry, "stack_parameters")
    params = parameters(ctx, entry, options) if parameters else {}
    stack = node_stack(ctx, entry["id"])
    say(f"  CloudFormation stack {stack}")
    _cloudformation(ctx, entry, stack, catalog_dir() / entry["stack"]["template"], params, capabilities=["CAPABILITY_NAMED_IAM"])
    ctx.cache.setdefault("addon_stack_outputs", {}).pop(entry["id"], None)          # read the new outputs


def _cloudformation(ctx: Context, entry: dict, stack: str, template: Path, params: dict, capabilities: list[str] = ()) -> None:
    try:
        ctx.cloud.aws("cloudformation", "deploy", "--stack-name", stack, "--template-file", str(template),
                      "--no-fail-on-empty-changeset", *(["--capabilities", *capabilities] if capabilities else []),
                      "--tags", f"dial-install:installation={ctx.name}", f"dial-install:version={__version__}",
                      f"dial-install:addon={entry['id']}", *[f"{k}={v}" for k, v in ctx.cfg["tags"].items()],
                      "--parameter-overrides", *[f"{k}={v}" for k, v in params.items()], write=True, stream=True)
    except CommandError as error:
        raise InstallerError(
            f"CloudFormation stack {stack} failed.\n{error.stderr}",
            f"See what failed:\n  aws cloudformation describe-stack-events --stack-name {stack} --region {ctx.region} --max-items 15 "
            "--query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' --output table\n"
            "If the stack is in ROLLBACK_COMPLETE, delete it first. Then run the same command again.") from error


def node_stack_exists(ctx: Context, name: str) -> bool:
    data = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", node_stack(ctx, name), check=False)
    if not data or not data.get("Stacks"):
        return False
    return "dial-install:addon" in {t["Key"] for t in data["Stacks"][0].get("Tags", [])}


def deploy_node_group(ctx: Context, entry: dict, options: dict[str, str], say: Say) -> None:
    """Create or update the stack with the add-on's dedicated node group (a node label and taint nobody else uses)."""
    group = entry["node_group"]
    instance_type = options["instance_type"]
    offered = ctx.cloud.aws_json("ec2", "describe-instance-type-offerings", "--location-type", "region",
                                 "--filters", f"Name=instance-type,Values={instance_type}", check=False)
    if offered is not None and not offered.get("InstanceTypeOfferings"):
        raise InstallerError(f"Instance type {instance_type} is not offered in {ctx.region}.", "Choose another with --instance-type.")
    stack = node_stack(ctx, entry["id"])
    template = catalog_dir() / "stacks" / "node-group.yaml"
    say(f"  Node group for {entry['display_name']}: {options['nodes']} x {instance_type} (stack {stack}); this takes a few minutes")
    params = {
        "ClusterName": ctx.cluster_name, "NodeRoleArn": ctx.outputs["compute"]["NodeRoleArn"],
        "PrivateSubnet1Id": ctx.outputs["network"]["PrivateSubnet1Id"], "PrivateSubnet2Id": ctx.outputs["network"]["PrivateSubnet2Id"],
        "InstanceType": instance_type, "Nodes": options["nodes"], "DiskSize": group.get("disk_gb", 50), "WorkloadLabel": group["workload"],
    }
    _cloudformation(ctx, entry, stack, template, params)


def delete_node_group(ctx: Context, name: str, say: Say) -> None:
    """Delete the add-on's stack (its node group, or its own resources such as an IAM role)."""
    stack = node_stack(ctx, name)
    say(f"  Deleting the stack {stack}")
    ctx.cloud.aws("cloudformation", "delete-stack", "--stack-name", stack, write=True)
    if not ctx.cloud.runner.dry_run:
        ctx.cloud.aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", stack, check=False)


def helm_install(ctx: Context, entry: dict, say: Say, options: dict[str, str] | None = None) -> None:
    helm = ctx.release["helm_release"]
    release = release_name(ctx, entry["id"])
    chart = catalog_dir() / entry["chart"]
    handle, path = tempfile.mkstemp(prefix="dial-addon-values-", suffix=".yaml")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(render_values(ctx, entry, options), stream, default_flow_style=False, allow_unicode=True)
        say(f"  helm upgrade --install {release} ({entry['display_name']} {entry['version']}" + (f", {options}" if options else "") + ")")
        ctx.cloud.helm("upgrade", "--install", release, str(chart), "--namespace", helm["namespace"],
                       "--values", path, "--timeout", "10m", write=True, stream=True)
    finally:
        os.unlink(path)


def wait_ready(ctx: Context, entry: dict, say: Say) -> None:
    if ctx.cloud.runner.dry_run:
        return
    namespace = ctx.release["helm_release"]["namespace"]
    service = _service(ctx, entry)
    say(f"  waiting for deployment/{service}")
    result = ctx.cloud.kubectl("-n", namespace, "rollout", "status", f"deployment/{service}", "--timeout=300s", check=False)
    if result.returncode != 0:
        raise InstallerError(
            f"{entry['display_name']} did not become ready.",
            f"See why: kubectl --context {ctx.cloud.kube_context} -n {namespace} describe pod -l app.kubernetes.io/instance="
            f"{release_name(ctx, entry['id'])}\nAn ImagePullBackOff means the image could not be pulled from {image_registry(ctx, entry)}. "
            "Run the same command again once it is fixed.")


def update_core(ctx: Context, say: Say) -> None:
    """Upgrade the platform release so that Core's configuration matches the installed add-ons, then restart Core.
    Only the Core configuration changes; the platform chart is the one that is installed now."""
    before = deploy.installed(ctx)
    deploy.registry_login(ctx.cloud, ctx.release["chart"]["repository"])
    deploy.prepare(ctx)
    say("  Updating the DIAL Core configuration")
    deploy.helm_upgrade(ctx, say)
    if ctx.cloud.runner.dry_run:
        return
    helm = ctx.release["helm_release"]
    core = f"deployment/{helm['name']}-core"
    ctx.cloud.kubectl("-n", helm["namespace"], "rollout", "restart", core, write=True)      # Core reads its configuration at start
    result = ctx.cloud.kubectl("-n", helm["namespace"], "rollout", "status", core, "--timeout=600s", check=False)
    if result.returncode != 0:
        undo = f"\nTo go back to the previous Core configuration:\n  {deploy.rollback_command(ctx, before['revision'])}" if before else ""
        raise InstallerError("DIAL Core did not become ready after its configuration changed.",
                             f"Check: kubectl --context {ctx.cloud.kube_context} -n {helm['namespace']} get pods{undo}")


def require_platform(ctx: Context) -> None:
    """The platform must be installed, at the release this package resolves to: updating Core's configuration upgrades
    the platform release, and an add-on command must never move the platform to another version by accident."""
    current = deploy.installed(ctx)
    if current is None:
        if ctx.cloud.runner.dry_run:
            return
        raise InstallerError("DIAL is not installed in this cluster yet.", "Run `./dial-install apply` first.")
    target = ctx.release["chart"]["version"]
    if not ctx.cache.get("local_chart") and current["chart_version"] != target:
        raise InstallerError(
            f"The installed platform is {current['chart']}, but this configuration selects release {target}.",
            "Run `./dial-install upgrade` first, or set `release:` in dial.yaml to the installed release.")


def _prepare(ctx: Context, entry: dict, options: dict[str, str], states: dict, say: Say) -> None:
    """Checks that come before anything is changed: the add-on's own rules, where it is attached, its own preflight."""
    validate = _hook(entry, "validate")
    if validate:
        validate(ctx, entry, options)
    check_attachments(ctx, entry, options, states)
    preflight = _hook(entry, "preflight")
    if preflight:
        preflight(ctx, entry, options, say)


def _deploy(ctx: Context, entry: dict, options: dict[str, str], say: Say) -> None:
    copy_images(ctx, entry, say)
    if dedicated(entry, options):
        deploy_node_group(ctx, entry, options, say)
    if entry.get("stack"):
        deploy_addon_stack(ctx, entry, options, say)
    helm_install(ctx, entry, say, options)


def _verify(ctx: Context, entry: dict, options: dict[str, str], say: Say) -> None:
    verify = _hook(entry, "verify")
    if verify and not ctx.cloud.runner.dry_run:
        say(f"  Checking that {entry['display_name']} works")
        verify(ctx, entry, options, stack_outputs(ctx, entry["id"]) if entry.get("stack") else {}, say)


def install(ctx: Context, name: str, say: Say, given: dict[str, str] | None = None) -> None:
    requested, settings = split_settings(given)
    entry = entry_for(name)
    check_given(entry, settings)
    options = resolve_options(entry, None, settings, ctx.region)
    require_platform(ctx)
    states = installed(ctx)
    state = states.get(name)
    if state:
        raise InstallerError(f"{entry['display_name']} is already installed (version {state['version']}).",
                             f"To move to the newest version or change a setting: ./dial-install add-ons upgrade {name}")
    _prepare(ctx, entry, options, states, say)
    entry = pin(ctx, entry, requested)
    say(f"Installing {entry['display_name']} {entry['version']}" + (f" ({_describe(options)})" if options else ""))
    _deploy(ctx, entry, options, say)
    wait_ready(ctx, entry, say)
    if registers_in_core(entry):                      # only an add-on that registers something touches Core
        update_core(ctx, say)
    _verify(ctx, entry, options, say)
    say(f"\n{entry['display_name']} {entry['version']} is installed" + (" and registered in DIAL Core." if registers_in_core(entry) else "."))
    _next_steps(ctx, entry, say)


def _describe(options: dict[str, str]) -> str:
    return ", ".join(f"{name}: {value}" for name, value in options.items())


def upgrade(ctx: Context, name: str, say: Say, given: dict[str, str] | None = None) -> bool:
    """Move to the newest version (looked up now) or the one asked for, or change a setting. Returns False when nothing needs to
    change; an add-on with its own checks is then checked again."""
    requested, settings = split_settings(given)
    entry = entry_for(name)
    check_given(entry, settings)
    require_platform(ctx)
    states = installed(ctx)
    state = states.get(name)
    if not state and not ctx.cloud.runner.dry_run:
        raise InstallerError(f"{entry['display_name']} is not installed.", f"Install it with: ./dial-install add-ons install {name}")
    entry = pin(ctx, entry, requested)
    if is_current(entry, state, settings, ctx.region):
        say(f"{entry['display_name']} is already at {entry['version']} with these settings. Nothing to change.")
        _verify(ctx, entry, state["options"], say)
        return False
    options = resolve_options(entry, state, settings, ctx.region)
    _prepare(ctx, entry, options, {k: v for k, v in states.items() if k != name}, say)
    say(f"Upgrading {entry['display_name']} {state['version'] if state else '?'} -> {entry['version']}" + (f" ({_describe(options)})" if options else ""))
    _deploy(ctx, entry, options, say)
    if state and entry.get("stack") and state.get("stackOutputs") and state["stackOutputs"] != stack_outputs(ctx, name):
        restart(ctx, entry, say)                      # e.g. a new role: credentials are injected only when a pod starts
    wait_ready(ctx, entry, say)
    if not dedicated(entry, options) and entry.get("node_group") and node_stack_exists(ctx, entry["id"]):
        delete_node_group(ctx, entry["id"], say)      # switched to the shared nodes: the pod has moved, the node group is not needed
    rendered = render_values(ctx, entry, options)["addon"]
    if state is None or any(state.get(key) != rendered[field] for key, field in (
            ("schemas", "applicationTypeSchemas"), ("applications", "applications"), ("interceptors", "interceptors"),
            ("attachments", "interceptorAttachments"))):
        update_core(ctx, say)                         # the registration changed; usually it does not
    _verify(ctx, entry, options, say)
    say(f"\n{entry['display_name']} is now at {entry['version']}.")
    return True


def restart(ctx: Context, entry: dict, say: Say) -> None:
    namespace = ctx.release["helm_release"]["namespace"]
    say(f"  restarting deployment/{_service(ctx, entry)}")
    ctx.cloud.kubectl("-n", namespace, "rollout", "restart", f"deployment/{_service(ctx, entry)}", write=True)


def _next_steps(ctx: Context, entry: dict, say: Say) -> None:
    kinds = ", ".join(f"'{k['display_name']}'" for k in entry.get("application_types", []))
    if kinds:
        say(f"Application type(s) now available in DIAL: {kinds}. Create an application of that type to use it.")


def repositories(ctx: Context, entry: dict) -> list[str]:
    """Repositories in the customer's ECR that belong to this add-on and carry this installation's tag."""
    names = []
    for image in entry["images"]:
        repo = image["repository"]
        described = ctx.cloud.aws_json("ecr", "describe-repositories", "--repository-names", repo, check=False)
        if not described:
            continue
        tags = ctx.cloud.aws_json("ecr", "list-tags-for-resource", "--resource-arn",
                                  described["repositories"][0]["repositoryArn"], check=False) or {}
        if {t["Key"]: t["Value"] for t in tags.get("tags", [])}.get("dial-install:installation") == ctx.name:
            names.append(repo)
    return names


def remove(ctx: Context, name: str, say: Say, *, keep_images: bool = False) -> list[str]:
    """Remove everything the add-on created. Returns what is still left (empty when clean)."""
    entry = entry_for(name)
    state = installed(ctx).get(name)
    helm = ctx.release["helm_release"]
    if state:
        require_platform(ctx)
        if state["schemas"] or state.get("applications") or state.get("interceptors"):
            say(f"  Unregistering {entry['display_name']} from DIAL Core")
            ctx.cache["addon_exclude"] = {name}       # first take it out of Core, so that Core never points at nothing
            update_core(ctx, say)
        say(f"  Removing the Helm release {state['release']}")
        ctx.cloud.helm("uninstall", state["release"], "--namespace", helm["namespace"], write=True)
    else:
        say(f"  {entry['display_name']} is not installed in the cluster.")
    if has_stack(entry) and node_stack_exists(ctx, name):
        delete_node_group(ctx, name, say)
    repos = [] if keep_images else repositories(ctx, entry)
    for repo in repos:
        say(f"  Deleting image repository {repo}")
        ctx.cloud.aws("ecr", "delete-repository", "--repository-name", repo, "--force", write=True)
    return leftovers(ctx, entry, keep_images=keep_images)


def leftovers(ctx: Context, entry: dict, *, keep_images: bool = False) -> list[str]:
    if ctx.cloud.runner.dry_run:
        return []
    left = []
    if entry["id"] in installed(ctx):
        left.append(f"Helm release {release_name(ctx, entry['id'])}")
    if has_stack(entry) and node_stack_exists(ctx, entry["id"]):
        left.append(f"stack {node_stack(ctx, entry['id'])}")
    if not keep_images:
        left += [f"image repository {repo}" for repo in repositories(ctx, entry)]
    return left


def release_names(ctx: Context) -> list[str]:
    """Helm releases of every installed add-on (used by `uninstall`)."""
    return [state["release"] for _, state in sorted(installed(ctx).items())]


# ---- listing -----------------------------------------------------------------------------------------------------
def rows(ctx: Context | None) -> list[dict]:
    """One row per catalog add-on. With a reachable installation `status` tells what is installed and the version is the newest
    one in the registry right now; otherwise `status` is None and a `latest` entry shows as `latest`."""
    current = installed(ctx) if ctx is not None else None
    result = []
    for name, entry in load_catalog().items():
        version = str(entry["version"])
        if ctx is not None and version == "latest":
            try:
                version = pin(ctx, entry)["version"]
            except InstallerError:
                pass                                  # the registry cannot be read from here: show `latest`
        state = (current or {}).get(name)
        if current is None:
            status = None
        elif not state:
            status = "not installed"
        elif state["version"] == version:
            status = f"installed {state['version']}" + (f" ({_describe(state['options'])})" if state.get("options") else "")
        elif version == "latest":
            status = f"installed {state['version']}"
        else:
            status = f"installed {state['version']} (upgrade to {version} available)"
        result.append({"id": name, "name": entry["display_name"], "version": version,
                       "description": entry["description"], "status": status})
    return result
