"""PaperMC downloads API ("fill" v3) — Velocity proxy builds.

https://fill.papermc.io/v3 replaced the sunset api.papermc.io/v2. Shapes:
- GET /projects/velocity                  → {"versions": {"4.0.0": ["4.1.0-SNAPSHOT", "4.0.0", …], …}}
  (groups and entries both newest-first; SNAPSHOTs mixed in)
- GET /projects/velocity/versions/{v}/builds/latest
  → {"channel": "STABLE", "downloads": {"server:default": {"name", "url",
     "checksums": {"sha256": …}}}}
"""

from __future__ import annotations

from typing import Any

from .base import get_json

BASE = "https://fill.papermc.io/v3"


# --- pure parsing (unit-tested) -------------------------------------------


def parse_release_versions(payload: dict[str, Any]) -> list[str]:
    """Flatten the grouped version map, dropping SNAPSHOTs; the API lists
    newest-first at both levels, so order is preserved."""
    out: list[str] = []
    for entries in (payload.get("versions") or {}).values():
        out.extend(v for v in entries if "SNAPSHOT" not in v.upper())
    return out


def parse_build_download(build: dict[str, Any]) -> dict[str, str]:
    """{url, name, sha256} of a build's server jar."""
    download = (build.get("downloads") or {}).get("server:default") or {}
    if not download.get("url"):
        raise ValueError("PaperMC build has no server download")
    return {
        "url": download["url"],
        "name": download.get("name", "server.jar"),
        "sha256": (download.get("checksums") or {}).get("sha256", ""),
    }


# --- network ---------------------------------------------------------------


async def list_velocity_versions() -> list[str]:
    payload = await get_json(f"{BASE}/projects/velocity", ttl=3600)
    return parse_release_versions(payload)


async def latest_velocity_build(version: str) -> dict[str, str]:
    build = await get_json(
        f"{BASE}/projects/velocity/versions/{version}/builds/latest", ttl=3600
    )
    return parse_build_download(build)
