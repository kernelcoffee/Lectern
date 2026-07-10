"""Self-hosted player face avatars.

Community avatar services (Crafatar, …) are flaky — and Lectern runs on a LAN,
so it renders avatars itself from the Mojang skin: fetch the skin PNG, crop the
8×8 face + hat overlay, scale it up (nearest-neighbour, so it stays pixel-crisp)
and cache the result on disk. Works for both 64×64 and legacy 64×32 skins (the
face region is the same). Returns ``None`` when the player has no resolvable
skin, so the UI can fall back to an initial tile.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import httpx
from PIL import Image

from ..config import get_settings
from ..providers import mojang
from ..providers.base import USER_AGENT

CACHE_TTL = 86400  # re-fetch a skin at most daily (skins rarely change)
_MAX_SKIN_BYTES = 256 * 1024  # a skin PNG is tiny; guard against a bad URL


def _cache_path(uuid: str, size: int) -> Path:
    d = get_settings().cache_dir / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{mojang.undash_uuid(uuid)}_{size}.png"


def _render_face(skin_png: bytes, size: int) -> bytes:
    skin = Image.open(io.BytesIO(skin_png)).convert("RGBA")
    face = skin.crop((8, 8, 16, 16))
    hat = skin.crop((40, 8, 48, 16))  # the "hat"/overlay layer
    face.alpha_composite(hat)
    face = face.resize((size, size), Image.NEAREST)
    out = io.BytesIO()
    face.save(out, "PNG")
    return out.getvalue()


async def face_png(uuid: str, size: int) -> bytes | None:
    """Cached face avatar for ``uuid`` at ``size``×``size`` px, or ``None``."""
    cache = _cache_path(uuid, size)
    if cache.exists() and (time.time() - cache.stat().st_mtime) < CACHE_TTL:
        return cache.read_bytes()

    skin_url = await mojang.get_skin_url(uuid)
    if skin_url is None:
        return None
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=10.0, follow_redirects=True
        ) as client:
            resp = await client.get(skin_url)
            resp.raise_for_status()
            skin_png = resp.content[:_MAX_SKIN_BYTES]
        png = _render_face(skin_png, size)
    except Exception:  # noqa: BLE001 — offline / bad skin → fall back to a tile
        return None
    cache.write_bytes(png)
    return png
