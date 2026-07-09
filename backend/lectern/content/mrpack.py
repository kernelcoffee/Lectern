"""``.mrpack`` (Modrinth modpack) import (M8).

An ``.mrpack`` is a zip holding ``modrinth.index.json`` plus optional
``overrides/`` and ``server-overrides/`` trees. The index pins everything:

- ``dependencies``: the Minecraft version and exactly one loader version
  (``fabric-loader`` for us; quilt/forge/neoforge are roadmap server types).
- ``files[]``: every mod/resource with a relative ``path`` (``mods/x.jar``),
  ``downloads`` URLs, sha512 ``hashes``, and an optional ``env`` section —
  ``env.server == "unsupported"`` marks client-only files we must skip
  (ref: mc-image-helper's install semantics; a client toggle re-includes
  them for mislabeled mods).

Import semantics (docs/technical.md §3 — manifest as reconciliation input):

1. The pack's pinned **loader version wins** over whatever the server was
   created with — the launcher jar is re-resolved when it differs.
2. Pack files become manifest items (source ``mrpack``); re-importing (an
   upgraded pack, say) diffs against the previous mrpack items and deletes
   files the new version no longer ships. Content installed outside the
   pack (Modrinth installs, uploads, VT) is left alone.
3. ``overrides/`` then ``server-overrides/`` are extracted over the server
   dir (server wins), with a zip-slip guard. Override files are config-ish
   and intentionally NOT tracked as manifest items — packs treat them as
   freely-editable defaults.
4. Downloads are sha512-verified and bounded to a few at a time.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

from sqlmodel import Session

from ..models import Server
from ..providers.base import download_file
from ..servers.types import get_server_type
from . import manager

INDEX_NAME = "modrinth.index.json"

# index dependency key → Lectern server type able to run it.
LOADER_KEYS = {"fabric-loader": "fabric", "quilt-loader": "quilt"}

_MAX_CONCURRENT_DOWNLOADS = 6

# Roots a pack file path may live under (guards both zip-slip and surprises
# like a pack writing into world/).
_ALLOWED_ROOTS = ("mods", "resourcepacks", "shaderpacks", "config", "datapacks")

_KIND_BY_ROOT = {"mods": "mod", "resourcepacks": "resourcepack", "datapacks": "datapack"}


class MrpackError(Exception):
    """Invalid or incompatible modpack."""


# --- parsing (pure, unit-tested) ---------------------------------------------


def parse_index(data: bytes) -> dict[str, Any]:
    """Read + sanity-check ``modrinth.index.json`` out of an .mrpack zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            index = json.loads(zf.read(INDEX_NAME))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise MrpackError("Not a .mrpack (missing or invalid modrinth.index.json)") from exc
    if index.get("game") not in (None, "minecraft"):
        raise MrpackError(f"Unsupported game: {index.get('game')}")
    if "dependencies" not in index or "files" not in index:
        raise MrpackError("modrinth.index.json lacks dependencies/files")
    return index


def pack_loader(index: dict[str, Any]) -> tuple[str, str]:
    """(server_type, loader_version) pinned by the pack."""
    deps = index["dependencies"]
    for key, server_type in LOADER_KEYS.items():
        if key in deps:
            return server_type, deps[key]
    raise MrpackError(
        "Pack pins no supported loader (fabric-loader/quilt-loader) — "
        f"dependencies: {sorted(deps)}"
    )


def server_files(
    index: dict[str, Any], *, include_client_only: bool = False
) -> tuple[list[dict], list[dict]]:
    """(files to install, client-only files skipped). A file with no ``env``
    is required everywhere."""
    wanted: list[dict] = []
    skipped: list[dict] = []
    for f in index.get("files", []):
        server_env = (f.get("env") or {}).get("server", "required")
        if server_env == "unsupported" and not include_client_only:
            skipped.append(f)
        else:
            wanted.append(f)
    return wanted, skipped


def _safe_relpath(path: str) -> Path:
    """Validate a pack-relative path: no traversal, under an allowed root."""
    rel = Path(path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise MrpackError(f"Unsafe path in pack: {path!r}")
    if rel.parts[0] not in _ALLOWED_ROOTS:
        raise MrpackError(f"Pack file outside allowed directories: {path!r}")
    return rel


def _file_item(f: dict, *, mc_version: str, pack_name: str, pack_version: str) -> dict:
    rel = _safe_relpath(f["path"])
    kind = _KIND_BY_ROOT.get(rel.parts[0], rel.parts[0].rstrip("s"))
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "source": "mrpack",
        "project_id": None,  # mrpack files carry hashes, not project ids
        "version_id": None,
        "version_number": None,
        "slug": None,
        "name": rel.name,
        "filename": rel.name,
        # Path root relative to the server dir — datapacks from packs keep the
        # pack's own path (datapacks/) rather than the world dir; Minecraft
        # only loads world datapacks, but packs shipping them expect users to
        # copy per-world. Mods/resourcepacks match our kind dirs exactly.
        "side": "both",
        "sha512": (f.get("hashes") or {}).get("sha512"),
        "game_version": mc_version,
        "loader": None,
        "channel": "release",
        "enabled": True,
        "mrpack_name": pack_name,
        "mrpack_version": pack_version,
        "mrpack_path": str(rel),
    }


