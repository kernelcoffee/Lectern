"""Vanilla Tweaks — generated resource packs (docs/technical.md §6.6).

Base: https://vanillatweaks.net. There is **no official/stable API** — these
are the endpoints the website itself uses (same ones mc-image-helper relies
on; see docs/references/mc-image-helper.md), so everything here is isolated
behind this module and fails as a clean ``VanillaTweaksError``.

- Catalog:    GET /assets/resources/json/{major.minor}/{rp|dp|ct}categories.json
- Share code: GET /assets/server/sharecode.php?code={code}
              → {"type": "resourcepacks"|"datapacks"|"craftingtweaks",
                 "version": "1.20", "packs": {...}}
- Generate:   POST /assets/server/zip{type}.php
              form: packs=<json {category: [packId, …]}>, version={major.minor}
              → {"status": "success", "link": "/download/…zip"}
- Download:   GET {base}{link}

Delivery differs per type (mirrors mc-image-helper): resource packs and
crafting tweaks are ONE zip (a crafting-tweaks zip is itself a datapack);
the datapacks download is a **zip of individual datapack zips** that must be
extracted into the world's datapacks dir (the link even says UNZIP_ME).

VT versions are MAJOR.MINOR ("1.20"), not patch releases — ``vt_version``
truncates. ``selection_fingerprint`` hashes the canonicalized selection so an
unchanged selection can skip regeneration (VT zips are built server-side on
every request; idempotence has to live on our side).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from .base import USER_AGENT, get_json

BASE = "https://vanillatweaks.net"

_CATEGORIES_TTL = 86400

# VT pack types → their category catalog file. Keys are VT's own type names
# (same strings share codes carry in "type").
PACK_TYPES = {
    "resourcepacks": "rpcategories",
    "datapacks": "dpcategories",
    "craftingtweaks": "ctcategories",
}


class VanillaTweaksError(Exception):
    """Vanilla Tweaks unreachable or answered something unexpected."""


# --- pure helpers (unit-tested) --------------------------------------------


def vt_version(mc_version: str) -> str:
    """"1.20.1" → "1.20" — VT catalogs/zips are keyed by major.minor."""
    return ".".join(mc_version.split(".")[:2])


def selection_fingerprint(
    packs: dict[str, list[str]], mc_version: str, pack_type: str = "resourcepacks"
) -> str:
    """Stable hash of a pack selection: category order and pack order within a
    category don't matter; the VT (major.minor) version and pack type do."""
    canonical = {
        category: sorted(names)
        for category, names in sorted(packs.items())
        if names
    }
    payload = json.dumps(
        {"type": pack_type, "version": vt_version(mc_version), "packs": canonical}
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# --- network ----------------------------------------------------------------


async def categories(
    mc_version: str, pack_type: str = "resourcepacks"
) -> dict[str, Any]:
    """Categories/packs of one VT type for a MC version (raw VT payload)."""
    catalog = PACK_TYPES.get(pack_type)
    if catalog is None:
        raise VanillaTweaksError(f"Unknown Vanilla Tweaks type: {pack_type}")
    url = f"{BASE}/assets/resources/json/{vt_version(mc_version)}/{catalog}.json"
    try:
        return await get_json(url, ttl=_CATEGORIES_TTL)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise VanillaTweaksError(
            f"Vanilla Tweaks {pack_type} categories unavailable for "
            f"{vt_version(mc_version)}: {exc}"
        ) from exc


async def resolve_share_code(code: str) -> dict[str, Any]:
    """A VT share code → its pack definition
    (``{"type", "version", "packs"}``). Unknown codes raise."""
    url = f"{BASE}/assets/server/sharecode.php"
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True
        ) as client:
            resp = await client.get(url, params={"code": code})
            resp.raise_for_status()
            definition = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise VanillaTweaksError(f"Could not resolve share code {code!r}: {exc}") from exc
    if not isinstance(definition, dict) or "packs" not in definition:
        raise VanillaTweaksError(f"Share code {code!r} returned no pack definition")
    return definition


async def generate(
    packs: dict[str, list[str]], mc_version: str, pack_type: str = "resourcepacks"
) -> str:
    """Ask VT to build a pack zip of ``pack_type``; returns the download URL."""
    if pack_type not in PACK_TYPES:
        raise VanillaTweaksError(f"Unknown Vanilla Tweaks type: {pack_type}")
    url = f"{BASE}/assets/server/zip{pack_type}.php"
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=120.0, follow_redirects=True
        ) as client:
            resp = await client.post(
                url,
                data={"packs": json.dumps(packs), "version": vt_version(mc_version)},
            )
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise VanillaTweaksError(f"Vanilla Tweaks generation failed: {exc}") from exc
    if body.get("status") != "success" or not body.get("link"):
        raise VanillaTweaksError(f"Vanilla Tweaks refused the selection: {body}")
    return f"{BASE}{body['link']}"
