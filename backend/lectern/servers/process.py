"""A single running server process.

Wraps an ``asyncio`` subprocess: one background task pumps stdout line-by-line
into the ``ConsoleHub`` (history + live subscribers), stdin carries console
commands, and stop escalates ``stop`` command → ``terminate`` → ``psutil`` tree
kill. Status is reported back to the manager via the ``on_state`` callback:

    starting → running (once the server prints "Done (") → stopping → stopped,
    or → crashed on an unexpected exit.

On 1.21.9+ the manager also hands over an ``MsmpClient`` (``msmp.py``): while
it is connected the management protocol seeds and updates the roster and its
``server/started`` / ``server/stopping`` notifications report ``running`` /
``stopping``. The console (roster lines, the ready marker) stays the fallback;
``running`` is reported at most once per process whichever source sees it
first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
import time
from collections.abc import Awaitable, Callable

import psutil

from ..ws import ConsoleHub
from .msmp import (
    M_PLAYERS,
    M_STATUS,
    N_JOINED,
    N_LEFT,
    N_STARTED,
    N_STOPPING,
    MsmpClient,
    MsmpError,
)
from .roster import Roster

log = logging.getLogger(__name__)

# Vanilla/Fabric print e.g. `[Server thread/INFO]: Done (12.345s)! For help, …`.
# Proxies differ — Velocity prints `Listening on /…:25577` when ready, so the
# manager passes a type-appropriate marker.
_DONE_MARKER = "Done ("
# Modded servers emit ANSI color/cursor codes; strip them before display.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Grace period after terminate() before escalating to a psutil tree kill.
_TERMINATE_GRACE = 10
# How often to announce the remaining time during a graceful stop.
_COUNTDOWN_STEP = 10
# How long stop()/kill() wait, after the exit, for the pump to report it.
_PUMP_SETTLE = 5

# (server_id, status, detail) — detail is a human-readable reason, only
# non-empty for "crashed" (exit code / signal explanation).
StateCallback = Callable[[str, str, str], Awaitable[None]]


def describe_exit(code: int) -> str:
    """Human-readable crash reason from a process exit code.

    Negative codes are deaths by signal (asyncio convention); 128+N is the
    same thing reported shell-style. SIGKILL gets the special mention because
    its overwhelmingly common cause is the kernel's OOM killer."""
    signum = -code if code < 0 else (code - 128 if code > 128 else None)
    if signum is not None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        if signum == signal.SIGKILL:
            return (
                f"killed by the system ({name}) — usually the host ran out of "
                "memory (OOM killer)"
            )
        if signum == signal.SIGTERM:
            return f"terminated by the system ({name})"
        return f"killed by {name}"
    return f"exited with code {code}"



