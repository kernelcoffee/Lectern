"""Install/remove/toggle/update content on a server (M6, mods first).

Source of truth is the per-server manifest ``.lectern/manifest.json``
(docs/technical.md §3): every operation edits the manifest's item list, then

1. **reconciles files** against it — replaced/removed filenames are deleted,
   missing files are downloaded (sha512-verified), and
2. **mirrors the items into SQLite** (``ContentItem`` rows, upserted by the
   ``id`` stored *in* the manifest so row ids stay stable across operations).

That makes install/remove/update convergent (re-running never duplicates or
orphans files) and lets a server directory be inspected/re-hydrated without
the database — the pattern borrowed from mc-image-helper (manifest as
reconciliation input, not a log).

Dependency handling follows the same reference: **required** dependencies
install by default, **optional** ones opt-in (the policy applies recursively),
with a processed-projects set guarding cycles/duplicates and one bulk
``/projects`` call resolving all dependency names.

Disabled items live on disk as ``{filename}.disabled`` so the loader ignores
them; the manifest keeps the plain filename plus ``enabled``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from ..models import ContentItem
from ..providers import modrinth
from ..providers.base import download_file

MANIFEST_PATH = ".lectern/manifest.json"

# Where each content kind lives inside a server directory.
KIND_DIRS = {"mod": "mods", "plugin": "plugins", "resourcepack": "resourcepacks"}

# ``ContentSource`` registry (docs/technical.md §2). Modules exposing:
# search / list_versions / get_project / get_projects / check_update.
SOURCES: dict[str, Any] = {modrinth.key: modrinth}

# One lock per server so concurrent installs can't race the manifest.
_locks: dict[str, asyncio.Lock] = {}


class ContentError(Exception):
    """Invalid content operation (unknown project/version/item…)."""


def get_source(key: str) -> Any:
    try:
        return SOURCES[key]
    except KeyError:
        raise ContentError(f"Unknown content source: {key}") from None


def _lock(server_id: str) -> asyncio.Lock:
    return _locks.setdefault(server_id, asyncio.Lock())


# --- manifest --------------------------------------------------------------


def read_manifest(server_dir: Path) -> list[dict]:
    path = server_dir / MANIFEST_PATH
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("items", [])


def write_manifest(server_dir: Path, items: list[dict]) -> None:
    path = server_dir / MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}, indent=2))


def item_file(server_dir: Path, item: dict) -> Path:
    """On-disk path of an item's file, honouring the ``.disabled`` suffix."""
    base = server_dir / KIND_DIRS.get(item["kind"], item["kind"]) / item["filename"]
    return base if item.get("enabled", True) else base.with_name(base.name + ".disabled")


def _delete_item_file(server_dir: Path, item: dict) -> None:
    """Remove an item's file whichever enabled-variant is on disk."""
    base = server_dir / KIND_DIRS.get(item["kind"], item["kind"]) / item["filename"]
    base.unlink(missing_ok=True)
    base.with_name(base.name + ".disabled").unlink(missing_ok=True)


def _sync_rows(session: Session, server_id: str, items: list[dict]) -> None:
    """Mirror manifest items into ``ContentItem`` rows (upsert by manifest id,
    delete rows the manifest no longer contains)."""
    rows = session.exec(
        select(ContentItem).where(ContentItem.server_id == server_id)
    ).all()
    by_id = {row.id: row for row in rows}
    seen: set[str] = set()
    for item in items:
        seen.add(item["id"])
        row = by_id.get(item["id"]) or ContentItem(
            id=item["id"],
            server_id=server_id,
            kind=item["kind"],
            source=item["source"],
            name=item["name"],
            filename=item["filename"],
        )
        row.kind = item["kind"]
        row.source = item["source"]
        row.project_id = item.get("project_id")
        row.version_id = item.get("version_id")
        row.version_number = item.get("version_number")
        row.slug = item.get("slug")
        row.name = item["name"]
        row.filename = item["filename"]
        row.sha512 = item.get("sha512")
        row.channel = item.get("channel", "release")
        row.enabled = item.get("enabled", True)
        session.add(row)
    for row in rows:
        if row.id not in seen:
            session.delete(row)
    session.commit()


def ensure_synced(session: Session, server_id: str, server_dir: Path) -> None:
    """Re-mirror rows from the manifest when the two disagree.

    The manifest is the source of truth; rows can go stale if a past sync
    failed after files were already placed (e.g. the pre-column-migration
    500) or if the server directory was restored/edited out of band. Cheap
    no-op when everything matches — safe to call from the list endpoint.
    """
    items = read_manifest(server_dir)
    rows = session.exec(
        select(ContentItem).where(ContentItem.server_id == server_id)
    ).all()
    manifest_state = {
        (
            i["id"],
            i.get("version_id"),
            i.get("channel", "release"),
            i.get("enabled", True),
            i["filename"],
        )
        for i in items
    }
    row_state = {
        (r.id, r.version_id, r.channel, r.enabled, r.filename) for r in rows
    }
    if manifest_state != row_state:
        _sync_rows(session, server_id, items)


