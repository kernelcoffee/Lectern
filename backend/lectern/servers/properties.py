"""``server.properties`` parsing, rendering, and typed validation (M5).

The editor is backed by a **typed definitions map** for common keys (pattern
borrowed from mc-image-helper's ``set-properties`` — see
docs/references/mc-image-helper.md): the frontend renders real widgets from it
and the PATCH endpoint validates against it. Unknown keys are accepted as
free-form strings so exotic/modded properties still work.

Values are stored as plain strings, exactly like the file itself; Minecraft
rewrites the file on boot (dropping comments and reordering), so we do the
same and don't try to preserve layout.
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


DEFINITIONS: dict[str, PropertyDef] = {
    "motd": PropertyDef("string", description="Message shown in the server list"),
    "gamemode": PropertyDef(
        "enum", values=("survival", "creative", "adventure", "spectator"),
        description="Default game mode for new players",
    ),
    "difficulty": PropertyDef(
        "enum", values=("peaceful", "easy", "normal", "hard"),
        description="World difficulty",
    ),
    "level-type": PropertyDef(
        "enum",
        values=("minecraft:normal", "minecraft:flat", "minecraft:large_biomes", "minecraft:amplified"),
        description="World generator preset",
    ),
    "max-players": PropertyDef("integer", min=1, max=1000, description="Player slots"),
    "view-distance": PropertyDef("integer", min=3, max=32, description="Server view distance (chunks)"),
    "simulation-distance": PropertyDef("integer", min=3, max=32, description="Tick distance (chunks)"),
    "spawn-protection": PropertyDef("integer", min=0, max=16384, description="Protected radius around spawn"),
    "server-port": PropertyDef("integer", min=1, max=65535, description="TCP port the server listens on"),
    "player-idle-timeout": PropertyDef("integer", min=0, max=1440, description="Kick idle players (minutes, 0 = never)"),
    "pvp": PropertyDef("boolean", description="Allow players to fight each other"),
    "hardcore": PropertyDef("boolean", description="Hardcore mode (death = spectator)"),
    "online-mode": PropertyDef("boolean", description="Verify players against Mojang (disable for offline LAN)"),
    "white-list": PropertyDef("boolean", description="Only whitelisted players may join"),
    "enforce-whitelist": PropertyDef("boolean", description="Kick non-whitelisted players on reload"),
    "allow-flight": PropertyDef("boolean", description="Don't kick survival players detected flying (needed by some mods)"),
    "allow-nether": PropertyDef("boolean", description="Enable the Nether dimension"),
    "spawn-monsters": PropertyDef("boolean", description="Spawn hostile mobs"),
    "enable-command-block": PropertyDef("boolean", description="Enable command blocks"),
    "level-name": PropertyDef("string", description="World folder name"),
    "level-seed": PropertyDef("string", description="World seed (blank = random)"),
    "resource-pack": PropertyDef("string", description="URL of the server resource pack"),
}

_BOOL_STRINGS = {"true": "true", "false": "false"}


def parse_properties(text: str) -> dict[str, str]:
    """Parse ``key=value`` lines; comments (#/!) and blanks are skipped."""
    props: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        props[key.strip()] = value.strip()
    return props


def render_properties(props: dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in sorted(props.items()))


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
        }
        for key, d in DEFINITIONS.items()
    }
