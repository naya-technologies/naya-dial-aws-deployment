#!/usr/bin/env python3
"""Vendor tool: `helm lint` the platform chart the way it is installed (placeholders filled in).

The chart's own values.yaml holds PLACEHOLDER_* tokens, which sub-chart schemas reject as host names, so linting the raw
chart is meaningless. This fills every placeholder with a harmless value and lints the result.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import yaml                                            # noqa: E402 (vendored; path set by dial_install)
from dial_install.values import substitute             # noqa: E402,F401

CHART = ROOT.parent / "charts" / "dial-platform"


def main() -> int:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    mapping = {name: "dummy" for name in substitute.find(values)}
    mapping.update({"IMAGE_REGISTRY": "123456789012.dkr.ecr.us-east-1.amazonaws.com", "IMAGE_PREFIX": "products"})
    document = substitute.overrides(values, mapping)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(document, handle)
    try:
        return subprocess.run(["helm", "lint", str(CHART), "-f", handle.name]).returncode
    finally:
        Path(handle.name).unlink()


if __name__ == "__main__":
    raise SystemExit(main())
