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

from .. import events
from ..db import engine
from ..models import Server, ServerStatus
from ..servers.install import build_launch_command, eula_accepted
from ..servers.types import is_proxy_type
from ..ws import ConsoleHub
from .process import ServerProcess

_RESTART_DELAY = 3  # seconds before a crash-restart
# Give up after this many consecutive crashes (counter resets when a start
# reaches `running`) so a boot-looping server doesn't restart forever.
_MAX_CRASH_RESTARTS = 3


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


class PortInUse(ManagerError):
    status_code = 409


class ServerManager:
    def __init__(self) -> None:
        self.hub = ConsoleHub()
        self._procs: dict[str, ServerProcess] = {}
        self._crash_counts: dict[str, int] = {}

    # --- queries -----------------------------------------------------------

    def is_running(self, server_id: str) -> bool:
        proc = self._procs.get(server_id)
        return bool(proc and proc.running)

    def get_process(self, server_id: str) -> ServerProcess | None:
        """The live process for a server, if any (used by the stats endpoint)."""
        proc = self._procs.get(server_id)
        return proc if proc is not None and proc.running else None

    def running_processes(self) -> list[tuple[str, ServerProcess]]:
        """(server_id, process) for every currently-running server — used by the
        stats sampler to record a resource snapshot per tick."""
        return [
            (server_id, proc)
            for server_id, proc in self._procs.items()
            if proc.running
        ]

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

    async def _on_state(self, server_id: str, status: str, detail: str = "") -> None:
        server = self._write_status(server_id, status)
        if status == ServerStatus.running.value:
            self._crash_counts.pop(server_id, None)
            events.record(server_id, "started")
        if status in (ServerStatus.stopped.value, ServerStatus.crashed.value):
            self._procs.pop(server_id, None)
            if status == ServerStatus.stopped.value:
                events.record(server_id, "stopped")
            if status == ServerStatus.crashed.value:
                # The reason (exit code / signal) — so "why?" is answerable
                # from the timeline instead of archaeology in kernel logs.
                events.record(server_id, "crashed", detail)
            if status == ServerStatus.crashed.value and server is not None and server.crash_restart:
                crashes = self._crash_counts.get(server_id, 0) + 1
                self._crash_counts[server_id] = crashes
                if crashes > _MAX_CRASH_RESTARTS:
                    events.record(
                        server_id,
                        "crash_gave_up",
                        f"crashed {crashes} times in a row",
                    )
                    self.hub.publish(
                        server_id,
                        f"[lectern] crashed {crashes} times in a row — giving up on auto-restart",
                    )
                    return
                events.record(
                    server_id,
                    "crash_restart",
                    f"attempt {crashes}/{_MAX_CRASH_RESTARTS}",
                )
                self.hub.publish(
                    server_id,
                    f"[lectern] crash detected — restarting… (attempt {crashes}/{_MAX_CRASH_RESTARTS})",
                )
                asyncio.create_task(self._delayed_restart(server_id))

    def reset_crash_count(self, server_id: str) -> None:
        """Forget crash history — called on a user-initiated start so a server
        that exhausted its auto-restarts gets a fresh set of attempts."""
        self._crash_counts.pop(server_id, None)

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
            # Proxies (Velocity) have no Minecraft EULA to accept.
            if not is_proxy_type(server.type) and not eula_accepted(Path(server.path)):
                raise EulaNotAccepted("The Minecraft EULA must be accepted first")
            # Never launch a java binary located *inside* the server directory —
            # a mod could drop one there and get it executed (ref: crafty-4).
            java = Path(server.java_path).resolve()
            server_dir = Path(server.path).resolve()
            if java == server_dir or server_dir in java.parents:
                raise NotInstalled(
                    "Refusing to launch: the Java binary is inside the server "
                    "directory (possible tampering)"
                )
            # Port guard: records aren't unique on port (see api/servers.py), so
            # reject launching onto a port a *running* server already binds —
            # otherwise the JVM just fails to bind with a cryptic log line.
            from .stats_sampler import effective_port

            my_port = effective_port(server)
            for other_id, _proc in self.running_processes():
                if other_id == server_id:
                    continue
                other = session.get(Server, other_id)
                if other is not None and effective_port(other) == my_port:
                    raise PortInUse(
                        f'Port {my_port} is already in use by "{other.name}"'
                    )
            proxy = is_proxy_type(server.type)
            argv = build_launch_command(
                server.java_path, server.memory_mb, server.server_jar, server.jvm_args,
                nogui=not proxy,
            )
            cwd = server.path
            # Drop rotated logs past the retention window before this run adds more.
            from . import logs as server_logs

            server_logs.prune_logs(Path(server.path), server.log_retention_days)
            server.status = ServerStatus.starting.value
            session.add(server)
            session.commit()

        self.hub.clear(server_id)
        # Proxies signal readiness differently (Velocity: "Listening on …").
        ready_marker = "Listening on" if proxy else "Done ("
        proc = ServerProcess(
            server_id, argv, cwd, self.hub, self._on_state, ready_marker=ready_marker
        )
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
        the UI doesn't show a phantom running server after a restart. Installs
        run in-process, so a record still `installing` after a restart can
        never finish — mark it failed rather than spinning forever."""
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
            installing = session.exec(
                select(Server).where(Server.status == ServerStatus.installing.value)
            ).all()
            for server in installing:
                server.status = ServerStatus.install_failed.value
                session.add(server)
            session.commit()

    def autostart(self) -> None:
        """Schedule a delayed start for every ``auto_start`` server (called from
        the app lifespan after ``reconcile``). Each waits its own
        ``auto_start_delay`` so a host with several auto-start servers doesn't
        launch every JVM at once. Runs only for installed, EULA-accepted
        servers; others are skipped with a console note rather than erroring."""
        with Session(engine) as session:
            rows = session.exec(
                select(Server).where(Server.auto_start == True)
            ).all()
            candidates = [(s.id, s.auto_start_delay) for s in rows]
        for server_id, delay in candidates:
            asyncio.create_task(self._delayed_autostart(server_id, delay))

    async def _delayed_autostart(self, server_id: str, delay: int) -> None:
        await asyncio.sleep(max(0, delay))
        self.reset_crash_count(server_id)
        try:
            await self.start(server_id)
        except EulaNotAccepted:
            self.hub.publish(
                server_id, "[lectern] auto-start skipped — EULA not accepted"
            )
        except ManagerError as exc:
            self.hub.publish(server_id, f"[lectern] auto-start skipped — {exc}")

    async def shutdown(self) -> None:
        for server_id in list(self._procs):
            with contextlib.suppress(Exception):
                await self.kill(server_id)


# Process-wide singleton (matches the single-instance, LAN-scale deployment).
manager = ServerManager()
