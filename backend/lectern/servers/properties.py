r"""``server.properties`` parsing, rendering, and typed validation (M5).

The editor is backed by a **typed definitions map** for common keys (pattern
borrowed from mc-image-helper's ``set-properties`` — see
docs/references/mc-image-helper.md): the frontend renders real widgets from it
and the PATCH endpoint validates against it. Unknown keys are accepted as
free-form strings so exotic/modded properties still work.

Values are stored as plain strings, exactly like the file itself; Minecraft
rewrites the file on boot (dropping comments and reordering), so we do the
same and don't try to preserve layout.

``server.properties`` is a **Java properties file**: Minecraft writes it via
``Properties.store()``, which escapes ``\ : = # !``, control characters, and
non-ASCII (``\uXXXX``) — e.g. MC 26.2 writes ``level-type=minecraft\:normal``.
Parse unescapes; render re-escapes the same way so round-trips are faithful.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from pathlib import Path

PROPERTIES_FILE = "server.properties"


@dataclass(frozen=True)
class PropertyDef:
    """Validation/UI metadata for one well-known server.properties key."""

    type: str  # "boolean" | "integer" | "enum" | "string"
    values: tuple[str, ...] | None = None  # enum choices
    min: int | None = None
    max: int | None = None
    description: str = ""
    # Vanilla's built-in default — what the server uses when the key is absent
    # from the file. Shown by the UI next to unset fields. None = no stable
    # default worth showing (random seed, empty resource-pack, ...).
    default: str | None = None
    # Written by Lectern at every start; read-only in the editor and rejected
    # by PATCH (the management-server keys, see servers/msmp.py).
    managed: bool = False


DEFINITIONS: dict[str, PropertyDef] = {
    "motd": PropertyDef("string", description="Message shown in the server list", default="A Minecraft Server"),
    "gamemode": PropertyDef(
        "enum", values=("survival", "creative", "adventure", "spectator"),
        description="Default game mode for new players", default="survival",
    ),
    "difficulty": PropertyDef(
        "enum", values=("peaceful", "easy", "normal", "hard"),
        description="World difficulty", default="easy",
    ),
    "level-type": PropertyDef(
        "enum",
        values=("minecraft:normal", "minecraft:flat", "minecraft:large_biomes", "minecraft:amplified"),
        description="World generator preset", default="minecraft:normal",
    ),
    "max-players": PropertyDef("integer", min=1, max=1000, description="Player slots", default="20"),
    "view-distance": PropertyDef("integer", min=3, max=32, description="Server view distance (chunks)", default="10"),
    "simulation-distance": PropertyDef("integer", min=3, max=32, description="Tick distance (chunks)", default="10"),
    "spawn-protection": PropertyDef("integer", min=0, max=16384, description="Protected radius around spawn", default="16"),
    "server-port": PropertyDef("integer", min=1, max=65535, description="TCP port the server listens on", default="25565"),
    "player-idle-timeout": PropertyDef("integer", min=0, max=1440, description="Kick idle players (minutes, 0 = never)", default="0"),
    "pvp": PropertyDef("boolean", description="Allow players to fight each other", default="true"),
    "hardcore": PropertyDef("boolean", description="Hardcore mode (death = spectator)", default="false"),
    "online-mode": PropertyDef("boolean", description="Verify players against Mojang (disable for offline LAN)", default="true"),
    "white-list": PropertyDef("boolean", description="Only whitelisted players may join", default="false"),
    "enforce-whitelist": PropertyDef("boolean", description="Kick non-whitelisted players on reload", default="false"),
    "allow-flight": PropertyDef("boolean", description="Don't kick survival players detected flying (needed by some mods)", default="false"),
    "allow-nether": PropertyDef("boolean", description="Enable the Nether dimension", default="true"),
    "spawn-monsters": PropertyDef("boolean", description="Spawn hostile mobs", default="true"),
    "enable-command-block": PropertyDef("boolean", description="Enable command blocks", default="false"),
    "level-name": PropertyDef("string", description="World folder name", default="world"),
    "level-seed": PropertyDef("string", description="World seed (blank = random)"),
    "resource-pack": PropertyDef("string", description="URL of the server resource pack"),
    "management-server-enabled": PropertyDef(
        "boolean", managed=True,
        description="Server Management Protocol; Lectern turns it on for 1.21.9+",
    ),
    "management-server-host": PropertyDef(
        "string", managed=True, description="Management protocol bind address (localhost only)",
    ),
    "management-server-port": PropertyDef(
        "integer", min=0, max=65535, managed=True,
        description="Management protocol port, chosen by Lectern",
    ),
    "management-server-tls-enabled": PropertyDef(
        "boolean", managed=True, description="TLS on the management protocol (off on localhost)",
    ),
    "management-server-secret": PropertyDef(
        "string", managed=True,
        description="Bearer token for Lectern's local management connection",
    ),
}

# Keys a running server applies at once through the Server Management Protocol
# (servers/msmp.py): ``server.properties`` key -> ``serversettings`` name, as in
# ``minecraft:serversettings/<name>/set``. Every other key applies at the next
# start. ``gamemode`` is the default for players joining for the first time.
LIVE_SETTINGS: dict[str, str] = {
    "motd": "motd",
    "difficulty": "difficulty",
    "max-players": "max_players",
    "view-distance": "view_distance",
    "white-list": "use_allowlist",
    "enforce-whitelist": "enforce_allowlist",
    "gamemode": "game_mode",
}

# Keys Lectern owns (written at start, never editable through the API).
MANAGED_KEYS: tuple[str, ...] = tuple(k for k, d in DEFINITIONS.items() if d.managed)

_BOOL_STRINGS = {"true": "true", "false": "false"}


def _unescape(text: str) -> str:
    """Undo Java-properties escaping (``\\:`` ``\\=`` ``\\uXXXX`` ...)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "u" and i + 6 <= len(text):
            try:
                out.append(chr(int(text[i + 2 : i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass  # malformed \\u — treat as a plain escape below
        out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(nxt, nxt))
        i += 2
    return "".join(out)


def _escape(text: str, *, is_key: bool) -> str:
    """Java ``Properties.store()``-style escaping: ``\\ = : # !`` and control
    chars always; spaces only in keys; non-ASCII becomes ``\\uXXXX``."""
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch in "=:#!":
            out.append("\\" + ch)
        elif ch == " " and is_key:
            out.append("\\ ")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\f":
            out.append("\\f")
        elif ord(ch) < 0x20 or ord(ch) > 0x7E:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _split_line(line: str) -> tuple[str, str] | None:
    """Split at the first unescaped ``=`` or ``:`` (both are legal separators
    in Java properties; Minecraft writes ``=``)."""
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch in "=:":
            return line[:i], line[i + 1 :]
        i += 1
    return None


def parse_properties(text: str) -> dict[str, str]:
    """Parse ``key=value`` lines with Java-properties unescaping; comments
    (#/!) and blanks are skipped."""
    props: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        parts = _split_line(stripped)
        if parts is None:
            continue
        key, value = parts
        props[_unescape(key.strip())] = _unescape(value.strip())
    return props


def render_properties(props: dict[str, str]) -> str:
    return "".join(
        f"{_escape(k, is_key=True)}={_escape(v, is_key=False)}\n"
        for k, v in sorted(props.items())
    )


def read_properties(server_dir: Path) -> dict[str, str]:
    path = server_dir / PROPERTIES_FILE
    if not path.exists():
        return {}
    return parse_properties(path.read_text(encoding="utf-8", errors="replace"))


class PropertiesPathError(OSError):
    """``server.properties`` resolves outside its server directory."""


def properties_file(server_dir: Path) -> Path:
    """``server_dir/server.properties``, refusing a symlink that leads out of
    the server directory (path-safety rule: writes stay confined)."""
    base = server_dir.resolve()
    path = (server_dir / PROPERTIES_FILE).resolve()
    if base not in path.parents:
        raise PropertiesPathError(f"{PROPERTIES_FILE} points outside the server directory")
    return path


def write_properties(server_dir: Path, props: dict[str, str]) -> None:
    properties_file(server_dir).write_text(render_properties(props), encoding="utf-8")


# One read-modify-write of a server's ``server.properties`` at a time (see
# ``edit_lock``). Weak values: a lock lives only while someone holds or waits
# on it.
_edit_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def edit_lock(server_dir: Path) -> asyncio.Lock:
    """The lock every read-modify-write of ``server_dir``'s
    ``server.properties`` takes. The properties PATCH reads the file, awaits
    the server's live sets, then writes a merge of that read; another writer
    getting in between would have its change reverted by that write."""
    return _edit_locks.setdefault(str(server_dir.resolve()), asyncio.Lock())


async def edit_properties(server_dir: Path, changes: dict[str, str | None]) -> dict[str, str]:
    """Apply ``changes`` (``None`` removes a key) to ``server.properties``
    under ``edit_lock`` and return the file as written."""
    async with edit_lock(server_dir):
        file_props = read_properties(server_dir)
        for key, value in changes.items():
            if value is None:
                file_props.pop(key, None)
            else:
                file_props[key] = value
        write_properties(server_dir, file_props)
        return file_props


# ``op-permission-level``: Minecraft's default, and the levels it knows.
DEFAULT_OP_LEVEL = 4
_MIN_OP_LEVEL = 0
_MAX_OP_LEVEL = 4


def clamp_op_level(level: int) -> int:
    """``level`` brought into the operator levels Minecraft knows (0..4)."""
    return min(max(level, _MIN_OP_LEVEL), _MAX_OP_LEVEL)


def op_permission_level(file_props: dict[str, str]) -> int:
    """The level a new operator gets on a server with these properties
    (``op-permission-level``): Minecraft's default 4 when the key is missing
    or not a number, clamped to 0..4 otherwise."""
    try:
        level = int(file_props.get("op-permission-level", ""))
    except ValueError:
        return DEFAULT_OP_LEVEL
    return clamp_op_level(level)


def normalize_value(key: str, value: str | int | bool) -> str:
    """Validate ``value`` against the key's definition and return the string
    to store. Raises ``ValueError`` with a user-facing message."""
    definition = DEFINITIONS.get(key)
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value).strip()

    if definition is None:
        return text  # unknown key: free-form string

    if definition.type == "boolean":
        normalized = _BOOL_STRINGS.get(text.lower())
        if normalized is None:
            raise ValueError(f"{key} must be true or false")
        return normalized

    if definition.type == "integer":
        try:
            number = int(text)
        except ValueError:
            raise ValueError(f"{key} must be an integer") from None
        if definition.min is not None and number < definition.min:
            raise ValueError(f"{key} must be ≥ {definition.min}")
        if definition.max is not None and number > definition.max:
            raise ValueError(f"{key} must be ≤ {definition.max}")
        return str(number)

    if definition.type == "enum":
        assert definition.values is not None
        if text not in definition.values:
            raise ValueError(f"{key} must be one of: {', '.join(definition.values)}")
        return text

    return text  # string


def live_value(key: str, text: str) -> str | int | bool:
    """The typed protocol value for a normalised ``text`` (``normalize_value``
    output): ``int`` for integer keys, ``bool`` for boolean keys, the string
    itself (enum names, the unescaped motd) otherwise."""
    definition = DEFINITIONS.get(key)
    kind = definition.type if definition is not None else "string"
    if kind == "integer":
        return int(text)
    if kind == "boolean":
        return text == "true"
    return text


def definitions_payload(*, include_managed: bool = True) -> dict[str, dict]:
    """JSON-friendly view of ``DEFINITIONS`` for the frontend. Without
    ``include_managed`` the managed keys are left out, so a server Lectern
    does not run the management server for shows any such lines as plain,
    editable rows. ``live`` marks the keys in ``LIVE_SETTINGS``."""
    return {
        key: {
            "type": d.type,
            "values": list(d.values) if d.values else None,
            "min": d.min,
            "max": d.max,
            "description": d.description,
            "default": d.default,
            "managed": d.managed,
            "live": key in LIVE_SETTINGS,
        }
        for key, d in DEFINITIONS.items()
        if include_managed or not d.managed
    }
