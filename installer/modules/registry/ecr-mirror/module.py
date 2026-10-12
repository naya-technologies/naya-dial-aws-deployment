"""registry/ecr-mirror - the cluster pulls product images from the customer's own ECR."""

from dial_install import deploy, mirror, tools
from dial_install.findings import fail, ok, warn


def check(ctx):
    findings = []
    source = mirror.source_host(ctx)
    if not (tools.which("crane") or ctx.cloud.runner.dry_run):
        findings.append(warn("registry", "crane (image copy tool) is not installed",
                             "It will be downloaded automatically (SHA-256 verified), or run `./dial-install install-tools`."))
    login = ctx.cloud.runner.run(["aws", "ecr", "get-login-password", "--region", mirror._region_of(source, ctx.region),
                                  *(["--profile", ctx.cloud.profile] if ctx.cloud.profile else [])], check=False)
    if login.returncode != 0:
        reason = (login.stderr or "").strip().splitlines()[-1:] or ["no details"]
        return findings + [fail("registry", f"cannot get a login for the Marketplace registry {source}: {reason[0][:200]}",
                                "Your AWS user needs ecr:GetAuthorizationToken, and the account needs an active Marketplace subscription.")]
    probe = ctx.cloud.aws("ecr", "describe-repositories", "--max-items", "1", check=False)
    if probe.returncode != 0:
        return findings + [fail("registry", "cannot list ECR repositories in your account",
                                "Your AWS user needs ECR permissions (ecr:DescribeRepositories, ecr:CreateRepository, "
                                "ecr:PutImage and the layer upload actions).")]
    try:
        images = mirror.discover(ctx, deploy.chart_defaults(ctx))
    except Exception as error:  # noqa: BLE001 - reported as a finding, not a crash
        return findings + [warn("registry", f"could not list the images of this release ({error})")]
    absent = mirror.missing(ctx, images)
    if absent:
        findings.append(fail("registry", f"{len(absent)} of {len(images)} images are not available in the Marketplace registry",
                             "Missing or not entitled:\n" + "\n".join(f"  {image}" for image in absent) +
                             "\nCheck the Marketplace subscription; if this is your own release, fix the tags in the release manifest."))
    else:
        findings.append(ok("registry", f"{len(images)} images are available and will be copied to {mirror.target_host(ctx)} "
                                       "(images already there are skipped)"))
    return findings


def before_platform(ctx, say):
    chart_values = deploy.chart_defaults(ctx)
    images = mirror.discover(ctx, chart_values)
    say(f"  {len(images)} images to place in {mirror.target_host(ctx)}")
    copied, skipped = mirror.copy_images(ctx, images, say)
    say(f"  images: {copied} copied, {skipped} already present")
    left = mirror.outside_registry(ctx, chart_values) if not ctx.cloud.runner.dry_run else []
    if left:
        say("  WARNING: these images will not be pulled from your registry:\n    " + "\n    ".join(left))
