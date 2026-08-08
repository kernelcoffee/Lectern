"""A single running server process.

Wraps an ``asyncio`` subprocess: one background task pumps stdout line-by-line
into the ``ConsoleHub`` (history + live subscribers), stdin carries console
commands, and stop escalates ``stop`` command → ``terminate`` → ``psutil`` tree
kill. Status is reported back to the manager via the ``on_state`` callback:

    starting → running (once the server prints "Done (") → stopping → stopped,
    or → crashed on an unexpected exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
import time
from collections.abc import Awaitable, Callable

import psutil

from ..ws import ConsoleHub
from .roster import Roster

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
        self.roster = Roster()  # online players, parsed from console output

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

    async def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                # Strip ANSI escape codes before publishing: the browser console
                # renders plain text, and modded servers love colored output.
                line = _ANSI_RE.sub("", raw.decode(errors="replace")).rstrip("\r\n")
                self.hub.publish(self.server_id, line)
                self.roster.feed(line)
                if not self._stopping and self.ready_marker in line:
                    await self.on_state(self.server_id, "running", "")
        finally:
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
            return True
        except TimeoutError:
            return False

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
