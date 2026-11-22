"""Graceful shutdown: ``ServerManager.shutdown`` stops every server the way the
Stop button does, all at once, and nothing can start a server afterwards.

The servers are real processes: a small Python "server" launched through a
fake ``java`` wrapper, so the whole ``start`` path runs (status writes, the
``on_state`` wiring, events). The fake records every stdin line it receives in
``received.txt`` and every boot in ``boots.txt`` in its server directory, so a
test can prove which command reached it and how many times.

Each test builds its own ``ServerManager``; the process-wide singleton is
never shut down here (``_singleton_flag_untouched`` checks it), so the
start refusal cannot leak into the rest of the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select
from websockets.sync.client import connect

from lectern import main as main_module
from lectern.db import engine, init_db
from lectern.models import Server, ServerEvent, ServerStatus
from lectern.scheduler import SchedulerService
from lectern.servers import manager as manager_module
from lectern.servers import process as process_module
from lectern.servers.manager import ServerManager, ShuttingDown
from lectern.servers.process import ServerProcess
from lectern.servers.stats_sampler import StatsSampler

FAKE_SERVER = r"""
import os, pathlib, select, signal, sys, time

stop_cmd, delay, mode = sys.argv[1], float(sys.argv[2]), sys.argv[3]
here = pathlib.Path.cwd()
parent = os.getppid()


def note(name, text):
    with open(here / name, "a") as f:
        f.write(text + "\n")


note("boots.txt", str(os.getpid()))
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "saves":
    # Like a JVM's shutdown hook: SIGTERM takes a while to save, then exits 0.
    def on_term(*_):
        time.sleep(1.5)
        note("saved.txt", "yes")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
print("fake server booting", flush=True)
if mode != "starting":
    print('[Server thread/INFO]: Done (0.1s)! For help, type "help"', flush=True)
deaf = mode in ("deaf", "stubborn")
outlives_eof = deaf or mode == "saves"
deadline = None
pending = b""
while True:
    wait = None if deadline is None else deadline - time.monotonic()
    if wait is not None and wait <= 0:
        sys.exit(0)
    ready, _, _ = select.select([0], [], [], wait)
    if not ready:
        continue
    chunk = os.read(0, 4096)
    if not chunk:
        note("received.txt", "<eof>")
        # Like a JVM, a deaf server outlives the end of its stdin, but not the
        # test run that started it (a stubborn one ignores the parent-death
        # SIGTERM too).
        while outlives_eof and os.getppid() == parent:
            time.sleep(0.1)
        sys.exit(0)
    pending += chunk
    *lines, pending = pending.split(b"\n")
    for raw in lines:
        line = raw.decode().strip()
        note("received.txt", line)
        if line == stop_cmd and not deaf and deadline is None:
            deadline = time.monotonic() + delay
