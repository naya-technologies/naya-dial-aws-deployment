"""The customer-facing commands: check, plan, apply, upgrade, status."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__, adapters, checks, licenses, cilium as cilium_mod, config, deploy, infra, mirror, modules as modules_mod, releases, uninstall as uninstall_mod
from .bootstrap import Bootstrap
from .cloud import Cloud
from .context import Context
from .errors import InstallerError
from .findings import FAIL, Finding, blocking, ok, render
from .modules import Module
from .runner import Runner


@dataclass
class Options:
    config_path: Path
    profile: str | None = None
    assume_yes: bool = False
    dry_run: bool = False
    release: str | None = None
    chart_path: Path | None = None
    with_infra: bool = False
    keep_images: bool = False
    keep_secret: bool = False
    keep_snapshot: bool = False
    allow_missing_images: int = 0


Say = Callable[[str], None]


def build_context(options: Options, runner: Runner) -> tuple[Context, list[Module]]:
    raw = config.load(options.config_path)
    cloud = Cloud(runner, raw["aws"]["region"], options.profile, kube_context=f"dial-{raw['name']}")
    source = raw.get("registry", {}).get("source")
    repository = releases.chart_repository(releases.load_defaults(), source)[1]
    if options.chart_path:                      # vendor pre-publish check: the chart and its release.yaml are local
        release = releases.from_local_chart(options.chart_path, source)
    else:
        release = releases.resolve(options.release or raw.get("release"), cloud,
                                   login=lambda: deploy.registry_login(cloud, repository), source=source)
    resolved = config.resolve(raw, release)
    problems = config.semantic_errors(resolved)
    if problems:
        raise InstallerError(
            f"{options.config_path} has {len(problems)} problem(s):\n  - " + "\n  - ".join(problems),
            "Fix the lines above and run the command again.",
        )
    selected = modules_mod.select(resolved)
    ctx = Context(cfg=resolved, release=release, cloud=cloud, modules=selected)
    if options.chart_path:
        ctx.cache["local_chart"] = options.chart_path
    ctx.cache["allow_missing_images"] = options.allow_missing_images
    return ctx, selected


def _release_label(ctx: Context) -> str:
    return f"{ctx.release['release']} (from {ctx.release['release_source']})"


# ---- releases ---------------------------------------------------------------
def cmd_releases(options: Options, runner: Runner, say: Say) -> int:
    index = releases.bundled_index()
    say("Releases included in this installer:")
    for release_id in index["releases"]:
        marker = "  (latest)" if release_id == index["latest"] else ""
        manifest = releases.load_bundled(release_id)
        say(f"  {release_id}{marker}")
        for line in (manifest.get("notes") or "").strip().splitlines():
            say(f"      {line}")
    say("\nAny release published in the Marketplace registry can also be used: set `release: <id>` in dial.yaml.")
    return 0


# ---- check ------------------------------------------------------------------
def collect_findings(ctx: Context, modules: list[Module], *, with_chart: bool = True) -> list[Finding]:
    findings: list[Finding] = [ok("config", "dial.yaml is valid")]
    findings += checks.tool_findings(ctx)
    identity = checks.identity_finding(ctx)
    findings += identity
    if any(f.level == FAIL for f in identity + [f for f in findings if f.subject == "tools"]):
        return findings                      # nothing else can be checked without tools and credentials
    findings += checks.secret_findings(ctx)
    findings += checks.certificate_findings(ctx)
    findings += checks.license_findings(ctx)
    chart = checks.chart_findings(ctx) if with_chart else []
    findings += chart
    for module in modules:
        if module.slot == "registry" and any(f.level == FAIL for f in chart):
            continue                          # the image list comes from the chart; nothing to check without it
        findings += module.check(ctx)
    findings += infra.template_size_findings(modules)
    findings += infra.state_findings(ctx, modules)
    return findings


def cmd_check(options: Options, runner: Runner, say: Say) -> int:
    ctx, modules = build_context(options, runner)
    say(f"Checking installation '{ctx.name}' in {ctx.region}, release {_release_label(ctx)} ...")
    findings = collect_findings(ctx, modules)
    for finding in findings:
        say(render(finding))
    problems = blocking(findings)
    say("")
    if problems:
        say(f"{len(problems)} problem(s) must be fixed before `apply`. Run `./dial-install check` again afterwards.")
        return 1
    say("All checks passed. Next: ./dial-install plan")
    return 0


# ---- uninstall --------------------------------------------------------------
def cmd_uninstall(options: Options, runner: Runner, say: Say, ask: Callable[[str], str] = input) -> int:
    """Remove everything this installation created. Asks for the installation name unless --yes."""
    ctx, _ = build_context(options, runner)
    identity = checks.identity_finding(ctx)
    if any(f.level == FAIL for f in identity):
        for finding in identity:
            say(render(finding))
        return 1
    worker = uninstall_mod.Uninstaller(ctx, say, keep_images=options.keep_images, keep_secret=options.keep_secret,
                                       keep_snapshot=options.keep_snapshot)
    say(f"This will permanently remove installation '{ctx.name}' from account {ctx.account_id()} in {ctx.region}:")
    for line in worker.plan():
        say(f"  - {line}")
    say("\nThe database and everything stored in it will be deleted.")
    if not options.dry_run and not options.assume_yes:
        if ask(f"Type the installation name ({ctx.name}) to continue: ").strip() != ctx.name:
            say("Cancelled. Nothing was changed.")
            return 0
    left = worker.run()
    say("")
    if left:
        say("Still present (run `./dial-install uninstall` again, it continues):")
        for item in left:
            say(f"  - {item}")
        return 1
    say("Done. Nothing of this installation is left." + (" (dry run: nothing was changed)" if options.dry_run else ""))
    return 0


# ---- mirror -----------------------------------------------------------------
def cmd_mirror(options: Options, runner: Runner, say: Say) -> int:
    """Copy the product images into the account's private ECR and nothing else. `apply` does the same step
    and skips images that are already there, so this can run anywhere with fast access to both registries,
    for example AWS CloudShell, before or while the infrastructure is being created."""
    ctx, modules = build_context(options, runner)
    module = next((m for m in modules if m.slot == "registry" and m.has_hook("before_platform")), None)
    if module is None:
        say("registry.provider is `public`: there is nothing to copy.")
        return 0
    problems = blocking(module.check(ctx))
    if problems:
        for finding in problems:
            say(render(finding))
        return 1
    deploy.registry_login(ctx.cloud, ctx.release["chart"]["repository"])
    if ctx.cfg["kubernetes"]["cni"] == "cilium":              # not product images, but needed first, and small
        copied, skipped = mirror.copy_pairs(ctx, list(cilium_mod.image_pairs(ctx).values()), say)
        say(f"  Cilium images: {copied} copied, {skipped} already present")
    module.before_platform(ctx, say)
    say("\nImages are in your registry. Run `./dial-install apply` to continue.")
    return 0


# ---- plan -------------------------------------------------------------------
def cmd_plan(options: Options, runner: Runner, say: Say) -> int:
    ctx, modules = build_context(options, runner)
    findings = collect_findings(ctx, modules)
    if blocking(findings):
        for finding in findings:
            say(render(finding))
        say("\nFix the problems above first (`./dial-install check` lists them).")
        return 1

    say(f"Plan for '{ctx.name}' in {ctx.region} (dial-install {__version__}, release {_release_label(ctx)})\n")
    say("Infrastructure (CloudFormation stacks):")
    for module in modules:
        action_name = infra.planned_action(ctx, module).upper()
        name = ctx.stack_name(module.slot) if module.stack_file else "(no stack)"
        say(f"  {action_name:<7} {name:<28} {module.label}: {module.description}")
    repository, version = deploy.chart_ref(ctx)
    say(f"\nPlatform: release {ctx.release['release']}, chart {version}")
    say(f"  Kubernetes {ctx.cfg['kubernetes']['version']}, Aurora PostgreSQL {ctx.cfg['database']['engine_version']}")
    hosts = ctx.cfg["domain"]["hosts"]
    say("Public host names:")
    for service, host in hosts.items():
        say(f"  {service:<9} https://{host}")
    say("\nAfter `apply` finishes you must:")
    say("  1. Create DNS records for the host names above (apply prints the exact records).")
    say("  2. Enable access to the Bedrock models you want to use in the AWS console.")
    say("\nNothing has been changed. To proceed: ./dial-install apply")
    return 0


# ---- apply ------------------------------------------------------------------
def _confirm(options: Options, say: Say, question: str, ask: Callable[[str], str] = input) -> bool:
    if options.assume_yes or options.dry_run:
        return True
    return ask(f"{question} [y/N]: ").strip().lower() in ("y", "yes")


def cmd_apply(options: Options, runner: Runner, say: Say, ask: Callable[[str], str] = input) -> int:
    ctx, modules = build_context(options, runner)
    findings = collect_findings(ctx, modules)
    for finding in findings:
        say(render(finding))
    if blocking(findings):
        say("\nFix the problems above first, then run `./dial-install apply` again.")
        return 1
    if not _confirm(options, say, "\nCreate or update the resources listed by `plan`?", ask):
        say("Cancelled. Nothing was changed.")
        return 0

    say("\n[1/2] Infrastructure")
    infra.deploy(ctx, modules, say)

    say("\n[2/2] Platform")
    _install_platform(ctx, say)
    return 0


def _install_platform(ctx: Context, say: Say) -> None:
    deploy.registry_login(ctx.cloud, ctx.release["chart"]["repository"])
    _before_platform(ctx, say)
    deploy.prepare(ctx)
    Bootstrap(ctx, say).run()
    _before_helm(ctx, say)
    deploy.helm_upgrade(ctx, say)
    _verify_and_report(ctx, say)


def _before_platform(ctx: Context, say: Say) -> None:
    for module in ctx.modules:
        if module.has_hook("before_platform"):
            say(f"  {module.label}")
            module.before_platform(ctx, say)


def _before_helm(ctx: Context, say: Say) -> None:
    for module in ctx.modules:
        if module.has_hook("before_helm"):
            module.before_helm(ctx, say)


def _verify_and_report(ctx: Context, say: Say) -> None:
    # The license is prepared first: DIAL Core does not start without it, so waiting for it earlier only burns time.
    deploy.finish_license_installer(ctx, say)
    deploy.ensure_api_key_license(ctx, say)
    pending = deploy.wait_until_ready(ctx, say)
    adapters.name_adapters(ctx, say, deploy.chart_defaults(ctx))
    # The Istio gateway's load balancer is created by the AWS Load Balancer Controller, which is part of the chart.
    Bootstrap(ctx, say).knative_private_dns()
    if pending:
        raise InstallerError(
            "Some workloads are not ready yet: " + ", ".join(pending),
            f"Check them with: kubectl --context {ctx.cloud.kube_context} -n "
            f"{ctx.release['helm_release']['namespace']} get pods\nThen run `./dial-install status`.",
        )
    say("\nDIAL is installed.")
    _print_dns(ctx, say)


def _print_dns(ctx: Context, say: Say) -> None:
    if ctx.cloud.runner.dry_run:
        return
    records = deploy.ingress_hostnames(ctx)
    say("\nCreate these DNS records (CNAME, or an ALIAS record in Route 53):")
    for host, target in records.items():
        say(f"  {host:<45} -> {target}")
    admin = ctx.cfg["domain"]["hosts"]
    say(f"\nOpen https://{admin['chat']} (chat) and https://{admin['admin']} (admin) once DNS resolves.")


# ---- upgrade ----------------------------------------------------------------
def cmd_upgrade(options: Options, runner: Runner, say: Say, ask: Callable[[str], str] = input) -> int:
    ctx, modules = build_context(options, runner)
    say(f"Upgrading '{ctx.name}' to release {_release_label(ctx)} ...")

    problems = blocking(
        checks.tool_findings(ctx) + checks.identity_finding(ctx))
    if problems:
        for finding in problems:
            say(render(finding))
        return 1
    secret_findings = checks.secret_findings(ctx)
    problems = blocking(secret_findings + checks.chart_findings(ctx))
    if problems:
        for finding in problems:
            say(render(finding))
        return 1

    wanted = int(ctx.release.get("infra_revision", 0))
    deployed = infra.deployed_revision(ctx, modules)
    if deployed is not None and deployed < wanted and not options.with_infra:
        raise InstallerError(
            f"Release {ctx.release['release']} needs infrastructure updates (installed revision {deployed}, needed {wanted}).",
            "Run: ./dial-install upgrade --with-infra",
        )
    if options.with_infra:
        infra.deploy(ctx, modules, say)
    else:
        infra.load_outputs(ctx, modules)

    ctx.cloud.aws("eks", "update-kubeconfig", "--name", ctx.cluster_name, "--alias", ctx.cloud.kube_context, write=True)
    current = deploy.installed(ctx)
    if current is None and not runner.dry_run:
        raise InstallerError("DIAL is not installed in this cluster yet.", "Run `./dial-install apply` first.")

    deploy.prepare(ctx)
    new_values = deploy.render(ctx)
    target = ctx.release["chart"]["version"]
    changes = [] if runner.dry_run or current is None else deploy.diff(deploy.live_values(ctx), new_values)

    say(f"\nInstalled chart : {current['chart'] if current else '(dry-run)'}")
    say(f"Target chart    : {target}")
    notes = (ctx.release.get("notes") or "").strip()
    if notes:
        say("\nRelease notes:\n" + "\n".join("  " + line for line in notes.splitlines()))
    # A local chart (vendor test) keeps its version while its content changes, so it is always installed.
    if current and current["chart"].endswith(target) and not changes and not ctx.cache.get("local_chart"):
        say("\nAlready up to date. Nothing to do.")
        return 0
    if changes:
        say(f"\nConfiguration changes ({len(changes)}):")
        for line in changes[:40]:
            say("  " + line)
        if len(changes) > 40:
            say(f"  ... and {len(changes) - 40} more")
    if current:
        say(f"\nTo undo this upgrade later:\n  {deploy.rollback_command(ctx, current['revision'])}")
    if not _confirm(options, say, "\nUpgrade now?", ask):
        say("Cancelled. Nothing was changed.")
        return 0

    deploy.registry_login(ctx.cloud, ctx.release["chart"]["repository"])
    _before_platform(ctx, say)
    _before_helm(ctx, say)
    deploy.helm_upgrade(ctx, say)
    _verify_and_report(ctx, say)
    return 0


# ---- verify-release -----------------------------------------------------------
def cmd_verify_release(options: Options, runner: Runner, say: Say) -> int:
    """Vendor check: every image a release needs exists in the Marketplace registry."""
    ctx, _ = build_context(options, runner)
    if not options.chart_path:
        deploy.registry_login(ctx.cloud, ctx.release["chart"]["repository"])
    images = mirror.discover(ctx, deploy.chart_defaults(ctx))
    say(f"Release {_release_label(ctx)} needs {len(images)} images:")
    absent = mirror.missing(ctx, images)
    for image in images:
        say(f"  {'MISSING' if image in absent else 'ok     '} {image}")
    if absent:
        say(f"\n{len(absent)} image(s) are missing or not readable with these credentials.")
        return 1
    say("\nAll images exist.")
    return 0


# ---- grant access -----------------------------------------------------------
def cmd_grant_access(options: Options, runner: Runner, say: Say, principal_arn: str) -> int:
    ctx, _ = build_context(options, runner)
    Bootstrap(ctx, say).grant_access([principal_arn])
    say("\nDone. They can now run:  aws eks update-kubeconfig --name "
        f"{ctx.cluster_name} --region {ctx.region} --alias {ctx.cloud.kube_context}")
    return 0


# ---- status -----------------------------------------------------------------
def cmd_status(options: Options, runner: Runner, say: Say) -> int:
    ctx, modules = build_context(options, runner)
    say(f"Installation '{ctx.name}' in {ctx.region}\n")
    say("Infrastructure:")
    for module in modules:
        if module.stack_file:
            status = infra.stack_status(ctx, ctx.stack_name(module.slot)) or "NOT DEPLOYED"
            say(f"  {ctx.stack_name(module.slot):<28} {status}")
    ctx.cloud.aws("eks", "update-kubeconfig", "--name", ctx.cluster_name, "--alias", ctx.cloud.kube_context, write=True)
    rows = licenses.consumed(ctx.cloud)
    say("\nLicense (units consumed in this account): " + (licenses.describe(rows) if rows else "none"))
    current = deploy.installed(ctx)
    say("\nPlatform:")
    if current is None:
        say("  not installed")
        return 0
    say(f"  chart {current['chart']}  revision {current['revision']}  status {current['status']}")
    say(f"  configured release {_release_label(ctx)} = chart {ctx.release['chart']['version']}")
    _print_dns(ctx, say)
    return 0
