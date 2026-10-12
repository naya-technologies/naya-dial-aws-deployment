"""Release selection: one release id -> the complete set of component versions.

Sources, in order:
  1. releases/<id>.yaml shipped inside this package (works offline);
  2. `release.yaml` at the root of the platform chart tagged <id> in the Marketplace registry
     (so a release published after this package was built can still be installed).

The result is merged over installer.yaml and handed to the rest of the installer as `ctx.release`.
"""

from __future__ import annotations

import copy
import re
import tempfile
from pathlib import Path
from typing import Callable

from . import ROOT, __version__
from .cloud import Cloud
from .config import load_yaml
from .errors import CommandError, InstallerError

RELEASES_DIR = ROOT / "releases"
MANIFEST_API = "dial-release/v1"
_REQUIRED = ("release", "chart", "kubernetes", "database", "addons")


def load_defaults() -> dict:
    return load_yaml(ROOT / "installer.yaml")


def bundled_index() -> dict:
    return load_yaml(RELEASES_DIR / "index.yaml")


def bundled_ids() -> list[str]:
    return list(bundled_index()["releases"])


def merge(base: dict, extra: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def validate(manifest: dict, origin: str) -> dict:
    if manifest.get("apiVersion") != MANIFEST_API:
        raise InstallerError(f"{origin}: not a DIAL release manifest (apiVersion must be {MANIFEST_API}).")
    missing = [key for key in _REQUIRED if key not in manifest]
    if missing:
        raise InstallerError(f"{origin}: release manifest is missing: {', '.join(missing)}")
    needed = manifest.get("min_installer_version", "0")
    if _version_tuple(__version__) < _version_tuple(needed):
        raise InstallerError(
            f"Release {manifest['release']} needs dial-install {needed} or newer (this is {__version__}).",
            "Download the latest installer package and run the command again.",
        )
    return manifest


def load_bundled(release_id: str) -> dict | None:
    path = RELEASES_DIR / f"{release_id}.yaml"
    return validate(load_yaml(path), str(path.name)) if path.is_file() else None


def chart_repository(defaults: dict, source: str | None) -> tuple[str, str]:
    """(registry source, chart reference): <oci://host/prefix>/<chart name>."""
    source = (source or defaults["registry"]["source"]).rstrip("/")
    return source, f"{source}/{defaults['chart']['name']}"


def fetch_from_chart(cloud: Cloud, repository: str, release_id: str, login: Callable[[], None]) -> dict:
    """Pull the chart tagged `release_id` and read the release.yaml it carries."""
    try:
        login()
        with tempfile.TemporaryDirectory(prefix="dial-release-") as directory:
            cloud.runner.run(["helm", "pull", repository, "--version", release_id, "--untar", "--untardir", directory])
            found = next(Path(directory).glob("*/release.yaml"), None)
            if not found:
                raise InstallerError(
                    f"Chart {release_id} does not contain a release manifest (release.yaml).",
                    "Use a release that ships inside this installer (see `./dial-install releases`), "
                    "or ask the vendor for an installer package that includes it.",
                )
            manifest = load_yaml(found)
    except CommandError as error:
        raise InstallerError(
            f"Release {release_id} is unknown: it is not in this installer and the chart could not be pulled.",
            f"{error.stderr}\nCheck the release id, your AWS Marketplace subscription, and run `./dial-install check`.",
        ) from error
    manifest = validate(manifest, f"chart {release_id}")
    if manifest["release"] != release_id:
        raise InstallerError(f"Chart {release_id} carries a manifest for release {manifest['release']}.")
    return manifest


def offline_latest(source: str | None = None) -> dict:
    """Defaults + the newest bundled release, with no network access (used by tools and tests)."""
    defaults = load_defaults()
    merged = merge(defaults, load_bundled(bundled_index()["latest"]))
    merged["registry"]["source"], merged["chart"]["repository"] = chart_repository(defaults, source)
    merged["release_source"] = "package"
    return merged


def from_local_chart(chart_dir: Path, source: str | None = None) -> dict:
    """Defaults + the release.yaml of a chart directory that is not published yet."""
    path = Path(chart_dir) / "release.yaml"
    if not path.is_file():
        raise InstallerError(f"{chart_dir} has no release.yaml.", "Point --chart at the chart directory (it must contain release.yaml).")
    defaults = load_defaults()
    merged = merge(defaults, validate(load_yaml(path), str(path)))
    merged["registry"]["source"], merged["chart"]["repository"] = chart_repository(defaults, source)
    merged["release_source"] = "local chart"
    return merged


def resolve(requested: str | None, cloud: Cloud, login: Callable[[], None], source: str | None = None) -> dict:
    """Return installer defaults merged with the manifest of the requested release."""
    defaults = load_defaults()
    source, repository = chart_repository(defaults, source)
    release_id = requested or "latest"
    if release_id == "latest":
        release_id = bundled_index()["latest"]
    manifest = load_bundled(release_id)
    if manifest is None:
        manifest = fetch_from_chart(cloud, repository, release_id, login)
        origin = "registry chart"
    else:
        origin = "package"
    merged = merge(defaults, manifest)
    merged["registry"]["source"], merged["chart"]["repository"] = source, repository
    merged["release_source"] = origin
    return merged
