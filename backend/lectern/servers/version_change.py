"""Change an existing server's Minecraft version (M9.5, F-SM-9).

Re-provisions the server jar / loader build and the matching Java runtime for a
new Minecraft version (via ``install.provision``), then migrates installed
content against it:

- **Modrinth** items are re-resolved under the new game version on the same
  loader + release channel. A compatible build → the file is swapped in place
  (identity + enabled state preserved); no build → the item is disabled and
  reported so the world can still boot.
- **Vanilla Tweaks** sets are regenerated (their version string is major.minor,
  so an upgrade across minors needs a fresh pack).
- **Uploads** and **mrpack** files are left untouched and reported as "kept" —
  an uploaded pack has no source to re-resolve, and a modpack is upgraded by
  re-importing the pack for the new version.

The world itself is upgraded **by Minecraft, in place and one-way**, at the next
start; the caller (endpoint) offers a pre-change backup. Downgrades are not
supported by Minecraft — selecting an older version is refused unless the caller
passes ``allow_downgrade`` (the supported path back is restoring a backup).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session

from ..models import Server, ServerStatus
from ..providers import modrinth, mojang
from ..providers.base import download_file
from . import install
from .manager import manager


class VersionChangeError(Exception):
    """Invalid version-change request (running server, downgrade, …)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class MigrationReport:
    """What happened to each installed item during the version change."""

    updated: list[str] = field(default_factory=list)      # re-resolved to a new build
    incompatible: list[str] = field(default_factory=list)  # disabled — no build
    regenerated: list[str] = field(default_factory=list)   # VT sets rebuilt
    kept: list[str] = field(default_factory=list)          # uploads / modpack files


def is_downgrade(current: str, target: str, versions: list[str]) -> bool:
    """True if ``target`` is older than ``current`` per Mojang's manifest order
    (``versions`` is newest-first). Unknown versions (snapshots not in the
    release list) can't be ordered — treated as *not* a downgrade so the change
    isn't blocked on data we don't have."""
    order = {v: i for i, v in enumerate(versions)}
    if current not in order or target not in order:
        return False
    return order[target] > order[current]


async def change_version(
    session: Session,
    server: Server,
    *,
    mc_version: str,
    loader_version: str | None = None,
    allow_downgrade: bool = False,
    backup_first: bool = True,
) -> MigrationReport:
    """Move ``server`` to ``mc_version``. The server must be stopped. Runs an
    optional pre-change backup, re-provisions jar + Java, migrates content, and
    returns a report. Commits the record after provisioning."""
    if not server.path:
        raise VersionChangeError("Server is not installed yet", 409)
    if manager.is_running(server.id):
        raise VersionChangeError("Stop the server before changing its version", 409)

    # Downgrade guard — compare via the manifest's ordering, not string compare
    # (Minecraft can't downgrade a world; only a backup restore gets you back).
    releases = await mojang.list_release_versions()
    if not allow_downgrade and is_downgrade(server.mc_version, mc_version, releases):
        raise VersionChangeError(
            f"{mc_version} is older than {server.mc_version}. Minecraft cannot "
            "downgrade a world in place — pass allow_downgrade to proceed anyway "
            "(restore a pre-upgrade backup for the supported path back).",
            400,
        )

    if backup_first:
        from ..backups import create_backup

        await create_backup(session, server, trigger="manual")

    await install.provision(
        server, mc_version=mc_version, loader_version=loader_version
    )
    server.status = ServerStatus.stopped.value
    session.add(server)
    session.commit()
    session.refresh(server)

    return await _migrate_content(session, server, mc_version)


