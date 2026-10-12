"""Minimal JSON Schema validator (stdlib only).

The customer environment cannot be assumed to have `jsonschema`, so this module
implements the small subset used by schema/dial.schema.json:
type, enum, const, required, properties, additionalProperties(false), pattern,
items, minItems, maxItems, minimum, anyOf, allOf, if/then and local $ref.

It returns plain, customer-readable messages: "network.vpc_id: is required".
"""

from __future__ import annotations

import re
from typing import Any

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def validate(instance: Any, schema: dict) -> list[str]:
    """Return a list of human-readable problems (empty when valid)."""
    errors: list[str] = []
    _walk(instance, schema, schema, "", errors)
    return errors


def _is_type(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _matches(instance: Any, schema: dict, root: dict) -> bool:
    probe: list[str] = []
    _walk(instance, schema, root, "", probe)
    return not probe


def _label(path: str) -> str:
    return path or "(top level)"


def _walk(value: Any, schema: dict, root: dict, path: str, errors: list[str]) -> None:
    if "$ref" in schema:
        _walk(value, _resolve(schema["$ref"], root), root, path, errors)
        return

    if "type" in schema and not _is_type(value, schema["type"]):
        errors.append(f"{_label(path)}: must be of type {schema['type']}")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{_label(path)}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(str(item) for item in schema["enum"])
        errors.append(f"{_label(path)}: must be one of: {allowed} (got {value!r})")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{_label(path)}: {value!r} does not look valid")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{_label(path)}: must be at least {schema['minimum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{_label(path)}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{_label(path)}: allows at most {schema['maxItems']} item(s)")
        if "items" in schema:
            for index, item in enumerate(value):
                _walk(item, schema["items"], root, f"{path}[{index}]", errors)

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{_join(path, key)}: is required")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        if extra is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{_join(path, key)}: unknown setting")
        elif isinstance(extra, dict):
            for key, item in value.items():
                if key not in properties:
                    _walk(item, extra, root, _join(path, key), errors)
        for key, sub in properties.items():
            if key in value:
                _walk(value[key], sub, root, _join(path, key), errors)

    if "anyOf" in schema and not any(_matches(value, option, root) for option in schema["anyOf"]):
        errors.append(f"{_label(path)}: {_describe_any_of(schema['anyOf'])}")
    for option in schema.get("allOf", []):
        _apply_conditional(value, option, root, path, errors)


def _apply_conditional(value: Any, option: dict, root: dict, path: str, errors: list[str]) -> None:
    if "if" in option:
        if _matches(value, option["if"], root) and "then" in option:
            _walk(value, option["then"], root, path, errors)
    else:
        _walk(value, option, root, path, errors)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _describe_any_of(options: list[dict]) -> str:
    # Only used for domain: base OR all six hosts. Keep the message generic.
    needs = []
    for option in options:
        required = option.get("required", [])
        needs.append(" and ".join(required) if required else "a valid alternative")
    return "set " + " or ".join(needs) + " (with every host listed when `base` is not set)"
