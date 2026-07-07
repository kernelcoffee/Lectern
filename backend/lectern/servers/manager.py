"""Server process registry + lifecycle.

Owns the ``{server_id: ServerProcess}`` map and the single ``ConsoleHub``, and is
the one place that writes ``Server.status`` as processes come and go. Endpoints
call ``start``/``stop``/``restart``/``kill``/``send``; the process layer calls
back into ``_on_state`` when a server changes state or exits.

Stop and restart are scheduled (fire-and-forget) so the HTTP request returns
immediately — the graceful-shutdown wait happens in the background and the UI
follows the status field.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from sqlmodel import Session, select

from ..db import engine
from ..models import Server, ServerStatus
from ..servers.install import build_launch_command, eula_accepted
from ..ws import ConsoleHub
from .process import ServerProcess

_RESTART_DELAY = 3  # seconds before a crash-restart


class ManagerError(Exception):
    """Base for lifecycle errors; ``status_code`` maps to the HTTP response."""

    status_code = 400


class NotInstalled(ManagerError):
    status_code = 409


class EulaNotAccepted(ManagerError):
    status_code = 409


class AlreadyRunning(ManagerError):
    status_code = 409


class NotRunning(ManagerError):
    status_code = 409


class ServerManager:
    def __init__(self) -> None:
        self.hub = ConsoleHub()
        self._procs: dict[str, ServerProcess] = {}

    # --- queries -----------------------------------------------------------

    def is_running(self, server_id: str) -> bool:
        proc = self._procs.get(server_id)
        return bool(proc and proc.running)

    # --- status plumbing ---------------------------------------------------

    def _write_status(self, server_id: str, status: str) -> Server | None:
        with Session(engine) as session:
            server = session.get(Server, server_id)
            if server is not None:
                server.status = status
                session.add(server)
                session.commit()
                session.refresh(server)
            return server

    async def _on_state(self, server_id: str, status: str) -> None:
        server = self._write_status(server_id, status)
        if status in (ServerStatus.stopped.value, ServerStatus.crashed.value):
            self._procs.pop(server_id, None)
            if status == ServerStatus.crashed.value and server is not None and server.crash_restart:
                self.hub.publish(server_id, "[lectern] crash detected — restarting…")
                asyncio.create_task(self._delayed_restart(server_id))

    async def _delayed_restart(self, server_id: str) -> None:
        await asyncio.sleep(_RESTART_DELAY)
        with contextlib.suppress(ManagerError):
            await self.start(server_id)

    # --- lifecycle ---------------------------------------------------------

    async def start(self, server_id: str) -> None:
        if self.is_running(server_id):
            raise AlreadyRunning("Server is already running")

        with Session(engine) as session:
            server = session.get(Server, server_id)
            if server is None:
                raise NotRunning("Server not found")
            if not server.server_jar or not server.java_path or not server.path:
                raise NotInstalled("Server is not installed yet")
            if not eula_accepted(Path(server.path)):
                raise EulaNotAccepted("The Minecraft EULA must be accepted first")
            argv = build_launch_command(
                server.java_path, server.memory_mb, server.server_jar, server.jvm_args
            )
            cwd = server.path
            server.status = ServerStatus.starting.value
            session.add(server)
            session.commit()

        self.hub.clear(server_id)
        proc = ServerProcess(server_id, argv, cwd, self.hub, self._on_state)
        self._procs[server_id] = proc
        self.hub.publish(server_id, f"[lectern] starting: {' '.join(argv)}")
        await proc.start()

    async def stop(self, server_id: str) -> None:
        proc = self._procs.get(server_id)
        if proc is None or not proc.running:
            raise NotRunning("Server is not running")
        stop_command, timeout = self._stop_params(server_id)
        # Graceful shutdown can take up to `timeout`; don't block the request.
        asyncio.create_task(proc.stop(stop_command, timeout))

    async def restart(self, server_id: str) -> None:
        proc = self._procs.get(server_id)
        if proc is not None and proc.running:
            asyncio.create_task(self._restart_flow(server_id, proc))
        else:
            await self.start(server_id)

    async def _restart_flow(self, server_id: str, proc: ServerProcess) -> None:
        stop_command, timeout = self._stop_params(server_id)
        await proc.stop(stop_command, timeout)
        with contextlib.suppress(ManagerError):
            await self.start(server_id)

    async def kill(self, server_id: str) -> None:
        proc = self._procs.get(server_id)
        if proc is None or not proc.running:
            raise NotRunning("Server is not running")
        await proc.kill()

    async def send(self, server_id: str, command: str) -> None:
        proc = self._procs.get(server_id)
        if proc is None or not proc.running:
            raise NotRunning("Server is not running")
        await proc.send(command)

    def _stop_params(self, server_id: str) -> tuple[str, int]:
        with Session(engine) as session:
            server = session.get(Server, server_id)
            if server is None:
                return "stop", 60
            return server.stop_command, server.shutdown_timeout

    # --- app lifecycle -----------------------------------------------------

    def reconcile(self) -> None:
        """At startup no processes exist; downgrade any stale live statuses so
        the UI doesn't show a phantom running server after a restart."""
        stale = (
            ServerStatus.starting.value,
            ServerStatus.running.value,
            ServerStatus.stopping.value,
        )
        with Session(engine) as session:
            rows = session.exec(select(Server).where(Server.status.in_(stale))).all()
            for server in rows:
                server.status = ServerStatus.stopped.value
                session.add(server)
            session.commit()

    async def shutdown(self) -> None:
        for server_id in list(self._procs):
            with contextlib.suppress(Exception):
                await self.kill(server_id)


# Process-wide singleton (matches the single-instance, LAN-scale deployment).
manager = ServerManager()
