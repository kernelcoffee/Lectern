"""Server creation pipeline (M3).

Turns a freshly-created ``Server`` record into a runnable server on disk:

    servers/{id}/  ← download server jar → provision Java → write eula.txt +
    server.properties → record the launch jar + Java path → mark ``stopped``.

Runs as a background task kicked off by ``POST /api/servers``. Progress is held
in a process-local registry (``get_progress``) surfaced by the progress
endpoint; a WebSocket feed is layered on in M4.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from ..config import get_settings
from ..db import engine
from ..models import Server, ServerStatus
from ..providers import adoptium, mojang
from ..providers.base import download_file
from .types import get_server_type

# --- progress registry -----------------------------------------------------


@dataclass
class InstallProgress:
    server_id: str
    step: str = "queued"
    message: str = "Queued"
    done: bool = False
    error: str | None = None


_progress: dict[str, InstallProgress] = {}


def get_progress(server_id: str) -> InstallProgress | None:
    return _progress.get(server_id)


def _set(server_id: str, step: str, message: str, *, done: bool = False, error: str | None = None) -> None:
    _progress[server_id] = InstallProgress(
        server_id=server_id, step=step, message=message, done=done, error=error
    )


# --- pure helpers (unit-tested) --------------------------------------------

DEFAULT_PROPERTIES: dict[str, str] = {
    "motd": "A Lectern server",
    "online-mode": "true",
    "max-players": "20",
}


def render_server_properties(port: int, extra: dict[str, str] | None = None) -> str:
    """Render a minimal ``server.properties``. Minecraft fills in the rest on
    first launch; we only pin what the manager owns (the port)."""
    props = {**DEFAULT_PROPERTIES, "server-port": str(port), **(extra or {})}
    return "".join(f"{k}={v}\n" for k, v in sorted(props.items()))


def build_launch_command(
    java_path: str, memory_mb: int, jar_name: str, jvm_args: str = ""
) -> list[str]:
    """Assemble the process argv for launching a server (consumed in M4)."""
    cmd = [java_path, f"-Xmx{memory_mb}M", f"-Xms{memory_mb}M"]
    if jvm_args.strip():
        cmd.extend(jvm_args.split())
    cmd.extend(["-jar", jar_name, "nogui"])
    return cmd


def eula_accepted(server_dir: Path) -> bool:
    """True if ``eula.txt`` in ``server_dir`` records acceptance."""
    eula = server_dir / "eula.txt"
    if not eula.exists():
        return False
    for line in eula.read_text().splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("eula="):
            return stripped[len("eula=") :].strip() == "true"
    return False


def set_eula(server_dir: Path, accepted: bool) -> None:
    (server_dir / "eula.txt").write_text(f"eula={'true' if accepted else 'false'}\n")


# --- pipeline --------------------------------------------------------------


def _record_deleted(session: Session, server_id: str) -> bool:
    """True if the server row vanished (deleted mid-install). Uses a fresh
    query — the session's identity map would happily hand back the cached
    object and a later commit would resurrect the deleted row."""
    session.expire_all()
    return session.exec(select(Server.id).where(Server.id == server_id)).first() is None


def _discard_install(server_id: str) -> None:
    """Drop the partially-installed directory of a deleted server."""
    shutil.rmtree(get_settings().servers_dir / server_id, ignore_errors=True)
    _set(server_id, "deleted", "Server was deleted during install", done=True, error="deleted")


async def install_server(server_id: str) -> None:
    """Provision the server on disk. Owns its own DB session (the request's is
    already closed by the time this background task runs) and never raises —
    failures are recorded on the record (``install_failed``) and in progress.
    If the server is deleted while installing, the pipeline discards its work
    instead of resurrecting the row."""
    _set(server_id, "starting", "Preparing…")
    try:
        with Session(engine) as session:
            server = session.get(Server, server_id)
            if server is None:
                _set(server_id, "error", "Server record not found", done=True, error="not found")
                return
            try:
                await _run(session, server)
                _set(server_id, "done", "Ready", done=True)
            except Exception as exc:  # noqa: BLE001 — surfaced to the user, not swallowed
                if _record_deleted(session, server_id):
                    _discard_install(server_id)
                    return
                server.status = ServerStatus.install_failed.value
                session.add(server)
                session.commit()
                _set(server_id, "error", "Install failed", done=True, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — a background task must never raise
        _set(server_id, "error", "Install failed", done=True, error=str(exc))


async def _run(session: Session, server: Server) -> None:
    settings = get_settings()
    # Resolve to absolute paths before persisting: the launch subprocess runs
    # with cwd=server dir, so a relative java_path (LECTERN_DATA=./data) would
    # be resolved against the wrong directory and fail with FileNotFoundError.
    server_dir = (settings.servers_dir / server.id).resolve()
    server_dir.mkdir(parents=True, exist_ok=True)

    provider = get_server_type(server.type)

    _set(server.id, "resolving", "Resolving server jar…")
    spec = await provider.resolve_jar(server.mc_version, server.loader_version)

    _set(server.id, "downloading-jar", f"Downloading {spec.jar_name}…")
    await download_file(
        spec.url, server_dir / spec.jar_name, expected_hash=spec.sha1, hash_algo="sha1"
    )

    # Prefer Mojang's declared requirement (authoritative, e.g. MC 26.2 → 25);
    # fall back to the version-range heuristic only when it's absent.
    java_major = await mojang.get_java_major(server.mc_version)
    if java_major is None:
        java_major = adoptium.java_major_for_mc(server.mc_version)
    _set(server.id, "installing-java", f"Provisioning Java {java_major}…")
    java_exe = await adoptium.ensure_java(java_major, settings.java_dir)

    _set(server.id, "writing-config", "Writing configuration…")
    (server_dir / "eula.txt").write_text("eula=false\n")
    (server_dir / "server.properties").write_text(render_server_properties(server.port))

    # The downloads above can take minutes — bail out (and clean up) if the
    # record was deleted meanwhile, instead of re-inserting it below.
    if _record_deleted(session, server.id):
        _discard_install(server.id)
        return

    server.path = str(server_dir)
    server.server_jar = spec.jar_name
    server.loader_version = spec.loader_version if provider.needs_loader else None
    server.java_major = java_major
    server.java_path = str(Path(java_exe).resolve())
    server.status = ServerStatus.stopped.value
    session.add(server)
    session.commit()
