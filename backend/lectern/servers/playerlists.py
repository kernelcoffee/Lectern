"""Per-server player lists — whitelist / ops / banned (F-PL).

Reads and writes the JSON files Minecraft itself keeps in the server directory
(``whitelist.json``, ``ops.json``, ``banned-players.json``). Those files are
the source of truth for membership; a global ``Player`` registry (models.py)
just supplies the names/UUIDs you add from.

UUIDs are stored **dashed** in the files (Minecraft's format) but compared
**undashed** everywhere else, so add/remove normalise both ways. Minecraft
caches these files in memory, so direct edits apply at the next start / list
reload — for a *running* server the API layer (``api/players.py``) sends the
equivalent console command instead and only falls back to these file edits.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..providers.mojang import dash_uuid, undash_uuid

# kind → filename in the server directory.
FILES = {
    "whitelist": "whitelist.json",
    "ops": "ops.json",
    "banned": "banned-players.json",
}


class PlayerListError(Exception):
    """Invalid player-list operation (unknown kind, …)."""


def _path(server_dir: Path, kind: str) -> Path:
    if kind not in FILES:
        raise PlayerListError(f"Unknown player list: {kind}")
    return server_dir / FILES[kind]


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text() or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _save(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries, indent=2))


def read_list(server_dir: Path, kind: str) -> list[dict]:
    """Members of a list as ``[{"uuid": <undashed>, "name": <name>}]``."""
    entries = _load(_path(server_dir, kind))
    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict) or "uuid" not in e:
            continue
        out.append({"uuid": undash_uuid(str(e["uuid"])), "name": str(e.get("name", ""))})
    return out


def _new_entry(kind: str, uuid: str, name: str) -> dict:
    dashed = dash_uuid(uuid)
    if kind == "ops":
        return {"uuid": dashed, "name": name, "level": 4, "bypassesPlayerLimit": False}
    if kind == "banned":
        return {
            "uuid": dashed,
            "name": name,
            "created": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S +0000"),
            "source": "Lectern",
            "expires": "forever",
            "reason": "Banned by an operator.",
        }
    return {"uuid": dashed, "name": name}  # whitelist


def add(server_dir: Path, kind: str, uuid: str, name: str) -> None:
    """Add a player to a list (no-op if already present)."""
    path = _path(server_dir, kind)
    entries = _load(path)
    target = undash_uuid(uuid)
    if any(undash_uuid(str(e.get("uuid", ""))) == target for e in entries):
        return
    entries.append(_new_entry(kind, uuid, name))
    _save(path, entries)


def remove(server_dir: Path, kind: str, uuid: str) -> None:
    """Remove a player from a list by UUID (no-op if absent)."""
    path = _path(server_dir, kind)
    target = undash_uuid(uuid)
    entries = [
        e for e in _load(path) if undash_uuid(str(e.get("uuid", ""))) != target
    ]
    _save(path, entries)
