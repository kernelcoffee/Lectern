"""Backups (M9): create / list / restore / prune server archives.

Archives are zips of the whole server directory, written to
``backups/{server_id}/{timestamp}.zip`` — **outside** the server dir by
construction, and guarded against ever pointing inside it. Per-server
settings live on the ``Server`` row (docs/implementation.md M9: one config
per server, not Crafty's named multi-configs):

- ``backup_excluded`` — comma-separated path prefixes (relative to the
  server dir) skipped when archiving, e.g. ``logs,crash-reports``.
- ``backup_max`` — retention: oldest backups beyond this count are pruned
  after every successful create.
- ``backup_compress`` — deflate vs store (uncompressed is faster on the
  Pi-class boxes Lectern targets; worlds compress ~50%).
- ``backup_stop_server`` — gracefully stop a running server before backing
  up (and restart it after). Backing up a *running* server otherwise is
  crash-consistent at best: Minecraft may be mid-write, so ``session.lock``
  and unreadable files are skipped rather than failing the backup.

Restore is the dangerous direction, so it is deliberately conservative:
the server must be stopped; the archive is validated first; the current
directory is moved aside and only deleted after a fully successful extract
(on any failure the original directory is put back). Extraction is
zip-slip guarded.
"""

from __future__ import annotations

import asyncio
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from .config import get_settings
from .models import Backup, Server


class BackupError(Exception):
    """Invalid backup operation (running server, missing archive, ...)."""


def _backups_dir(server_id: str) -> Path:
    return get_settings().backups_dir / server_id


def _excluded_prefixes(server: Server) -> list[str]:
    return [p.strip() for p in (server.backup_excluded or "").split(",") if p.strip()]


def _is_excluded(rel: Path, prefixes: list[str]) -> bool:
    text = rel.as_posix()
    return any(text == p or text.startswith(p + "/") for p in prefixes)


# --- create -------------------------------------------------------------------


def _write_archive(server_dir: Path, dest: Path, *, prefixes: list[str], compress: bool) -> int:
    """Zip ``server_dir`` into ``dest`` (blocking — run in a thread).
    Returns the archive size in bytes."""
    if dest.resolve().is_relative_to(server_dir.resolve()):
        raise BackupError("Backup destination must be outside the server directory")
    dest.parent.mkdir(parents=True, exist_ok=True)
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    tmp = dest.with_suffix(".part")
    try:
        with zipfile.ZipFile(tmp, "w", method) as zf:
            for path in sorted(server_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(server_dir)
                if _is_excluded(rel, prefixes) or rel.name == "session.lock":
                    continue
                try:
                    zf.write(path, rel.as_posix())
                except OSError:
                    # A file vanished or is unreadable mid-walk (running
                    # server) — skip it rather than fail the whole backup.
                    continue
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest.stat().st_size


async def create_backup(
    session: Session, server: Server, *, trigger: str = "manual"
) -> Backup:
    """Archive the server now (optionally stop/restart around it) and prune
    old backups past the retention limit."""
    if not server.path:
        raise BackupError("Server is not installed yet")
    server_dir = Path(server.path)

    # Optionally take the server down for a consistent snapshot.
    from .servers.manager import ManagerError, manager  # late import (cycle)

    was_running = manager.is_running(server.id)
    if was_running and server.backup_stop_server:
        try:
            await manager.stop(server.id)
            while manager.is_running(server.id):
                await asyncio.sleep(0.5)
        except ManagerError as exc:
            raise BackupError(f"Could not stop server for backup: {exc}") from exc

    # Microseconds keep filenames unique even for back-to-back backups —
    # a same-second collision would silently overwrite the previous archive.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    filename = f"{timestamp}.zip"
    dest = _backups_dir(server.id) / filename
    try:
        size = await asyncio.to_thread(
            _write_archive,
            server_dir,
            dest,
            prefixes=_excluded_prefixes(server),
            compress=server.backup_compress,
        )
    finally:
        if was_running and server.backup_stop_server:
            try:
                await manager.start(server.id)
            except ManagerError:
                pass  # backup outcome matters more; the UI shows status

    backup = Backup(
        server_id=server.id, filename=filename, size_bytes=size, trigger=trigger
    )
    session.add(backup)
    session.commit()
    session.refresh(backup)
    _prune(session, server)
    return backup


def _prune(session: Session, server: Server) -> None:
    rows = session.exec(
        select(Backup)
        .where(Backup.server_id == server.id)
        .order_by(Backup.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    for stale in rows[max(server.backup_max, 1):]:
        (_backups_dir(server.id) / stale.filename).unlink(missing_ok=True)
        session.delete(stale)
    session.commit()


# --- list / delete ---------------------------------------------------------------


def list_backups(session: Session, server_id: str) -> list[Backup]:
    return list(
        session.exec(
            select(Backup)
            .where(Backup.server_id == server_id)
            .order_by(Backup.created_at.desc())  # type: ignore[attr-defined]
        ).all()
    )


def backup_path(backup: Backup) -> Path:
    """On-disk path of a backup archive (for downloads)."""
    return _backups_dir(backup.server_id) / backup.filename


def delete_backup(session: Session, backup: Backup) -> None:
    (_backups_dir(backup.server_id) / backup.filename).unlink(missing_ok=True)
    session.delete(backup)
    session.commit()


# --- restore ---------------------------------------------------------------------


def _validate_archive(path: Path) -> None:
    if not path.exists():
        raise BackupError("Backup archive is missing on disk")
    if not zipfile.is_zipfile(path):
        raise BackupError("Backup archive is corrupt (not a zip)")
    with zipfile.ZipFile(path) as zf:
        if zf.testzip() is not None:
            raise BackupError("Backup archive is corrupt (CRC mismatch)")
        for name in zf.namelist():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts:
                raise BackupError(f"Backup contains an unsafe path: {name!r}")


def _swap_restore(archive: Path, server_dir: Path) -> None:
    """Extract ``archive`` as the new server dir; the old contents are moved
    aside first and put back if anything fails (blocking — thread)."""
    aside = server_dir.with_name(server_dir.name + ".pre-restore")
    if aside.exists():
        shutil.rmtree(aside)
    server_dir.rename(aside)
    try:
        server_dir.mkdir()
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(server_dir)  # paths validated in _validate_archive
    except BaseException:
        shutil.rmtree(server_dir, ignore_errors=True)
        aside.rename(server_dir)
        raise
    shutil.rmtree(aside)


async def restore_backup(session: Session, server: Server, backup: Backup) -> None:
    """Replace the server directory with the archive's contents."""
    from .servers.manager import manager  # late import (cycle)

    if manager.is_running(server.id):
        raise BackupError("Stop the server before restoring a backup")
    if not server.path:
        raise BackupError("Server is not installed yet")
    archive = _backups_dir(server.id) / backup.filename
    _validate_archive(archive)
    await asyncio.to_thread(_swap_restore, archive, Path(server.path))
