"""Runtime-editable app settings (the Settings UI).

A small registry of **tunables** — app-level knobs safe to change while running
(upload caps, the create-form default memory). Each has an env/config default
(from ``config.Settings``); a UI edit persists an override in the ``Setting``
table (key/value), so the effective value is *DB override → config default*.

Deployment settings (``LECTERN_DATA``, host, port) are intentionally **not**
here — changing those at runtime doesn't make sense; they stay env-only.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session

from .config import get_settings
from .models import Setting


@dataclass(frozen=True)
class Tunable:
    key: str
    label: str
    help: str
    unit: str
    minimum: int
    maximum: int
    config_attr: str  # attribute on ``config.Settings`` giving the default
    category: str  # UI grouping


TUNABLES: dict[str, Tunable] = {
    t.key: t
    for t in (
        Tunable(
            "max_file_upload_mb",
            "File manager upload limit",
            "Largest single file you can upload through the file manager "
            "(e.g. a world/modpack zip to unzip in place).",
            "MB", 1, 102400, "max_file_upload_mb", "Uploads",
        ),
        Tunable(
            "max_world_upload_mb",
            "World import upload limit",
            "Largest world archive you can upload when creating a server.",
            "MB", 1, 102400, "max_world_upload_mb", "Uploads",
        ),
        Tunable(
            "default_memory_mb",
            "Default server memory",
            "Memory pre-filled for a new server in the create form.",
            "MB", 256, 65536, "default_memory_mb", "Server defaults",
        ),
    )
}


def _default(t: Tunable) -> int:
    return int(getattr(get_settings(), t.config_attr))


def get_int(session: Session, key: str) -> int:
    """Effective value: the DB override if set (and valid), else the default."""
    tunable = TUNABLES[key]
    row = session.get(Setting, key)
    if row is not None:
        try:
            return int(row.value)
        except (TypeError, ValueError):
            pass  # corrupt override — fall back to the default
    return _default(tunable)


def all_values(session: Session) -> dict[str, int]:
    return {key: get_int(session, key) for key in TUNABLES}


def set_values(session: Session, updates: dict[str, int]) -> None:
    """Persist overrides. Raises ``ValueError`` on an unknown key or a value
    outside the tunable's bounds; nothing is written if any value is invalid."""
    validated: list[tuple[str, int]] = []
    for key, value in updates.items():
        tunable = TUNABLES.get(key)
        if tunable is None:
            raise ValueError(f"Unknown setting: {key}")
        if not isinstance(value, int) or isinstance(value, bool):
            # ValueError on purpose (not TypeError): the API layer maps
            # ValueError from here to a 400, and this is input validation.
            raise ValueError(f"{tunable.label} must be a whole number")  # noqa: TRY004
        if not (tunable.minimum <= value <= tunable.maximum):
            raise ValueError(
                f"{tunable.label} must be between {tunable.minimum} and {tunable.maximum}"
            )
        validated.append((key, value))
    for key, value in validated:
        row = session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=str(value))
        else:
            row.value = str(value)
        session.add(row)
    session.commit()
