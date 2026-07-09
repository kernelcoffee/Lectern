"""Server CRUD + lifecycle endpoints.

Creation kicks off the M3 install pipeline (download jar, provision Java, write
config) as a background task. M4 adds process control (start/stop/restart/kill),
EULA acceptance, and the live console WebSocket, all driven by the process
``manager``.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlmodel import Session, select

from ..config import get_settings
from ..db import get_session
from ..models import (
    InstallProgressRead,
    PingRead,
    PropertiesRead,
    Server,
    ServerCreate,
    ServerDetailRead,
    ServerRead,
    ServerSettingsUpdate,
    ServerSuggestRead,
    ServerStatsRead,
    ServerStatus,
)
from ..servers import properties as props
from ..servers import stats as stats_mod
from ..servers.install import eula_accepted, get_progress, install_server, set_eula
from ..servers.manager import ManagerError, manager

router = APIRouter(prefix="/api/servers", tags=["servers"])

# WebSocket console lives outside the /api prefix (see docs/technical.md §5),
# and Vite proxies /ws separately.
console_router = APIRouter(tags=["console"])

_ACTIONS = {"start", "stop", "restart", "kill"}


def _detail(server: Server) -> ServerDetailRead:
    data = ServerRead.model_validate(server, from_attributes=True).model_dump()
    return ServerDetailRead(
        **data,
        jvm_args=server.jvm_args,
        auto_start=server.auto_start,
        crash_restart=server.crash_restart,
        stop_command=server.stop_command,
        shutdown_timeout=server.shutdown_timeout,
        backup_excluded=server.backup_excluded,
        backup_max=server.backup_max,
        backup_compress=server.backup_compress,
        backup_stop_server=server.backup_stop_server,
        eula_accepted=eula_accepted(Path(server.path)) if server.path else False,
        running=manager.is_running(server.id),
    )


@router.get("", response_model=list[ServerRead])
def list_servers(session: Session = Depends(get_session)) -> list[Server]:
    return list(session.exec(select(Server).order_by(Server.created_at)).all())


# NB: declared before GET /{server_id} so "suggest" isn't read as an id.
@router.get("/suggest", response_model=ServerSuggestRead)
def suggest_defaults(session: Session = Depends(get_session)) -> ServerSuggestRead:
    """First free name/port for the create form (mirrors _check_conflicts:
    names compared case/whitespace-insensitively, ports across all servers)."""
    servers = session.exec(select(Server)).all()
    taken_names = {s.name.strip().lower() for s in servers}
    base = "New server"
    name, n = base, 2
    while name.lower() in taken_names:
        name = f"{base} {n}"
        n += 1
    taken_ports = {s.port for s in servers}
    port = 25565
    while port in taken_ports:
        port += 1
    return ServerSuggestRead(name=name, port=port)


def _check_conflicts(
    session: Session, *, name: str | None, port: int | None, exclude_id: str | None = None
) -> None:
    """409 when another server already uses this name (case-insensitive) or
    port. Ports must be unique across all servers — several records could
    share one if never run together, but that's a foot-gun, not a feature."""
    for other in session.exec(select(Server)).all():
        if other.id == exclude_id:
            continue
        if name is not None and other.name.strip().lower() == name.strip().lower():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f'A server named "{other.name}" already exists',
            )
        if port is not None and other.port == port:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f'Port {port} is already used by "{other.name}"',
            )


