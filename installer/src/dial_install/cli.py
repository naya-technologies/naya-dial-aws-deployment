"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, tools, wizard, workflow
from .releases import load_defaults
from .errors import CommandError, InstallerError
from .runner import Runner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dial-install",
        description="Install and upgrade DIAL on AWS from one config file.",
        epilog="Typical use: init -> check -> plan -> apply.  Later: upgrade.",
    )
    parser.add_argument("--version", action="version", version=f"dial-install {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", default="dial.yaml", help="config file (default: dial.yaml)")
        p.add_argument("--profile", help="AWS CLI profile to use (default: the CLI's default credentials)")
        p.add_argument("--release", help="DIAL release to use instead of `release:` in the config (see `releases`)")

    sub.add_parser("init", help="ask a few questions and write dial.yaml")
    common(sub.add_parser("check", help="verify everything is ready (changes nothing)"))
    mirror = sub.add_parser("mirror", help="copy the product images into your private ECR only (apply does this too)")
    common(mirror)
    mirror.add_argument("--allow-missing-images", type=int, default=0, metavar="N",
                        help="continue when up to N images cannot be copied (default 0)")
    common(sub.add_parser("plan", help="show what apply will create or update (changes nothing)"))

    apply = sub.add_parser("apply", help="create the infrastructure and install DIAL")
    common(apply)
    apply.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    apply.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
    apply.add_argument("--chart", help="vendor: install from a local chart directory (with release.yaml) to test it before publishing")
    apply.add_argument("--allow-missing-images", type=int, default=0, metavar="N",
                       help="continue when up to N images cannot be copied (default 0); the affected components stay down")

    upgrade = sub.add_parser("upgrade", help="move an installed DIAL to the version in this package")
    common(upgrade)
    upgrade.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    upgrade.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
    upgrade.add_argument("--chart", help="vendor: upgrade to a local chart directory (with release.yaml) to test it before publishing")
    upgrade.add_argument("--with-infra", action="store_true",
                         help="also update the CloudFormation stacks (needed when a release changes infrastructure)")

    remove = sub.add_parser("uninstall", aliases=["remove"],
                            help="remove everything this installation created (asks you to type its name)")
    common(remove)
    remove.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    remove.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
    remove.add_argument("--keep-images", action="store_true", help="keep the mirrored product images in your ECR (faster next install)")
    remove.add_argument("--keep-secret", action="store_true", help="keep the Secrets Manager secret")
    remove.add_argument("--keep-snapshot", action="store_true", help="keep the final database snapshot")

    sub.add_parser("releases", help="list the DIAL releases included in this package")
    verify = sub.add_parser("verify-release", help="check that every image of a release exists in the Marketplace registry")
    common(verify)
    verify.add_argument("--chart", help="vendor: check a local chart directory (with release.yaml) before publishing it")
    grant = sub.add_parser("grant-access", help="give another IAM user or role cluster-admin access to the cluster")
    common(grant)
    grant.add_argument("principal_arn", help="ARN of the IAM user or role")
    common(sub.add_parser("status", help="show stacks, version and the DNS records to create"))
    sub.add_parser("install-tools", help="download kubectl, helm and crane into ~/.dial-install/bin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "remove":                     # alias of uninstall
        args.command = "uninstall"
    try:
        return _dispatch(args)
    except InstallerError as error:
        print(f"\nERROR: {error.message}", file=sys.stderr)
        if error.hint:
            print("\n" + "\n".join("  " + line for line in error.hint.splitlines()), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. It is safe to run the command again.", file=sys.stderr)
        return 130


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        answers = wizard.Wizard().run()
        wizard.write_outputs(answers, Path.cwd(), print)
        return 0
    if args.command == "releases":
        return workflow.cmd_releases(None, None, print)
    if args.command == "install-tools":
        release = load_defaults()["tools"]
        for name, installer, version in (("kubectl", tools.install_kubectl, release["kubectl_version"]),
                                         ("helm", tools.install_helm, release["helm_version"]),
                                         ("crane", tools.install_crane, release["crane_version"])):
            print(f"Installing {name} {version} ...")
            print(f"  -> {installer(version)}")
        print(f"\nDone. `dial-install` finds these automatically (they live in {tools.BIN_DIR}).")
        return 0

    options = workflow.Options(
        config_path=Path(args.config),
        profile=args.profile,
        assume_yes=getattr(args, "yes", False),
        dry_run=getattr(args, "dry_run", False),
        release=getattr(args, "release", None),
        chart_path=Path(args.chart) if getattr(args, "chart", None) else None,
        with_infra=getattr(args, "with_infra", False),
        keep_images=getattr(args, "keep_images", False),
        keep_secret=getattr(args, "keep_secret", False),
        keep_snapshot=getattr(args, "keep_snapshot", False),
        allow_missing_images=getattr(args, "allow_missing_images", 0),
    )
    runner = Runner(dry_run=options.dry_run)
    commands = {
        "check": workflow.cmd_check,
        "plan": workflow.cmd_plan,
        "mirror": workflow.cmd_mirror,
        "uninstall": workflow.cmd_uninstall,
        "apply": workflow.cmd_apply,
        "upgrade": workflow.cmd_upgrade,
        "status": workflow.cmd_status,
        "verify-release": workflow.cmd_verify_release,
    }
    try:
        if args.command == "grant-access":
            return workflow.cmd_grant_access(options, runner, print, args.principal_arn)
        return commands[args.command](options, runner, print)
    except CommandError as error:
        raise InstallerError(error.message) from error
