"""Load, validate and resolve dial.yaml.

`load()` returns the file as written. `resolve()` returns a fully populated
dictionary (defaults applied, host names derived) that every module reads.
Nothing outside this file needs to know a default value.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import ROOT
from .errors import InstallerError
from .schema import validate

SERVICES = ("chat", "admin", "core", "themes", "grafana", "superset")
NODE_GROUPS = ("core", "dynamic", "system")
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def load_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InstallerError(f"Cannot read {path}: {error.strerror}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise InstallerError(f"{path} is not valid YAML:\n{error}") from error
    if not isinstance(data, dict):
        raise InstallerError(f"{path} must contain a YAML mapping.")
    return data


def load_schema() -> dict:
    return json.loads((ROOT / "schema" / "dial.schema.json").read_text(encoding="utf-8"))


def load(path: Path) -> dict:
    """Read dial.yaml and fail early with every schema problem listed."""
    if not path.exists():
        raise InstallerError(
            f"Config file not found: {path}",
            "Run `./dial-install init` to create it.",
        )
    raw = load_yaml(path)
    _reject_placeholders(raw)
    problems = validate(raw, load_schema())
    if problems:
        raise InstallerError(
            f"{path} has {len(problems)} problem(s):\n  - " + "\n  - ".join(problems),
            "Fix the lines above and run the command again.",
        )
    return raw


def _reject_placeholders(raw: dict) -> None:
    marked = []

    def scan(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                scan(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                scan(value, f"{path}[{index}]")
        elif isinstance(node, str) and ("TODO" in node or "CHANGEME" in node):
            marked.append(path)

    scan(raw, "")
    if marked:
        raise InstallerError(
            "These settings still contain a TODO placeholder: " + ", ".join(marked),
            "Edit dial.yaml, replace each placeholder with the real value, and run the command again.",
        )


def resolve(raw: dict, release: dict) -> dict:
    """Return the config with every default applied and host names derived."""
    cfg = copy.deepcopy(raw)

    cfg.setdefault("network", {})
    network = cfg["network"]
    network.setdefault("provider", "create")
    network.setdefault("exposure", "internal")
    network.setdefault("allowed_cidrs", ["0.0.0.0/0"])
    network.setdefault("create_vpc_endpoints", network["provider"] == "create")
    if network["provider"] == "create":
        network.setdefault("cidr", "10.0.0.0/16")
        network.setdefault("subnet_cidrs", _default_subnet_cidrs(network["cidr"]))

    identity = cfg.setdefault("identity", {})
    identity.setdefault("provider", "cognito")
    identity.setdefault("self_registration", False)
    identity.setdefault("user_pool_name", "dial-users")
    if "saml" in identity:
        identity["saml"].setdefault("idp_signout", False)

    registry = cfg.setdefault("registry", {})
    registry.setdefault("provider", "ecr-mirror")
    registry.setdefault("source", release["registry"]["source"])

    database = cfg.setdefault("database", {})
    database.setdefault("provider", "aurora-create")
    database.setdefault("engine_version", release["database"]["engine_version"])
    database.setdefault("auth", "password")
    database.setdefault("iam_user", "dial_admin_iam")
    database.setdefault("backup_retention_days", 7)
    database.setdefault("deletion_protection", True)
    database.setdefault("min_capacity", release["database"]["min_capacity"])
    database.setdefault("max_capacity", release["database"]["max_capacity"])

    kube = cfg.setdefault("kubernetes", {})
    kube.setdefault("version", release["kubernetes"]["default_version"])
    kube.setdefault("cni", "vpc-cni")
    if kube["cni"] == "cilium":
        kube.setdefault("pod_cidr", release["cilium"]["pod_cidr"])
    kube.setdefault("public_endpoint", True)
    kube.setdefault("admin_arns", [])
    cfg.setdefault("tags", {})

    features = cfg.setdefault("features", {})
    features.setdefault("analytics", True)
    features.setdefault("deployment_manager", True)
    # Container Insights puts a webhook on the control plane's path to the pods, which an overlay network cannot reach.
    features.setdefault("container_logs", kube["cni"] != "cilium")

    cfg["release"] = release["release"]

    overrides = cfg.setdefault("sizing", {}).get("node_groups", {})
    groups = copy.deepcopy(release["node_groups"])
    if kube["cni"] == "cilium" and "system" not in overrides:
        # Webhooks must use host networking with the overlay; two of them need two different nodes.
        groups["system"].update(min=2, desired=2, max=2)
    for name, values in overrides.items():
        groups[name].update(values)
    cfg["sizing"]["node_groups"] = groups

    domain = cfg["domain"]
    base = domain.get("base")
    explicit = domain.get("hosts", {})
    domain["hosts"] = {
        service: explicit.get(service) or f"{service}.{base}" for service in SERVICES
    }
    return cfg


def semantic_errors(cfg: dict) -> list[str]:
    """Rules a JSON schema cannot express. `cfg` must be resolved."""
    problems: list[str] = []
    region = cfg["aws"]["region"]

    for label, arn in (("domain.certificate_arn", cfg["domain"]["certificate_arn"]),
                       ("secrets.arn", cfg["secrets"]["arn"])):
        arn_region = arn.split(":")[3]
        if arn_region != region:
            problems.append(f"{label}: is in region {arn_region} but aws.region is {region}")

    for service, host in cfg["domain"]["hosts"].items():
        if not _HOSTNAME.match(host):
            problems.append(f"domain.hosts.{service}: {host!r} is not a valid host name")
    if len(set(cfg["domain"]["hosts"].values())) != len(SERVICES):
        problems.append("domain.hosts: every service needs its own distinct host name")

    for name, group in cfg["sizing"]["node_groups"].items():
        if not group["min"] <= group["desired"] <= group["max"]:
            problems.append(f"sizing.node_groups.{name}: needs min <= desired <= max")
        if "disk_gb" in group and name != "core":
            problems.append(f"sizing.node_groups.{name}.disk_gb: only the core node group supports disk_gb")

    network = cfg["network"]
    kube = cfg["kubernetes"]
    if kube["cni"] == "cilium":
        problems.extend(_pod_cidr_problems(kube["pod_cidr"], network))
    elif "pod_cidr" in kube:
        problems.append("kubernetes.pod_cidr: only valid with kubernetes.cni `cilium`")
    if network["provider"] == "create":
        problems.extend(_subnet_cidr_problems(network))
        for forbidden in ("vpc_id", "subnets"):
            if forbidden in network:
                problems.append(f"network.{forbidden}: only valid with existing-vpc or shared-vpc")
    else:
        for forbidden in ("cidr", "subnet_cidrs"):
            if forbidden in network:
                problems.append(f"network.{forbidden}: only valid with provider `create`")
        if network["exposure"] == "public" and len(network["subnets"].get("public", [])) < 2:
            problems.append("network.subnets.public: at least two public subnets are required for exposure `public`")
    for cidr in network["allowed_cidrs"]:
        try:
            ipaddress.ip_network(cidr)
        except ValueError:
            problems.append(f"network.allowed_cidrs: {cidr!r} is not a valid CIDR")
    for key in cfg["tags"]:
        if key.lower().startswith(("aws:", "dial-install:")) or not 1 <= len(key) <= 128:
            problems.append(f"tags.{key}: tag keys must be 1-128 characters and not start with aws: or dial-install:")
    if len(cfg["tags"]) > 40:
        problems.append("tags: at most 40 tags are allowed")
    return problems


def _default_subnet_cidrs(cidr: str) -> dict | None:
    try:
        net = ipaddress.ip_network(cidr)
    except ValueError:
        return None
    if net.version != 4 or net.prefixlen != 16:
        return None
    base = net.network_address.packed
    def sub(third: int) -> str:
        return f"{base[0]}.{base[1]}.{third}.0/24"
    return {"public": [sub(1), sub(2)], "private": [sub(10), sub(11)]}


def _subnet_cidr_problems(network: dict) -> list[str]:
    try:
        vpc = ipaddress.ip_network(network["cidr"])
    except ValueError:
        return [f"network.cidr: {network['cidr']!r} is not a valid CIDR"]
    subnets = network.get("subnet_cidrs")
    if not subnets:
        return ["network.subnet_cidrs: required when network.cidr is not a /16 (give 2 public and 2 private CIDRs)"]
    problems = []
    seen: list[ipaddress.IPv4Network] = []
    for kind in ("public", "private"):
        for value in subnets.get(kind, []):
            try:
                net = ipaddress.ip_network(value)
            except ValueError:
                problems.append(f"network.subnet_cidrs.{kind}: {value!r} is not a valid CIDR")
                continue
            if not net.subnet_of(vpc):
                problems.append(f"network.subnet_cidrs.{kind}: {value} is outside {network['cidr']}")
            if any(net.overlaps(other) for other in seen):
                problems.append(f"network.subnet_cidrs.{kind}: {value} overlaps another subnet")
            seen.append(net)
    if len(subnets.get("public", [])) != 2 or len(subnets.get("private", [])) != 2:
        problems.append("network.subnet_cidrs: needs exactly 2 public and 2 private CIDRs")
    return problems


def _pod_cidr_problems(pod_cidr: str, network: dict) -> list[str]:
    try:
        pods = ipaddress.ip_network(pod_cidr)
    except ValueError:
        return [f"kubernetes.pod_cidr: {pod_cidr!r} is not a valid CIDR"]
    problems = []
    if pods.version != 4 or pods.prefixlen > 20:
        problems.append("kubernetes.pod_cidr: use an IPv4 range of /20 or larger (one /24 is given to each node)")
    services = ipaddress.ip_network("172.20.0.0/16")          # the cluster's service range (compute stack)
    if pods.overlaps(services):
        problems.append(f"kubernetes.pod_cidr: {pod_cidr} overlaps the Kubernetes service range {services}")
    cidr = network.get("cidr")
    if cidr:
        try:
            if pods.overlaps(ipaddress.ip_network(cidr)):
                problems.append(f"kubernetes.pod_cidr: {pod_cidr} overlaps the VPC {cidr}")
        except ValueError:
            pass
    return problems
