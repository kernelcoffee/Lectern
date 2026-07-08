"""Generated/uploaded packs beyond Modrinth: Vanilla Tweaks + zip uploads.

Both paths produce ordinary manifest items and reuse the manager's
manifest/lock/row plumbing, so list/remove work unchanged. Kinds involved:
VT *resourcepacks* → kind ``resourcepack`` (one zip in ``resourcepacks/``);
VT *craftingtweaks* → kind ``datapack`` (one zip — a crafting-tweaks zip IS a
datapack — in ``{world}/datapacks/``); VT *datapacks* → kind ``datapack``,
but the download is a **zip of individual datapack zips** which we extract
into ``{world}/datapacks/``, one manifest item per member.

- **Vanilla Tweaks** items carry the selection + fingerprint
  (``vt_packs``/``vt_fingerprint``/``vt_type``). A server keeps at most ONE
  generated VT set per type — regenerating replaces it; an *unchanged*
  selection is a no-op returning the existing items (VT builds zips
  server-side per request, so idempotence lives here — ref mc-image-helper).
- **Uploads** carry no source project; ``pack.mcmeta`` supplies a display
  name (both resource packs and datapacks have one).

``sha1_of``/`set_server_resource_pack`` support the optional in-game prompt:
Minecraft clients download the pack from ``resource-pack`` (a URL) and verify
it against ``resource-pack-sha1``.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
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

# VT pack type → the Lectern content kind its output lands as.
VT_KINDS = {
    "resourcepacks": "resourcepack",
    "datapacks": "datapack",
    "craftingtweaks": "datapack",
}

_TYPE_LABEL = {
    "resourcepacks": "Vanilla Tweaks",
    "datapacks": "Vanilla Tweaks datapacks",
    "craftingtweaks": "Vanilla Tweaks crafting tweaks",
}


def _vt_items(items: list[dict], pack_type: str) -> list[dict]:
    """Existing VT items of one type (legacy items without vt_type count as
    resourcepacks — the only type that existed before)."""
    return [
        i
        for i in items
        if i.get("source") == "vanillatweaks"
        and i.get("vt_type", "resourcepacks") == pack_type
    ]


def _vt_base_item(
    *, pack_type: str, packs: dict[str, list[str]], fingerprint: str,
    mc_version: str, name: str, filename: str,
) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "kind": VT_KINDS[pack_type],
        "source": "vanillatweaks",
        "project_id": None,
        "version_id": None,
        "version_number": vt.vt_version(mc_version),
        "slug": None,
        "name": name,
        "filename": filename,
        "side": "client" if pack_type == "resourcepacks" else "server",
        "sha512": None,  # VT publishes no hash; filled after download
        "game_version": mc_version,
        "loader": None,
        "channel": "release",
        "enabled": True,
        "vt_packs": packs,
        "vt_fingerprint": fingerprint,
        "vt_type": pack_type,
    }


async def install_vanillatweaks(
    session: Session,
    server_id: str,
    server_dir: Path,
    *,
    packs: dict[str, list[str]],
    mc_version: str,
    pack_type: str = "resourcepacks",
) -> list[ContentItem]:
    """Generate + install the VT selection, replacing the server's previous
    VT set of the same type. Unchanged selection (same fingerprint) →
    returns the existing items without regenerating.

    Resource packs and crafting tweaks are one zip → one item; the datapacks
    download is a zip of datapack zips → one item per extracted member."""
    if pack_type not in VT_KINDS:
        raise ResourcePackError(f"Unknown Vanilla Tweaks type: {pack_type}")
    packs = {c: names for c, names in packs.items() if names}
    if not packs:
        raise ResourcePackError("Empty Vanilla Tweaks selection")
    fingerprint = vt.selection_fingerprint(packs, mc_version, pack_type)

    async with manager._lock(server_id):
        items = manager.read_manifest(server_dir)
        existing = _vt_items(items, pack_type)
        if (
            existing
            and all(i.get("vt_fingerprint") == fingerprint for i in existing)
            and all(manager.item_file(server_dir, i).exists() for i in existing)
        ):
            rows = [session.get(ContentItem, i["id"]) for i in existing]
            if all(r is not None for r in rows):
                return rows  # same selection — nothing to do

        url = await vt.generate(packs, mc_version, pack_type)
        pack_count = sum(len(v) for v in packs.values())
        base_name = f"{_TYPE_LABEL[pack_type]} ({pack_count} packs)"

        # Out with the previous set of this type (files + manifest entries).
        for old in existing:
            manager._delete_item_file(server_dir, old)
            items.remove(old)

        new_items: list[dict] = []
        if pack_type == "datapacks":
            # Zip-of-zips: extract each member datapack, one item per member.
            with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
                await download_file(url, Path(tmp.name))
                with zipfile.ZipFile(tmp.name) as outer:
                    members = [
                        m for m in outer.namelist()
                        if m.endswith(".zip") and "/" not in m.strip("/")
                    ]
                    if not members:
                        raise ResourcePackError(
                            "Vanilla Tweaks datapack archive contained no packs"
                        )
                    target_dir = manager.kind_dir(server_dir, "datapack")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for member in members:
                        safe = Path(member).name  # zip-slip guard
                        data = outer.read(member)
                        (target_dir / safe).write_bytes(data)
                        item = _vt_base_item(
                            pack_type=pack_type, packs=packs,
                            fingerprint=fingerprint, mc_version=mc_version,
                            name=f"VT: {safe.removesuffix('.zip')}",
                            filename=safe,
                        )
                        item["sha512"] = hashlib.sha512(data).hexdigest()
                        new_items.append(item)
        else:
            filename = (
                f"VanillaTweaks-{pack_type}-{vt.vt_version(mc_version)}"
                f"-{fingerprint[:8]}.zip"
            )
            item = _vt_base_item(
                pack_type=pack_type, packs=packs, fingerprint=fingerprint,
                mc_version=mc_version, name=base_name, filename=filename,
            )
            dest = manager.item_file(server_dir, item)
            await download_file(url, dest)
            item["sha512"] = _sha512_of(dest)
            new_items.append(item)

        items.extend(new_items)
        manager.write_manifest(server_dir, items)
        manager._sync_rows(session, server_id, items)

    rows = [session.get(ContentItem, i["id"]) for i in new_items]
    assert all(r is not None for r in rows)
    return rows


# --- uploads -----------------------------------------------------------------


async def install_upload(
    session: Session,
    server_id: str,
    server_dir: Path,
    *,
    filename: str,
    data: bytes,
    kind: str = "resourcepack",
) -> ContentItem:
    """Store an uploaded pack zip as a manifest item (source ``upload``).
    ``kind`` may be "resourcepack" or "datapack" — both are zips with a
    ``pack.mcmeta``; only the target directory differs."""
    if kind not in ("resourcepack", "datapack"):
        raise ResourcePackError(f"Cannot upload content of kind {kind!r}")
    description, _pack_format = read_pack_meta(data)  # validates the zip
    safe_name = Path(filename).name or "pack.zip"
    if not safe_name.endswith(".zip"):
        safe_name += ".zip"

    async with manager._lock(server_id):
        items = manager.read_manifest(server_dir)
        if any(i["filename"] == safe_name and i["kind"] == kind for i in items):
            raise ResourcePackError(f"A pack named {safe_name} is already installed")
        item = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "source": "upload",
            "project_id": None,
            "version_id": None,
            "version_number": None,
            "slug": None,
            "name": description or safe_name.removesuffix(".zip"),
            "filename": safe_name,
            "side": "client" if kind == "resourcepack" else "server",
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
