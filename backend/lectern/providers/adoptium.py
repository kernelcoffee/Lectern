"""Adoptium (Temurin) — per-Minecraft-version Java runtime provisioning.

The download endpoint redirects to a JRE archive:
  https://api.adoptium.net/v3/binary/latest/{feature}/ga/{os}/{arch}/jre/hotspot/normal/eclipse
"""

from __future__ import annotations

import asyncio
import platform
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .base import download_file

API = "https://api.adoptium.net/v3/binary/latest"


def java_major_for_mc(mc_version: str) -> int:
    """Map a Minecraft *release* version to the required Java major version.

    Boundaries: ≤1.16 → 8, 1.17 → 16, 1.18–1.20.4 → 17, ≥1.20.5 → 21.
    Unparseable ids (e.g. snapshots) fall back to the newest LTS (21); snapshot
    handling is deferred.
    """
    parts = mc_version.split(".")
    try:
        nums = tuple(int(p) for p in parts[:3])
    except ValueError:
        return 21
    # pad to length 3 for stable tuple comparison (e.g. "1.21" -> (1, 21, 0))
    nums = nums + (0,) * (3 - len(nums))

    if nums >= (1, 20, 5):
        return 21
    if nums >= (1, 18, 0):
        return 17
    if nums >= (1, 17, 0):
        return 16
    return 8


def detect_os_arch() -> tuple[str, str]:
    """Return Adoptium (os, arch) tokens for the current host."""
    system = platform.system().lower()
    os_token = {"linux": "linux", "darwin": "mac", "windows": "windows"}.get(system, "linux")

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    elif machine in ("i386", "i686", "x86"):
        arch = "x86"
    else:
        arch = "x64"
    return os_token, arch


def binary_url(java_major: int, os_token: str | None = None, arch: str | None = None) -> str:
    if os_token is None or arch is None:
        detected_os, detected_arch = detect_os_arch()
        os_token = os_token or detected_os
        arch = arch or detected_arch
    return f"{API}/{java_major}/ga/{os_token}/{arch}/jre/hotspot/normal/eclipse"


# --- runtime provisioning --------------------------------------------------


def _java_exe_name(os_token: str) -> str:
    return "java.exe" if os_token == "windows" else "java"


def find_java_exe(root: Path, os_token: str) -> Path | None:
    """Locate the ``bin/java`` executable within an extracted JRE tree."""
    name = _java_exe_name(os_token)
    for candidate in root.rglob(name):
        if candidate.parent.name == "bin":
            return candidate
    return None


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest)


async def ensure_java(
    java_major: int,
    java_dir: Path,
    *,
    os_token: str | None = None,
    arch: str | None = None,
) -> Path:
    """Ensure a Temurin JRE of ``java_major`` exists under ``java_dir`` and
    return the path to its ``java`` executable.

    Runtimes are shared across servers, keyed by major version
    (``java_dir/temurin-{major}/``), so each Java is downloaded at most once.
    """
    detected_os, detected_arch = detect_os_arch()
    os_token = os_token or detected_os
    arch = arch or detected_arch

    target = java_dir / f"temurin-{java_major}"
    existing = find_java_exe(target, os_token) if target.exists() else None
    if existing is not None:
        return existing

    suffix = ".zip" if os_token == "windows" else ".tar.gz"
    url = binary_url(java_major, os_token, arch)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"temurin-{java_major}{suffix}"
        await download_file(url, archive)
        await asyncio.to_thread(_extract, archive, target)

    exe = find_java_exe(target, os_token)
    if exe is None:
        raise RuntimeError(f"Java executable not found after extracting {url}")
    return exe
