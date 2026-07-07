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
import time
from collections.abc import Awaitable, Callable

import psutil

from ..ws import ConsoleHub

# Vanilla/Fabric print e.g. `[Server thread/INFO]: Done (12.345s)! For help, …`.
_DONE_MARKER = "Done ("
# Modded servers emit ANSI color/cursor codes; strip them before display.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Grace period after terminate() before escalating to a psutil tree kill.
_TERMINATE_GRACE = 10

StateCallback = Callable[[str, str], Awaitable[None]]


class ServerProcess:
    def __init__(
        self,
        server_id: str,
        argv: list[str],
        cwd: str,
        hub: ConsoleHub,
        on_state: StateCallback,
    ) -> None:
        self.server_id = server_id
        self.argv = argv
        self.cwd = cwd
        self.hub = hub
        self.on_state = on_state
        self._proc: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.started_at: float | None = None  # wall-clock, for uptime display

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
                if not self._stopping and _DONE_MARKER in line:
                    await self.on_state(self.server_id, "running")
        finally:
            code = await self._proc.wait()
            if self._stopping:
                final = "stopped"
            else:
                final = "stopped" if code == 0 else "crashed"
            self.hub.publish(self.server_id, f"[lectern] process exited (code {code})")
            await self.on_state(self.server_id, final)

    async def send(self, command: str) -> None:
        if self._proc is not None and self._proc.stdin is not None and self.running:
            self._proc.stdin.write((command + "\n").encode())
            with contextlib.suppress(Exception):
                await self._proc.stdin.drain()

    async def stop(self, stop_command: str = "stop", timeout: int = 60) -> None:
        if not self.running or self._proc is None:
            return
        self._stopping = True
        await self.on_state(self.server_id, "stopping")

        await self.send(stop_command)
        if await self._wait(timeout):
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
        except asyncio.TimeoutError:
            return False
