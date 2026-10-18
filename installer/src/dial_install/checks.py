"""Read-only pre-checks that are not specific to one module."""

from __future__ import annotations

import fnmatch
import shutil

from . import secrets as secrets_mod
from . import tools
from .context import Context
from .errors import InstallerError
from .findings import Finding, fail, ok, warn


def tool_findings(ctx: Context) -> list[Finding]:
    findings = []
    versions = {"aws": ("aws", "--version"), "kubectl": ("kubectl", "version", "--client"),
                "helm": ("helm", "version", "--short")}
    install_hint = "Run `./dial-install install-tools` (installs kubectl and helm into ~/.dial-install/bin)."
    for tool, argv in versions.items():
        path = shutil.which(tool) or tools.which(tool)
        if not path:
            hint = ("Install the AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
                    if tool == "aws" else install_hint)
            findings.append(fail("tools", f"{tool} is not installed", hint))
            continue
        result = ctx.cloud.runner.run([path, *argv[1:]], check=False)
        first = (result.stdout or result.stderr).strip().splitlines()[:1]
        if result.returncode != 0:
            findings.append(warn("tools", f"{tool} is installed but did not run cleanly: {first[0] if first else path}"))
        else:
            findings.append(ok("tools", f"{tool}: {first[0] if first else path}"))
    return findings


def identity_finding(ctx: Context) -> list[Finding]:
    data = ctx.cloud.aws_json("sts", "get-caller-identity", check=False)
    if not data:
        return [fail("aws", "cannot call AWS with the current credentials",
                     "In CloudShell this just works. Elsewhere run `aws configure` or pass `--profile <name>`.")]
    wanted = ctx.cfg["aws"].get("account_id")
    if wanted and wanted != data["Account"]:
        return [fail("aws", f"credentials belong to account {data['Account']} but dial.yaml expects {wanted}",
                     "Switch to the right account or fix aws.account_id.")]
    return [ok("aws", f"account {data['Account']}, {data['Arn']}")]


def secret_findings(ctx: Context) -> list[Finding]:
    try:
        data = secrets_mod.fetch(ctx.cloud, ctx.secret_arn)
    except InstallerError as error:
        return [fail("secret", error.message, error.hint)]
    ctx.secret = data
    findings = []
    status = secrets_mod.audit(data)
    bad = {name: state for name, state in status.items() if state != "present"}
    if bad:
        for name, state in bad.items():
            findings.append(fail("secret", f"key {name}: {state}",
                                 "Re-create the secret from the file produced by `./dial-install init` "
                                 "(or `./dial-install init --secrets-only`)."))
    else:
        findings.append(ok("secret", f"all {len(status)} required keys are present"))
    return findings


def _covers(pattern: str, host: str) -> bool:
    """TLS wildcard rule: `*.example.com` covers exactly one extra label."""
    if pattern.startswith("*."):
        return fnmatch.fnmatchcase(host, pattern) and host.count(".") == pattern.count(".")
    return pattern.lower() == host.lower()


def certificate_findings(ctx: Context) -> list[Finding]:
    arn = ctx.cfg["domain"]["certificate_arn"]
    data = ctx.cloud.aws_json("acm", "describe-certificate", "--certificate-arn", arn, check=False)
    if not data:
        return [fail("certificate", f"cannot read {arn}",
                     "Check the ARN, that it is in this account and region, and that you may call acm:DescribeCertificate.")]
    cert = data["Certificate"]
    findings = []
    if cert["Status"] != "ISSUED":
        findings.append(fail("certificate", f"status is {cert['Status']}, it must be ISSUED",
                             "Finish the DNS or email validation in AWS Certificate Manager."))
    names = cert.get("SubjectAlternativeNames") or [cert.get("DomainName", "")]
    uncovered = [h for h in ctx.cfg["domain"]["hosts"].values() if not any(_covers(n, h) for n in names)]
    if uncovered:
        findings.append(fail("certificate", "does not cover: " + ", ".join(uncovered),
                             "Certificate names: " + ", ".join(names) + "\nRequest a certificate that includes every host name."))
    if not findings:
        findings.append(ok("certificate", f"issued and covers all {len(ctx.cfg['domain']['hosts'])} host names"))
    return findings


def license_findings(ctx: Context) -> list[Finding]:
    """Report the license units this account has already consumed (read only; units cannot be returned)."""
    from . import licenses
    if ctx.cloud.runner.dry_run:
        return []
    rows = licenses.consumed(ctx.cloud)
    if not rows:
        return [ok("license", "no license unit consumed in this account yet (an installation consumes one)")]
    return [ok("license", f"already consumed in this account: {licenses.describe(rows)}. An installation consumes one unit; "
                          "units of a PERPETUAL license cannot be returned")]


def chart_findings(ctx: Context) -> list[Finding]:
    """Can this account pull the platform chart from the Marketplace registry?"""
    from . import deploy
    repository, version = deploy.chart_ref(ctx)
    if ctx.cache.get("local_chart"):                      # vendor test of a chart that is not published yet
        result = ctx.cloud.runner.run(["helm", "show", "chart", repository], check=False)
        if result.returncode != 0:
            return [fail("chart", f"cannot read the local chart {repository}", result.stderr.strip())]
        return [ok("chart", f"local chart {repository} (not from the registry)")]
    try:
        deploy.registry_login(ctx.cloud, repository)
    except Exception as error:  # noqa: BLE001 - any failure here means "cannot pull"
        return [fail("chart", f"cannot log in to the chart registry ({error})",
                     "Subscribe to the DIAL product in AWS Marketplace with this account, "
                     "and make sure your AWS user may call ecr:GetAuthorizationToken.")]
    result = ctx.cloud.runner.run(["helm", "show", "chart", repository, "--version", version], check=False)
    if result.returncode != 0:
        return [fail("chart", f"cannot pull chart {version}", result.stderr.strip() or
                     "Check your AWS Marketplace subscription and the chart version.")]
    return [ok("chart", f"chart {version} is available")]

