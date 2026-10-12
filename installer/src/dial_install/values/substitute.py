"""Replace PLACEHOLDER_<NAME> tokens in a chart's default values.

The chart owns the structure of its values; the installer only supplies the environment-specific
strings. `overrides()` returns the smallest values document that carries every substituted string,
so everything else keeps coming from the chart version being installed.
"""

from __future__ import annotations

import re
from typing import Any

TOKEN = re.compile(r"PLACEHOLDER_([A-Z0-9_]+)")


class UnresolvedPlaceholders(Exception):
    def __init__(self, names: list[str]) -> None:
        self.names = sorted(names)
        super().__init__("no value for: " + ", ".join(self.names))


def find(node: Any) -> set[str]:
    """Names (without the PLACEHOLDER_ prefix) used anywhere in `node`."""
    if isinstance(node, dict):
        return set().union(*(find(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(find(v) for v in node)) if node else set()
    if isinstance(node, str):
        return set(TOKEN.findall(node))
    return set()


def _replace(text: str, mapping: dict[str, str]) -> str:
    return TOKEN.sub(lambda match: mapping[match.group(1)], text)


def _walk(node: Any, mapping: dict[str, str]) -> tuple[Any, bool]:
    """Return (value, changed). Dicts keep only changed children; a list is all-or-nothing (Helm replaces lists)."""
    if isinstance(node, str):
        return (_replace(node, mapping), True) if TOKEN.search(node) else (node, False)
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            new, changed = _walk(value, mapping)
            if changed:
                result[key] = new
        return result, bool(result)
    if isinstance(node, list):
        items = [_walk(item, mapping) for item in node]
        if any(changed for _, changed in items):
            return [_full(item, mapping) for item in node], True
        return node, False
    return node, False


def _full(node: Any, mapping: dict[str, str]) -> Any:
    """Substitute everywhere in `node`, keeping unchanged parts (used inside lists)."""
    if isinstance(node, str):
        return _replace(node, mapping)
    if isinstance(node, dict):
        return {key: _full(value, mapping) for key, value in node.items()}
    if isinstance(node, list):
        return [_full(item, mapping) for item in node]
    return node


def map_strings(node: Any, fn) -> dict:
    """The smallest values tree holding every string for which `fn` returns something different (lists: all or nothing)."""
    return _map(node, fn)[0]


def _map(node: Any, fn) -> tuple[Any, bool]:
    if isinstance(node, str):
        new = fn(node)
        return (new, True) if new != node else (node, False)
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            mapped, changed = _map(value, fn)
            if changed:
                result[key] = mapped
        return result, bool(result)
    if isinstance(node, list):
        mapped = [_map(item, fn) for item in node]
        if any(changed for _, changed in mapped):
            return [value for value, _ in mapped], True
        return node, False
    return node, False


def overrides(defaults: dict, mapping: dict[str, str]) -> dict:
    missing = find(defaults) - set(mapping)
    if missing:
        raise UnresolvedPlaceholders(sorted(missing))
    result, _ = _walk(defaults, mapping)
    return result
