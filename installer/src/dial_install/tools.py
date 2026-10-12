"""Client tools: detection and SHA-256 verified download of kubectl, helm and crane.

An offline installer package carries them in its own bin/ directory and nothing is downloaded. Otherwise they are
installed under ~/.dial-install/bin (no root needed, works in CloudShell). The launcher puts both on PATH.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import stat
import tarfile
import urllib.request
from pathlib import Path

from . import ROOT
from .errors import InstallerError

BIN_DIR = Path.home() / ".dial-install" / "bin"
PACKAGED_BIN = ROOT / "bin"


def machine_arch(override: str | None = None) -> str:
    if override:
        return override
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise InstallerError(f"Unsupported CPU architecture: {machine}")


def _fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 (https URLs only)
            return response.read()
    except OSError as error:
        raise InstallerError(f"Download failed: {url}", f"{error}. Check internet access and try again.") from error


def download_text(url: str) -> str:
    return _fetch(url).decode("utf-8")


def _verified(url: str, checksum_url: str) -> bytes:
    data = _fetch(url)
    expected = _fetch(checksum_url).decode().split()[0].strip().lower()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise InstallerError(f"Checksum mismatch for {url}", "The download was corrupted or tampered with. Nothing was installed.")
    return data


def _install_binary(name: str, content: bytes, directory: Path | None = None) -> Path:
    directory = directory or BIN_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(content)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _from_tarball(data: bytes, member_suffix: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and (member.name == member_suffix or member.name.endswith("/" + member_suffix)):
                extracted = archive.extractfile(member)
                assert extracted is not None
                return extracted.read()
    raise InstallerError(f"{member_suffix} not found in the downloaded archive")


def install_kubectl(version: str, directory: Path | None = None) -> Path:
    arch = machine_arch()
    base = f"https://dl.k8s.io/release/{version}/bin/linux/{arch}/kubectl"
    return _install_binary("kubectl", _verified(base, base + ".sha256"), directory)


def install_helm(version: str, directory: Path | None = None) -> Path:
    arch = machine_arch()
    url = f"https://get.helm.sh/helm-{version}-linux-{arch}.tar.gz"
    data = _verified(url, url + ".sha256sum")
    return _install_binary("helm", _from_tarball(data, f"linux-{arch}/helm"), directory)


def install_crane(version: str, directory: Path | None = None) -> Path:
    """crane copies images registry-to-registry (all architectures, same digest) without storing layers locally."""
    arch = {"amd64": "x86_64", "arm64": "arm64"}[machine_arch()]
    base = f"https://github.com/google/go-containerregistry/releases/download/{version}"
    archive = f"go-containerregistry_Linux_{arch}.tar.gz"
    data = _fetch(f"{base}/{archive}")
    sums = {line.split()[1]: line.split()[0] for line in _fetch(f"{base}/checksums.txt").decode().splitlines() if line.strip()}
    if hashlib.sha256(data).hexdigest() != sums.get(archive, "").lower():
        raise InstallerError(f"Checksum mismatch for {archive}", "The download was corrupted or tampered with. Nothing was installed.")
    return _install_binary("crane", _from_tarball(data, "crane"), directory)


def which(tool: str) -> str | None:
    for directory in [str(PACKAGED_BIN), str(BIN_DIR), *os.environ.get("PATH", "").split(os.pathsep)]:
        candidate = Path(directory) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