# --- resolution ------------------------------------------------------------


def _side(project: dict) -> str:
    client = project.get("client_side") != "unsupported"
    server = project.get("server_side") != "unsupported"
    if client and server:
        return "both"
    return "client" if client else "server"


def _build_item(
    project: dict, version: dict, file: dict, *, loader: str | None,
    mc_version: str, channel: str,
) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "kind": project.get("project_type", "mod"),
        "source": "modrinth",
        "project_id": version["project_id"],
        "version_id": version["id"],
        "version_number": version.get("version_number"),
        "slug": project.get("slug"),
        "name": project.get("title") or project.get("slug") or version["project_id"],
        "filename": file["filename"],
        "side": _side(project),
        "sha512": (file.get("hashes") or {}).get("sha512"),
        "game_version": mc_version,
        "loader": loader,
        "channel": channel,
        "enabled": True,
    }


async def _resolve_install_set(
    source: Any,
    project_ref: str,
    *,
    version_id: str | None,
    loader: str | None,
    mc_version: str,
    channel: str,
    include_optional: bool,
    skip_projects: set[str],
) -> list[tuple[dict, dict]]:
    """The requested project + its dependency closure, as (project, version)
    pairs. ``skip_projects`` (already-installed ids) prune the walk; a
    processed set guards cycles. Dependency names come from one bulk call.

    The loader facet only applies to mods/plugins — resource packs are
    loaderless on Modrinth (only the game version filters them), and a mod
    can't be installed at all without a loader (vanilla server)."""
    root_project = await source.get_project(project_ref)
    root_id = root_project["id"]

    kind = root_project.get("project_type", "mod")
    if kind in ("mod", "plugin"):
        if loader is None:
            raise ContentError(
                f"{root_project.get('title', project_ref)} is a {kind} — "
                "this server type cannot load it"
            )
    else:
        loader = None

    versions = await source.list_versions(root_id, loader=loader, mc_version=mc_version)
    if version_id is not None:
        root_version = next((v for v in versions if v["id"] == version_id), None)
        if root_version is None:
            raise ContentError(
                f"Version {version_id} not found for {project_ref} "
                f"(loader={loader}, mc={mc_version})"
            )
    else:
        root_version = modrinth.select_version(versions, channel)
        if root_version is None:
            raise ContentError(
                f"No compatible version of {project_ref} for {loader} {mc_version}"
            )

    resolved: list[tuple[dict, dict]] = [(root_project, root_version)]
    processed: set[str] = {root_id} | skip_projects

    # Breadth-first dependency walk; queue holds project ids to resolve.
    queue = [
        pid
        for pid in modrinth.dependency_ids(root_version, include_optional=include_optional)
        if pid not in processed
    ]
    while queue:
        batch, queue = queue, []
        processed.update(batch)
        projects = {p["id"]: p for p in await source.get_projects(batch)}
        for pid in batch:
            project = projects.get(pid)
            if project is None:
                continue  # unavailable dependency — skip, don't fail the batch
            dep_versions = await source.list_versions(
                pid, loader=loader, mc_version=mc_version
            )
            dep_version = modrinth.select_version(dep_versions, channel)
            if dep_version is None:
                continue  # no compatible build; surfaceable later, not fatal
            resolved.append((project, dep_version))
            queue.extend(
                dpid
                for dpid in modrinth.dependency_ids(
                    dep_version, include_optional=include_optional
                )
                if dpid not in processed and dpid not in queue
            )
    return resolved


# --- operations ------------------------------------------------------------


async def install(
    session: Session,
    server_id: str,
    server_dir: Path,
    *,
    project_id: str,
    source_key: str = "modrinth",
    version_id: str | None = None,
    loader: str | None,
    mc_version: str,
    channel: str = "release",
    include_optional_deps: bool = False,
) -> list[ContentItem]:
    """Install a project (+ dependency closure) and return the affected rows.

    Re-installing an already-present project replaces its file/version in
    place (row id and enabled state survive).
    """
    source = get_source(source_key)
    async with _lock(server_id):
        items = read_manifest(server_dir)
        installed_projects = {
            i["project_id"] for i in items if i.get("project_id")
        }
        resolved = await _resolve_install_set(
            source,
            project_id,
            version_id=version_id,
            loader=loader,
            mc_version=mc_version,
            channel=channel,
            include_optional=include_optional_deps,
            # The requested project itself may be reinstalled; deps that are
            # already present are skipped.
            skip_projects=installed_projects,
        )

        by_project = {i.get("project_id"): i for i in items if i.get("project_id")}
        affected_ids: list[str] = []
        for project, version in resolved:
            file = modrinth.primary_file(version)
            if file is None:
                raise ContentError(f"{project.get('title')}: version has no files")
            # Store the loader the item was actually resolved under — None
            # for loaderless kinds (resource packs), whatever the server is.
            item_loader = (
                loader
                if project.get("project_type", "mod") in ("mod", "plugin")
                else None
            )
            new_item = _build_item(
                project, version, file,
                loader=item_loader, mc_version=mc_version, channel=channel,
            )
            old_item = by_project.get(new_item["project_id"])
            if old_item is not None:
                # Replace in place: keep identity + enabled state, drop old file.
                new_item["id"] = old_item["id"]
                new_item["enabled"] = old_item.get("enabled", True)
                new_item["channel"] = channel if project is resolved[0][0] else old_item.get("channel", channel)
                _delete_item_file(server_dir, old_item)
                items[items.index(old_item)] = new_item
            else:
                items.append(new_item)
            await download_file(
                file["url"], item_file(server_dir, new_item),
                expected_hash=new_item["sha512"],
            )
            affected_ids.append(new_item["id"])

        write_manifest(server_dir, items)
        _sync_rows(session, server_id, items)
    rows = session.exec(
        select(ContentItem).where(ContentItem.server_id == server_id)
    ).all()
    return [r for r in rows if r.id in affected_ids]