class ServerProcess:
    def __init__(
        self,
        server_id: str,
        argv: list[str],
        cwd: str,
        hub: ConsoleHub,
        on_state: StateCallback,
        ready_marker: str = _DONE_MARKER,
        msmp: MsmpClient | None = None,
    ) -> None:
        self.server_id = server_id
        self.ready_marker = ready_marker
        self.argv = argv
        self.cwd = cwd
        self.hub = hub
        self.on_state = on_state
        self._proc: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.started_at: float | None = None  # wall-clock, for uptime display
        self.roster = Roster()  # online players: MSMP first, console fallback
        self.msmp = msmp
        self._running_reported = False
        # Roster notifications that arrive while the connect-time snapshot is
        # in flight; replayed on top of it so none is lost or overwritten.
        self._roster_backlog: list[tuple[str, list]] | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.started_at = time.time()
        self._pump_task = asyncio.create_task(self._pump())
        await self._start_msmp()

    async def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                # Strip ANSI escape codes before publishing: the browser console
                # renders plain text, and modded servers love colored output.
                await self._handle_line(
                    _ANSI_RE.sub("", raw.decode(errors="replace")).rstrip("\r\n")
                )
        finally:
            # Cancel the management client without yielding, report the final
            # state, and only then wait for the client's teardown: a restart
            # resumes as soon as stop() sees the exit, and must find this run
            # already reported as over.
            if self.msmp is not None:
                self.msmp.abort()
            code = await self._proc.wait()
            if self._stopping or code == 0:
                final, detail = "stopped", ""
                self.hub.publish(
                    self.server_id, f"[lectern] process exited (code {code})"
                )
            else:
                final, detail = "crashed", describe_exit(code)
                self.hub.publish(self.server_id, f"[lectern] crashed: {detail}")
            await self.on_state(self.server_id, final, detail)
            if self.msmp is not None:
                await self.msmp.close()

    async def _handle_line(self, line: str) -> None:
        self.hub.publish(self.server_id, line)
        self.roster.feed(line)
        if self.ready_marker in line:
            await self._report_running()

    async def _report_running(self) -> None:
        """``running`` once per process, whether the console marker or the
        management protocol sees it first (each report records an event)."""
        if self._stopping or self._running_reported:
            return
        self._running_reported = True
        await self.on_state(self.server_id, "running", "")

    # --- management protocol -------------------------------------------------

    async def _start_msmp(self) -> None:
        if self.msmp is None:
            return
        self.msmp.on_connect = self._msmp_connected
        self.msmp.on_notification = self._msmp_notification
        await self.msmp.start()

    async def _msmp_connected(self) -> None:
        assert self.msmp is not None
        self._roster_backlog = []
        try:
            players = await self.msmp.call(M_PLAYERS)
            if isinstance(players, list):
                self.roster.sync(players)
        except MsmpError as exc:
            log.info("msmp %s: player list failed: %s", self.server_id, exc)
        finally:
            backlog, self._roster_backlog = self._roster_backlog, None
            for method, params in backlog:
                self._apply_roster(method, params)
        try:
            status = await self.msmp.call(M_STATUS)
        except MsmpError as exc:
            log.info("msmp %s: status failed: %s", self.server_id, exc)
            return
        if isinstance(status, dict) and status.get("started"):
            await self._report_running()

    async def _msmp_notification(self, method: str, params: list) -> None:
        if method in (N_JOINED, N_LEFT):
            if self._roster_backlog is not None:
                self._roster_backlog.append((method, params))
            else:
                self._apply_roster(method, params)
        elif method == N_STARTED:
            await self._report_running()
        elif method == N_STOPPING and not self._stopping:
            # Not ``_stopping``: exit classification stays exit-code based.
            await self.on_state(self.server_id, "stopping", "")

    def _apply_roster(self, method: str, params: list) -> None:
        player = params[0] if params else None
        if not isinstance(player, dict) or not isinstance(player.get("name"), str):
            return
        if method == N_JOINED:
            uuid = player.get("id")
            self.roster.joined(player["name"], uuid if isinstance(uuid, str) else None)
        else:
            self.roster.left(player["name"])

    async def send(self, command: str) -> None:
        if self._proc is not None and self._proc.stdin is not None and self.running:
            self._proc.stdin.write((command + "\n").encode())
            with contextlib.suppress(Exception):
                await self._proc.stdin.drain()

    async def stop(self, stop_command: str = "stop", timeout: int = 60) -> None:
        if not self.running or self._proc is None:
            return
        self._stopping = True
        await self.on_state(self.server_id, "stopping", "")

        await self.send(stop_command)
        if await self._wait_countdown(timeout):
            return

        # Graceful stop timed out — ask the JVM to terminate.
        self.hub.publish(self.server_id, "[lectern] stop timed out — terminating")
        with contextlib.suppress(ProcessLookupError):
            self._proc.terminate()
        if await self._wait(_TERMINATE_GRACE):
            return

        await self.kill()

    async def kill(self) -> None:
        self._stopping = True
        if self._proc is None:
            return
        self.hub.publish(self.server_id, "[lectern] killing process tree")
        with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
            parent = psutil.Process(self._proc.pid)
            for child in parent.children(recursive=True):
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            parent.kill()
        await self._wait(5)

    async def _wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for exit; True if the process has exited."""
        if self._proc is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self._proc.wait()), timeout)
        except TimeoutError:
            return False
        # The pump reports the final state right after the exit; let it, so a
        # caller that starts the next run (restart) never races the old one's
        # ``stopped``. Bounded, in case a child still holds stdout open.
        if self._pump_task is not None:
            await asyncio.wait([self._pump_task], timeout=_PUMP_SETTLE)
        return True

    async def _wait_countdown(self, timeout: int) -> bool:
        """Wait for a graceful exit, announcing the remaining seconds into the
        console every ``_COUNTDOWN_STEP`` (ref: crafty-4). Each chunk returns
        immediately when the process actually exits, so this stays responsive.
        True if it exited within ``timeout``."""
        remaining = timeout
        while remaining > 0:
            chunk = min(_COUNTDOWN_STEP, remaining)
            if await self._wait(chunk):
                return True
            remaining -= chunk
            if remaining > 0:
                self.hub.publish(
                    self.server_id,
                    f"[lectern] waiting for graceful stop — {remaining}s until force stop",
                )
        return False
