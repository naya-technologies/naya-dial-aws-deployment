"""Infrastructure: one CloudFormation stack per module.

CloudFormation is the only state. Stacks are named <name>-<slot>, so any machine
with the right credentials can re-read the outputs; nothing is stored locally.
"""

from __future__ import annotations

from typing import Callable

from . import __version__
from .context import Context
from .errors import CommandError, InstallerError
from .findings import Finding, fail
from .modules import Module

# A stack in one of these states cannot be updated; the customer must look at it first.
_BLOCKING_STATES = {
    "ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "CREATE_FAILED", "DELETE_FAILED",
    "UPDATE_ROLLBACK_FAILED", "IMPORT_ROLLBACK_FAILED",
}

_MAX_INLINE_TEMPLATE_BYTES = 51_200
REVISION_TAG = "dial-install:infra-revision"


class Pending(dict):
    """Placeholder outputs used by --dry-run for stacks that do not exist yet."""

    def __missing__(self, key: str) -> str:
        return f"<{key}>"


def stack_status(ctx: Context, stack: str) -> str | None:
    data = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", stack, check=False)
    if not data or not data.get("Stacks"):
        return None
    return data["Stacks"][0]["StackStatus"]


def state_findings(ctx: Context, modules: list[Module]) -> list[Finding]:
    """Fail for stacks that need a human before they can be touched."""
    findings = []
    for module in modules:
        if not module.stack_file:
            continue
        stack = ctx.stack_name(module.slot)
        status = stack_status(ctx, stack)
        if status is None:
            continue
        if status in _BLOCKING_STATES:
            findings.append(fail(module.slot, f"stack {stack} is in state {status}",
                                 "Open CloudFormation, read the failed events, delete the stack, then run again."))
        elif status.endswith("_IN_PROGRESS"):
            findings.append(fail(module.slot, f"stack {stack} is still changing ({status})",
                                 "Wait for it to finish, then run again."))
    return findings


def template_size_findings(modules: list[Module]) -> list[Finding]:
    findings = []
    for module in modules:
        if module.stack_file and module.stack_file.stat().st_size > _MAX_INLINE_TEMPLATE_BYTES:
            findings.append(fail(module.slot, f"template {module.label} exceeds the 51,200 byte CloudFormation limit"))
    return findings


def deployed_revision(ctx: Context, modules: list[Module]) -> int | None:
    """Infrastructure revision recorded on the deployed stacks (the lowest one), or None if unknown."""
    revisions = []
    for module in modules:
        if not module.stack_file:
            continue
        data = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name",
                                  ctx.stack_name(module.slot), check=False)
        if not data or not data.get("Stacks"):
            return None
        tags = {t["Key"]: t["Value"] for t in data["Stacks"][0].get("Tags", [])}
        revisions.append(int(tags.get(REVISION_TAG, 0)))
    return min(revisions) if revisions else None


def planned_action(ctx: Context, module: Module) -> str:
    if not module.stack_file:
        return "run" if module.has_hook("before_platform") else "skip"
    return "update" if stack_status(ctx, ctx.stack_name(module.slot)) else "create"


def read_outputs(ctx: Context, module: Module) -> dict[str, str] | None:
    """Stack outputs passed through the module's finalize hook, or None if not deployed."""
    raw: dict[str, str] = {}
    if module.stack_file:
        data = ctx.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name",
                                  ctx.stack_name(module.slot), check=False)
        if not data or not data.get("Stacks"):
            return None
        raw = {o["OutputKey"]: o["OutputValue"] for o in data["Stacks"][0].get("Outputs", [])}
    outputs = module.finalize_outputs(ctx, raw)
    missing = [key for key in module.provides if key not in outputs]
    if missing:
        raise InstallerError(
            f"Module {module.label} did not produce: {', '.join(missing)}",
            "This is a bug in the installer package; please report it.",
        )
    return outputs


def load_outputs(ctx: Context, modules: list[Module]) -> None:
    """Fill ctx.outputs for every module; error if infrastructure is missing."""
    for module in modules:
        outputs = read_outputs(ctx, module)
        if outputs is None:
            raise InstallerError(
                f"Infrastructure stack {ctx.stack_name(module.slot)} does not exist.",
                "Run `./dial-install apply` first.",
            )
        ctx.outputs[module.slot] = outputs


def deploy(ctx: Context, modules: list[Module], say: Callable[[str], None]) -> None:
    for module in modules:
        stack = ctx.stack_name(module.slot)
        if not module.stack_file:
            ctx.outputs[module.slot] = module.finalize_outputs(ctx, {})
            say(f"  {module.slot}: nothing to deploy ({module.label})")
            continue

        say(f"  {module.slot}: {planned_action(ctx, module)} stack {stack} ...")
        for phase in module.deploy_phases(ctx):
            _deploy_stack(ctx, module, stack, {**module.parameters(ctx), **phase.get("parameters", {})}, say)
            outputs = read_outputs(ctx, module)
            if outputs is None:
                if not ctx.cloud.runner.dry_run:
                    raise InstallerError(f"Stack {stack} has no outputs after deployment.")
                outputs = Pending()
            ctx.outputs[module.slot] = outputs
            after = phase.get("after")
            if after:
                after(ctx, say)


def _deploy_stack(ctx: Context, module: Module, stack: str, params: dict[str, str], say: Callable[[str], None]) -> None:
    argv = [
        "cloudformation", "deploy",
        "--stack-name", stack,
        "--template-file", str(module.stack_file),
        "--capabilities", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND",
        "--no-fail-on-empty-changeset",
        "--tags", f"dial-install:installation={ctx.name}", f"dial-install:version={__version__}",
        f"{REVISION_TAG}={ctx.release.get('infra_revision', 0)}",
        *[f"{key}={value}" for key, value in ctx.cfg["tags"].items()],
    ]
    if params:
        argv += ["--parameter-overrides", *[f"{k}={v}" for k, v in params.items()]]
    try:
        ctx.cloud.aws(*argv, write=True, stream=True)
    except CommandError as error:
        raise InstallerError(
            f"CloudFormation stack {stack} failed.\n{error.stderr}",
            "See what failed:\n  aws cloudformation describe-stack-events --stack-name "
            f"{stack} --region {ctx.region} --max-items 15 "
            "--query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' "
            "--output table\nFix the cause, then run `./dial-install apply` again (it resumes).",
        ) from error
