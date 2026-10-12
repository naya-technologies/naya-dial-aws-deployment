"""Module discovery, selection and ordering.

A module is a directory  modules/<slot>/<name>/  containing:

    module.yaml   slot, providers, requires, provides, description   (required)
    stack.yaml    CloudFormation template                            (optional)
    module.py     hooks: check(ctx), parameters(ctx), finalize_outputs(ctx, raw)

Hooks that are not defined fall back to harmless defaults, so a minimal module
is just module.yaml + stack.yaml.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

from . import ROOT
from .config import load_yaml
from .context import Context
from .errors import InstallerError
from .findings import Finding

MODULES_DIR = ROOT / "modules"

# Where the customer's choice for a slot lives in dial.yaml. Slots not listed here
# have exactly one implementation and use the provider name "default".
PROVIDER_KEY = {
    "network": ("network", "provider"),
    "database": ("database", "provider"),
    "identity": ("identity", "provider"),
    "registry": ("registry", "provider"),
    "cni": ("kubernetes", "cni"),
}

# Slots whose provider is derived from another setting rather than named directly.
DERIVED_PROVIDER = {
    "endpoints": lambda cfg: "create" if cfg["network"]["create_vpc_endpoints"] else "none",
}

# Readable default order; real order is derived from `requires` (see order()).
PREFERRED_ORDER = ["network", "endpoints", "storage", "compute", "cni", "links", "cache", "database", "iam", "identity", "registry"]


class Module:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        meta = load_yaml(directory / "module.yaml")
        self.slot: str = meta["slot"]
        self.providers: list[str] = meta.get("providers", ["default"])
        self.requires: list[str] = meta.get("requires", [])
        self.provides: list[str] = meta.get("provides", [])
        self.description: str = meta.get("description", "")
        self._hooks = self._load_hooks(directory / "module.py")

    @property
    def stack_file(self) -> Path | None:
        path = self.directory / "stack.yaml"
        return path if path.exists() else None

    @property
    def label(self) -> str:
        return f"{self.slot}/{self.directory.name}"

    @staticmethod
    def _load_hooks(path: Path) -> Any:
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"dial_module_{path.parent.name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _hook(self, name: str):
        return getattr(self._hooks, name, None) if self._hooks else None

    def check(self, ctx: Context) -> list[Finding]:
        hook = self._hook("check")
        return list(hook(ctx)) if hook else []

    def parameters(self, ctx: Context) -> dict[str, str]:
        hook = self._hook("parameters")
        return {k: str(v) for k, v in (hook(ctx) if hook else {}).items()}

    def deploy_phases(self, ctx: Context) -> list[dict]:
        """How this module's stack is deployed: a list of phases, each {"parameters": {...}, "after": callable}.
        Most modules have one phase; the compute module with Cilium has two (control plane, then nodes)."""
        hook = self._hook("deploy_phases")
        return hook(ctx) if hook else [{}]

    def has_hook(self, name: str) -> bool:
        return self._hook(name) is not None

    def before_platform(self, ctx: Context, say: Callable[[str], None]) -> None:
        """Runs after the infrastructure exists and before Helm installs or upgrades the platform."""
        hook = self._hook("before_platform")
        if hook:
            hook(ctx, say)

    def before_helm(self, ctx: Context, say: Callable[[str], None]) -> None:
        """Runs once the cluster is prepared (namespace, add-ons) and just before the platform is installed or upgraded."""
        hook = self._hook("before_helm")
        if hook:
            hook(ctx, say)

    def value_overrides(self, ctx: Context, chart_values: dict) -> dict:
        """Extra Helm values this module contributes (merged with the placeholder values)."""
        hook = self._hook("value_overrides")
        return hook(ctx, chart_values) if hook else {}

    def finalize_outputs(self, ctx: Context, raw: dict[str, str]) -> dict[str, str]:
        hook = self._hook("finalize_outputs")
        return hook(ctx, raw) if hook else raw


def discover() -> list[Module]:
    return [Module(meta.parent) for meta in sorted(MODULES_DIR.glob("*/*/module.yaml"))]


def provider_for(cfg: dict, slot: str) -> str:
    if slot in DERIVED_PROVIDER:
        return DERIVED_PROVIDER[slot](cfg)
    path = PROVIDER_KEY.get(slot)
    if not path:
        return "default"
    section, key = path
    return cfg[section][key]


def select(cfg: dict, available: list[Module] | None = None) -> list[Module]:
    """Pick one module per slot according to the config, in execution order."""
    available = available if available is not None else discover()
    by_slot: dict[str, list[Module]] = {}
    for module in available:
        by_slot.setdefault(module.slot, []).append(module)

    chosen: dict[str, Module] = {}
    for slot, candidates in by_slot.items():
        provider = provider_for(cfg, slot)
        match = [m for m in candidates if provider in m.providers]
        if not match:
            known = sorted({p for m in candidates for p in m.providers})
            raise InstallerError(
                f"No module provides {slot} = {provider!r}.",
                "Available: " + ", ".join(known),
            )
        chosen[slot] = match[0]
    return order(chosen)


def order(chosen: dict[str, Module]) -> list[Module]:
    remaining = sorted(chosen, key=lambda s: PREFERRED_ORDER.index(s) if s in PREFERRED_ORDER else 99)
    done: list[str] = []
    result: list[Module] = []
    while remaining:
        for slot in remaining:
            module = chosen[slot]
            missing = [r for r in module.requires if r not in done]
            unknown = [r for r in missing if r not in chosen]
            if unknown:
                raise InstallerError(f"Module {module.label} requires missing slot(s): {', '.join(unknown)}")
            if not missing:
                done.append(slot)
                result.append(module)
                remaining.remove(slot)
                break
        else:
            raise InstallerError("Circular module dependency among: " + ", ".join(remaining))
    return result
