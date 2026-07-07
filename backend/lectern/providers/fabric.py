"""Fabric — loader metadata and the ready-to-run server launcher jar.

Meta API: https://meta.fabricmc.net/v2
- game versions:   /versions/game
- loader versions: /versions/loader/{mc}
- installers:      /versions/installer
- server jar:      /versions/loader/{mc}/{loader}/{installer}/server/jar
"""

from __future__ import annotations

from typing import Any

from .base import get_json

BASE = "https://meta.fabricmc.net/v2"
GAME_URL = f"{BASE}/versions/game"
INSTALLER_URL = f"{BASE}/versions/installer"


def _loader_url(mc_version: str) -> str:
    return f"{BASE}/versions/loader/{mc_version}"


def server_jar_url(mc_version: str, loader_version: str, installer_version: str) -> str:
    """Direct download URL for a runnable Fabric server jar (used in M3)."""
    return f"{BASE}/versions/loader/{mc_version}/{loader_version}/{installer_version}/server/jar"


# --- pure parsing (unit-tested) -------------------------------------------


def parse_stable_game_versions(data: list[dict[str, Any]]) -> list[str]:
    return [v["version"] for v in data if v.get("stable")]


def parse_loader_versions(data: list[dict[str, Any]]) -> list[str]:
    # Each entry looks like {"loader": {"version": ..., "stable": ...}, "intermediary": {...}}
    return [entry["loader"]["version"] for entry in data if "loader" in entry]


def parse_latest_stable_installer(data: list[dict[str, Any]]) -> str | None:
    for entry in data:  # newest first
        if entry.get("stable"):
            return entry.get("version")
    return data[0].get("version") if data else None


# --- network ---------------------------------------------------------------


async def list_game_versions() -> list[str]:
    return parse_stable_game_versions(await get_json(GAME_URL, ttl=3600))


async def list_loader_versions(mc_version: str) -> list[str]:
    return parse_loader_versions(await get_json(_loader_url(mc_version), ttl=3600))


async def latest_installer_version() -> str | None:
    return parse_latest_stable_installer(await get_json(INSTALLER_URL, ttl=86400))