async def _migrate_content(
    session: Session, server: Server, mc_version: str
) -> MigrationReport:
    """Re-resolve installed content against ``mc_version``."""
    from ..content import manager as content
    from ..content import resourcepacks

    server_dir = Path(server.path)
    report = MigrationReport()

    # Modrinth items: re-resolve on the same loader + channel, swap or disable.
    # Held under the content lock so a concurrent install can't race the
    # manifest; VT regeneration takes the same lock, so it runs afterwards.
    vt_groups: dict[str, dict] = {}
    async with content._lock(server.id):
        items = content.read_manifest(server_dir)
        for item in items:
            source = item.get("source")
            if source == "vanillatweaks":
                # Group by type; the stored selection is regenerated below.
                vt_groups.setdefault(item.get("vt_type", "resourcepacks"), item)
                continue
            if source == "mrpack" or not item.get("project_id"):
                report.kept.append(item["name"])
                continue
            await _migrate_modrinth_item(server_dir, item, mc_version, report)
        content.write_manifest(server_dir, items)
        content._sync_rows(session, server.id, items)

    for pack_type, sample in vt_groups.items():
        packs = sample.get("vt_packs")
        if not packs:
            report.kept.append(sample["name"])
            continue
        try:
            await resourcepacks.install_vanillatweaks(
                session, server.id, server_dir,
                packs=packs, mc_version=mc_version, pack_type=pack_type,
            )
            report.regenerated.append(resourcepacks._TYPE_LABEL[pack_type])
        except resourcepacks.ResourcePackError:
            # VT may not have a build for the new version — leave the old set.
            report.kept.append(sample["name"])

    return report


async def _resolve_item_build(
    item: dict, mc_version: str
) -> tuple[dict, dict] | None:
    """Newest qualifying ``(version, file)`` of a Modrinth item under
    ``mc_version`` (same loader + channel), or ``None`` when the project has
    no compatible build. Shared by the migration and the preview."""
    versions = await modrinth.list_versions(
        item["project_id"], loader=item.get("loader"), mc_version=mc_version
    )
    newest = modrinth.select_version(versions, item.get("channel", "release"))
    file = modrinth.primary_file(newest) if newest is not None else None
    if newest is None or file is None:
        return None
    return newest, file


async def preview_migration(server: Server, mc_version: str) -> MigrationReport:
    """Dry-run of ``_migrate_content``: classify every installed item against
    ``mc_version`` without touching anything. Powers the pre-upgrade
    compatibility check in the UI — safe to call while the server runs."""
    from ..content import manager as content

    if not server.path:
        raise VersionChangeError("Server is not installed yet", 409)
    report = MigrationReport()
    vt_groups: dict[str, dict] = {}
    for item in content.read_manifest(Path(server.path)):
        source = item.get("source")
        if source == "vanillatweaks":
            vt_groups.setdefault(item.get("vt_type", "resourcepacks"), item)
            continue
        if source == "mrpack" or not item.get("project_id"):
            report.kept.append(item["name"])
            continue
        if await _resolve_item_build(item, mc_version) is not None:
            report.updated.append(item["name"])
        else:
            report.incompatible.append(item["name"])
    for pack_type, sample in vt_groups.items():
        from ..content import resourcepacks

        if sample.get("vt_packs"):
            report.regenerated.append(resourcepacks._TYPE_LABEL[pack_type])
        else:
            report.kept.append(sample["name"])
    return report


async def _migrate_modrinth_item(
    server_dir: Path, item: dict, mc_version: str, report: MigrationReport
) -> None:
    """Swap a Modrinth item to the newest build for ``mc_version`` (same loader
    + channel), or disable it when none exists. Mutates ``item`` in place."""
    from ..content import manager as content

    build = await _resolve_item_build(item, mc_version)
    if build is not None:
        newest, file = build
        content._delete_item_file(server_dir, item)
        item.update(
            version_id=newest["id"],
            version_number=newest.get("version_number"),
            filename=file["filename"],
            sha512=(file.get("hashes") or {}).get("sha512"),
            game_version=mc_version,
        )
        await download_file(
            file["url"], content.item_file(server_dir, item),
            expected_hash=item["sha512"],
        )
        report.updated.append(item["name"])
        return

    # No compatible build — disable so the server still boots (a disabled item
    # is already ignored; only rename when it was enabled).
    if item.get("enabled", True):
        old_path = content.item_file(server_dir, item)
        item["enabled"] = False
        item["game_version"] = mc_version
        new_path = content.item_file(server_dir, item)
        if old_path.exists():
            old_path.rename(new_path)
    else:
        item["game_version"] = mc_version
    report.incompatible.append(item["name"])
