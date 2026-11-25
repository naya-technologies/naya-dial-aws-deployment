"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, tools, wizard, workflow
from .releases import load_defaults
from .errors import CommandError, InstallerError
from .runner import Runner


_BUILT_IN_FLAGS = {"--model", "--version", "--instance-type", "--nodes", "--no-dedicated-nodes"}


def _catalog_flags() -> dict[str, dict]:
    """name -> option, for every add-on setting in the catalog that has its own flag (e.g. --guardrail-id)."""
    from . import addons
    found: dict[str, dict] = {}
    for entry in addons.load_catalog().values():
        for name, option in (entry.get("options") or {}).items():
            if option.get("flag") and option["flag"] not in _BUILT_IN_FLAGS:
                found.setdefault(name, option)
    return found


def _addon_settings(args: argparse.Namespace) -> dict | None:
    settings = {key: value for key, value in (
        ("model", getattr(args, "model", None)), ("version", getattr(args, "addon_version", None)),
        ("instance_type", getattr(args, "instance_type", None)), ("nodes", getattr(args, "nodes", None)),
        ("dedicated_nodes", "false" if getattr(args, "no_dedicated_nodes", False) else None)) if value}
    settings.update({key[4:]: value for key, value in vars(args).items() if key.startswith("opt_") and value is not None})
    return settings or None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dial-install",
        description="Install and upgrade DIAL on AWS from one config file.",
        epilog="Typical use: init -> check -> plan -> apply.  Later: upgrade.  Optional products: add-ons list | install | upgrade | remove.",
    )
    parser.add_argument("--version", action="version", version=f"dial-install {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", default="dial.yaml", help="config file (default: dial.yaml)")
        p.add_argument("--profile", help="AWS CLI profile to use (default: the CLI's default credentials)")
        p.add_argument("--release", help="DIAL release to use instead of `release:` in the config (see `releases`)")
        p.add_argument("--chart", help="vendor: use a local chart directory (with release.yaml) instead of the registry, to test a release before publishing it")

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
    apply.add_argument("--allow-missing-images", type=int, default=0, metavar="N",
                       help="continue when up to N images cannot be copied (default 0); the affected components stay down")

    upgrade = sub.add_parser("upgrade", help="move an installed DIAL to the version in this package")
    common(upgrade)
    upgrade.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    upgrade.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
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

    addon = sub.add_parser("add-ons", aliases=["addons"], help="list, install, upgrade or remove optional add-ons (AI Brain, ...)")
    actions = addon.add_subparsers(dest="addon_action", required=True, metavar="action")
    common(actions.add_parser("list", help="the add-ons in this package and which are installed (changes nothing)"))
    for action, text in (("install", "copy an add-on's images into your ECR and install it"),
                         ("upgrade", "move an installed add-on to the version in this package"),
                         ("remove", "remove an add-on and everything it created")):
        one = actions.add_parser(action, help=text)
        common(one)
        one.add_argument("addon", help="add-on id (see `add-ons list`)")
        one.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
        one.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
        if action in ("install", "upgrade"):
            one.add_argument("--version", dest="addon_version", help="a specific version (registry tag) instead of the newest one")
            one.add_argument("--instance-type", help="add-ons with their own nodes (AI RAG): EC2 instance type of the node group")
            one.add_argument("--nodes", help="add-ons with their own nodes: number of nodes (1-10)")
            one.add_argument("--no-dedicated-nodes", action="store_true",
                             help="add-ons with their own nodes: run on the shared platform nodes instead (not recommended)")
            for name, option in _catalog_flags().items():          # settings declared by add-ons in the catalog
                if option.get("pattern") == "^(true|false)$":
                    one.add_argument(option["flag"], dest=f"opt_{name}", action="store_const", const="true",
                                     help=option.get("description", ""))
                else:
                    one.add_argument(option["flag"], dest=f"opt_{name}", metavar="VALUE", help=option.get("description", ""))
            one.add_argument("--model", help="add-ons that use a model (AI RAG): the DIAL model (deployment id) to use; "
                                             "default: the system default model, or the one already installed")
        if action == "remove":
            one.add_argument("--keep-images", action="store_true", help="keep the add-on's images in your ECR")

    sub.add_parser("releases", help="list the DIAL releases included in this package")
    verify = sub.add_parser("verify-release", help="check that every image of a release exists in the Marketplace registry")
    common(verify)
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
    if args.command == "addons":                     # alias of add-ons
        args.command = "add-ons"
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
        addon=getattr(args, "addon", None),
        addon_settings=_addon_settings(args),
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
        if args.command == "add-ons":
            action = {"list": workflow.cmd_addons_list, "install": workflow.cmd_addons_install,
                      "upgrade": workflow.cmd_addons_upgrade, "remove": workflow.cmd_addons_remove}[args.addon_action]
            return action(options, runner, print)
        if args.command == "grant-access":
            return workflow.cmd_grant_access(options, runner, print, args.principal_arn)
        return commands[args.command](options, runner, print)
    except CommandError as error:
        raise InstallerError(error.message) from error