@router.post("", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
def create_server(
    payload: ServerCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Server:
    _check_conflicts(session, name=payload.name, port=payload.port)
    server = Server(
        name=payload.name,
        type=payload.type.value,
        mc_version=payload.mc_version,
        loader_version=payload.loader_version,
        port=payload.port,
        memory_mb=payload.memory_mb,
        status=ServerStatus.installing.value,
    )
    session.add(server)
    session.commit()
    session.refresh(server)
    # Download jar + provision Java after the response is sent; the client polls
    # the progress endpoint / re-reads the record for status.
    background_tasks.add_task(install_server, server.id)
    return server


@router.get("/{server_id}", response_model=ServerDetailRead)
def get_server(
    server_id: str, session: Session = Depends(get_session)
) -> ServerDetailRead:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    return _detail(server)


@router.post("/{server_id}/action/{action}", response_model=ServerDetailRead)
async def server_action(
    server_id: str, action: str, session: Session = Depends(get_session)
) -> ServerDetailRead:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if action not in _ACTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown action: {action}")
    try:
        if action == "start":
            # A user-initiated start wipes crash history so a server that
            # exhausted its auto-restart attempts gets a fresh set.
            manager.reset_crash_count(server_id)
        await getattr(manager, action)(server_id)
    except ManagerError as exc:
        raise HTTPException(exc.status_code, str(exc))
    session.refresh(server)
    return _detail(server)


@router.post("/{server_id}/eula", response_model=ServerDetailRead)
def accept_eula(
    server_id: str, session: Session = Depends(get_session)
) -> ServerDetailRead:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if not server.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not installed yet")
    set_eula(Path(server.path), True)
    return _detail(server)


@router.get("/{server_id}/progress", response_model=InstallProgressRead)
def server_progress(
    server_id: str, session: Session = Depends(get_session)
) -> InstallProgressRead:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    progress = get_progress(server_id)
    if progress is not None:
        return InstallProgressRead(**progress.__dict__)
    # No in-memory progress (e.g. after a restart): derive from the record.
    done = server.status != ServerStatus.installing.value
    failed = server.status == ServerStatus.install_failed.value
    return InstallProgressRead(
        server_id=server_id,
        step=server.status,
        message="Install failed" if failed else ("Ready" if done else "Installing…"),
        done=done,
        error="Install failed" if failed else None,
    )


@router.patch("/{server_id}", response_model=ServerDetailRead)
def update_server_settings(
    server_id: str,
    payload: ServerSettingsUpdate,
    session: Session = Depends(get_session),
) -> ServerDetailRead:
    """Update Lectern-owned settings (JSON-merge semantics: only fields present
    in the request are applied). Memory/JVM changes take effect at the next
    start; a port change is also written into ``server.properties`` because
    that file — not the DB column — is what the Minecraft process reads."""
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")

    updates = payload.model_dump(exclude_unset=True)
    _check_conflicts(
        session,
        name=updates.get("name"),
        port=updates.get("port"),
        exclude_id=server_id,
    )
    for key, value in updates.items():
        setattr(server, key, value)

    if "port" in updates and server.path:
        server_dir = Path(server.path)
        file_props = props.read_properties(server_dir)
        file_props["server-port"] = str(updates["port"])
        props.write_properties(server_dir, file_props)

    session.add(server)
    session.commit()
    session.refresh(server)
    return _detail(server)


@router.get("/{server_id}/properties", response_model=PropertiesRead)
def get_properties(
    server_id: str, session: Session = Depends(get_session)
) -> PropertiesRead:
    """Current ``server.properties`` plus the typed definitions map the UI
    renders widgets from. 409 until the install pipeline has created the
    server directory."""
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if not server.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not installed yet")
    return PropertiesRead(
        properties=props.read_properties(Path(server.path)),
        definitions=props.definitions_payload(),
    )


@router.patch("/{server_id}/properties", response_model=PropertiesRead)
def patch_properties(
    server_id: str,
    updates: dict[str, str | int | bool | None],
    session: Session = Depends(get_session),
) -> PropertiesRead:
    """Merge ``updates`` into ``server.properties``.

    Values are validated against the typed definitions (bad enum/int/bool →
    422 listing every offending key); ``null`` removes a key; unknown keys are
    stored as free-form strings. ``server-port`` is mirrored to the record's
    ``port`` column so the UI shows the truth. Changes apply at next start —
    Minecraft only reads the file on boot."""
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if not server.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not installed yet")

    server_dir = Path(server.path)
    file_props = props.read_properties(server_dir)

    errors: list[str] = []
    for key, value in updates.items():
        if value is None:
            file_props.pop(key, None)
            continue
        try:
            file_props[key] = props.normalize_value(key, value)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(errors))

    # A server-port change flows into the record below — reject conflicts
    # before anything is written.
    requested_port = file_props.get("server-port")
    if "server-port" in updates and requested_port is not None and requested_port.isdigit():
        _check_conflicts(session, name=None, port=int(requested_port), exclude_id=server_id)

    props.write_properties(server_dir, file_props)

    # Keep the record's port column in sync with the file (the file wins).
    new_port = file_props.get("server-port")
    if new_port is not None and new_port.isdigit() and int(new_port) != server.port:
        server.port = int(new_port)
        session.add(server)
        session.commit()

    return PropertiesRead(
        properties=file_props, definitions=props.definitions_payload()
    )


@router.get("/{server_id}/stats", response_model=ServerStatsRead)
async def server_stats(
    server_id: str, session: Session = Depends(get_session)
) -> ServerStatsRead:
    """Point-in-time stats snapshot (frontend polls this; no background loop).

    Resource usage comes from psutil on the live process; player counts/MOTD
    from a Server List Ping to localhost on the *effective* port — read from
    ``server.properties``, not the DB column, since that's what the process
    actually bound (see docs/references/crafty-4.md)."""
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")

    proc = manager.get_process(server_id)
    if proc is None or proc.pid is None:
        return ServerStatsRead(running=False)

    usage = stats_mod.resource_usage(proc.pid)

    # Effective port: the file wins over the DB column.
    port = server.port
    if server.path:
        file_port = props.read_properties(Path(server.path)).get("server-port")
        if file_port is not None and file_port.isdigit():
            port = int(file_port)

    ping = await stats_mod.server_list_ping("127.0.0.1", port)

    return ServerStatsRead(
        running=True,
        pid=proc.pid,
        uptime_seconds=(
            int(time.time() - proc.started_at) if proc.started_at else None
        ),
        cpu_percent=usage.cpu_percent if usage else None,
        memory_mb=usage.memory_mb if usage else None,
        ping=PingRead(**ping.__dict__) if ping else None,
    )


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: str, session: Session = Depends(get_session)
) -> None:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    # Stop a running process before removing its files.
    if manager.is_running(server_id):
        with contextlib.suppress(ManagerError):
            await manager.kill(server_id)
    # server.path is only set once install completes, so also remove the
    # canonical directory — covers deleting a server mid-install.
    if server.path:
        shutil.rmtree(Path(server.path), ignore_errors=True)
    shutil.rmtree(get_settings().servers_dir / server_id, ignore_errors=True)
    session.delete(server)
    session.commit()


@console_router.websocket("/ws/servers/{server_id}/console")
async def console_ws(websocket: WebSocket, server_id: str) -> None:
    """Replay console history, stream live output, and forward inbound text as
    console commands to the server's stdin."""
    await websocket.accept()
    hub = manager.hub
    for line in hub.history(server_id):
        await websocket.send_text(line)

    queue = hub.subscribe(server_id)

    async def forward() -> None:
        while True:
            await websocket.send_text(await queue.get())

    forward_task = asyncio.create_task(forward())
    try:
        while True:
            command = await websocket.receive_text()
            with contextlib.suppress(ManagerError):
                await manager.send(server_id, command)
    except WebSocketDisconnect:
        pass
    finally:
        forward_task.cancel()
        hub.unsubscribe(server_id, queue)
