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
  up (and restart it after).

Backing up a *running* server without stopping it brackets the archive: the
console ``save-off`` stops automatic saving, the world is flushed to disk
(``minecraft:server/save [true]`` through the management protocol when the
client is connected, which answers once the save has finished; else the
console ``save-all flush`` plus a short grace period), the zip is written, and
``save-on`` always follows, even when the archive fails. A running proxy
(Velocity) has no world and none of these commands: it is archived as is. An operator's own
``save-off`` (read through the protocol) is left alone, and backups of the
same server take turns so one's ``save-on`` never lands during another's zip.
The ``backup_created`` event says how the world was flushed. Files can still
change during the zip (player data on logout), so ``session.lock`` and
unreadable files are skipped rather than failing the backup.

Restore is the dangerous direction, so it is deliberately conservative:
the server must be stopped; the archive is validated first; the current
directory is moved aside and only deleted after a fully successful extract
(on any failure the original directory is put back). Extraction is
zip-slip guarded, and an archive whose uncompressed total would not fit on
the disk (zip bomb, or simply too big) is refused before anything moves.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import weakref
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from . import app_settings, events
from .config import get_settings
from .models import Backup, Server
from .servers import msmp
from .servers.types import is_proxy_type

log = logging.getLogger(__name__)

# How long a protocol ``server/save`` may take (a big world on a slow disk).
_SAVE_TIMEOUT = 120.0
# The console ``save-all flush`` gives no completion signal Lectern waits on;
# this pause lets a small world finish writing before the zip starts.
_CONSOLE_FLUSH_GRACE = 2.0
# One save bracket per server at a time (see ``_bracket_lock``). Weak values:
# a lock lives only while a backup holds or waits on it, so ids of deleted
# servers leave nothing behind.
_bracket_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)

FLUSHED_MSMP = "flushed via management protocol"
FLUSHED_CONSOLE = "flushed via console"
NOT_FLUSHED = "not flushed"


class BackupError(Exception):
    """Invalid backup operation (running server, missing archive, ...)."""


def _backups_dir(server_id: str) -> Path:
    return get_settings().backups_dir / server_id


def _excluded_prefixes(server: Server) -> list[str]:
    return [p.strip() for p in (server.backup_excluded or "").split(",") if p.strip()]


def _is_excluded(rel: Path, prefixes: list[str]) -> bool:
    text = rel.as_posix()
    return any(text == p or text.startswith(p + "/") for p in prefixes)


def _is_upload_part(rel: Path) -> bool:
    """A staged file-manager upload: ``.lectern/<tmp>.part`` (see api/upload.py)."""
    return len(rel.parts) == 2 and rel.parts[0] == ".lectern" and rel.suffix == ".part"


def sweep_stale_parts(session: Session) -> int:
    """Startup only (no request can be streaming yet): delete ``.part`` files a
    killed process left behind — staged uploads in each server's ``.lectern/``
    (hidden from and undeletable via the file manager) and unfinished archives
    or uploads in the backups dirs. Returns how many were removed."""
    dirs = [d for d in get_settings().backups_dir.glob("*") if d.is_dir()]
    for server in session.exec(select(Server)).all():
        if server.path:
            dirs.append(Path(server.path) / ".lectern")
    removed = 0
    for d in dirs:
        if d.is_symlink() or not d.is_dir():
            continue
        for part in d.glob("*.part"):
            if part.is_file() and not part.is_symlink():
                part.unlink(missing_ok=True)
                removed += 1
    return removed


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
                if _is_upload_part(rel):
                    continue  # a file-manager upload in flight (or orphaned)
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


def _bracket_lock(server_id: str) -> asyncio.Lock:
    """Backups of one running server take turns: an overlapping second
    backup's zip would otherwise run after the first one's ``save-on``."""
    return _bracket_locks.setdefault(server_id, asyncio.Lock())


def _client(server_id: str) -> msmp.MsmpClient | None:
    """The process's connected management client, or None."""
    from .servers.manager import manager  # late import (cycle)

    proc = manager.get_process(server_id)
    client = proc.msmp if proc is not None else None
    return client if client is not None and client.connected else None


async def _autosave_off(server_id: str) -> bool:
    """True when the server reports automatic saving already off: an
    operator's deliberate ``save-off``, which the backup must not undo.
    Without a client (or on any error) Lectern cannot tell and assumes on."""
    client = _client(server_id)
    if client is None:
        return False
    try:
        return await client.call(msmp.M_AUTOSAVE) is False
    except msmp.MsmpError as exc:
        log.info("backup of %s: autosave state unknown (%s)", server_id, exc)
        return False


