"""`dial-install init`: ask a few questions, write dial.yaml (and the secret template)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Callable

from . import releases
from . import secrets as secrets_mod
from .config import SERVICES
from .errors import InstallerError

Ask = Callable[[str], str]
Say = Callable[[str], None]

SECRETS_FILE = "dial-secrets.json"
_PLACEHOLDER_ARN = "TODO: paste the ARN printed by the create-secret command"


class Wizard:
    def __init__(self, ask: Ask = input, say: Say = print) -> None:
        self._ask = ask
        self.say = say

    # ---- primitives ---------------------------------------------------------
    def text(self, question: str, default: str = "", pattern: str | None = None, hint: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self._ask(f"? {question}{suffix}: ").strip() or default
            if not answer:
                self.say("  A value is required.")
            elif pattern and not re.fullmatch(pattern, answer):
                self.say(f"  That does not look right. {hint}".rstrip())
            else:
                return answer

    def optional(self, question: str) -> str:
        return self._ask(f"? {question} (Enter to skip): ").strip()

    def choice(self, question: str, options: list[str], default: int = 1) -> int:
        self.say(f"? {question}")
        for number, label in enumerate(options, 1):
            self.say(f"    {number}) {label}")
        while True:
            answer = self._ask(f"  Choose 1-{len(options)} [{default}]: ").strip() or str(default)
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer)
            self.say("  Please enter one of the numbers shown.")

    def yes_no(self, question: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        answer = self._ask(f"? {question} [{hint}]: ").strip().lower()
        return default if not answer else answer in ("y", "yes")

    def id_list(self, question: str, prefix: str, minimum: int) -> list[str]:
        pattern = re.compile(rf"^{prefix}-[0-9a-f]+$")
        while True:
            raw = self._ask(f"? {question} (comma separated): ").strip()
            ids = [part.strip() for part in raw.split(",") if part.strip()]
            if len(ids) >= minimum and all(pattern.match(i) for i in ids):
                return ids
            self.say(f"  Enter at least {minimum} ids like {prefix}-0123456789abcdef0.")

    # ---- the questionnaire --------------------------------------------------
    def run(self) -> dict:
        say = self.say
        say("DIAL installer: answer a few questions and dial.yaml will be written for you.\n")
        answers: dict = {"apiVersion": "dial/v1"}
        answers["name"] = self.text("Installation name (short, lowercase)", "dial", r"[a-z][a-z0-9-]{1,20}",
                                    "Use 2-21 lowercase letters, digits or dashes.")
        region_default = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        answers["aws"] = {"region": self.text("AWS region", region_default, r"[a-z]{2}(-[a-z]+)+-[0-9]")}

        index = releases.bundled_index()
        say("Available DIAL releases in this package: " + ", ".join(index["releases"]))
        answers["release"] = self.text("DIAL release to install (any release published in AWS Marketplace also works)",
                                       index["latest"], r"[A-Za-z0-9][A-Za-z0-9._-]*")

        domain: dict = {"base": self.text("Base domain (chat., admin., core. ... are created under it)",
                                          pattern=r"[a-z0-9.-]+\.[a-z]{2,}", hint="Example: dial.example.com")}
        if self.yes_no("Use different host names than chat.<domain>, admin.<domain> ...?", False):
            hosts = {}
            for service in SERVICES:
                hosts[service] = self.text(f"Host name for {service}", f"{service}.{domain['base']}")
            domain["hosts"] = hosts
        domain["certificate_arn"] = self.text(
            "ACM certificate ARN (must be ISSUED and cover all host names)",
            pattern=r"arn:aws[a-z-]*:acm:[a-z0-9-]+:\d{12}:certificate/.+",
            hint="It looks like arn:aws:acm:<region>:<account>:certificate/<id>")
        answers["domain"] = domain

        answers["network"] = self._network()
        answers["identity"] = self._identity()
        if self.choice("How does DIAL Admin sign in to the database?",
                       ["Password (simple)", "Its IAM role (no password, short-lived tokens)"]) == 2:
            answers["database"] = {"auth": "iam"}
        if not self.yes_no("Install analytics (Superset, Grafana)?", True):
            answers["features"] = {"analytics": False}
        if not self.yes_no("Copy the product images into your own private ECR (recommended)?", True):
            answers["registry"] = {"provider": "public"}

        secret_arn = self.optional("ARN of the Secrets Manager secret, if you already created it")
        answers["secrets"] = {"arn": secret_arn or _PLACEHOLDER_ARN}
        return answers

    def _network(self) -> dict:
        kind = self.choice("Network", ["Create a new VPC", "Use an existing VPC", "Use a VPC shared with this account (AWS RAM)"])
        network: dict = {"provider": ["create", "existing-vpc", "shared-vpc"][kind - 1]}
        if kind == 1:
            cidr = self.text("VPC CIDR", "10.0.0.0/16", r"\d+\.\d+\.\d+\.\d+/\d+")
            try:
                ipaddress.ip_network(cidr)
            except ValueError:
                raise InstallerError(f"{cidr} is not a valid CIDR") from None
            if cidr != "10.0.0.0/16":
                network["cidr"] = cidr
                if not cidr.endswith("/16"):
                    network["subnet_cidrs"] = {
                        "public": [self.text("Public subnet 1 CIDR"), self.text("Public subnet 2 CIDR")],
                        "private": [self.text("Private subnet 1 CIDR"), self.text("Private subnet 2 CIDR")],
                    }
        else:
            network["vpc_id"] = self.text("VPC id", pattern=r"vpc-[0-9a-f]+", hint="Example: vpc-0123456789abcdef0")
            subnets = {"private": self.id_list("Private subnet ids (two Availability Zones)", "subnet", 2)}
            public = self.optional("Public subnet ids, needed only for an internet-facing load balancer")
            if public:
                subnets["public"] = [p.strip() for p in public.split(",") if p.strip()]
            network["subnets"] = subnets
        exposure = self.choice("Load balancer", ["Internal (private)", "Internet-facing (public)"])
        network["exposure"] = "internal" if exposure == 1 else "public"
        while True:
            raw = self.text("Networks allowed to reach the application on HTTPS (CIDRs, comma separated, up to 5)", "0.0.0.0/0")
            cidrs = [part.strip() for part in raw.split(",") if part.strip()]
            try:
                for cidr in cidrs:
                    ipaddress.ip_network(cidr)
            except ValueError:
                self.say("  Enter CIDRs such as 10.0.0.0/8.")
                continue
            if 1 <= len(cidrs) <= 5:
                break
            self.say("  Enter between 1 and 5 networks.")
        if cidrs != ["0.0.0.0/0"]:
            network["allowed_cidrs"] = cidrs
        return network

    def _identity(self) -> dict:
        kind = self.choice("How do users sign in?", ["Amazon Cognito users", "SAML (your identity provider, through Cognito)"])
        if kind == 1:
            return {}
        return {
            "provider": "cognito-saml",
            "saml": {
                "provider_name": self.text("Name for the identity provider", "CorporateIdP", r"[A-Za-z0-9_.-]+"),
                "metadata_url": self.text("SAML metadata URL", pattern=r"https://.+", hint="It must start with https://"),
                "idp_signout": self.yes_no("Sign users out of the identity provider too?", False),
            },
        }


# ---- file output --------------------------------------------------------------
def render_config(answers: dict) -> str:
    """dial.yaml text with comments (hand-written so every setting is explained)."""
    domain = answers["domain"]
    network = answers["network"]
    lines = [
        "# DIAL installation config. Generated by `./dial-install init`; safe to edit by hand.",
        "# Check it with:  ./dial-install check",
        "",
        "apiVersion: dial/v1",
        f"name: {answers['name']}                       # prefix of all AWS resource names",
        "",
        f"release: {answers['release']}              # every component version comes from this release",
        "",
        "aws:",
        f"  region: {answers['aws']['region']}",
        "",
        "domain:",
        f"  base: {domain['base']}        # chat.<base>, admin.<base>, core.<base>, ...",
        f"  certificate_arn: {domain['certificate_arn']}",
    ]
    if "hosts" in domain:
        lines.append("  hosts:                          # full host names; override any service")
        lines += [f"    {service}: {host}" for service, host in domain["hosts"].items()]
    lines += [
        "",
        "secrets:",
        "  # Create it with the command printed by `init`, then paste the ARN it returns.",
        f"  arn: {_yaml_scalar(answers['secrets']['arn'])}",
        "",
        "network:",
        f"  provider: {network['provider']}               # create | existing-vpc | shared-vpc",
    ]
    for key in ("cidr", "vpc_id"):
        if key in network:
            lines.append(f"  {key}: {network[key]}")
    if "subnet_cidrs" in network:
        lines.append("  subnet_cidrs:")
        for kind, values in network["subnet_cidrs"].items():
            lines.append(f"    {kind}: [{', '.join(values)}]")
    if "subnets" in network:
        lines.append("  subnets:")
        for kind, values in network["subnets"].items():
            lines.append(f"    {kind}: [{', '.join(values)}]")
    lines.append(f"  exposure: {network['exposure']}                # internal | public")
    if "allowed_cidrs" in network:
        lines.append(f"  allowed_cidrs: [{', '.join(network['allowed_cidrs'])}]")

    identity = answers["identity"]
    if identity:
        saml = identity["saml"]
        lines += [
            "",
            "identity:",
            "  provider: cognito-saml",
            "  saml:",
            f"    provider_name: {saml['provider_name']}",
            f"    metadata_url: {saml['metadata_url']}",
            f"    idp_signout: {str(saml['idp_signout']).lower()}",
        ]
    if answers.get("database"):
        lines += ["", "database:", f"  auth: {answers['database']['auth']}              # password | iam"]
    if answers.get("features"):
        lines += ["", "features:", "  analytics: false"]
    if answers.get("registry"):
        lines += ["", "registry:", "  provider: public                # pull images directly from the AWS Marketplace registry"]
    lines += [
        "",
        "# More options (node sizes, Kubernetes version, database capacity): see docs/CONFIGURATION.md",
        "",
    ]
    return "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    return json.dumps(value) if value.startswith("TODO") else value


def write_outputs(answers: dict, directory: Path, say: Say) -> None:
    config_path = directory / "dial.yaml"
    if config_path.exists():
        raise InstallerError(f"{config_path} already exists.", "Rename or delete it first; init never overwrites your config.")
    config_path.write_text(render_config(answers), encoding="utf-8")
    say(f"\nWrote {config_path}")
    if answers["secrets"]["arn"].startswith("TODO"):
        say(write_secrets_file(directory, answers["name"], answers["aws"]["region"]))
    say("\nNext: ./dial-install check")


def write_secrets_file(directory: Path, name: str, region: str) -> str:
    path = directory / SECRETS_FILE
    if path.exists():
        raise InstallerError(f"{path} already exists.", "Delete it first if you want fresh random values.")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(secrets_mod.generate(), stream, indent=2)
        stream.write("\n")
    return (
        f"\nWrote {path} (permissions 600) with random values for every secret DIAL needs.\n"
        "Create the secret in your AWS account:\n\n"
        f"  {secrets_mod.create_command(name, region, SECRETS_FILE)}\n\n"
        f"Then DELETE {SECRETS_FILE} and paste the ARN from the command output into dial.yaml (secrets.arn).\n"
        "Never change these values after DIAL is installed: stored data would become unreadable."
    )
