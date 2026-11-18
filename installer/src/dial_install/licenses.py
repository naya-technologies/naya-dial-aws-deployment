"""Report account-wide consumed AWS License Manager entitlement units (read only).

Core owns tier activation and provisional renewal. Historical PERPETUAL consumption may also be present and cannot be
returned through the API. The installer only reports usage; it never checks units out or returns them.
"""

from __future__ import annotations

from .cloud import Cloud


def consumed(cloud: Cloud) -> list[tuple[str, str, str]]:
    """[(entitlement, consumed, maximum)] for every entitlement with something consumed, e.g. ("small", "14", "100")."""
    data = cloud.aws_json("license-manager", "list-received-licenses", check=False) or {}
    rows: list[tuple[str, str, str]] = []
    for lic in data.get("Licenses", []):
        usage = cloud.aws_json("license-manager", "get-license-usage", "--license-arn", lic["LicenseArn"], check=False) or {}
        for entry in usage.get("LicenseUsage", {}).get("EntitlementUsages", []):
            if entry.get("ConsumedValue") not in (None, "0") and entry.get("Name") != "AWS::Marketplace::Usage":
                rows.append((entry["Name"], str(entry["ConsumedValue"]), str(entry.get("MaxCount", "?"))))
    return rows


def describe(rows: list[tuple[str, str, str]]) -> str:
    return ", ".join(f"{name} {used}/{maximum}" for name, used, maximum in rows)