async def _flush_running(server_id: str) -> str:
    """Write the world to disk before the zip and say how: the protocol's
    ``server/save [true]`` answers once the save and its flush are done; the
    console ``save-all flush`` gets a fixed grace period instead."""
    from .servers.manager import ManagerError, manager  # late import (cycle)

    client = _client(server_id)
    if client is not None:
        try:
            if await client.call(msmp.M_SAVE, [True], timeout=_SAVE_TIMEOUT) is True:
                return FLUSHED_MSMP
            log.info("backup of %s: server/save did not confirm, using the console", server_id)
        except msmp.MsmpError as exc:
            log.info("backup of %s: server/save failed (%s), using the console", server_id, exc)
    try:
        await manager.send(server_id, "save-all flush")
    except ManagerError:
        return NOT_FLUSHED
    await asyncio.sleep(_CONSOLE_FLUSH_GRACE)
    return FLUSHED_CONSOLE


async def create_backup(
    session: Session, server: Server, *, trigger: str = "manual"
) -> Backup:
    """Archive the server now and prune old backups past the retention limit.
    A running server is either stopped and restarted around the archive
    (``backup_stop_server``) or kept up with saving bracketed (module doc)."""
    if not server.path:
        raise BackupError("Server is not installed yet")
    server_dir = Path(server.path)
    from .servers.manager import manager  # late import (cycle)

    was_running = manager.is_running(server.id)
    if not was_running or server.backup_stop_server or is_proxy_type(server.type):
        return await _create(session, server, server_dir, trigger, was_running)
    async with _bracket_lock(server.id):
        # Ask again once it is our turn: the server may have stopped while an
        # earlier backup held the lock, and a stopped world needs no bracket.
        return await _create(
            session, server, server_dir, trigger, manager.is_running(server.id)
        )


async def _create(
    session: Session, server: Server, server_dir: Path, trigger: str, was_running: bool
) -> Backup:
    # Optionally take the server down for a consistent snapshot.
    from .servers.manager import ManagerError, manager  # late import (cycle)

    # A proxy has no world to flush and no save commands to send.
    bracket = (
        was_running and not server.backup_stop_server and not is_proxy_type(server.type)
    )
    if was_running and server.backup_stop_server:
        try:
            await manager.stop(server.id)
            while manager.is_running(server.id):
                await asyncio.sleep(0.5)
        except ManagerError as exc:
            events.record(
                server.id, "backup_failed", f"could not stop server: {exc}"
            )
            raise BackupError(f"Could not stop server for backup: {exc}") from exc

    filename = _new_filename()
    dest = _backups_dir(server.id) / filename
    saving_off = False
    flushed: str | None = None
    try:
        if bracket:
            try:
                if not await _autosave_off(server.id):
                    await manager.send(server.id, "save-off")
                    saving_off = True
                flushed = await _flush_running(server.id)
            except ManagerError:
                flushed = NOT_FLUSHED  # stopped meanwhile: archive what is on disk
        size = await asyncio.to_thread(
            _write_archive,
            server_dir,
            dest,
            prefixes=_excluded_prefixes(server),
            compress=server.backup_compress,
        )
    except Exception as exc:
        events.record(server.id, "backup_failed", str(exc))
        raise
    finally:
        if saving_off:
            # Stopped meanwhile: saving is back on with the next start anyway.
            with contextlib.suppress(ManagerError):
                await manager.send(server.id, "save-on")
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
    how = trigger if flushed is None else f"{trigger}, {flushed}"
    events.record(server.id, "backup_created", f"{filename} ({how})")
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


# --- upload ---------------------------------------------------------------------


def _new_filename() -> str:
    # Microseconds keep filenames unique even for back-to-back backups —
    # a same-second collision would silently overwrite the previous archive.
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f") + ".zip"


def upload_staging_dir(server_id: str) -> Path:
    """Where an upload is streamed before validation: the server's own backups
    dir (same filesystem, so adopting it is an atomic rename)."""
    return _backups_dir(server_id)  # created by the upload helper once headers pass


# Root-level entries that mark a whole-server archive. Restore replaces the
# entire server dir, so a world-only or folder-wrapped zip would wipe the jar,
# mods, configs and manifest; upload refuses those shapes up front.
SERVER_ROOT_MARKERS = frozenset({"server.properties", "eula.txt", "velocity.toml", ".lectern"})


def _has_root_marker(names: list[str], prefix: str = "") -> bool:
    return any(
        name.startswith(prefix) and name[len(prefix):].split("/", 1)[0] in SERVER_ROOT_MARKERS
        for name in names
    )


