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


class ReleaseChannel(str, enum.Enum):
    """Least-stable release type allowed when installing/updating an item."""

    release = "release"
    beta = "beta"
    alpha = "alpha"


class ScheduleAction(str, enum.Enum):
    """What a cron schedule does when it fires (M10)."""

    start = "start"
    stop = "stop"
    restart = "restart"
    backup = "backup"
    command = "command"  # send a raw console command (needs `command`)


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
    # Seconds to wait before auto-starting on boot — staggers JVM startups when
    # several servers auto-start together (ref: crafty-4's auto_start_delay).
    auto_start_delay: int = 10
    crash_restart: bool = False
    stop_command: str = "stop"
    shutdown_timeout: int = 60
    # Per-server backup settings (M9) — one config per server, not Crafty's
    # named multi-configs. excluded = comma-separated dir/file prefixes
    # relative to the server dir (e.g. "logs,cache").
    backup_excluded: str = "logs,crash-reports"
    backup_max: int = 10
    backup_compress: bool = True
    backup_stop_server: bool = False
    status: str = ServerStatus.stopped.value
    created_at: datetime = Field(default_factory=_now)


class ContentItem(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    server_id: str = Field(foreign_key="server.id", index=True)
    kind: str  # mod | plugin | resourcepack | modpack
    source: str  # modrinth | vanillatweaks
    project_id: str | None = None
    version_id: str | None = None
    version_number: str | None = None  # human-readable, e.g. "0.92.0+1.20.1"
    slug: str | None = None
    name: str
    filename: str
    sha512: str | None = None
    # Least-stable release type this item may update to (M6): a mod pinned to
    # "beta" also accepts releases; the default only ever takes releases.
    channel: str = ReleaseChannel.release.value
    enabled: bool = True
    installed_at: datetime = Field(default_factory=_now)


class Backup(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    server_id: str = Field(foreign_key="server.id", index=True)
    filename: str
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=_now)
    trigger: str = "manual"  # manual | scheduled


class ServerStat(SQLModel, table=True):
    """One point in a server's resource-usage time series (M11 monitoring).

    Sampled on a timer for *running* servers; pruned to a retention window.
    Integer autoincrement PK — these are high-volume and short-lived, so a UUID
    would be wasteful.
    """

    id: int | None = Field(default=None, primary_key=True)
    server_id: str = Field(foreign_key="server.id", index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    players_online: int = 0


class Schedule(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    server_id: str = Field(foreign_key="server.id", index=True)
    action: str  # start | stop | restart | backup | command
    cron: str
    # Raw console command sent when action == "command" (M10).
    command: str | None = None
    # One-shot: the schedule deletes itself after its first firing (M10).
    one_time: bool = False
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


class ServerCloneRequest(SQLModel):
    """Payload for cloning a server. ``name``/``port`` default to a suggested
    free name/port when omitted; ``include_world`` copies the world folder
    (off → a fresh world on first start, same jars/mods/config)."""

    name: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    include_world: bool = True


class ServerSuggestRead(SQLModel):
    """Suggested defaults for the create-server form: the first name/port not
    already taken (name "New server", "New server 2", …; port 25565 upward)
    plus the configured default memory."""

    name: str
    port: int
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
    auto_start_delay: int
    crash_restart: bool
    stop_command: str
    shutdown_timeout: int
    backup_excluded: str
    backup_max: int
    backup_compress: bool
    backup_stop_server: bool
    eula_accepted: bool
    running: bool


class InstallProgressRead(SQLModel):
    """Live progress of the create/install pipeline for a server."""

    server_id: str
    step: str
    message: str
    done: bool
    error: str | None


class ServerSettingsUpdate(SQLModel):
    """PATCH payload for the Lectern-owned settings of a server (M5).

    These are the knobs stored on the ``Server`` row (as opposed to Minecraft's
    own ``server.properties``, edited via the properties endpoints). Every
    field is optional — only the fields present in the request are applied,
    the rest keep their current values (standard JSON-merge PATCH semantics).

    Semantics of each field:

    - ``name`` — display name; no on-disk effect.
    - ``port`` — the TCP game port. Special-cased in the endpoint: it is also
      written to ``server-port`` in ``server.properties``, because the file is
      what the Minecraft process actually reads; the DB column is only used as
      the default when rendering a fresh file and for display.
    - ``memory_mb`` — JVM heap; becomes ``-Xmx/-Xms`` in the launch command at
      the *next* start (running servers are unaffected until restarted).
    - ``jvm_args`` — extra whitespace-separated JVM flags, inserted between the
      memory flags and ``-jar``.
    - ``auto_start`` — start this server when Lectern boots (wired in M11).
    - ``crash_restart`` — restart automatically after a crash (capped at 3
      consecutive attempts; see ``servers/manager.py``).
    - ``stop_command`` — console command sent for a graceful stop (``stop`` for
      vanilla/Fabric; Bedrock or plugins may differ).
    - ``shutdown_timeout`` — seconds to wait after ``stop_command`` before
      escalating to terminate/kill.

    Bounds are enforced here so bad values are rejected with a 422 before
    touching the record (e.g. a 70 GB heap or a 0-second timeout).
    """

    name: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    memory_mb: int | None = Field(default=None, ge=256, le=65536)
    jvm_args: str | None = None
    auto_start: bool | None = None
    auto_start_delay: int | None = Field(default=None, ge=0, le=600)
    crash_restart: bool | None = None
    stop_command: str | None = None
    shutdown_timeout: int | None = Field(default=None, ge=5, le=600)
    # Backup settings (M9).
    backup_excluded: str | None = None
    backup_max: int | None = Field(default=None, ge=1, le=100)
    backup_compress: bool | None = None
    backup_stop_server: bool | None = None


class VersionChangeRequest(SQLModel):
    """POST payload to change a server's Minecraft version (M9.5, F-SM-9).

    - ``mc_version`` — the target Minecraft version.
    - ``loader_version`` — optional explicit loader build (Fabric/Quilt);
      omitted → the newest for the target version is resolved.
    - ``allow_downgrade`` — required to select a version older than the current
      one (Minecraft can't downgrade a world in place; the world may be
      unusable — the supported path back is restoring a pre-upgrade backup).
    - ``backup_first`` — run a backup before touching anything (default on; the
      world is upgraded one-way on next start).
    """

    mc_version: str
    loader_version: str | None = None
    allow_downgrade: bool = False
    backup_first: bool = True


class MigrationReportRead(SQLModel):
    """What happened to each installed item during a version change: content
    re-resolved to a new build (``updated``), disabled for lack of a compatible
    build (``incompatible``), Vanilla Tweaks sets rebuilt (``regenerated``), and
    uploads / modpack files left untouched (``kept`` — reimport the modpack to
    move those)."""

    updated: list[str]
    incompatible: list[str]
    regenerated: list[str]
    kept: list[str]


class VersionChangeRead(SQLModel):
    """Response of ``POST /servers/{id}/version`` — the updated server plus the
    content migration report."""

    server: ServerDetailRead
    report: MigrationReportRead


class WorldImportRead(SQLModel):
    """Result of importing a world (F-SM-10): the updated server plus how many
    files were written and how many were skipped by an exclude pattern (e.g.
    Distant Horizons LOD caches)."""

    server: ServerDetailRead
    written: int
    skipped: int


class SettingRead(SQLModel):
    """One app-level tunable for the Settings UI: its current effective value
    plus the metadata to render/validate an input."""

    key: str
    label: str
    help: str
    unit: str
    value: int
    min: int
    max: int
    category: str


class FileEntryRead(SQLModel):
    """One entry in a server-directory listing (M12)."""

    name: str
    is_dir: bool
    size: int
    mtime: float  # epoch seconds
    mode: str  # unix permission string (e.g. "-rw-r--r--")


class DirListingRead(SQLModel):
    path: str  # the listed directory, relative to the server root ("" = root)
    entries: list[FileEntryRead]


class FileContentRead(SQLModel):
    """Text contents of a file, or a flag saying why it wasn't loaded."""

    path: str
    content: str | None  # None when binary or too large
    binary: bool
    too_large: bool
    size: int


class WriteFileRequest(SQLModel):
    content: str


class MkdirRequest(SQLModel):
    path: str


class UnzipRequest(SQLModel):
    path: str  # a .zip file to extract in place (into its own directory)


class RenameRequest(SQLModel):
    path: str
    to: str


class PropertiesRead(SQLModel):
    """Response of ``GET /servers/{id}/properties``.

    - ``properties`` — the parsed ``server.properties`` key/value pairs, all
      values as raw strings exactly as stored in the file. Empty dict if the
      file doesn't exist yet (server still installing).
    - ``definitions`` — the typed metadata map for *well-known* keys
      (``servers/properties.py::DEFINITIONS``): type (boolean/integer/enum/
      string), enum values, integer min/max, and a human description. The
      frontend uses it to render proper widgets (toggle, select, number input)
      and the PATCH endpoint validates against it. Keys not present in the map
      are legal free-form strings — modded servers add their own.
    """

    properties: dict[str, str]
    definitions: dict[str, dict]


class BackupRead(SQLModel):
    """One backup archive of a server (M9)."""

    id: str
    server_id: str
    filename: str
    size_bytes: int
    created_at: datetime
    trigger: str


class ScheduleCreate(SQLModel):
    """POST payload for a new cron schedule (M10).

    - ``cron`` — standard 5-field crontab expression (validated; local time).
    - ``action`` — what fires; ``command`` additionally requires ``command``.
    - ``one_time`` — the schedule deletes itself after its first firing.
    """

    action: ScheduleAction
    cron: str
    command: str | None = None
    one_time: bool = False
    enabled: bool = True


class ScheduleUpdate(SQLModel):
    """PATCH payload — JSON-merge semantics, only present fields applied."""

    action: ScheduleAction | None = None
    cron: str | None = None
    command: str | None = None
    one_time: bool | None = None
    enabled: bool | None = None


class ScheduleRead(SQLModel):
    """One schedule row plus its computed next firing time (``None`` when
    disabled or the expression has no future occurrence)."""

    id: str
    server_id: str
    action: str
    cron: str
    command: str | None
    one_time: bool
    enabled: bool
    next_run: datetime | None


class ContentItemRead(SQLModel):
    """One installed content item (mirrors the manifest entry)."""

    id: str
    server_id: str
    kind: str
    source: str
    project_id: str | None
    version_id: str | None
    version_number: str | None
    slug: str | None
    name: str
    filename: str
    sha512: str | None
    channel: str
    enabled: bool
    installed_at: datetime


class ContentInstallRequest(SQLModel):
    """POST payload to install content on a server (M6).

    - ``project_id`` — Modrinth project id *or* slug.
    - ``version_id`` — a specific version; omitted → newest qualifying under
      ``channel`` for the server's loader + MC version.
    - ``channel`` — stored on the item and governs future updates.
    - ``include_optional_deps`` — required dependencies always install;
      optional ones only when this is set.
    """

    project_id: str
    version_id: str | None = None
    channel: ReleaseChannel = ReleaseChannel.release
    include_optional_deps: bool = False
    source: str = "modrinth"
    # Force the content kind: "datapack" installs a project's datapack build
    # (Modrinth models those as mods with the pseudo-loader "datapack").
    # None → the project's own type decides.
    kind: str | None = None


class ContentItemUpdate(SQLModel):
    """PATCH payload for an installed item — toggle and/or channel change."""

    enabled: bool | None = None
    channel: ReleaseChannel | None = None


class VanillaTweaksInstallRequest(SQLModel):
    """POST payload to generate + install a Vanilla Tweaks pack (M7).

    Either a ``share_code`` (resolved through VT's API — the friendlier path)
    or an explicit ``packs`` selection ``{category: [packId, …]}``. When both
    are present the share code wins — including its own pack type.
    ``pack_type`` is a VT type name: resourcepacks | datapacks | craftingtweaks.
    """

    share_code: str | None = None
    packs: dict[str, list[str]] | None = None
    pack_type: str = "resourcepacks"


class ServerResourcePackUpdate(SQLModel):
    """POST payload for the optional in-game resource-pack prompt: enable
    points ``resource-pack``/``resource-pack-sha1`` at the item's download
    URL; disable clears both keys."""

    enabled: bool = True


class ContentUpdateRead(SQLModel):
    """One entry of ``GET /servers/{id}/content/updates`` — an installed item
    with a newer qualifying version available."""

    item_id: str
    name: str
    installed_version: str | None  # version_number (fallback: id)
    new_version_id: str
    new_version_number: str


class PingRead(SQLModel):
    """Server List Ping result — what the Minecraft server itself reports.

    Only present in ``ServerStatsRead`` when the server answered the status
    ping (i.e. fully booted and listening); ``None`` while starting.
    ``favicon`` is the ``data:image/png;base64,…`` URI straight from the
    server, or ``None`` when it has no icon.
    """

    online: int
    max: int
    players: list[str]  # sample of online player names (server-truncated)
    motd: str  # flattened to plain text (formatting codes stripped)
    version: str  # e.g. "1.20.1" or a modded brand string
    favicon: str | None


class StatSampleRead(SQLModel):
    """One historical resource sample (from ``GET /servers/{id}/stats/history``)."""

    created_at: datetime
    cpu_percent: float
    memory_mb: float
    players_online: int


class ServerSizeRead(SQLModel):
    """Cached on-disk size of a server (computed off-request by the sampler).
    ``null`` fields mean it hasn't been measured yet."""

    world_bytes: int | None = None
    server_bytes: int | None = None
    computed_at: datetime | None = None


class ServerStatsRead(SQLModel):
    """Response of ``GET /servers/{id}/stats`` — a point-in-time snapshot.

    Computed on request (frontend polls; no background loop, see technical.md
    §5). Field availability degrades gracefully:

    - server not running → ``running=False`` and every other field ``None``;
    - process up but still booting → resource fields set, ``ping`` ``None``;
    - fully up → everything set (subject to the server having an icon etc.).

    ``cpu_percent`` is measured between two consecutive polls of this endpoint
    (psutil semantics), so the very first reading after a start is ``0.0``.
    """

    running: bool
    pid: int | None = None
    uptime_seconds: int | None = None
    cpu_percent: float | None = None
    memory_mb: float | None = None  # RSS of the java process (+children)
    ping: PingRead | None = None
