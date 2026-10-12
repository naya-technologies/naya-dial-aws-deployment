"""Helm values = the chart's own placeholders, filled in.

    chart defaults (helm show values)  ->  replace PLACEHOLDER_<NAME>  ->  + overlay  ->  values file

The chart is the single source of truth for structure (images, probes, node selectors, ...). The installer
supplies only the environment-specific strings, so a new chart release needs no installer change unless it
introduces a placeholder the installer cannot fill; that is reported before anything is changed.
"""

from __future__ import annotations

import copy

from ..context import Context
from ..errors import InstallerError
from . import overlay as overlay_mod, placeholders, registry, substitute


class ConflictError(Exception):
    pass


def merge(target: dict, source: dict, path: str = "") -> dict:
    for key, value in source.items():
        here = f"{path}.{key}" if path else key
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            merge(target[key], value, here)
        elif key in target and target[key] != value:
            raise ConflictError(f"the placeholders and the overlay both set {here}")
        else:
            target[key] = value
    return target


def layer_over(base: dict, top: dict) -> dict:
    """Deep-merge `top` over `base`; where both set a key, `top` wins (a later, deliberate layer)."""
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            layer_over(base[key], value)
        else:
            base[key] = value
    return base


def _document(ctx: Context, chart_values: dict) -> dict:
    """Layers: 1. placeholders filled   2. feature switches   3. module contributions (e.g. image registry),
    each computed on top of the values as they are after the previous layer."""
    wanted = substitute.find(chart_values)
    mapping = placeholders.resolve(ctx, wanted)
    try:
        document = substitute.overrides(chart_values, mapping)
    except substitute.UnresolvedPlaceholders as error:
        raise InstallerError(
            f"This release's chart needs values this installer cannot provide: {', '.join(error.names)}",
            "Download the latest installer package (see `./dial-install releases`) and try again.",
        ) from error
    merge(document, overlay_mod.overlay(ctx))
    if ctx.cfg["database"]["auth"] == "iam":                     # chart-owned value set for the IAM database login
        profile = layer_over(copy.deepcopy(chart_values), document)["installer"]["profiles"]["rdsIam"]["values"]
        layer_over(document, profile)
    layer_over(document, registry.redirect(ctx, layer_over(copy.deepcopy(chart_values), document)))   # image registry
    effective_so_far = layer_over(copy.deepcopy(chart_values), document)   # what the chart would see so far
    contributions: dict = {}
    for module in ctx.modules:
        merge(contributions, module.value_overrides(ctx, effective_so_far))   # two modules must not disagree
    return layer_over(document, contributions)


def build(ctx: Context, chart_values: dict) -> dict:
    """The values document to pass to `helm upgrade --install`."""
    document = _document(ctx, chart_values)
    document.pop("installer", None)          # read by dial-install only; no Helm template uses it
    return document


def effective(ctx: Context, chart_values: dict) -> dict:
    """The chart's values as they will be installed, including the `installer` section (add-on images, redirected)."""
    return layer_over(copy.deepcopy(chart_values), _document(ctx, chart_values))
