"""Point every image reference of the chart at the registry the cluster should pull from.

The chart's defaults name one registry (`installer.registry`, what the Marketplace validates). This module replaces it with
`registry.source` when that differs, or with the customer's private ECR when the images were copied there.
"""

from __future__ import annotations

from ..context import Context
from ..errors import InstallerError
from . import placeholders, substitute


def chart_default(chart_values: dict) -> tuple[str, str]:
    """(host, repository prefix) the chart's own references use."""
    try:
        host, _, prefix = chart_values["installer"]["registry"].partition("/")
    except KeyError:
        raise InstallerError("This release's chart does not declare its image registry (installer.registry).",
                             "Use a chart built for dial-install 2.x, or download a newer installer package.") from None
    return host, prefix


def wanted(ctx: Context) -> tuple[str, str]:
    """(host, prefix) the cluster must pull from."""
    return placeholders.image_registry(ctx), placeholders.source_prefix(ctx)


def redirect_to(chart_values: dict, host: str, prefix: str) -> dict:
    """Values overrides replacing the chart's registry host and repository prefix by (host, prefix)."""
    old_host, old_prefix = chart_default(chart_values)
    if (old_host, old_prefix) == (host, prefix):
        return {}

    def fix(text: str) -> str:
        if old_host in text:                                    # full references and the `registry` field
            return text.replace(f"{old_host}/{old_prefix}/", f"{host}/{prefix}/").replace(old_host, host)
        if text.startswith(old_prefix + "/"):                   # a `repository` field next to a separate `registry`
            return prefix + text[len(old_prefix):]
        return text

    return substitute.map_strings(chart_values, fix)


def redirect(ctx: Context, chart_values: dict) -> dict:
    return redirect_to(chart_values, *wanted(ctx))
