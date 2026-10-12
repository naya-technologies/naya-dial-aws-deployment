#!/usr/bin/env python3
"""Build the customer package: dist/dial-install-<version>.tar.gz (+ .sha256)."""

import argparse
import hashlib
import re
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HELM = ROOT.parent / "charts"
INCLUDE = ["dial-install", "installer.yaml", "releases", "README.md", "src", "modules", "assets", "schema", "examples", "docs", "vendor"]
SKIP = ("__pycache__", ".pyc", ".DS_Store")


def add(archive, path: Path, arcname: str, mode: int | None = None) -> None:
    info = archive.gettarinfo(str(path), arcname=arcname)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if mode is not None:
        info.mode = mode
    with path.open("rb") as stream:
        archive.addfile(info, stream)


def download_tools(directory: Path, arch: str) -> None:
    """kubectl, helm and crane for `arch`, checksum verified, so the package needs no internet access."""
    sys.path.insert(0, str(ROOT / "src"))
    from dial_install import tools                       # noqa: E402 (needs the path above)
    from dial_install.releases import load_defaults      # noqa: E402

    wanted = load_defaults()["tools"]
    tools.machine_arch = lambda override=None: arch      # build for the requested architecture
    tools.install_kubectl(wanted["kubectl_version"], directory)
    tools.install_helm(wanted["helm_version"], directory)
    tools.install_crane(wanted["crane_version"], directory)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the customer package.")
    parser.add_argument("--offline", action="store_true", help="also bundle kubectl, helm and crane (larger package, no downloads at install time)")
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64", help="CPU architecture of the bundled tools")
    args = parser.parse_args()

    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "dial_install" / "__init__.py").read_text()).group(1)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    suffix = f"-offline-{args.arch}" if args.offline else ""
    target = dist / f"dial-install-{version}{suffix}.tar.gz"
    prefix = f"dial-install-{version}"
    with tempfile.TemporaryDirectory() as scratch, tarfile.open(target, "w:gz") as archive:
        # Cluster prerequisite chart ships inside the package; the platform chart comes from the registry.
        base = HELM / "dial-cluster-base"
        for path in sorted(base.rglob("*")):
            if path.is_file() and not any(part in str(path) for part in SKIP):
                add(archive, path, f"{prefix}/charts/dial-cluster-base/{path.relative_to(base)}")
        for name in INCLUDE:
            for path in sorted((ROOT / name).rglob("*") if (ROOT / name).is_dir() else [ROOT / name]):
                if any(part in str(path) for part in SKIP) or path.is_dir():
                    continue
                add(archive, path, f"{prefix}/{path.relative_to(ROOT)}")
        if args.offline:
            bin_dir = Path(scratch)
            download_tools(bin_dir, args.arch)
            for path in sorted(bin_dir.iterdir()):
                add(archive, path, f"{prefix}/bin/{path.name}", mode=0o755)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (dist / f"{target.name}.sha256").write_text(f"{digest}  {target.name}\n")
    print(f"{target}\nsha256 {digest}")


if __name__ == "__main__":
    main()