def _find(items: list[dict], item_id: str) -> dict:
    for item in items:
        if item["id"] == item_id:
            return item
    raise ContentError("Content item not found")


async def patch_item(
    session: Session,
    server_id: str,
    server_dir: Path,
    item_id: str,
    *,
    enabled: bool | None = None,
    channel: str | None = None,
) -> ContentItem:
    """Enable/disable (rename on disk) and/or change the release channel."""
    async with _lock(server_id):
        items = read_manifest(server_dir)
        item = _find(items, item_id)
        if enabled is not None and enabled != item.get("enabled", True):
            old_path = item_file(server_dir, item)
            item["enabled"] = enabled
            new_path = item_file(server_dir, item)
            if old_path.exists():
                old_path.rename(new_path)
        if channel is not None:
            item["channel"] = channel
        write_manifest(server_dir, items)
        _sync_rows(session, server_id, items)
    row = session.get(ContentItem, item_id)
    assert row is not None
    return row


async def remove(
    session: Session, server_id: str, server_dir: Path, item_id: str
) -> None:
    """Uninstall: delete the file (either variant), the manifest entry, the row."""
    async with _lock(server_id):
        items = read_manifest(server_dir)
        item = _find(items, item_id)
        _delete_item_file(server_dir, item)
        items.remove(item)
        write_manifest(server_dir, items)
        _sync_rows(session, server_id, items)


async def check_updates(server_dir: Path, *, mc_version: str) -> list[dict]:
    """Installed items with a newer qualifying version. Returns
    ``[{item, new_version}]`` (manifest item + Modrinth version dict).
    Each item is checked with the loader it was installed under (``None``
    for loaderless kinds like resource packs)."""
    results: list[dict] = []
    for item in read_manifest(server_dir):
        if not item.get("project_id") or not item.get("version_id"):
            continue  # generated/uploaded file — nothing to check against
        source = get_source(item["source"])
        if not hasattr(source, "check_update"):
            continue  # e.g. vanillatweaks — regenerate instead of update
        newest = await source.check_update(
            item["project_id"],
            item["version_id"],
            loader=item.get("loader"),
            mc_version=mc_version,
            channel=item.get("channel", "release"),
        )
        if newest is not None:
            results.append({"item": item, "new_version": newest})
    return results


async def apply_update(
    session: Session,
    server_id: str,
    server_dir: Path,
    item_id: str,
    *,
    mc_version: str,
) -> ContentItem:
    """Swap an installed item's file for the newest qualifying version
    (checked under the loader the item was installed with)."""
    async with _lock(server_id):
        items = read_manifest(server_dir)
        item = _find(items, item_id)
        if not item.get("project_id"):
            raise ContentError("Item has no source project — cannot update")
        source = get_source(item["source"])
        if not hasattr(source, "check_update"):
            raise ContentError("This item is regenerated, not updated")
        newest = await source.check_update(
            item["project_id"],
            item["version_id"] or "",
            loader=item.get("loader"),
            mc_version=mc_version,
            channel=item.get("channel", "release"),
        )
        if newest is None:
            raise ContentError("Already up to date")
        file = modrinth.primary_file(newest)
        if file is None:
            raise ContentError("New version has no files")
        _delete_item_file(server_dir, item)
        item.update(
            version_id=newest["id"],
            version_number=newest.get("version_number"),
            filename=file["filename"],
            sha512=(file.get("hashes") or {}).get("sha512"),
        )
        await download_file(
            file["url"], item_file(server_dir, item), expected_hash=item["sha512"]
        )
        write_manifest(server_dir, items)
        _sync_rows(session, server_id, items)
    row = session.get(ContentItem, item_id)
    assert row is not None
    return row
