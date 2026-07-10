"""Mojang — vanilla server jars and the Minecraft version list.

Version manifest → per-version JSON → ``downloads.server.url``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .base import get_json

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

# Player profile lookups (name ↔ UUID), for the whitelist/ops/ban lists.
_PROFILE_BY_NAME = "https://api.mojang.com/users/profiles/minecraft/{name}"
_PROFILE_BY_UUID = "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"
_PROFILE_TTL = 86400  # profiles rarely change; a day is fine


def undash_uuid(value: str) -> str:
    """Canonical (undashed, lowercase) form of a UUID."""
    return value.replace("-", "").lower()


def dash_uuid(value: str) -> str:
    """Dashed UUID form used by Minecraft's ``*.json`` player lists."""
    u = undash_uuid(value)
    return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"


def _is_uuid(value: str) -> bool:
    u = undash_uuid(value)
    return len(u) == 32 and all(c in "0123456789abcdef" for c in u)


async def resolve_profile(query: str) -> dict[str, str] | None:
    """Resolve a player by **username or UUID** to ``{"uuid": <undashed>,
    "name": <current name>}`` via Mojang, or ``None`` if no such player.
    Never raises — an unknown player / offline Mojang is a normal outcome."""
    q = query.strip()
    if not q:
        return None
    url = (
        _PROFILE_BY_UUID.format(uuid=undash_uuid(q))
        if _is_uuid(q)
        else _PROFILE_BY_NAME.format(name=q)
    )
    try:
        data = await get_json(url, ttl=_PROFILE_TTL)
    except Exception:  # noqa: BLE001 — 400/404/network all mean "not found"
        return None
    if not isinstance(data, dict) or "id" not in data or "name" not in data:
        return None
    return {"uuid": undash_uuid(str(data["id"])), "name": str(data["name"])}


async def get_skin_url(uuid: str) -> str | None:
    """The player's current skin PNG URL (on textures.minecraft.net), or
    ``None`` if unknown/offline. Used to render self-hosted avatars."""
    try:
        data = await get_json(
            _PROFILE_BY_UUID.format(uuid=undash_uuid(uuid)), ttl=_PROFILE_TTL
        )
    except Exception:  # noqa: BLE001
        return None
    for prop in data.get("properties", []) if isinstance(data, dict) else []:
        if prop.get("name") != "textures":
            continue
        try:
            blob = json.loads(base64.b64decode(prop["value"]))
        except Exception:  # noqa: BLE001
            return None
        url = blob.get("textures", {}).get("SKIN", {}).get("url")
        return url if isinstance(url, str) else None
    return None


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