"""


@pytest.fixture(autouse=True)
def _singleton_flag_untouched() -> Iterator[None]:
    yield
    assert manager_module.manager._shutting_down is False


def _install_fake(
    root: Path,
    *,
    port: int,
    mode: str = "graceful",
    delay: float = 0.0,
    stop_command: str = "stop",
    shutdown_timeout: int = 10,
    crash_restart: bool = False,
    auto_start: bool = False,
    target_engine=engine,
) -> tuple[str, Path]:
    """An installed server record whose ``java`` runs the fake server."""
    server_dir = root / "srv"
    server_dir.mkdir(parents=True)
    (server_dir / "eula.txt").write_text("eula=true\n")
    (server_dir / "server.jar").write_bytes(b"JAR")
    (server_dir / "server.properties").write_text(f"server-port={port}\n")
    script = root / "fake_server.py"
    script.write_text(FAKE_SERVER)
    java = root / "jre" / "bin" / "java"  # outside the server directory
    java.parent.mkdir(parents=True)
    args = " ".join(
        shlex.quote(a)
        for a in (sys.executable, "-u", str(script), stop_command, str(delay), mode)
    )
    java.write_text(f"#!/bin/sh\nexec {args}\n")
    java.chmod(0o755)
    server = Server(
        name=f"fake-{mode}", mc_version="1.20.1", path=str(server_dir),
        server_jar="server.jar", java_path=str(java), port=port,
        stop_command=stop_command, shutdown_timeout=shutdown_timeout,
        crash_restart=crash_restart, auto_start=auto_start, auto_start_delay=0,
    )
    with Session(target_engine) as session:
        session.add(server)
        session.commit()
        session.refresh(server)
        return server.id, server_dir


def _status_of(server_id: str) -> str:
    with Session(engine) as session:
        return session.get(Server, server_id).status


def _event_kinds(server_id: str, target_engine=engine) -> list[str]:
    with Session(target_engine) as session:
        rows = session.exec(
            select(ServerEvent).where(ServerEvent.server_id == server_id)
        ).all()
        return [row.kind for row in rows]


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _cleanup(*server_ids: str) -> None:
    with Session(engine) as session:
        for server_id in server_ids:
            for event in session.exec(
                select(ServerEvent).where(ServerEvent.server_id == server_id)
            ).all():
                session.delete(event)
            row = session.get(Server, server_id)
            if row is not None:
                session.delete(row)
        session.commit()


async def _reap(manager: ServerManager) -> None:
    """Never leave a fake server behind when a test fails half-way."""
    for proc in list(manager._procs.values()):
        if proc.running:
            await proc.kill()


async def _wait_for(predicate, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _record(caplog: pytest.LogCaptureFixture, message: str) -> logging.LogRecord:
    """The one captured record with exactly this message."""
    matches = [r for r in caplog.records if r.getMessage() == message]
    assert len(matches) == 1, (message, caplog.messages)
    return matches[0]


async def _start_running(manager: ServerManager, server_id: str) -> ServerProcess:
    await manager.start(server_id)
    assert await _wait_for(lambda: _status_of(server_id) == ServerStatus.running.value)
    return manager._procs[server_id]


@pytest.fixture
def manager() -> Iterator[ServerManager]:
    init_db()
    yield ServerManager()


# --- stops ---------------------------------------------------------------------


@pytest.mark.integration
async def test_shutdown_sends_each_servers_own_stop_command(tmp_path, manager, caplog):
    server_id, server_dir = _install_fake(tmp_path, port=25801, stop_command="halt")
    try:
        proc = await _start_running(manager, server_id)
        with caplog.at_level(logging.INFO, logger="lectern.servers.manager"):
            await manager.shutdown()
        assert not proc.running
        assert _lines(server_dir / "received.txt") == ["halt"]
        assert _status_of(server_id) == ServerStatus.stopped.value
        kinds = _event_kinds(server_id)
        assert "stopped" in kinds
        assert "crashed" not in kinds
        assert server_id not in manager._procs
        assert f"shutdown: server {server_id} exited after the stop command" in caplog.messages
    finally:
        await _reap(manager)
        _cleanup(server_id)


@pytest.mark.integration
async def test_shutdown_stops_servers_in_parallel(tmp_path, manager):
    delay = 1.5
    a, a_dir = _install_fake(tmp_path / "a", port=25811, delay=delay)
    b, b_dir = _install_fake(tmp_path / "b", port=25812, delay=delay)
    try:
        await _start_running(manager, a)
        await _start_running(manager, b)
        loop = asyncio.get_running_loop()
        began = loop.time()
        await manager.shutdown()
        elapsed = loop.time() - began
        # One after the other would take at least 2 * delay.
        assert delay <= elapsed < 2 * delay
        for server_id, server_dir in ((a, a_dir), (b, b_dir)):
            assert _lines(server_dir / "received.txt") == ["stop"]
            assert _status_of(server_id) == ServerStatus.stopped.value
            assert "crashed" not in _event_kinds(server_id)
    finally:
        await _reap(manager)
        _cleanup(a, b)


@pytest.mark.integration
async def test_shutdown_terminates_a_server_that_ignores_the_stop_command(
    tmp_path, manager, caplog
):
    server_id, server_dir = _install_fake(
        tmp_path, port=25821, mode="deaf", shutdown_timeout=1
    )
    try:
        proc = await _start_running(manager, server_id)
        with caplog.at_level(logging.INFO, logger="lectern.servers.manager"):
            await manager.shutdown()
        assert not proc.running
        assert proc._proc.returncode == -signal.SIGTERM
        assert _lines(server_dir / "received.txt") == ["stop"]
        assert _status_of(server_id) == ServerStatus.stopped.value
        assert "crashed" not in _event_kinds(server_id)
        ending = _record(
            caplog, f"shutdown: server {server_id} was terminated after its graceful-stop timeout"
        )
        assert ending.levelno == logging.WARNING  # the last save may be lost
    finally:
        await _reap(manager)
        _cleanup(server_id)


@pytest.mark.integration
async def test_shutdown_kills_a_server_that_also_ignores_terminate(
    tmp_path, manager, caplog, monkeypatch
):
    monkeypatch.setattr(process_module, "_TERMINATE_GRACE", 0.5)
    server_id, _ = _install_fake(tmp_path, port=25831, mode="stubborn", shutdown_timeout=1)
    try:
        proc = await _start_running(manager, server_id)
        with caplog.at_level(logging.INFO, logger="lectern.servers.manager"):
            await manager.shutdown()
        assert not proc.running
        assert proc._proc.returncode == -signal.SIGKILL
        assert _status_of(server_id) == ServerStatus.stopped.value
        ending = _record(
            caplog, f"shutdown: server {server_id} was killed after its graceful-stop timeout"
        )
        assert ending.levelno == logging.WARNING
    finally:
        await _reap(manager)
        _cleanup(server_id)


@pytest.mark.integration
async def test_shutdown_stops_a_server_still_starting(tmp_path, manager):
    server_id, server_dir = _install_fake(tmp_path, port=25841, mode="starting")
    try:
        await manager.start(server_id)
        assert await _wait_for(
            lambda: "fake server booting" in manager.hub.history(server_id)
        )
        assert _status_of(server_id) == ServerStatus.starting.value
        await manager.shutdown()
        assert _lines(server_dir / "received.txt") == ["stop"]
        assert _status_of(server_id) == ServerStatus.stopped.value
        assert "crashed" not in _event_kinds(server_id)
    finally:
        await _reap(manager)
        _cleanup(server_id)


@pytest.mark.integration
async def test_shutdown_waits_for_a_launch_in_flight(tmp_path, manager):
    """A start already past the refusal but still spawning its process is
    registered yet not ``running``: shutdown waits for the spawn, then stops
    it, rather than skipping it and leaving it running behind Lectern."""
    server_id, server_dir = _install_fake(tmp_path, port=25851)
    try:
        launch = asyncio.create_task(manager.start(server_id))
        await asyncio.sleep(0)  # let start() run up to the spawn
        proc = manager._procs[server_id]
        assert not proc.running  # the precondition this test is about
        await manager.shutdown()
        await launch
        assert not proc.running
        assert _lines(server_dir / "received.txt") == ["stop"]
        assert _status_of(server_id) == ServerStatus.stopped.value
    finally:
        await _reap(manager)
        _cleanup(server_id)


@pytest.mark.integration
async def test_shutdown_waits_for_a_server_already_stopping(tmp_path, manager):
    """A user stop in flight is waited for: one stop command only, its
    timeout applies, and the server ends ``stopped``, not ``crashed``."""
    server_id, server_dir = _install_fake(tmp_path, port=25861, delay=1.0)
    try:
        proc = await _start_running(manager, server_id)
        await manager.stop(server_id)
        assert await _wait_for(lambda: _lines(server_dir / "received.txt") == ["stop"])
        assert proc.running  # still inside its 1 s exit delay
        await manager.shutdown()
        assert not proc.running
        # The fake keeps reading stdin during its exit delay: a second stop
        # command would have been recorded.
        assert _lines(server_dir / "received.txt") == ["stop"]
        assert _status_of(server_id) == ServerStatus.stopped.value
        kinds = _event_kinds(server_id)
        assert "stopped" in kinds
        assert "crashed" not in kinds
    finally:
        await _reap(manager)
        _cleanup(server_id)


@pytest.mark.integration
async def test_one_failing_stop_does_not_block_the_others(tmp_path, manager, caplog):
    a, _ = _install_fake(tmp_path / "a", port=25871)
    b, b_dir = _install_fake(tmp_path / "b", port=25872)
    try:
        broken = await _start_running(manager, a)
        await _start_running(manager, b)

        async def failing_stop(stop_command: str = "stop", timeout: int = 60):
            raise RuntimeError("stdin is gone")

        broken.stop = failing_stop
        with caplog.at_level(logging.INFO, logger="lectern.servers.manager"):
            await manager.shutdown()
        assert _lines(b_dir / "received.txt") == ["stop"]
        assert _status_of(b) == ServerStatus.stopped.value
        assert not broken.running  # not left behind
        # Terminated (the JVM's shutdown hook still saves), not killed outright.
        assert broken._proc.returncode == -signal.SIGTERM
        failure = _record(caplog, f"shutdown: server {a} could not be stopped gracefully")
        assert failure.levelno == logging.ERROR
        assert isinstance(failure.exc_info[1], RuntimeError)
        ending = _record(caplog, f"shutdown: server {a} was terminated after its stop failed")
        assert ending.levelno == logging.WARNING
    finally:
        await _reap(manager)
        _cleanup(a, b)


@pytest.mark.integration
async def test_a_server_that_cannot_be_stopped_at_all_does_not_block_the_others(
    tmp_path, manager, caplog
):
    """When even the fallback stop fails, the failure is logged with its
    cause, and the shutdown still stops the other servers and returns."""
    a, _ = _install_fake(tmp_path / "a", port=25971)
    b, b_dir = _install_fake(tmp_path / "b", port=25972)
    try:
        broken = await _start_running(manager, a)
        await _start_running(manager, b)

        async def failing_stop(stop_command: str = "stop", timeout: int = 60):
            raise RuntimeError("stdin is gone")

        async def failing_force_stop():
            raise PermissionError("not allowed to signal it")

        broken.stop = failing_stop
        broken.force_stop = failing_force_stop
        with caplog.at_level(logging.INFO, logger="lectern.servers.manager"):
            await asyncio.wait_for(manager.shutdown(), timeout=15)
        assert _lines(b_dir / "received.txt") == ["stop"]
        assert _status_of(b) == ServerStatus.stopped.value
        failure = _record(caplog, f"shutdown: server {a} could not be stopped at all")
        assert failure.levelno == logging.ERROR
        assert isinstance(failure.exc_info[1], PermissionError)
    finally:
        await _reap(manager)
        _cleanup(a, b)


@pytest.mark.integration
async def test_a_server_that_survives_its_kill_is_logged_as_an_error(
    tmp_path, manager, caplog, monkeypatch
):
    """A server still running after the whole escalation (stuck in
    uninterruptible IO) is reported at ERROR as still running, never as
    killed, and the shutdown returns."""
    monkeypatch.setattr(process_module, "_TERMINATE_GRACE", 0.2)
    monkeypatch.setattr(process_module, "_KILL_WAIT", 0.2)
    server_id, _ = _install_fake(tmp_path, port=25981, mode="stubborn", shutdown_timeout=1)
    real_kill = psutil.Process.kill
    proc: ServerProcess | None = None
    try:
        proc = await _start_running(manager, server_id)
        monkeypatch.setattr(psutil.Process, "kill", lambda self: None)  # the kill does not land
        with caplog.at_level(logging.INFO, logger="lectern.servers.manager"):
            await asyncio.wait_for(manager.shutdown(), timeout=15)
        assert proc.running
        ending = _record(
            caplog, f"shutdown: server {server_id} is still running after being killed"
        )
        assert ending.levelno == logging.ERROR
    finally:
        monkeypatch.setattr(psutil.Process, "kill", real_kill)
        if proc is not None and proc.running:
            await proc.kill()
        _cleanup(server_id)


@pytest.mark.integration
async def test_a_failed_status_write_does_not_stop_the_stop(tmp_path, manager, monkeypatch):
    """Writing ``stopping`` to the database is telemetry: when it fails (SQLite
    'database is locked', a full SD card), the server still gets its stop
    command and exits on its own, rather than being terminated or killed."""
    server_id, server_dir = _install_fake(tmp_path, port=25951)
    real_write = manager._write_status

    def flaky_write(sid: str, status: str):
        if status == ServerStatus.stopping.value:
            raise OperationalError("UPDATE server", {}, Exception("database is locked"))
        return real_write(sid, status)

    monkeypatch.setattr(manager, "_write_status", flaky_write)
    try:
        proc = await _start_running(manager, server_id)
        await manager.shutdown()
        assert not proc.running
        assert _lines(server_dir / "received.txt") == ["stop"]
        assert proc._proc.returncode == 0
    finally:
        await _reap(manager)
        _cleanup(server_id)


async def test_shutdown_with_no_servers_returns_at_once(manager):
    await manager.shutdown()
    assert manager._shutting_down is True


# --- nothing starts once shutdown has begun ---------------------------------------


@pytest.fixture
def no_spawn(monkeypatch) -> None:
    """Register processes without launching them: these tests are about
    whether ``start`` gets that far, not about the process."""

    async def settled_without_spawning(self: ServerProcess) -> None:
        self._spawn_settled.set()

    monkeypatch.setattr(ServerProcess, "start", settled_without_spawning)


async def test_start_after_shutdown_is_refused_until_reconcile(tmp_path, manager, no_spawn):
    server_id, _ = _install_fake(tmp_path, port=25881)
    try:
        await manager.shutdown()
        with pytest.raises(ShuttingDown) as refused:
            await manager.start(server_id)
        assert refused.value.status_code == 409
        assert server_id not in manager._procs
        assert _status_of(server_id) == ServerStatus.stopped.value  # untouched

        manager.reconcile()  # the next boot
        await manager.start(server_id)
        assert server_id in manager._procs
        assert _status_of(server_id) == ServerStatus.starting.value
    finally:
        _cleanup(server_id)


async def test_pending_autostart_is_refused_after_shutdown(tmp_path, manager, no_spawn):
    server_id, _ = _install_fake(tmp_path, port=25882, auto_start=True)
    try:
        await manager.shutdown()
        await manager._delayed_autostart(server_id, 0)
        assert server_id not in manager._procs
        assert (
            "[lectern] auto-start skipped — Lectern is shutting down"
            in manager.hub.history(server_id)
        )
    finally:
        _cleanup(server_id)


async def test_crash_after_shutdown_schedules_no_restart(tmp_path, manager):
    server_id, _ = _install_fake(tmp_path, port=25883, crash_restart=True)
    restarts: list[str] = []

    async def record_restart(sid: str) -> None:
        restarts.append(sid)

    manager._delayed_restart = record_restart
    try:
        await manager.shutdown()
        await manager._on_state(server_id, ServerStatus.crashed.value, "exited with code 1")
        await asyncio.sleep(0)  # a scheduled restart task would have run by now
        assert restarts == []
        kinds = _event_kinds(server_id)
        assert "crashed" in kinds  # the crash itself is still recorded
        assert "crash_restart" not in kinds
        assert _status_of(server_id) == ServerStatus.crashed.value
    finally:
        _cleanup(server_id)


@pytest.mark.integration
async def test_restart_in_flight_does_not_relaunch_during_shutdown(tmp_path, manager):
    """The second half of a restart (its ``start``) is refused once shutdown
    has begun; the stop it began is shared, so one command is sent."""
    server_id, server_dir = _install_fake(tmp_path, port=25891, delay=0.5)
    try:
        proc = await _start_running(manager, server_id)
        await asyncio.gather(manager._restart_flow(server_id, proc), manager.shutdown())
        assert server_id not in manager._procs
        assert len(_lines(server_dir / "boots.txt")) == 1
        assert _lines(server_dir / "received.txt") == ["stop"]
        assert _status_of(server_id) == ServerStatus.stopped.value
    finally:
        await _reap(manager)
        _cleanup(server_id)


# --- a terminal hangup -----------------------------------------------------------------


@pytest.fixture
def sighup_default() -> Iterator[None]:
    previous = signal.signal(signal.SIGHUP, signal.SIG_DFL)
    yield
    signal.signal(signal.SIGHUP, previous)


def test_hangup_hands_over_to_the_sigterm_handler(sighup_default):
    """While the lifespan runs, SIGHUP runs whatever handles SIGTERM (uvicorn's
    graceful shutdown) instead of killing Lectern; afterwards it is restored."""
    received: list[int] = []
    previous_term = signal.signal(signal.SIGTERM, lambda signum, _frame: received.append(signum))
    try:
        with main_module._hangup_shuts_down():
            # Checked first: with SIGHUP still at its default, raising it
            # would end the test run instead of failing this test.
            assert signal.getsignal(signal.SIGHUP) is main_module._hangup_as_terminate
            signal.raise_signal(signal.SIGHUP)
        assert received == [signal.SIGTERM]
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous_term)


def test_hangup_stays_ignored_under_nohup():
    """``nohup`` ignores SIGHUP so that Lectern survives the terminal; the
    lifespan must not turn the hangup into a shutdown."""
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with main_module._hangup_shuts_down():
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


async def test_hangup_handler_is_skipped_off_the_main_thread(sighup_default):
    """The test client runs the lifespan on a worker thread, where Python
    refuses to set a signal handler; the lifespan must still start."""

    def enter() -> object:
        with main_module._hangup_shuts_down():
            return signal.getsignal(signal.SIGHUP)

    assert await asyncio.to_thread(enter) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL


# --- the app lifespan ------------------------------------------------------------------


@pytest.mark.integration
async def test_lifespan_end_stops_the_servers_it_auto_started(
    tmp_path, manager, monkeypatch, sighup_default
):
    """The app lifespan in-process, on the main thread as under uvicorn: it
    auto-starts a server and turns SIGHUP into a graceful shutdown while it
    runs; when it ends, the server gets its own stop command and ends
    ``stopped``, starts are refused, and SIGHUP is back at its default. Fresh
    manager, scheduler and sampler stand in for the singletons, so nothing
    leaks into the rest of the suite."""
    monkeypatch.setattr(main_module, "manager", manager)
    monkeypatch.setattr(main_module, "scheduler_service", SchedulerService())
    monkeypatch.setattr(main_module, "stats_sampler", StatsSampler())
    server_id, server_dir = _install_fake(
        tmp_path, port=25991, stop_command="halt", auto_start=True
    )
    try:
        async with main_module.lifespan(main_module.app):
            assert signal.getsignal(signal.SIGHUP) is main_module._hangup_as_terminate
            assert await _wait_for(
                lambda: _status_of(server_id) == ServerStatus.running.value
            )
            proc = manager._procs[server_id]
        assert not proc.running
        assert _lines(server_dir / "received.txt") == ["halt"]
        assert _status_of(server_id) == ServerStatus.stopped.value
        assert "crashed" not in _event_kinds(server_id)
        assert manager._shutting_down is True
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL
    finally:
        await _reap(manager)
        _cleanup(server_id)


# --- end to end ------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _poll(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _listening(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class _Lectern:
    uvicorn: subprocess.Popen[bytes]
    port: int
    server_id: str
    server_dir: Path
    engine: Engine
    log: Path

    def output(self) -> str:
        return self.log.read_bytes().decode(errors="replace") if self.log.exists() else ""

    def stop_with(self, signum: int) -> str:
        """Send ``signum`` to uvicorn and return its output once it exits."""
        self.uvicorn.send_signal(signum)
        self.uvicorn.wait(timeout=30)
        return self.output()


@contextlib.contextmanager
def _run_lectern(
    tmp_path: Path,
    *,
    mode: str = "graceful",
    uvicorn_args: tuple[str, ...] = (),
    own_session: bool = False,
) -> Iterator[_Lectern]:
    """A real uvicorn serving Lectern from its own data directory, with one
    auto-start fake server (stop command ``halt``, 0.5 s to exit) that has
    booted. ``own_session`` puts uvicorn in a process group of its own, as a
    shell does for a foreground job, so a test can signal the whole group the
    way a terminal's Ctrl+C does."""
    data = tmp_path / "data"
    data.mkdir()
    own_engine = create_engine(f"sqlite:///{data / 'lectern.sqlite'}")
    SQLModel.metadata.create_all(own_engine)
    server_id, server_dir = _install_fake(
        tmp_path / "server", port=25901, delay=0.5, stop_command="halt",
        mode=mode, auto_start=True, target_engine=own_engine,
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("LECTERN_")}
    env["LECTERN_DATA"] = str(data)
    http_port = _free_port()
    log = tmp_path / "uvicorn.log"
    with open(log, "wb") as log_file:
        uvicorn = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "lectern.main:app",
             "--host", "127.0.0.1", "--port", str(http_port), *uvicorn_args],
            cwd=tmp_path, env=env, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=own_session,
        )
    try:
        assert _poll(lambda: bool(_lines(server_dir / "boots.txt")), timeout=30), (
            "the auto-start server never booted"
        )
        yield _Lectern(uvicorn, http_port, server_id, server_dir, own_engine, log)
    finally:
        if uvicorn.poll() is None:
            uvicorn.kill()
            uvicorn.wait()
        for proc in psutil.process_iter(["cwd"]):
            if proc.info["cwd"] == str(server_dir):
                proc.kill()
        own_engine.dispose()


