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

from ..db import get_session
from ..models import (
    InstallProgressRead,
    Server,
    ServerCreate,
    ServerDetailRead,
    ServerRead,
    ServerStatus,
)
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
        eula_accepted=eula_accepted(Path(server.path)) if server.path else False,
        running=manager.is_running(server.id),
    )


@router.get("", response_model=list[ServerRead])
def list_servers(session: Session = Depends(get_session)) -> list[Server]:
    return list(session.exec(select(Server).order_by(Server.created_at)).all())


@router.post("", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
def create_server(
    payload: ServerCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Server:
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
    if server.path:
        shutil.rmtree(Path(server.path), ignore_errors=True)
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