def _validate_upload(path: Path) -> str:
    """Restore's structural definition of a safe archive, plus: it must hold
    files and be shaped like a whole-server archive (a marker such as
    ``server.properties`` at the root). Returns the folder prefix to strip:
    ``""`` when the files already sit at the root, ``"name/"`` when the whole
    server is wrapped in one folder, as most desktop zip tools produce when
    an unpacked backup is zipped again. No disk-space check: uploading never
    unpacks, and restore runs the zip-bomb guard against the server's disk."""
    _validate_archive(path)
    with zipfile.ZipFile(path) as zf:
        files = [info.filename for info in zf.infolist() if not info.is_dir()]
    if not files:
        raise BackupError("Backup archive is empty")
    if _has_root_marker(files):
        return ""
    # macOS "Compress" adds a __MACOSX/ resource-fork tree beside the folder.
    tops = {name.split("/", 1)[0] for name in files} - {"__MACOSX"}
    if len(tops) == 1:
        prefix = f"{next(iter(tops))}/"
        if _has_root_marker(files, prefix):
            return prefix
    raise BackupError(
        "Backup archive is not a whole-server archive: no server.properties, eula.txt, "
        "velocity.toml or .lectern/ at its root (restore would replace the whole server)"
    )


def _unwrap_archive(src: Path, dest: Path, prefix: str, *, compress: bool) -> int:
    """Re-pack ``src`` into ``dest`` with ``prefix`` stripped from every entry
    and any ``__MACOSX/`` tree dropped, so the stored backup has the server
    files at its root like every other one (blocking — run in a thread).
    Entries are streamed one at a time; multi-GB archives never sit in memory.
    Returns the new archive's size in bytes."""
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    tmp = dest.with_suffix(".part")
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", method) as zout:
            for info in zin.infolist():
                if info.is_dir() or info.filename.startswith("__MACOSX/"):
                    continue
                if not info.filename.startswith(prefix):
                    raise BackupError(f"Entry outside the wrapping folder: {info.filename!r}")
                out = zipfile.ZipInfo(info.filename[len(prefix):], date_time=info.date_time)
                out.compress_type = method
                with zin.open(info) as fin, zout.open(out, "w") as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest.stat().st_size


async def import_backup(
    session: Session, server: Server, tmp_path: Path, *, size: int
) -> Backup:
    """Adopt an uploaded archive (already streamed to ``tmp_path`` inside this
    server's backups dir) as a backup: validate it, unwrap it if the server
    files sit in one top-level folder, give it a timestamped name, record it
    and prune. ``size`` is the uploaded byte count; a re-packed archive is
    recorded with its own size. The temp file is always removed."""
    try:
        if not server.path:
            raise BackupError("Server is not installed yet")
        dest_dir = _backups_dir(server.id)
        if not tmp_path.resolve().is_relative_to(dest_dir.resolve()):
            raise BackupError("Uploaded archive must be staged in the backups directory")
        if dest_dir.resolve().is_relative_to(Path(server.path).resolve()):
            raise BackupError("Backup destination must be outside the server directory")
        prefix = await asyncio.to_thread(_validate_upload, tmp_path)
        filename = _new_filename()
        if prefix:
            size = await asyncio.to_thread(
                _unwrap_archive,
                tmp_path,
                dest_dir / filename,
                prefix,
                compress=server.backup_compress,
            )
        else:
            tmp_path.replace(dest_dir / filename)
    except Exception as exc:
        events.record(server.id, "backup_failed", f"upload: {exc}")
        raise
    finally:
        tmp_path.unlink(missing_ok=True)

    backup = Backup(
        server_id=server.id, filename=filename, size_bytes=size, trigger="uploaded"
    )
    session.add(backup)
    session.commit()
    session.refresh(backup)
    _prune(session, server)
    how = f"uploaded, unwrapped from {prefix}" if prefix else "uploaded"
    events.record(server.id, "backup_created", f"{filename} ({how})")
    return backup


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


def _validate_archive(
    path: Path, extract_to: Path | None = None, *, reserve: int = 0
) -> None:
    """One definition of a safe archive, shared by restore and upload.

    With ``extract_to``, also the zip-bomb guard: the total uncompressed size
    must fit in that filesystem's free space minus ``reserve`` bytes (the
    ``restore_disk_reserve_mb`` app setting, passed by ``restore_backup``).
    ``testzip`` has already proven the declared sizes honest by then."""
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
        if extract_to is not None:
            total = sum(info.file_size for info in zf.infolist())
            free = shutil.disk_usage(extract_to).free
            if total > free - reserve:
                raise BackupError(
                    f"Backup unpacks to {total} bytes, more than the disk can hold "
                    f"({free} bytes free, {reserve} kept in reserve)"
                )


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
    reserve = app_settings.get_int(session, "restore_disk_reserve_mb") * 1024 * 1024
    # testzip() reads every byte: keep multi-GB archives off the event loop.
    await asyncio.to_thread(_validate_archive, archive, Path(server.path), reserve=reserve)
    try:
        await asyncio.to_thread(_swap_restore, archive, Path(server.path))
    except Exception as exc:
        events.record(server.id, "backup_failed", f"restore of {backup.filename}: {exc}")
        raise
    events.record(server.id, "backup_restored", backup.filename)
