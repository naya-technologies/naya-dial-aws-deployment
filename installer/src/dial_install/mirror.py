"""Copy the product images from the AWS Marketplace registry into the customer's private ECR.

What is copied is discovered, never hard-coded: the chart is rendered with placeholder values and every
image its workloads use (plus images named in its values) under the Marketplace registry is mirrored with
`crane copy`, which streams registry-to-registry, keeps every architecture and keeps the digest.
Images that already exist with the same digest are skipped, so upgrades only copy what changed.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
import shutil
import tempfile
from pathlib import Path
from typing import Callable

import yaml

from . import tools
from .context import Context
from .errors import CommandError, InstallerError
from .values import overlay, placeholders, registry, substitute

_WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet", "Pod", "CronJob")


# ---- hosts ---------------------------------------------------------------------------------------
def source_host(ctx: Context) -> str:
    return placeholders.source_host(ctx)


def source_prefix(ctx: Context) -> str:
    return placeholders.source_prefix(ctx)


def target_host(ctx: Context) -> str:
    return f"{ctx.account_id()}.dkr.ecr.{ctx.region}.amazonaws.com"


def _region_of(host: str, fallback: str) -> str:
    match = re.search(r"\.ecr\.([a-z0-9-]+)\.amazonaws\.com$", host)
    return match.group(1) if match else fallback


def _split(ref: str, host: str) -> tuple[str, str]:
    """'host/path/name:tag' -> ('path/name', 'tag')"""
    path, _, tag = ref.removeprefix(host + "/").rpartition(":")
    return path, tag


# ---- discovery ---------------------------------------------------------------------------------------
def _filled(ctx: Context, chart_values: dict, registry_host: str) -> dict:
    """Chart values with dummy text in every placeholder and every image pointing at `registry_host`:
    enough to render the workloads and read every image reference, without any infrastructure."""
    document = substitute.overrides(chart_values, {name: "dummy" for name in substitute.find(chart_values)})
    for key, value in overlay.overlay(ctx).items():
        document.setdefault(key, {}).update(value)
    effective = copy.deepcopy(chart_values)
    _merge_rewrite(effective, document)
    _merge_rewrite(document, registry.redirect_to(effective, registry_host, source_prefix(ctx)))
    return document


def _merge_rewrite(document: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            _merge_rewrite(document[key], value)
        else:
            document[key] = value


def deploy_chart_ref(ctx: Context) -> tuple[str, str | None]:
    from . import deploy                      # imported late: deploy imports the values package, which this module uses
    return deploy.chart_ref(ctx)


def _render_images(ctx: Context, document: dict) -> set[str]:
    repository, version = deploy_chart_ref(ctx)
    memo = ctx.cache.setdefault("rendered_images", {})
    key = (repository, version, json.dumps(document, sort_keys=True))
    if key in memo:                          # check, mirror and verification all ask for the same render
        return set(memo[key])
    handle, path = tempfile.mkstemp(prefix="dial-render-", suffix=".yaml")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(document, stream)
        helm = ctx.cloud.runner.run(["helm", "template", ctx.release["helm_release"]["name"], repository,
                                     *(["--version", version] if version else []),
                                     "--namespace", ctx.release["helm_release"]["namespace"], "--values", path])
    except CommandError as error:
        raise InstallerError(f"Could not render the platform chart to list its images.\n{error.stderr}",
                             "Run `./dial-install check`; the chart must be reachable.") from error
    finally:
        os.unlink(path)
    images: set[str] = set()
    for item in yaml.safe_load_all(helm.stdout):
        if not item or item.get("kind") not in _WORKLOADS:
            continue
        spec = item["spec"]
        spec = spec["jobTemplate"]["spec"]["template"]["spec"] if item["kind"] == "CronJob" else \
            spec if item["kind"] == "Pod" else spec["template"]["spec"]
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            images.add(container["image"])
    memo[key] = frozenset(images)
    return images


def split_ref(ref: str) -> tuple[str, str, str]:
    """'host/path/name:tag' -> ('host', 'path/name', 'tag')"""
    host, _, rest = ref.partition("/")
    repository, _, tag = rest.rpartition(":")
    return host, repository, tag


def addons(chart_values: dict) -> dict:
    """The add-on image references pinned by the chart (`installer.addons`)."""
    try:
        return chart_values["installer"]["addons"]
    except KeyError:
        raise InstallerError("This release's chart does not pin the cluster add-on images (installer.addons).",
                             "Use a chart built for dial-install 2.x, or download a newer installer package.") from None


def discover(ctx: Context, chart_values: dict) -> list[str]:
    """Every image reference under the source registry that this release needs, sorted: workloads, build tools named
    in the values, and the cluster add-ons."""
    host = source_host(ctx)
    document = _filled(ctx, chart_values, host)
    found = {image for image in _render_images(ctx, document) if image.startswith(host + "/")}
    effective = copy.deepcopy(chart_values)
    _merge_rewrite(effective, document)
    named = re.findall(re.escape(host) + r"/[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+", json.dumps(effective))
    extra = set()
    database = ctx.cfg["database"]
    if database["auth"] == "iam" and not database.get("bootstrap_image"):         # only the IAM login needs this image
        from . import db_bootstrap
        extra.add(db_bootstrap.pinned_image(ctx, host))
    return sorted(found | set(named) | extra)


def missing(ctx: Context, images: list[str]) -> list[str]:
    """Images that do not exist in the Marketplace registry (or cannot be read with these credentials)."""
    config_dir = _docker_config(ctx, [source_host(ctx)])
    try:
        crane = crane_path(ctx)
        return [image for image in images
                if ctx.cloud.runner.run([crane, "digest", image], check=False, env={"DOCKER_CONFIG": str(config_dir)}).returncode != 0]
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


# ---- add-on manifests ----------------------------------------------------------------------------------------------
_KNATIVE_IMAGE = re.compile(r"gcr\.io/knative-releases/knative\.dev/(serving|net-istio)/cmd/([a-z-]+)@sha256:[a-f0-9]{64}")


def redirect_knative(images: dict, manifest: str) -> str:
    """Replace the images of an upstream Knative manifest with the references pinned by the chart."""
    def replace(match: re.Match) -> str:
        key = f"{match.group(1)}-{match.group(2)}"
        if key not in images:
            raise InstallerError(f"The Knative manifest uses an image the chart does not pin: {match.group(0)[:80]}",
                                 f"Add `{key}` under installer.addons.knative in the platform chart.")
        return images[key]

    return _KNATIVE_IMAGE.sub(replace, manifest)


def redirect_istio(images: dict, manifest: str) -> str:
    """Fill the image tokens of the pre-rendered Istio manifest (assets/istio, see tools/render_istio.py) with the pinned images."""
    pilot_host, pilot_repo, pilot_tag = split_ref(images["pilot"])
    proxy_host, proxy_repo, proxy_tag = split_ref(images["proxy"])
    hub, pilot_name = pilot_repo.rsplit("/", 1)
    proxy_hub, proxy_name = proxy_repo.rsplit("/", 1)
    if (pilot_host, hub, pilot_tag) != (proxy_host, proxy_hub, proxy_tag):
        raise InstallerError("The Istio pilot and proxy images must share registry, repository prefix and tag.")
    out = (manifest.replace("registry.invalid/dial-hub", f"{pilot_host}/{hub}").replace("dial-image-tag", pilot_tag)
           .replace("dial-pilot-image", pilot_name).replace("dial-proxy-image", proxy_name))
    left = [token for token in ("registry.invalid", "dial-image-tag", "dial-pilot-image", "dial-proxy-image") if token in out]
    if left:
        raise InstallerError(f"The Istio manifest still contains unresolved tokens: {', '.join(left)}")
    return out


def outside_registry(ctx: Context, chart_values: dict) -> list[str]:
    """Workload images that would NOT come from the registry the cluster is configured to use (a chart that
    still names another registry directly, or a sub-chart default)."""
    host = placeholders.image_registry(ctx) if not ctx.cloud.runner.dry_run else source_host(ctx)
    document = _filled(ctx, chart_values, host)
    return sorted(i for i in _render_images(ctx, document) if not i.startswith(host + "/"))


# ---- copying -----------------------------------------------------------------------------------------------------
def _docker_config(ctx: Context, hosts: list[str]) -> Path:
    """A private docker config with a fresh ECR token per registry (never passed on a command line)."""
    auths = {}
    for host in hosts:
        region = _region_of(host, ctx.region)
        argv = ["aws", "ecr", "get-login-password", "--region", region]
        if ctx.cloud.profile:
            argv += ["--profile", ctx.cloud.profile]
        token = ctx.cloud.runner.run(argv).stdout.strip()
        auths[host] = {"auth": base64.b64encode(f"AWS:{token}".encode()).decode()}
    directory = Path(tempfile.mkdtemp(prefix="dial-registry-"))
    directory.chmod(0o700)
    (directory / "config.json").write_text(json.dumps({"auths": auths}))
    (directory / "config.json").chmod(0o600)
    return directory


def _ensure_repository(ctx: Context, name: str) -> None:
    exists = ctx.cloud.aws("ecr", "describe-repositories", "--repository-names", name, check=False)
    if exists.returncode == 0:
        return
    tags = [f"Key=dial-install:installation,Value={ctx.name}", *[f"Key={k},Value={v}" for k, v in ctx.cfg["tags"].items()]]
    ctx.cloud.aws("ecr", "create-repository", "--repository-name", name,
                  "--image-scanning-configuration", "scanOnPush=true", "--tags", *tags, write=True)


def crane_path(ctx: Context) -> str:
    path = tools.which("crane")
    if path:
        return path
    if ctx.cloud.runner.dry_run:
        return "crane"
    return str(tools.install_crane(ctx.release["tools"]["crane_version"]))


COPY_WORKERS = 6        # images copied at the same time; the network, not the CPU, is the limit


def copy_images(ctx: Context, images: list[str], say: Callable[[str], None]) -> tuple[int, int]:
    """Copy `images` to the customer's ECR, several at a time. Returns (copied, skipped)."""
    source, target = source_host(ctx), target_host(ctx)
    crane = crane_path(ctx)
    config_dir = _docker_config(ctx, [source, target])
    env = {"DOCKER_CONFIG": str(config_dir)}
    runner = ctx.cloud.runner
    total = len(images)

    failures: list[str] = []
    allowed = int(ctx.cache.get("allow_missing_images", 0))

    def one(index: int, image: str) -> bool | None:
        """True when the image was copied, False when it was already there, None when it failed (and may be skipped)."""
        path, tag = _split(image, source)
        destination = f"{target}/{path}:{tag}"
        wanted = runner.run([crane, "digest", image], check=False, env=env).stdout.strip()
        present = runner.run([crane, "digest", destination], check=False, env=env).stdout.strip()
        if wanted and wanted == present:
            say(f"  [{index}/{total}] {path}:{tag} already in your registry")
            return False
        say(f"  [{index}/{total}] copying {path}:{tag}")
        _ensure_repository(ctx, path)
        try:
            runner.run([crane, "copy", image, destination], write=True, env=env)
        except CommandError as error:
            if allowed:
                failures.append(f"{path}:{tag}")
                say(f"  [{index}/{total}] FAILED {path}:{tag} (allowed to continue)")
                return None
            raise InstallerError(
                f"Could not copy {image}\n{error.stderr[-600:]}",
                "Check that the AWS Marketplace subscription is active for this account and that your AWS user may "
                "push to ECR (ecr:CreateRepository, ecr:PutImage, ecr:InitiateLayerUpload, ecr:UploadLayerPart, "
                "ecr:CompleteLayerUpload, ecr:BatchCheckLayerAvailability).",
            ) from error
        say(f"  [{index}/{total}] done {path}:{tag}")
        return True

    try:
        with ThreadPoolExecutor(max_workers=1 if runner.dry_run else COPY_WORKERS) as pool:
            results = list(pool.map(lambda pair: one(*pair), enumerate(images, 1)))
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)
    if len(failures) > allowed:
        raise InstallerError(f"{len(failures)} images could not be copied (allowed: {allowed}):\n  " + "\n  ".join(failures),
                             "Run the command again; images already copied are skipped.")
    if failures:
        say("  WARNING: these images are missing and their components will not start: " + ", ".join(failures))
    copied = sum(1 for r in results if r is True)
    return copied, sum(1 for r in results if r is False)