@pytest.fixture
def real_lectern(tmp_path) -> Iterator[_Lectern]:
    with _run_lectern(tmp_path) as lectern:
        yield lectern


@pytest.mark.integration
def test_sigterm_to_uvicorn_stops_servers_before_it_exits(real_lectern):
    """A real uvicorn receiving SIGTERM (what ``docker stop`` sends) runs the
    lifespan shutdown: the server gets its own stop command and is reaped
    before uvicorn exits, and Lectern's log says how it ended."""
    fake_pid = int(_lines(real_lectern.server_dir / "boots.txt")[0])
    output = real_lectern.stop_with(signal.SIGTERM)
    # Recent uvicorn re-raises SIGTERM once it is done, so the exit code
    # varies by version; the completed lifespan shutdown is what counts.
    assert "Application shutdown complete." in output
    assert (
        f"shutdown: server {real_lectern.server_id} exited after the stop command" in output
    ), output
    # The custom command arrived on stdin (not an EOF after uvicorn died)
    # and the fake was reaped by uvicorn itself, not orphaned.
    assert _lines(real_lectern.server_dir / "received.txt") == ["halt"]
    assert not psutil.pid_exists(fake_pid)
    with Session(real_lectern.engine) as session:
        assert session.get(Server, real_lectern.server_id).status == ServerStatus.stopped.value
    kinds = _event_kinds(real_lectern.server_id, real_lectern.engine)
    assert "stopped" in kinds
    assert "crashed" not in kinds


