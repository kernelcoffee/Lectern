"""Database models (SQLModel) and API schemas.

Table models map 1:1 to the schema in docs/technical.md §4. Enum-like columns
(`type`, `status`, `kind`, `source`, `trigger`, `action`) are stored as plain
strings for simple, migration-free SQLite storage; the allowed values are
expressed as ``str`` enums used by the API layer for validation and OpenAPI.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Enumerations (validation / OpenAPI; stored as strings) ---------------


class ServerType(str, enum.Enum):
    vanilla = "vanilla"
    fabric = "fabric"
    quilt = "quilt"
    paper = "paper"


class ServerStatus(str, enum.Enum):
    installing = "installing"
    install_failed = "install_failed"
    stopped = "stopped"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    crashed = "crashed"


# --- Tables ---------------------------------------------------------------


class Server(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    path: str = ""
    type: str = ServerType.vanilla.value
    mc_version: str
    loader_version: str | None = None
    java_major: int | None = None
    java_path: str | None = None
    # Jar filename (inside the server dir) that the launch command runs. Set by
    # the M3 install pipeline; the full command is derived from it at launch (M4).
    server_jar: str = ""
    port: int = 25565
    memory_mb: int = 2048
    jvm_args: str = ""
    auto_start: bool = False
    crash_restart: bool = False
    stop_command: str = "stop"
    shutdown_timeout: int = 60
    status: str = ServerStatus.stopped.value
    created_at: datetime = Field(default_factory=_now)


class ContentItem(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    server_id: str = Field(foreign_key="server.id", index=True)
    kind: str  # mod | plugin | resourcepack | modpack
    source: str  # modrinth | vanillatweaks
    project_id: str | None = None
    version_id: str | None = None
    slug: str | None = None
    name: str
    filename: str
    sha512: str | None = None
    enabled: bool = True
    installed_at: datetime = Field(default_factory=_now)


class Backup(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    server_id: str = Field(foreign_key="server.id", index=True)
    filename: str
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=_now)
    trigger: str = "manual"  # manual | scheduled


class Schedule(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    server_id: str = Field(foreign_key="server.id", index=True)
    action: str  # start | stop | restart | backup
    cron: str
    enabled: bool = True


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


# --- API schemas ----------------------------------------------------------


class ServerCreate(SQLModel):
    """Payload for creating a server record.

    In M1 this only persists a record; the download/install pipeline (jar,
    Java, files) is added in M3, so most operational fields are defaulted.
    """

    name: str
    type: ServerType = ServerType.vanilla
    mc_version: str
    loader_version: str | None = None
    port: int = 25565
    memory_mb: int = 2048


class ServerRead(SQLModel):
    id: str
    name: str
    type: str
    mc_version: str
    loader_version: str | None
    port: int
    memory_mb: int
    status: str
    created_at: datetime


class ServerDetailRead(ServerRead):
    """Server view for the detail page — adds operational + EULA state."""

    jvm_args: str
    auto_start: bool
    crash_restart: bool
    stop_command: str
    shutdown_timeout: int
    eula_accepted: bool
    running: bool


class InstallProgressRead(SQLModel):
    """Live progress of the create/install pipeline for a server."""

    server_id: str
    step: str
    message: str
    done: bool
    error: str | None
