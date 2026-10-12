"""dial-install: configuration-driven installer and upgrader for DIAL on AWS."""

import sys
from pathlib import Path

# The package ships its own pure-Python PyYAML so customers need no `pip install`.
_VENDOR = Path(__file__).resolve().parents[2] / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# Root of the installer package (the directory that holds installer.yaml, releases, modules/, assets/ ...).
ROOT = Path(__file__).resolve().parents[2]

__version__ = "2.0.0"