@pytest.mark.integration
def test_hangup_to_uvicorn_stops_servers_gracefully(real_lectern):
    """A closed terminal or a dropped SSH session sends SIGHUP, which uvicorn
    does not handle. Lectern runs its graceful shutdown on it, so the server,
    in its own session and never hung up on, still gets its stop command."""
    output = real_lectern.stop_with(signal.SIGHUP)
    assert "Application shutdown complete." in output
    assert _lines(real_lectern.server_dir / "received.txt") == ["halt"]


@pytest.mark.integration
def test_an_open_install_socket_does_not_hold_back_the_shutdown(real_lectern):
    """A browser tab watching an install keeps its progress socket open. The
    handler notices uvicorn closing it at shutdown, so uvicorn reaches the
    lifespan shutdown and the server gets its stop command."""
    assert _poll(lambda: _listening(real_lectern.port), timeout=30)
    url = f"ws://127.0.0.1:{real_lectern.port}/ws/servers/other-server/install"
    with connect(url):
        try:
            output = real_lectern.stop_with(signal.SIGTERM)
        except subprocess.TimeoutExpired:
            pytest.fail("uvicorn never reached the lifespan shutdown with an install socket open")
    assert "Application shutdown complete." in output
    assert _lines(real_lectern.server_dir / "received.txt") == ["halt"]


