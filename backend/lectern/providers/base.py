"""Shared HTTP helpers and provider interfaces.

``get_json`` is a tiny cached GET: responses are cached on disk under
``cache/http`` with a TTL so repeated catalog lookups don't hammer upstream
APIs. The ``ContentSource`` and ``ServerTypeProvider`` protocols define the two
extensibility seams (see docs/technical.md §2) — Modrinth/Fabric are the first
implementations; CurseForge/Quilt/Paper/etc. slot in behind the same shapes.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from ..config import get_settings

USER_AGENT = "Lectern/0.1 (self-hosted Minecraft server manager)"

_DEFAULT_TTL = 3600  # seconds


def _cache_path(url: str, params: Any) -> Path:
    cache_dir = get_settings().cache_dir / "http"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{url}?{params!r}".encode()).hexdigest()[:32]
    return cache_dir / f"{key}.json"


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    ttl: int = _DEFAULT_TTL,
) -> Any:
    """GET ``url`` and return parsed JSON, with a simple on-disk TTL cache."""
    cache_file = _cache_path(url, params)
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl:
        return json.loads(cache_file.read_text())

    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    async with httpx.AsyncClient(headers=request_headers, timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    cache_file.write_text(json.dumps(data))
    return data


async def get_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    ttl: int = _DEFAULT_TTL,
) -> str:
    """GET ``url`` and return the raw body (XML metadata etc.), with the same
    on-disk TTL cache as ``get_json``."""
    cache_file = _cache_path(url, None)
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl:
        return cache_file.read_text()

    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    async with httpx.AsyncClient(headers=request_headers, timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text

    cache_file.write_text(text)
    return text


class ChecksumMismatch(Exception):
    """Downloaded file's hash does not match the expected one."""


async def download_file(
    url: str,
    dest: Path,
    *,
    expected_hash: str | None = None,
    hash_algo: str = "sha512",
) -> Path:
    """Stream ``url`` to ``dest`` (created, parents included). No caching —
    used for large one-off binaries (server jars, Java archives, mods).

    When ``expected_hash`` is given the file is hashed while streaming
    (``hash_algo``: Mojang publishes sha1, Modrinth sha512) and a mismatch
    removes the partial file and raises ``ChecksumMismatch``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.new(hash_algo) if expected_hash else None
    request_headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(
        headers=request_headers, timeout=300.0, follow_redirects=True
    ) as client, client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)
                if hasher:
                    hasher.update(chunk)
    if hasher and expected_hash and hasher.hexdigest() != expected_hash.lower():
        dest.unlink(missing_ok=True)
        raise ChecksumMismatch(
            f"{dest.name}: {hash_algo} mismatch (expected {expected_hash}, got {hasher.hexdigest()})"
        )
    return dest


# --- Extensibility seams ---------------------------------------------------


@runtime_checkable
class ServerTypeProvider(Protocol):
    """A server flavour: Vanilla, Fabric, (later) Quilt/Paper/Forge.

    M2 only needs version discovery; the install/launch methods (prepare jar,
    build command) are added in M3.
    """

    key: str
    needs_loader: bool

    async def list_minecraft_versions(self) -> list[str]: ...

    async def list_loader_versions(self, mc_version: str) -> list[str]: ...


@runtime_checkable
class ContentSource(Protocol):
    """A content provider: Modrinth first, (later) CurseForge.

    Defined here so M6 (content) and the frontend code against one interface.
    """

    key: str

    async def search(self, query: str, *, loader: str, mc_version: str) -> list[dict]: ...

    async def list_versions(self, project_id: str, *, loader: str, mc_version: str) -> list[dict]: ...
