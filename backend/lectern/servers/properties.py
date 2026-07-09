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
}

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


def write_properties(server_dir: Path, props: dict[str, str]) -> None:
    (server_dir / PROPERTIES_FILE).write_text(render_properties(props), encoding="utf-8")


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


def definitions_payload() -> dict[str, dict]:
    """JSON-friendly view of ``DEFINITIONS`` for the frontend."""
    return {
        key: {
            "type": d.type,
            "values": list(d.values) if d.values else None,
            "min": d.min,
            "max": d.max,
            "description": d.description,
            "default": d.default,
        }
        for key, d in DEFINITIONS.items()
    }