# --- added by QA: refusals and the next boot ---------------------------------------


def _delayed_restart_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_delayed_restart"]


async def test_crash_restart_pending_when_shutdown_begins_is_refused(
    tmp_path, manager, no_spawn, monkeypatch
):
    """A crash just before the shutdown has already scheduled its restart; when
    the restart delay runs out inside the shutdown, the relaunch is refused
    and nothing is registered or marked ``starting``."""
    monkeypatch.setattr(manager_module, "_RESTART_DELAY", 0.05)
    server_id, _ = _install_fake(tmp_path, port=25885, crash_restart=True)
    try:
        await manager._on_state(server_id, ServerStatus.crashed.value, "exited with code 1")
        assert "crash_restart" in _event_kinds(server_id)  # scheduled before the flag
        pending = _delayed_restart_tasks()
        assert len(pending) == 1
        await manager.shutdown()
        await asyncio.wait_for(pending[0], timeout=5)
        assert server_id not in manager._procs
        assert _status_of(server_id) == ServerStatus.crashed.value
    finally:
        _cleanup(server_id)


async def test_next_boot_clears_stale_statuses_and_auto_starts(tmp_path, manager, no_spawn):
    """After a shutdown, the next boot (``reconcile`` then ``autostart`` on the
    same singleton) shows no server ``running`` or ``stopping`` and starts the
    auto-start servers as before."""
    auto, _ = _install_fake(tmp_path / "auto", port=25886, auto_start=True)
    stale, _ = _install_fake(tmp_path / "stale", port=25887)
    try:
        await manager.shutdown()
        with Session(engine) as session:
            for sid, status in ((auto, "running"), (stale, "stopping")):
                row = session.get(Server, sid)
                row.status = status
                session.add(row)
            session.commit()

        manager.reconcile()
        assert _status_of(auto) == ServerStatus.stopped.value
        assert _status_of(stale) == ServerStatus.stopped.value
        manager.autostart()
        assert await _wait_for(lambda: auto in manager._procs, timeout=5)
        assert stale not in manager._procs
        assert _status_of(auto) == ServerStatus.starting.value
    finally:
        _cleanup(auto, stale)