def copy_pairs(ctx: Context, pairs: list[tuple[str, str]], say: Callable[[str], None]) -> tuple[int, int]:
    """Copy images that are not part of the product registry: `pairs` are (source reference, `path:tag` in the
    account's ECR). Used for add-ons with their own upstream, such as Cilium. Returns (copied, skipped)."""
    target = target_host(ctx)
    crane = crane_path(ctx)
    config_dir = _docker_config(ctx, [target])           # the sources are public; only the target needs a login
    env = {"DOCKER_CONFIG": str(config_dir)}
    runner = ctx.cloud.runner
    copied = skipped = 0
    try:
        for source, destination in pairs:
            path, _, _ = destination.rpartition(":")
            wanted = runner.run([crane, "digest", source], check=False, env=env).stdout.strip()
            present = runner.run([crane, "digest", f"{target}/{destination}"], check=False, env=env).stdout.strip()
            if wanted and wanted == present:
                skipped += 1
                continue
            say(f"    copying {source}")
            _ensure_repository(ctx, path)
            try:
                runner.run([crane, "copy", source, f"{target}/{destination}"], write=True, env=env)
            except CommandError as error:
                raise InstallerError(f"Could not copy {source}\n{error.stderr[-600:]}",
                                     "Check that this machine can reach the source registry and may push to your ECR.") from error
            copied += 1
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)
    return copied, skipped

