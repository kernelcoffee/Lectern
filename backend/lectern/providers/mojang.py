"""Mojang — vanilla server jars and the Minecraft version list.

Version manifest → per-version JSON → ``downloads.server.url``.
"""

from __future__ import annotations

from typing import Any

from .base import get_json

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"


# --- pure parsing (unit-tested) -------------------------------------------


def parse_release_versions(manifest: dict[str, Any]) -> list[str]:
    """Release version ids, newest first (manifest is already newest-first)."""
    return [v["id"] for v in manifest.get("versions", []) if v.get("type") == "release"]


def find_version_meta_url(manifest: dict[str, Any], version: str) -> str | None:
    for v in manifest.get("versions", []):
        if v.get("id") == version:
            return v.get("url")
    return None


def parse_server_jar_url(version_meta: dict[str, Any]) -> str | None:
    return version_meta.get("downloads", {}).get("server", {}).get("url")


def parse_server_jar_sha1(version_meta: dict[str, Any]) -> str | None:
    return version_meta.get("downloads", {}).get("server", {}).get("sha1")


def parse_java_major(version_meta: dict[str, Any]) -> int | None:
    """The Java major version Mojang declares this build needs, if present.

    Modern manifests carry ``"javaVersion": {"majorVersion": N}`` — the
    authoritative requirement (e.g. MC 26.2 → 25). Absent on very old versions.
    """
    java = version_meta.get("javaVersion")
    if isinstance(java, dict):
        return java.get("majorVersion")
    return None


# --- network ---------------------------------------------------------------


async def list_release_versions() -> list[str]:
    return parse_release_versions(await get_json(MANIFEST_URL, ttl=3600))


async def _version_meta(version: str) -> dict[str, Any] | None:
    manifest = await get_json(MANIFEST_URL, ttl=3600)
    meta_url = find_version_meta_url(manifest, version)
    if meta_url is None:
        return None
    return await get_json(meta_url, ttl=86400)


async def get_server_jar_url(version: str) -> str | None:
    """Resolve the download URL for a vanilla server jar (used in M3)."""
    meta = await _version_meta(version)
    return parse_server_jar_url(meta) if meta is not None else None


async def get_server_jar_sha1(version: str) -> str | None:
    """Mojang-published SHA1 of the vanilla server jar (verified on download)."""
    meta = await _version_meta(version)
    return parse_server_jar_sha1(meta) if meta is not None else None


async def get_java_major(version: str) -> int | None:
    """Java major version Mojang requires for ``version`` (``None`` if unknown)."""
    meta = await _version_meta(version)
    return parse_java_major(meta) if meta is not None else None
