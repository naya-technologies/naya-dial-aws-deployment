"""Settings that cannot be a text placeholder (booleans)."""

from __future__ import annotations

from ..context import Context


def overlay(ctx: Context) -> dict:
    features = ctx.cfg["features"]
    result = {
        "superset": {"enabled": features["analytics"]},
        "deployment-manager": {"enabled": features["deployment_manager"]},
    }
    if ctx.cache.get("license_helper_needed") is False:      # set by deploy.prepare; see deploy.license_helper_needed
        result["lic-ins"] = {"enabled": False}
    return result
