"""Import an existing Minecraft world into a server (creation-only).

Accepts a world archive (uploaded ``.zip`` or downloaded from a URL) and
extracts its world folder into the server directory, so a freshly-created
server starts on an existing map instead of generating a new one.

World zips in the wild put ``level.dat`` either at the archive root or nested
one folder deep (``MyWorld/level.dat`` — how most map sites package them). We
locate the **shallowest** ``level.dat``, treat its directory as the world root,
and extract that subtree into ``{server_dir}/{level-name}`` (default ``world``),
stripping the wrapper folder. Every member is zip-slip guarded; the world is
built in a staging dir and swapped in at the end, so a corrupt archive can
never leave a half-written world behind.
"""

from __future__ import annotations

import fnmatch
import shutil
import zipfile
from pathlib import Path

LEVEL_DAT = "level.dat"

# Excluded by default: Distant Horizons stores a multi-gigabyte LOD cache
# (``DistantHorizons.sqlite`` + ``-wal``/``-shm``) inside the world — pure
# cache, regenerated on demand, and the usual reason a world zip balloons.
DEFAULT_EXCLUDES = ["*DistantHorizons*"]


def _matches(rel_posix: str, patterns: list[str]) -> bool:
    """True if the world-relative path matches any exclude pattern. A pattern
    is tried against the whole path and against each path segment, so
    ``*DistantHorizons*`` catches both a file and a folder anywhere in the
    tree, and ``*.sqlite`` catches the file by name."""
    parts = rel_posix.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel_posix, pat) or any(
            fnmatch.fnmatch(part, pat) for part in parts
        ):
            return True
    return False


class WorldImportError(Exception):
    """The archive isn't a usable Minecraft world (no ``level.dat``, unsafe
    path, or not a zip)."""


def find_world_root(names: list[str]) -> str | None:
    """The prefix (a directory path within the zip, ``""`` at the archive root)
    whose direct child is ``level.dat``, choosing the shallowest such directory.
    ``None`` when no ``level.dat`` is present.

    A ``level.dat`` at the root returns ``""``; ``MyMap/level.dat`` returns
    ``"MyMap/"``; deeper matches lose to shallower ones so a backup world buried
    in ``world/DIM1/…`` never wins over the top-level ``world``.
    """
    matches = []
    for raw in names:
        name = raw.replace("\\", "/")
        if name == LEVEL_DAT or name.endswith("/" + LEVEL_DAT):
            matches.append(name)
    if not matches:
        return None
    shallowest = min(matches, key=lambda n: n.count("/"))
    return shallowest[: -len(LEVEL_DAT)]  # keeps the trailing slash, or "" at root


def extract_world(
    zip_path: Path,
    server_dir: Path,
    *,
    level_name: str = "world",
    exclude: list[str] | None = None,
) -> tuple[int, int]:
    """Extract the world in ``zip_path`` into ``{server_dir}/{level_name}``,
    replacing any existing world there. Returns ``(written, skipped)`` file
    counts — ``skipped`` are members matched by an ``exclude`` pattern (world-
    relative, glob), e.g. Distant Horizons LOD caches.

    Raises ``WorldImportError`` if the archive isn't a zip, has no ``level.dat``,
    contains a traversal path, or the resolved target escapes ``server_dir``.
    """
    patterns = exclude if exclude is not None else DEFAULT_EXCLUDES
    server_dir = server_dir.resolve()
    target = (server_dir / level_name).resolve()
    # level-name is normally "world", but it comes from server.properties which
    # a user could set to a traversal — confine the target to the server dir.
    if server_dir != target.parent and server_dir not in target.parents:
        raise WorldImportError("Invalid world location")

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise WorldImportError("The world file is not a valid .zip archive") from exc

    with zf:
        prefix = find_world_root(zf.namelist())
        if prefix is None:
            raise WorldImportError(
                "Not a Minecraft world — no level.dat found in the archive"
            )

        staging = target.with_name(target.name + ".importing")
        shutil.rmtree(staging, ignore_errors=True)
        staging_root = staging.resolve()
        written = 0
        skipped = 0
        try:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if not name.startswith(prefix):
                    continue
                rel = name[len(prefix):]
                if not rel:
                    continue
                if _matches(rel, patterns):
                    skipped += 1
                    continue
                rel_path = Path(rel)
                if rel_path.is_absolute() or ".." in rel_path.parts:
                    raise WorldImportError(f"Unsafe path in archive: {info.filename!r}")
                dest = (staging / rel_path).resolve()
                if dest != staging_root and staging_root not in dest.parents:
                    raise WorldImportError(f"Unsafe path in archive: {info.filename!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Stream large members instead of reading them whole into memory
                # (Distant Horizons regions and .mca files can be hundreds of MB).
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, length=1 << 20)
                written += 1
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    # Swap in: drop the old world, move staging into place. (Kept out of the
    # try/except above so a failure never deletes the existing world.)
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return written, skipped