# --- import -------------------------------------------------------------------


def _extract_tree(zf: zipfile.ZipFile, prefix: str, dest: Path) -> int:
    """Extract ``prefix/**`` of the pack over ``dest`` (zip-slip guarded)."""
    count = 0
    for info in zf.infolist():
        name = info.filename
        if not name.startswith(prefix + "/") or info.is_dir():
            continue
        rel = Path(name[len(prefix) + 1 :])
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise MrpackError(f"Unsafe override path: {name!r}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(info))
        count += 1
    return count


async def import_mrpack(
    session: Session,
    server_id: str,
    server_dir: Path,
    data: bytes,
    *,
    include_client_only: bool = False,
) -> dict[str, Any]:
    """Import a pack onto an existing server. Returns a summary dict."""
    index = parse_index(data)
    pack_name = index.get("name") or "modpack"
    pack_version = str(index.get("versionId") or "")

    server = session.get(Server, server_id)
    assert server is not None

    pack_mc = index["dependencies"].get("minecraft")
    if pack_mc and pack_mc != server.mc_version:
        raise MrpackError(
            f"Pack is for Minecraft {pack_mc}, this server runs {server.mc_version} — "
            "create a matching server first"
        )
    loader_type, loader_version = pack_loader(index)
    if server.type != loader_type:
        raise MrpackError(
            f"Pack needs a {loader_type} server, this one is {server.type}"
        )

    wanted, skipped = server_files(index, include_client_only=include_client_only)

    async with manager._lock(server_id):
        items = manager.read_manifest(server_dir)
        old_pack_items = [i for i in items if i.get("source") == "mrpack"]

        new_items = [
            _file_item(
                f, mc_version=server.mc_version,
                pack_name=pack_name, pack_version=pack_version,
            )
            for f in wanted
        ]

        # Reconcile: delete files the new pack version no longer ships, keep
        # ids stable for files that persist (matched by pack-relative path).
        old_by_path = {i.get("mrpack_path"): i for i in old_pack_items}
        for item in new_items:
            old = old_by_path.pop(item["mrpack_path"], None)
            if old is not None:
                item["id"] = old["id"]
                item["enabled"] = old.get("enabled", True)
                items.remove(old)
        for gone in old_by_path.values():
            (server_dir / gone["mrpack_path"]).unlink(missing_ok=True)
            items.remove(gone)

        # Bounded, verified downloads (skip files already present + matching
        # is handled by download always overwriting — packs are small enough).
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

        async def fetch(f: dict, item: dict) -> None:
            urls = f.get("downloads") or []
            if not urls:
                raise MrpackError(f"{f.get('path')}: no download URL")
            async with semaphore:
                await download_file(
                    urls[0],
                    server_dir / item["mrpack_path"],
                    expected_hash=item["sha512"],
                )

        await asyncio.gather(
            *(fetch(f, item) for f, item in zip(wanted, new_items))
        )

        # Overrides: generic first, then server-specific on top.
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            overrides = _extract_tree(zf, "overrides", server_dir)
            overrides += _extract_tree(zf, "server-overrides", server_dir)

        items.extend(new_items)
        manager.write_manifest(server_dir, items)
        manager._sync_rows(session, server_id, items)

    # The pack's loader version wins: re-resolve the launcher jar if needed.
    loader_changed = server.loader_version != loader_version
    if loader_changed:
        provider = get_server_type(server.type)
        spec = await provider.resolve_jar(server.mc_version, loader_version)
        await download_file(
            spec.url, server_dir / spec.jar_name,
            expected_hash=spec.sha1, hash_algo="sha1",
        )
        server.loader_version = spec.loader_version or loader_version
        server.server_jar = spec.jar_name
        session.add(server)
        session.commit()

    return {
        "pack_name": pack_name,
        "pack_version": pack_version,
        "installed": len(new_items),
        "skipped_client_only": [f["path"] for f in skipped],
        "overrides_applied": overrides,
        "loader_version": server.loader_version,
        "loader_changed": loader_changed,
    }
