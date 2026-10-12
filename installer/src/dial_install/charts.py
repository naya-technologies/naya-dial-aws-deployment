"""Charts that ship with the installer (cluster prerequisites). The platform chart comes from Marketplace."""

from __future__ import annotations

from pathlib import Path

import yaml

from . import ROOT
from .errors import InstallerError
from .values import substitute


def chart_dir(name: str) -> Path:
    """Packaged location first (charts/<name>), then the source tree next to this package (charts/<name>)."""
    for candidate in (ROOT / "charts" / name, ROOT.parent / "charts" / name):
        if (candidate / "Chart.yaml").is_file():
            return candidate
    raise InstallerError(f"Chart {name} was not found in this installer package.", "Download the installer package again.")


def filled_values(directory: Path, mapping: dict[str, str]) -> dict:
    """The chart's own values.yaml with its PLACEHOLDER tokens replaced (only the changed entries)."""
    defaults = yaml.safe_load((directory / "values.yaml").read_text()) or {}
    try:
        return substitute.overrides(defaults, mapping)
    except substitute.UnresolvedPlaceholders as error:
        raise InstallerError(f"Chart {directory.name} needs values the installer did not provide: {', '.join(error.names)}") from error
