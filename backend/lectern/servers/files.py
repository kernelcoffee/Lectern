"""In-server file browser/editor (M12).

The escape hatch for config Lectern doesn't model (mod ``config/``, ``ops.json``,
reading ``logs/``). Everything funnels through ``safe_path`` which **confines
every operation to the server directory**: it rejects absolute paths and ``..``
traversal, and — because it resolves symlinks — a link pointing outside the
tree resolves out of bounds and is refused too. Lectern's own ``.lectern/``
manifest dir is hidden from listings and protected from edits so the content
manifest can't be corrupted by hand.

Text files are read up to a size cap (bigger files, and anything that looks
binary, are flagged so the UI offers a download instead of a garbled editor).
Writes only land inside an existing directory (use mkdir first) — never
outside, never onto a directory.
"""

from __future__ import annotations

import shutil
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Files larger than this aren't loaded into the editor (download instead).
TEXT_EDIT_MAX = 1 * 1024 * 1024  # 1 MiB
# Cap on an uploaded file placed through the browser.
UPLOAD_MAX = 200 * 1024 * 1024  # 200 MiB

# Lectern's own bookkeeping — hidden and read-only through the file manager.
PROTECTED = ".lectern"


class FileManagerError(Exception):
    """Invalid file operation; ``status_code`` maps to the HTTP response."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    mtime: float
    mode: str  # unix permission string, e.g. "-rw-r--r--" / "drwxr-xr-x"


def safe_path(server_dir: Path, rel: str) -> Path:
    """Resolve ``rel`` against the server dir, confined to it. Raises
    ``FileManagerError`` on any path that escapes (absolute, ``..``, or a
    symlink pointing out). ``rel`` of ``""``/``"."`` is the server root."""
    base = server_dir.resolve()
    cleaned = rel.replace("\\", "/").strip()
    # Absolute paths would make `base / rel` discard the base — reject outright
    # rather than silently reinterpreting them as relative.
    if PurePosixPath(cleaned).is_absolute():
        raise FileManagerError("Path is outside the server directory", 400)
    # .resolve() collapses `..` and follows symlinks, so a link pointing out of
    # the tree resolves out of bounds and fails the containment check below.
    candidate = (base / cleaned).resolve()
    if candidate != base and base not in candidate.parents:
        raise FileManagerError("Path is outside the server directory", 400)
    return candidate


def _guard_mutable(server_dir: Path, target: Path) -> None:
    """Refuse writes/deletes/renames on the server root itself or anything
    under the protected ``.lectern`` dir."""
    base = server_dir.resolve()
    if target == base:
        raise FileManagerError("Refusing to modify the server root", 400)
    if PROTECTED in target.relative_to(base).parts:
        raise FileManagerError(f"{PROTECTED}/ is managed by Lectern", 403)


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


# --- operations ------------------------------------------------------------


def list_dir(server_dir: Path, rel: str) -> list[Entry]:
    """Directory listing, dirs first then case-insensitive name. ``.lectern``
    is hidden."""
    target = safe_path(server_dir, rel)
    if not target.is_dir():
        raise FileManagerError("Not a directory", 400)
    entries: list[Entry] = []
    for child in target.iterdir():
        if child.name == PROTECTED:
            continue
        try:
            st = child.stat()
        except OSError:
            continue  # broken symlink etc. — skip rather than fail the listing
        entries.append(
            Entry(
                name=child.name,
                is_dir=child.is_dir(),
                size=st.st_size,
                mtime=st.st_mtime,
                mode=stat_mod.filemode(st.st_mode),
            )
        )
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def read_file(server_dir: Path, rel: str) -> dict:
    """Text contents of a file, or a flag saying why not (too big / binary)."""
    target = safe_path(server_dir, rel)
    if not target.is_file():
        raise FileManagerError("Not a file", 400)
    size = target.stat().st_size
    if size > TEXT_EDIT_MAX:
        return {"content": None, "size": size, "binary": False, "too_large": True}
    data = target.read_bytes()
    if _is_binary(data):
        return {"content": None, "size": size, "binary": True, "too_large": False}
    return {
        "content": data.decode("utf-8", errors="replace"),
        "size": size,
        "binary": False,
        "too_large": False,
    }


def write_file(server_dir: Path, rel: str, content: str) -> None:
    """Overwrite (or create) a text file. Its parent directory must already
    exist within the tree; a directory can't be overwritten."""
    target = safe_path(server_dir, rel)
    _guard_mutable(server_dir, target)
    if target.is_dir():
        raise FileManagerError("A directory exists at that path", 400)
    if not target.parent.is_dir():
        raise FileManagerError("Parent directory does not exist", 400)
    target.write_text(content)


def make_dir(server_dir: Path, rel: str) -> None:
    target = safe_path(server_dir, rel)
    _guard_mutable(server_dir, target)
    if target.exists():
        raise FileManagerError("That path already exists", 409)
    target.mkdir(parents=True)


def rename(server_dir: Path, rel: str, to: str) -> None:
    src = safe_path(server_dir, rel)
    dst = safe_path(server_dir, to)
    _guard_mutable(server_dir, src)
    _guard_mutable(server_dir, dst)
    if not src.exists():
        raise FileManagerError("Source does not exist", 404)
    if dst.exists():
        raise FileManagerError("Destination already exists", 409)
    if not dst.parent.is_dir():
        raise FileManagerError("Destination directory does not exist", 400)
    src.rename(dst)


def delete(server_dir: Path, rel: str) -> None:
    target = safe_path(server_dir, rel)
    _guard_mutable(server_dir, target)
    if not target.exists():
        raise FileManagerError("Path does not exist", 404)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def upload_dest(server_dir: Path, dir_rel: str, filename: str) -> Path:
    """Resolve the destination path for an uploaded file into ``dir_rel``,
    confined and with a sanitised name."""
    directory = safe_path(server_dir, dir_rel)
    if not directory.is_dir():
        raise FileManagerError("Upload target is not a directory", 400)
    safe_name = Path(filename).name  # strip any path components
    if not safe_name or safe_name in (".", ".."):
        raise FileManagerError("Invalid file name", 400)
    dest = safe_path(server_dir, str(directory.relative_to(server_dir.resolve()) / safe_name))
    _guard_mutable(server_dir, dest)
    return dest
