"""Velocity proxy configuration — velocity.toml scaffold + server linking.

Lectern owns velocity.toml for proxy servers: it writes the initial config at
install time (bind port, modern forwarding, generated secret) and rewrites the
``[servers]`` section when backends are linked/unlinked. Comments in a
hand-edited file are NOT preserved across a rewrite (documented in the UI).

Parsing uses stdlib ``tomllib``; writing uses a minimal serializer that covers
velocity.toml's actual shape (top-level scalars, one level of tables, string
values/arrays) — a full TOML writer dependency isn't warranted for this.
"""

from __future__ import annotations

import secrets
import tomllib
from pathlib import Path

CONFIG_NAME = "velocity.toml"
SECRET_NAME = "forwarding.secret"

# Keys every scaffold carries; Velocity itself fills in anything missing from
# its defaults on boot, so this stays deliberately small.
_SCAFFOLD_TOP = {
    "config-version": "2.7",
    "motd": "A Lectern proxy",
    "player-info-forwarding-mode": "modern",
    "forwarding-secret-file": SECRET_NAME,
    "online-mode": True,
}


class VelocityConfigError(Exception):
    """velocity.toml missing or unparseable."""


# --- minimal TOML writing (unit-tested) -------------------------------------


def _toml_key(key: str) -> str:
    if key and all(c.isalnum() or c in "-_" for c in key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def toml_dumps(data: dict) -> str:
    """Serialize the velocity.toml shape: top-level scalars first, then one
    level of ``[section]`` tables. Deterministic ordering (dict order)."""
    top = [(k, v) for k, v in data.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    lines = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in top]
    for name, table in tables:
        lines.append("")
        lines.append(f"[{_toml_key(name)}]")
        lines.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items())
    return "\n".join(lines) + "\n"


# --- config I/O --------------------------------------------------------------


def read_config(server_dir: Path) -> dict:
    path = server_dir / CONFIG_NAME
    if not path.exists():
        raise VelocityConfigError(f"{CONFIG_NAME} not found — has the proxy installed?")
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise VelocityConfigError(f"{CONFIG_NAME} is not valid TOML: {exc}") from exc


def write_config(server_dir: Path, config: dict) -> None:
    (server_dir / CONFIG_NAME).write_text(toml_dumps(config))


def write_initial_config(server_dir: Path, *, port: int) -> None:
    """Install-time scaffold: bind port, modern forwarding, fresh secret,
    no linked servers yet."""
    config: dict = {
        **_SCAFFOLD_TOP,
        "bind": f"0.0.0.0:{port}",
        "servers": {"try": []},
        # Explicitly empty: if the key is absent Velocity merges in its
        # example forced-hosts, which point at servers that don't exist and
        # fail config validation on boot.
        "forced-hosts": {},
    }
    write_config(server_dir, config)
    secret_path = server_dir / SECRET_NAME
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(24))


def read_secret(server_dir: Path) -> str:
    path = server_dir / SECRET_NAME
    if not path.exists():
        path.write_text(secrets.token_urlsafe(24))
    return path.read_text().strip()


def set_bind_port(server_dir: Path, port: int) -> None:
    config = read_config(server_dir)
    host = str(config.get("bind", "0.0.0.0:25565")).rsplit(":", 1)[0]
    config["bind"] = f"{host}:{port}"
    write_config(server_dir, config)


def get_bind_port(server_dir: Path) -> int | None:
    try:
        bind = str(read_config(server_dir).get("bind", ""))
        return int(bind.rsplit(":", 1)[1])
    except (VelocityConfigError, IndexError, ValueError):
        return None


# --- linking -----------------------------------------------------------------


def linked_servers(server_dir: Path) -> dict[str, str]:
    """{name: "host:port"} of the proxy's registered backends (the ``try``
    key lives in the same table and is excluded)."""
    servers = read_config(server_dir).get("servers") or {}
    return {k: str(v) for k, v in servers.items() if k != "try" and not isinstance(v, dict)}


def write_links(server_dir: Path, links: dict[str, str], try_order: list[str]) -> None:
    """Replace the proxy's backend registry. ``try_order`` is the connection
    order (first entry = where new players land)."""
    config = read_config(server_dir)
    config["servers"] = {**links, "try": [n for n in try_order if n in links]}
    write_config(server_dir, config)


def link_name(server_name: str) -> str:
    """A stable, TOML/Velocity-friendly name for a backend ("Main Server" →
    "main-server")."""
    cleaned = "".join(
        c if (c.isascii() and c.isalnum()) or c in "-_" else "-"
        for c in server_name.lower()
    )
    cleaned = "-".join(filter(None, cleaned.split("-")))
    return cleaned or "server"
