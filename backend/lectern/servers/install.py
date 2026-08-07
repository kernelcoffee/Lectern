"""Server creation pipeline (M3).

Turns a freshly-created ``Server`` record into a runnable server on disk:

    servers/{id}/  ← download server jar → provision Java → write eula.txt +
    server.properties → record the launch jar + Java path → mark ``stopped``.

Runs as a background task kicked off by ``POST /api/servers``. Progress is held
in a process-local registry (``get_progress``) surfaced by the progress
endpoint; a WebSocket feed is layered on in M4.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlmodel import Session

from ..config import get_settings
from ..db import engine
from ..models import Server, ServerStatus
from ..providers import adoptium, mojang
from ..providers.base import download_file
from ..ws import progress_hub
from .types import JarSpec, get_server_type

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
    progress = InstallProgress(
        server_id=server_id, step=step, message=message, done=done, error=error
    )
    _progress[server_id] = progress
    # Live subscribers (WS /ws/servers/{id}/install) get every update; the
    # registry above stays the source of truth for snapshots/polling.
    progress_hub.publish(server_id, asdict(progress))


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
    """Assemble the process argv for launching a server (consumed in M4).

    ``jar_name`` is either a jar (``-jar name.jar``) or, for installer-based
    types (Forge/NeoForge), a JVM @args file reference (``@libraries/…/
    unix_args.txt``) passed through verbatim — that file carries the module
    path and main class the installer laid down."""
    cmd = [java_path, f"-Xmx{memory_mb}M", f"-Xms{memory_mb}M"]
    if jvm_args.strip():
        cmd.extend(jvm_args.split())
    if jar_name.startswith("@"):
        cmd.append(jar_name)
    else:
        cmd.extend(["-jar", jar_name])
    cmd.append("nogui")
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


# --- provisioning (shared by create + version change) ----------------------


async def provision(
    server: Server,
    *,
    mc_version: str,
    loader_version: str | None,
    emit: Callable[[str, str], None] | None = None,
) -> Path:
    """Resolve + download the server jar and matching Java for
    ``(mc_version, loader_version)`` and update the record's jar/loader/java
    fields in place. Shared by the M3 create pipeline and the M9.5 version
    change; the caller owns the session and the commit. Returns the server dir.

    Does **not** touch ``eula.txt`` or ``server.properties`` — those are
    create-only (a version change must preserve the accepted EULA and the
    existing world/config). ``emit(step, message)`` surfaces progress; the
    create path wires it to the progress registry, the version change ignores
    it (the request is synchronous)."""
    step = emit or (lambda _s, _m: None)
    settings = get_settings()
    # Resolve to absolute paths before persisting: the launch subprocess runs
    # with cwd=server dir, so a relative java_path (LECTERN_DATA=./data) would
    # be resolved against the wrong directory and fail with FileNotFoundError.
    server_dir = (settings.servers_dir / server.id).resolve()
    server_dir.mkdir(parents=True, exist_ok=True)

    provider = get_server_type(server.type)

    step("resolving", "Resolving server jar…")
    spec = await provider.resolve_jar(mc_version, loader_version)

    # Java first: installer-based types (Quilt/Forge/NeoForge) need it to run
    # their installer. Prefer Mojang's declared requirement (authoritative,
    # e.g. MC 26.2 → 25); fall back to the range heuristic when absent.
    java_major = await mojang.get_java_major(mc_version)
    if java_major is None:
        java_major = adoptium.java_major_for_mc(mc_version)
    step("installing-java", f"Provisioning Java {java_major}…")
    java_exe = await adoptium.ensure_java(java_major, settings.java_dir)
    java_path = str(Path(java_exe).resolve())

    step("downloading-jar", f"Downloading {spec.jar_name}…")
    await download_file(
        spec.url, server_dir / spec.jar_name, expected_hash=spec.sha1, hash_algo="sha1"
    )

    if spec.is_installer:
        step(
            "running-installer",
            f"Running the {server.type} installer (downloads libraries — can take a few minutes)…",
        )
        launch_target = await run_installer(server_dir, java_path, spec)
    else:
        launch_target = spec.jar_name

    server.path = str(server_dir)
    server.mc_version = mc_version
    server.server_jar = launch_target
    server.loader_version = spec.loader_version if provider.needs_loader else None
    server.java_major = java_major
    server.java_path = java_path
    return server_dir


_INSTALLER_TIMEOUT = 900  # seconds — installers download every library


async def run_installer(server_dir: Path, java_path: str, spec: JarSpec) -> str:
    """Run an installer jar (Quilt/Forge/NeoForge) inside the server dir and
    return the launch target it produced: a jar name, or ``@<relpath>`` for a
    JVM args file. Output is kept in ``installer.log`` only on failure; the
    installer jar itself is removed on success."""
    log_path = server_dir / "installer.log"
    proc = await asyncio.create_subprocess_exec(
        java_path,
        "-jar",
        spec.jar_name,
        *spec.installer_args,
        cwd=server_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_INSTALLER_TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"{spec.jar_name} timed out after {_INSTALLER_TIMEOUT}s"
        ) from None
    if proc.returncode != 0:
        log_path.write_bytes(out or b"")
        tail = (out or b"").decode(errors="replace").strip().splitlines()[-8:]
        raise RuntimeError(
            f"{spec.jar_name} failed (exit {proc.returncode}; full output in "
            f"installer.log): " + " | ".join(tail)
        )
    matches = sorted(server_dir.glob(spec.launch_glob or ""))
    if not matches:
        log_path.write_bytes(out or b"")
        raise RuntimeError(
            f"Installer finished but nothing matched {spec.launch_glob} "
            "(full output in installer.log)"
        )
    target = matches[-1]  # newest when several versions linger after upgrades
    (server_dir / spec.jar_name).unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    rel = target.relative_to(server_dir)
    return f"@{rel}" if target.suffix == ".txt" else str(rel)


# --- pipeline --------------------------------------------------------------


def _record_deleted(server_id: str) -> bool:
    """True if the server row vanished (deleted mid-install).

    Probed on a **separate** session on purpose: expiring the working session
    (the old approach) would also discard the uncommitted field changes that
    ``provision`` just made to the in-memory record (path, jar, java_path), so
    the final commit would persist ``status=stopped`` with an empty ``path``
    and leave the server un-startable. A fresh read touches nothing pending."""
    with Session(engine) as probe:
        return probe.get(Server, server_id) is None


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
                if _record_deleted(server_id):
                    _discard_install(server_id)
                    return
                server.status = ServerStatus.install_failed.value
                session.add(server)
                session.commit()
                _set(server_id, "error", "Install failed", done=True, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — a background task must never raise
        _set(server_id, "error", "Install failed", done=True, error=str(exc))


async def _run(session: Session, server: Server) -> None:
    server_dir = await provision(
        server,
        mc_version=server.mc_version,
        loader_version=server.loader_version,
        emit=lambda step, message: _set(server.id, step, message),
    )

    _set(server.id, "writing-config", "Writing configuration…")
    (server_dir / "eula.txt").write_text("eula=false\n")
    # Create-time server.properties overrides: white-list secure-by-default, and
    # a chosen world seed. Both only matter for the first launch/world-gen.
    extra: dict[str, str] = {}
    if server.whitelist:
        extra["white-list"] = "true"
    if server.seed.strip():
        extra["level-seed"] = server.seed.strip()
    (server_dir / "server.properties").write_text(
        render_server_properties(server.port, extra or None)
    )

    # The downloads above can take minutes — bail out (and clean up) if the
    # record was deleted meanwhile, instead of re-inserting it below.
    if _record_deleted(server.id):
        _discard_install(server.id)
        return

    server.status = ServerStatus.stopped.value
    session.add(server)
    session.commit()
