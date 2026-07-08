"""Resource packs beyond Modrinth (M7): Vanilla Tweaks generation + uploads.

Both paths produce ordinary manifest items (kind ``resourcepack``) in
``resourcepacks/`` and reuse the manager's manifest/lock/row plumbing, so
list/remove work unchanged. What differs from Modrinth content:

- **Vanilla Tweaks** items carry the pack selection + its fingerprint
  (``vt_packs``/``vt_fingerprint``). A server keeps at most ONE generated VT
  pack — regenerating replaces it; regenerating with an *unchanged* selection
  is a no-op that returns the existing item (VT builds zips server-side per
  request, so idempotence lives here — ref mc-image-helper).
- **Uploads** carry no source project at all; ``pack.mcmeta`` supplies a
  display name and the pack format.

``sha1_of``/`set_server_resource_pack`` support the optional in-game prompt:
Minecraft clients download the pack from ``resource-pack`` (a URL) and verify
it against ``resource-pack-sha1``.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

from sqlmodel import Session

from ..models import ContentItem
from ..providers import vanillatweaks as vt
from ..providers.base import download_file
from ..servers import properties as props
from . import manager


class ResourcePackError(Exception):
    """Invalid pack upload / generation input."""


# --- pack.mcmeta -------------------------------------------------------------


def read_pack_meta(data: bytes) -> tuple[str | None, int | None]:
    """(description, pack_format) from a pack zip's ``pack.mcmeta``;
    raises ``ResourcePackError`` if the zip has no parseable pack.mcmeta."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            meta = json.loads(zf.read("pack.mcmeta"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ResourcePackError(
            "Not a resource pack (missing or invalid pack.mcmeta)"
        ) from exc
    pack = meta.get("pack", {})
    description = pack.get("description")
    if isinstance(description, dict):  # text-component form
        description = description.get("text")
    return (
        description if isinstance(description, str) else None,
        pack.get("pack_format"),
    )


def sha1_of(path: Path) -> str:
    hasher = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha512_of(path: Path) -> str:
    hasher = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# --- Vanilla Tweaks ----------------------------------------------------------


def _find_vt_item(items: list[dict]) -> dict | None:
    return next((i for i in items if i.get("source") == "vanillatweaks"), None)


async def install_vanillatweaks(
    session: Session,
    server_id: str,
    server_dir: Path,
    *,
    packs: dict[str, list[str]],
    mc_version: str,
) -> ContentItem:
    """Generate + install the VT pack for ``packs``, replacing any previous
    VT pack. Unchanged selection (same fingerprint) → returns the existing
    item without regenerating."""
    packs = {c: names for c, names in packs.items() if names}
    if not packs:
        raise ResourcePackError("Empty Vanilla Tweaks selection")
    fingerprint = vt.selection_fingerprint(packs, mc_version)

    async with manager._lock(server_id):
        items = manager.read_manifest(server_dir)
        existing = _find_vt_item(items)
        if (
            existing is not None
            and existing.get("vt_fingerprint") == fingerprint
            and manager.item_file(server_dir, existing).exists()
        ):
            row = session.get(ContentItem, existing["id"])
            if row is not None:
                return row  # same selection — nothing to do

        url = await vt.generate(packs, mc_version)
        pack_count = sum(len(v) for v in packs.values())
        filename = f"VanillaTweaks-{vt.vt_version(mc_version)}-{fingerprint[:8]}.zip"
        new_item = {
            "id": existing["id"] if existing else uuid.uuid4().hex,
            "kind": "resourcepack",
            "source": "vanillatweaks",
            "project_id": None,
            "version_id": None,
            "version_number": vt.vt_version(mc_version),
            "slug": None,
            "name": f"Vanilla Tweaks ({pack_count} packs)",
            "filename": filename,
            "side": "client",
            "sha512": None,  # VT publishes no hash; filled from the download
            "game_version": mc_version,
            "loader": None,
            "channel": "release",
            "enabled": True,
            "vt_packs": packs,
            "vt_fingerprint": fingerprint,
        }
        if existing is not None:
            manager._delete_item_file(server_dir, existing)
            items[items.index(existing)] = new_item
        else:
            items.append(new_item)
        dest = manager.item_file(server_dir, new_item)
        await download_file(url, dest)
        new_item["sha512"] = _sha512_of(dest)
        manager.write_manifest(server_dir, items)
        manager._sync_rows(session, server_id, items)

    row = session.get(ContentItem, new_item["id"])
    assert row is not None
    return row


# --- uploads -----------------------------------------------------------------


async def install_upload(
    session: Session,
    server_id: str,
    server_dir: Path,
    *,
    filename: str,
    data: bytes,
) -> ContentItem:
    """Store an uploaded pack zip as a manifest item (source ``upload``)."""
    description, _pack_format = read_pack_meta(data)  # validates the zip
    safe_name = Path(filename).name or "resource-pack.zip"
    if not safe_name.endswith(".zip"):
        safe_name += ".zip"

    async with manager._lock(server_id):
        items = manager.read_manifest(server_dir)
        if any(i["filename"] == safe_name for i in items):
            raise ResourcePackError(f"A pack named {safe_name} is already installed")
        item = {
            "id": uuid.uuid4().hex,
            "kind": "resourcepack",
            "source": "upload",
            "project_id": None,
            "version_id": None,
            "version_number": None,
            "slug": None,
            "name": description or safe_name.removesuffix(".zip"),
            "filename": safe_name,
            "side": "client",
            "sha512": hashlib.sha512(data).hexdigest(),
            "game_version": None,
            "loader": None,
            "channel": "release",
            "enabled": True,
        }
        dest = manager.item_file(server_dir, item)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        items.append(item)
        manager.write_manifest(server_dir, items)
        manager._sync_rows(session, server_id, items)

    row = session.get(ContentItem, item["id"])
    assert row is not None
    return row


# --- server resource-pack prompt (optional, F-RP-3) ---------------------------


def set_server_resource_pack(server_dir: Path, *, url: str, sha1: str) -> None:
    """Point ``server.properties`` at a downloadable pack so clients are
    prompted to use it. Empty url clears both keys."""
    file_props = props.read_properties(server_dir)
    if url:
        file_props["resource-pack"] = url
        file_props["resource-pack-sha1"] = sha1
    else:
        file_props.pop("resource-pack", None)
        file_props.pop("resource-pack-sha1", None)
    props.write_properties(server_dir, file_props)