@pytest.mark.parametrize("action", ["start", "restart"])
def test_start_action_answers_409_once_shutdown_has_begun(client, engine, action):
    """Through the API, a start (or a restart of a stopped server) during the
    shutdown is a state conflict, 409 with the reason, and launches nothing."""
    init_db()
    with Session(engine) as session:
        server = Server(
            name="api-refused", mc_version="1.20.1", path="/nonexistent",
            server_jar="server.jar", java_path="/nonexistent/java", port=25888,
        )
        session.add(server)
        session.commit()
        server_id = server.id
    singleton = manager_module.manager
    asyncio.run(singleton.shutdown())
    try:
        response = client.post(f"/api/servers/{server_id}/action/{action}")
    finally:
        singleton.reconcile()
    assert response.status_code == 409
    assert response.json()["detail"] == "Lectern is shutting down"
    assert server_id not in singleton._procs


# --- added by QA: Lectern's own log output ----------------------------------------------


def test_lectern_log_lines_reach_stderr_without_a_logging_config(monkeypatch, capsys):
    """Without a logging config (plain ``uvicorn``), the lifespan prints
    Lectern's INFO lines to stderr, and leaves the logger as it found it."""
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    lectern_logger = logging.getLogger("lectern")
    monkeypatch.setattr(lectern_logger, "handlers", [])
    monkeypatch.setattr(lectern_logger, "level", logging.NOTSET)
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "level", logging.NOTSET)
    with main_module._lectern_log_output():
        logging.getLogger("lectern.servers.manager").info("shutdown: server x was killed")
    logging.getLogger("lectern.servers.manager").info("after the lifespan")
    err = capsys.readouterr().err
    assert err.count("shutdown: server x was killed") == 1
    assert "after the lifespan" not in err
    assert lectern_logger.handlers == []
    assert lectern_logger.level == logging.NOTSET


