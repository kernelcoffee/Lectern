"""Quilt — loader metadata + the installer CLI jar.

Meta API mirrors Fabric's shape (https://meta.quiltmc.org/v3) but there is NO
direct server-launch-jar endpoint — the install pipeline downloads the Quilt
installer and runs ``install server <mc> <loader> --install-dir=. \
--download-server``, which produces ``quilt-server-launch.jar``.
"""

from __future__ import annotations

import re
from typing import Any

from .base import get_json

BASE = "https://meta.quiltmc.org/v3"
GAME_URL = f"{BASE}/versions/game"
INSTALLER_URL = f"{BASE}/versions/installer"
MAVEN = "https://maven.quiltmc.org/repository/release"


def _loader_url(mc_version: str) -> str:
    return f"{BASE}/versions/loader/{mc_version}"


def installer_jar_url(installer_version: str) -> str:
    return (
        f"{MAVEN}/org/quiltmc/quilt-installer/{installer_version}/"
        f"quilt-installer-{installer_version}.jar"
    )


# --- pure parsing (unit-tested) -------------------------------------------


def _version_sort_key(version: str) -> tuple:
    """Sortable key for Quilt loader versions ("0.20.0-beta.9" style).

    Numeric components compare numerically; a release outranks its own
    pre-releases ("0.20.0" > "0.20.0-beta.9" > "0.20.0-beta.1").
    """
    release, _, pre = version.partition("-")
    nums = tuple(int(n) for n in re.findall(r"\d+", release))
    if not pre:
        return (nums, 1, ())
    pre_nums = tuple(int(n) for n in re.findall(r"\d+", pre))
    return (nums, 0, pre_nums)


def parse_stable_game_versions(data: list[dict[str, Any]]) -> list[str]:
    return [v["version"] for v in data if v.get("stable")]


def parse_loader_versions(data: list[dict[str, Any]]) -> list[str]:
    """Loader builds, newest first. Quilt's meta does NOT return these sorted
    (unlike Fabric's), so sort here."""
    versions = [entry["loader"]["version"] for entry in data if "loader" in entry]
    return sorted(versions, key=_version_sort_key, reverse=True)


def parse_latest_installer(data: list[dict[str, Any]]) -> str | None:
    return data[0].get("version") if data else None


# --- network ---------------------------------------------------------------


async def list_game_versions() -> list[str]:
    return parse_stable_game_versions(await get_json(GAME_URL, ttl=3600))


async def list_loader_versions(mc_version: str) -> list[str]:
    return parse_loader_versions(await get_json(_loader_url(mc_version), ttl=3600))


async def latest_installer_version() -> str | None:
    return parse_latest_installer(await get_json(INSTALLER_URL, ttl=86400))
