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

import shutil
import zipfile
from pathlib import Path

LEVEL_DAT = "level.dat"


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
    zip_path: Path, server_dir: Path, *, level_name: str = "world"
) -> int:
    """Extract the world in ``zip_path`` into ``{server_dir}/{level_name}``,
    replacing any existing world there. Returns the number of files written.

    Raises ``WorldImportError`` if the archive isn't a zip, has no ``level.dat``,
    contains a traversal path, or the resolved target escapes ``server_dir``.
    """
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
        count = 0
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
                rel_path = Path(rel)
                if rel_path.is_absolute() or ".." in rel_path.parts:
                    raise WorldImportError(f"Unsafe path in archive: {info.filename!r}")
                dest = (staging / rel_path).resolve()
                if dest != staging_root and staging_root not in dest.parents:
                    raise WorldImportError(f"Unsafe path in archive: {info.filename!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(info))
                count += 1
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    # Swap in: drop the old world, move staging into place. (Kept out of the
    # try/except above so a failure never deletes the existing world.)
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return count