def test_lectern_log_level_follows_uvicorn_log_level(monkeypatch, capsys):
    """``uvicorn --log-level warning`` silences Lectern's INFO lines too."""
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    lectern_logger = logging.getLogger("lectern")
    monkeypatch.setattr(lectern_logger, "handlers", [])
    monkeypatch.setattr(lectern_logger, "level", logging.NOTSET)
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "level", logging.WARNING)
    with main_module._lectern_log_output():
        logging.getLogger("lectern.servers.manager").info("an info line")
        logging.getLogger("lectern.servers.manager").warning("a warning line")
    err = capsys.readouterr().err
    assert "an info line" not in err
    assert err.count("a warning line") == 1


def test_lectern_log_output_adds_nothing_under_an_existing_config():
    """A ``--log-config`` (or pytest) already handles the records: no second
    handler, so no line is printed twice."""
    lectern_logger = logging.getLogger("lectern")
    assert logging.getLogger().handlers  # pytest's capture handler
    before = list(lectern_logger.handlers)
    with main_module._lectern_log_output():
        assert lectern_logger.handlers == before
    assert lectern_logger.handlers == before


# --- added by QA: the shipped container settings ---------------------------------------

_REPO = Path(__file__).resolve().parents[2]


def test_image_cuts_open_requests_after_15_seconds():
    """The image's uvicorn command bounds how long open requests hold back the
    server stops (the two-minute grace period counts on 15 s)."""
    text = (_REPO / "Dockerfile").read_text()
    cmd_line = text[text.rindex("\nCMD ") + len("\nCMD "):].replace("\\\n", " ")
    argv = json.loads(cmd_line.strip())
    assert argv[:2] == ["uvicorn", "lectern.main:app"]
    flag = argv.index("--timeout-graceful-shutdown")
    assert argv[flag + 1] == "15"


