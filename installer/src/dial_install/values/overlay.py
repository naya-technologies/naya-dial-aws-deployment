"""Settings that cannot be a text placeholder (booleans)."""

from __future__ import annotations

from ..context import Context


def overlay(ctx: Context) -> dict:
    features = ctx.cfg["features"]
    return {
        "superset": {"enabled": features["analytics"]},
        "deployment-manager": {"enabled": features["deployment_manager"]},
    }
