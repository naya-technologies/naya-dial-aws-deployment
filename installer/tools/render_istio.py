#!/usr/bin/env python3
"""Vendor tool: pre-render the Istio manifest that dial-install applies, so no istioctl is needed at install time.

    tools/render_istio.py /path/to/istioctl 1.29.1

Writes assets/istio/istio-<version>.yaml from assets/istio/operator.yaml. Image-related values are rendered as fixed
tokens (see mirror.redirect_istio) that dial-install replaces with the images pinned by the platform chart.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {
    "hub": "registry.invalid/dial-hub",
    "tag": "dial-image-tag",
    "values.pilot.image": "dial-pilot-image",
    "values.global.proxy.image": "dial-proxy-image",
    "values.global.proxy_init.image": "dial-proxy-image",
}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    istioctl, version = sys.argv[1], sys.argv[2]
    reported = subprocess.run([istioctl, "version", "--remote=false"], capture_output=True, text=True).stdout.strip()
    if version not in reported:
        raise SystemExit(f"istioctl reports {reported!r}, expected {version}")
    flags = [item for key, value in TOKENS.items() for item in ("--set", f"{key}={value}")]
    result = subprocess.run([istioctl, "manifest", "generate", "-f", str(ROOT / "assets/istio/operator.yaml"), *flags],
                            capture_output=True, text=True, check=True)
    target = ROOT / "assets" / "istio" / f"istio-{version}.yaml"
    target.write_text(result.stdout)
    print(f"wrote {target} ({len(result.stdout)} bytes)")


if __name__ == "__main__":
    main()