def test_compose_gives_the_container_two_minutes_to_stop():
    lines = (_REPO / "docker-compose.yml").read_text().splitlines()
    service = lines.index("  lectern:")
    following = lines[service + 1:]
    end = next(
        (i for i, line in enumerate(following) if line and not line.startswith("    ")
         and not line.startswith("  #")),
        len(following),
    )
    assert "    stop_grace_period: 2m" in following[:end]


# --- added by QA: end to end outside Docker ---------------------------------------------


def _hold_a_request_open(port: int) -> socket.socket:
    """A request whose body never finishes arriving: uvicorn counts it as open
    until its graceful-shutdown timeout, like a slow upload or download."""
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(
        b"POST /api/servers HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"
    )
    return sock


@pytest.mark.integration
def test_ctrl_c_in_the_terminal_stops_servers_gracefully(tmp_path):
    """A Ctrl+C reaches the whole foreground process group. The server, in a
    session of its own, does not get the SIGINT (which would crash it); Lectern
    does, and stops it with its stop command. The shutdown line is printed
    once."""
    with _run_lectern(tmp_path, own_session=True) as lectern:
        fake_pid = int(_lines(lectern.server_dir / "boots.txt")[0])
        os.killpg(lectern.uvicorn.pid, signal.SIGINT)
        lectern.uvicorn.wait(timeout=30)
        output = lectern.output()
        assert "Application shutdown complete." in output
        assert _lines(lectern.server_dir / "received.txt") == ["halt"]
        line = f"shutdown: server {lectern.server_id} exited after the stop command"
        assert output.count(line) == 1, output
        assert not psutil.pid_exists(fake_pid)
        with Session(lectern.engine) as session:
            assert session.get(Server, lectern.server_id).status == ServerStatus.stopped.value
        assert "crashed" not in _event_kinds(lectern.server_id, lectern.engine)


@pytest.mark.integration
def test_an_open_request_delays_the_stops_by_at_most_the_graceful_timeout(tmp_path):
    """With ``--timeout-graceful-shutdown`` (15 s in the image, 2 s here), a
    request still open at SIGTERM is cut after the timeout and the server then
    gets its stop command, instead of waiting for the request to finish."""
    timeout = 2
    with _run_lectern(
        tmp_path, uvicorn_args=("--timeout-graceful-shutdown", str(timeout))
    ) as lectern:
        assert _poll(lambda: _listening(lectern.port), timeout=30)
        with _hold_a_request_open(lectern.port):
            began = time.monotonic()
            lectern.uvicorn.send_signal(signal.SIGTERM)
            assert _poll(
                lambda: _lines(lectern.server_dir / "received.txt") == ["halt"],
                timeout=timeout + 20,
            ), lectern.output()
            elapsed = time.monotonic() - began
            lectern.uvicorn.wait(timeout=30)
        # The request did hold the stops back (the test is about that), but
        # no longer than the timeout plus the shutdown's own bookkeeping.
        assert timeout - 0.5 <= elapsed < timeout + 5, elapsed
        assert "Application shutdown complete." in lectern.output()


@pytest.mark.integration
def test_forced_quit_terminates_the_server_and_waits_for_its_save(tmp_path):
    """A second Ctrl+C while uvicorn waits for an open request skips the
    lifespan shutdown (no stop command). Each server then gets SIGTERM and
    Lectern waits for it to exit, however long its save takes: the save
    completes, the server is not orphaned and ends ``stopped``."""
    with _run_lectern(tmp_path, mode="saves", own_session=True) as lectern:
        fake_pid = int(_lines(lectern.server_dir / "boots.txt")[0])
        assert _poll(lambda: _listening(lectern.port), timeout=30)
        with _hold_a_request_open(lectern.port):
            os.killpg(lectern.uvicorn.pid, signal.SIGINT)
            assert _poll(
                lambda: "Waiting for connections to close" in lectern.output(), timeout=10
            ), lectern.output()
            os.killpg(lectern.uvicorn.pid, signal.SIGINT)
            try:
                lectern.uvicorn.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pytest.fail("Lectern hangs after a forced quit, waiting on its server")
        output = lectern.output()
        assert "Application shutdown complete." not in output  # a forced quit indeed
        assert "halt" not in _lines(lectern.server_dir / "received.txt")
        assert _lines(lectern.server_dir / "saved.txt") == ["yes"], output
        assert not psutil.pid_exists(fake_pid)
        with Session(lectern.engine) as session:
            assert session.get(Server, lectern.server_id).status == ServerStatus.stopped.value
        assert "crashed" not in _event_kinds(lectern.server_id, lectern.engine)
