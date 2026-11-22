"""M4 process/console tests.

Drive a real ``ServerProcess`` against a tiny fake "server" (a Python script that
prints the Minecraft ``Done (`` marker, echoes stdin, and exits on ``stop``), so
start → running → console → graceful stop is exercised without a Minecraft jar.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import signal
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest

from lectern.servers import install
from lectern.servers import process as process_module
from lectern.servers.process import ServerProcess, StopOutcome
from lectern.ws import ConsoleHub

FAKE_SERVER = r"""
import sys
print("hello from fake server", flush=True)
print('[Server thread/INFO]: Done (1.0s)! For help, type "help"', flush=True)
for line in sys.stdin:
    line = line.strip()
    print(f"echo: {line}", flush=True)
    if line == "stop":
        print("Stopping the server", flush=True)
        break
"""


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def test_process_lifecycle(tmp_path: Path):
    states: list[str] = []

    async def on_state(_server_id: str, status: str, detail: str = "") -> None:
        states.append(status)

    hub = ConsoleHub()

    async def run() -> None:
        proc = ServerProcess(
            "s1", [sys.executable, "-u", "-c", FAKE_SERVER], str(tmp_path), hub, on_state
        )
        await proc.start()
        assert await _wait_for(lambda: "running" in states), "never reached running"

        await proc.send("say hi")
        assert await _wait_for(
            lambda: any("echo: say hi" in line for line in hub.history("s1"))
        )

        await proc.stop("stop", timeout=5)
        assert await _wait_for(lambda: not proc.running), "process did not exit"

    asyncio.run(run())

    assert states[-1] == "stopped"  # graceful stop, not crashed
    history = hub.history("s1")
    assert any("hello from fake server" in line for line in history)


def test_process_crash_is_reported(tmp_path: Path):
    states: list[tuple[str, str]] = []

    async def on_state(_server_id: str, status: str, detail: str = "") -> None:
        states.append((status, detail))

    hub = ConsoleHub()

    async def run() -> None:
        # Exit non-zero without being asked to stop -> crashed.
        proc = ServerProcess(
            "s2", [sys.executable, "-c", "import sys; sys.exit(1)"], str(tmp_path), hub, on_state
        )
        await proc.start()
        assert await _wait_for(lambda: bool(states))

    asyncio.run(run())
    status, detail = states[-1]
    assert status == "crashed"
    assert detail == "exited with code 1"
    assert any("crashed: exited with code 1" in line for line in hub.history("s2"))


def test_process_sigkill_reports_oom_hint(tmp_path: Path):
    states: list[tuple[str, str]] = []

    async def on_state(_server_id: str, status: str, detail: str = "") -> None:
        states.append((status, detail))

    hub = ConsoleHub()

    async def run() -> None:
        # A process killed from outside (the OOM killer's signature).
        proc = ServerProcess(
            "s3",
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
            str(tmp_path),
            hub,
            on_state,
        )
        await proc.start()
        assert await _wait_for(lambda: bool(states))

    asyncio.run(run())
    status, detail = states[-1]
    assert status == "crashed"
    assert "SIGKILL" in detail
    assert "out of memory" in detail


def test_describe_exit():
    from lectern.servers.process import describe_exit

    assert describe_exit(1) == "exited with code 1"
    assert "SIGKILL" in describe_exit(-9)
    assert "out of memory" in describe_exit(-9)
    assert describe_exit(137) == describe_exit(-9)  # shell-style 128+9
    assert "SIGTERM" in describe_exit(-15)
    assert "SIGSEGV" in describe_exit(-11)


def test_eula_helpers(tmp_path: Path):
    assert install.eula_accepted(tmp_path) is False  # no file
    install.set_eula(tmp_path, False)
    assert install.eula_accepted(tmp_path) is False
    install.set_eula(tmp_path, True)
    assert install.eula_accepted(tmp_path) is True


# Exits 0.5 s after "stop" and keeps echoing stdin meanwhile, so a second stop
# command sent during that window would show up in the console history.
SLOW_STOPPING_SERVER = r"""
import os, select, sys, time
print('[Server thread/INFO]: Done (1.0s)! For help, type "help"', flush=True)
deadline = None
pending = b""
while True:
    wait = None if deadline is None else deadline - time.monotonic()
    if wait is not None and wait <= 0:
        sys.exit(0)
    if not select.select([0], [], [], wait)[0]:
        continue
    chunk = os.read(0, 4096)
    if not chunk:
        sys.exit(0)
    pending += chunk
    *lines, pending = pending.split(b"\n")
    for raw in lines:
        line = raw.decode().strip()
        print(f"echo: {line}", flush=True)
        if line == "stop" and deadline is None:
            deadline = time.monotonic() + 0.5
"""


@pytest.mark.integration
async def test_concurrent_stops_share_one_sequence(tmp_path: Path):
    """A second ``stop`` while one is in flight sends nothing and gets the
    same outcome; cancelling the first caller does not abort the stop the
    second one is waiting for."""
    states: list[str] = []

    async def on_state(_server_id: str, status: str, detail: str = "") -> None:
        states.append(status)

    hub = ConsoleHub()
    proc = ServerProcess(
        "s5", [sys.executable, "-u", "-c", SLOW_STOPPING_SERVER], str(tmp_path), hub, on_state
    )
    await proc.start()
    assert await _wait_for(lambda: "running" in states)

    first = asyncio.create_task(proc.stop("stop", timeout=5))
    assert await _wait_for(lambda: "echo: stop" in hub.history("s5"))
    second = asyncio.create_task(proc.stop("stop", timeout=5))
    first.cancel()

    assert await second == StopOutcome.exited
    assert first.cancelled()
    assert not proc.running
    assert hub.history("s5").count("echo: stop") == 1
    assert states == ["running", "stopping", "stopped"]
    assert await proc.stop("stop", timeout=5) == StopOutcome.not_running


# Deaf to stdin and, like a JVM, alive after it closes; it still ends with the
# test run that started it, even when it also ignores SIGTERM.
_DEAF = (
    "import os, time\nparent = os.getppid()\nprint('ready', flush=True)\n"
    "while os.getppid() == parent: time.sleep(0.1)\n"
)
_STUBBORN = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + _DEAF


async def _no_state(_server_id: str, status: str, detail: str = "") -> None:
    return None


async def _started(tmp_path: Path, script: str) -> tuple[ServerProcess, ConsoleHub]:
    hub = ConsoleHub()
    proc = ServerProcess("s7", [sys.executable, "-u", "-c", script], str(tmp_path), hub, _no_state)
    await proc.start()
    assert await _wait_for(lambda: "ready" in hub.history("s7"))
    return proc, hub


@pytest.mark.integration
async def test_stop_reports_how_it_ended(tmp_path: Path, monkeypatch):
    """The outcome names the escalation step that ended the process."""
    monkeypatch.setattr("lectern.servers.process._TERMINATE_GRACE", 0.5)
    for script, expected in ((_DEAF, StopOutcome.terminated), (_STUBBORN, StopOutcome.killed)):
        proc, _ = await _started(tmp_path, script)
        assert await proc.stop("stop", timeout=1) == expected
        assert not proc.running


@pytest.mark.integration
async def test_server_runs_in_its_own_session(tmp_path: Path):
    """A Ctrl+C in Lectern's terminal signals Lectern's process group; the
    server must not be in it, or it dies before the graceful stop begins."""
    proc, _ = await _started(tmp_path, _DEAF)
    try:
        assert os.getsid(proc.pid) == proc.pid
        assert os.getpgid(proc.pid) != os.getpgid(0)
    finally:
        await proc.kill()


# Writes ``ready`` once its SIGTERM handler is in place and ``terminated`` when
# that handler runs; outlives the end of its stdin, like a JVM, for up to 60 s.
_RECORDS_SIGTERM = r"""
import pathlib, signal, sys, time
def on_term(*_):
    pathlib.Path("terminated").write_text("yes")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
pathlib.Path("ready").write_text("yes")
for _ in range(600):
    time.sleep(0.1)
"""

# A stand-in for Lectern: starts one server and prints its pid, then waits.
_LAUNCHER = r"""
import asyncio, sys
from lectern.servers.process import ServerProcess
from lectern.ws import ConsoleHub

async def on_state(_server_id, _status, _detail=""):
    return None

async def main():
    proc = ServerProcess(
        "orphan", [sys.executable, "-c", sys.argv[1]], sys.argv[2], ConsoleHub(), on_state
    )
    await proc.start()
    print(proc.pid, flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""


@pytest.mark.integration
@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the parent-death signal is Linux-only"
)
async def test_server_is_terminated_when_lectern_dies_abruptly(tmp_path: Path):
    """In its own session, no terminal or group signal reaches the server. If
    Lectern, run as an ordinary process, dies without its shutdown (SIGKILL,
    a crash), the server still gets SIGTERM, so the JVM's shutdown hook saves
    the world, instead of running on where Lectern can no longer see it. (As
    PID 1 of a container this does not apply: the kernel kills the server.)"""
    launcher = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _LAUNCHER, _RECORDS_SIGTERM, str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
    )
    server_pid: int | None = None
    try:
        assert launcher.stdout is not None
        server_pid = int(await asyncio.wait_for(launcher.stdout.readline(), 15))
        assert await _wait_for(lambda: (tmp_path / "ready").exists(), timeout=15)
        launcher.kill()
        await launcher.wait()
        assert await _wait_for(lambda: (tmp_path / "terminated").exists(), timeout=10)
    finally:
        if launcher.returncode is None:
            launcher.kill()
            await launcher.wait()
        if server_pid is not None and not (tmp_path / "terminated").exists():
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(server_pid).kill()


# On SIGTERM it takes 1.5 s to "save", writes ``saved`` and exits 0, like a
# JVM whose shutdown hook flushes the world; it ends with its test run anyway.
_SAVES_ON_SIGTERM = r"""
import os, pathlib, signal, sys, time
def on_term(*_):
    time.sleep(1.5)
    pathlib.Path("saved").write_text("yes")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
parent = os.getppid()
print("ready", flush=True)
while os.getppid() == parent:
    time.sleep(0.1)
"""


@pytest.mark.integration
async def test_a_cancelled_pump_lets_its_server_save(tmp_path: Path, monkeypatch):
    """When Lectern exits without its shutdown (a second Ctrl+C), the event
    loop's teardown cancels each pump. The pump sends its server SIGTERM and
    waits for the exit, however long the save takes: it neither hangs on an
    exit nothing asked for, nor kills a server that is still saving. The
    server ends ``stopped``."""
    # Shorter than the save: a kill after the terminate grace would lose it.
    monkeypatch.setattr("lectern.servers.process._TERMINATE_GRACE", 0.2)
    states: list[str] = []

    async def on_state(_server_id: str, status: str, detail: str = "") -> None:
        states.append(status)

    hub = ConsoleHub()
    proc = ServerProcess(
        "s8", [sys.executable, "-u", "-c", _SAVES_ON_SIGTERM], str(tmp_path), hub, on_state
    )
    await proc.start()
    try:
        assert await _wait_for(lambda: "ready" in hub.history("s8"))
        pump = proc._pump_task
        assert pump is not None
        pump.cancel()
        done, _ = await asyncio.wait([pump], timeout=10)
        assert done, "the cancelled pump is still waiting for its server"
        assert pump.cancelled()
        assert proc._proc.returncode == 0
        assert (tmp_path / "saved").exists()
        assert states == ["stopped"]
        history = hub.history("s8")
        assert "[lectern] Lectern is exiting — terminating" in history
        assert "[lectern] killing process tree" not in history
    finally:
        if proc.running:
            await proc.kill()


# A server that crashes while a process it spawned keeps its output open (as a
# launch script's leftover child can): the exit is known, the output never ends.
# It prints the leftover's pid so the test can end it; the leftover gives up
# on its own after 30 s.
_CRASHES_LEAVING_OUTPUT_OPEN = r"""
import subprocess, sys
leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
print(leftover.pid, flush=True)
sys.exit(3)
"""


@pytest.mark.integration
async def test_a_cancelled_pump_leaves_an_exited_server_alone(tmp_path: Path):
    """A server that has already exited when Lectern exits gets no SIGTERM and
    no 'Lectern is exiting' line, and its crash is still reported as a crash,
    not turned into a ``stopped``."""
    states: list[tuple[str, str]] = []

    async def on_state(_server_id: str, status: str, detail: str = "") -> None:
        states.append((status, detail))

    hub = ConsoleHub()
    proc = ServerProcess(
        "s9", [sys.executable, "-u", "-c", _CRASHES_LEAVING_OUTPUT_OPEN],
        str(tmp_path), hub, on_state,
    )
    await proc.start()
    leftover: int | None = None
    try:
        assert await _wait_for(lambda: bool(hub.history("s9")))
        leftover = int(hub.history("s9")[0])
        assert await _wait_for(lambda: proc._proc.returncode is not None)
        pump = proc._pump_task
        assert pump is not None
        assert not pump.done()  # still reading the output the leftover holds open
        pump.cancel()
        done, _ = await asyncio.wait([pump], timeout=10)
        assert done
        assert pump.cancelled()
        assert states == [("crashed", "exited with code 3")]
        assert "[lectern] Lectern is exiting — terminating" not in hub.history("s9")
    finally:
        if leftover is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(leftover).kill()


async def test_kill_before_launch_has_nothing_to_kill(tmp_path: Path):
    """A process that was never launched (its spawn failed) counts as ended,
    and no kill notice reaches its console."""
    hub = ConsoleHub()
    proc = ServerProcess("s10", ["never-launched"], str(tmp_path), hub, _no_state)
    assert await proc.kill() is True
    assert hub.history("s10") == []


def test_parent_death_signal_is_not_set_outside_linux(monkeypatch):
    """prctl is Linux-only: elsewhere servers are launched without a
    ``preexec_fn``, rather than failing to launch."""
    monkeypatch.setattr(process_module.sys, "platform", "darwin")
    assert process_module._end_with_parent() is None


_PR_GET_PDEATHSIG = 2


@pytest.fixture
def pdeathsig() -> Iterator[Callable[[], int]]:
    """Read this thread's parent-death signal; cleared again afterwards, since
    the tests below arm it on the test process itself."""
    libc = ctypes.CDLL(None)

    def current() -> int:
        value = ctypes.c_int()
        assert libc.prctl(_PR_GET_PDEATHSIG, ctypes.byref(value), 0, 0, 0) == 0
        return value.value

    assert current() == 0
    try:
        yield current
    finally:
        libc.prctl(process_module._PR_SET_PDEATHSIG, 0, 0, 0, 0)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the parent-death signal is Linux-only"
)
def test_launch_hook_arms_sigterm_on_parent_death(monkeypatch, pdeathsig):
    """What runs in the child between fork and exec, run here in-process: it
    asks the kernel for SIGTERM on its parent's death. (Pretend this process's
    parent is the 'Lectern' that built the hook, as it is in the child.)"""
    arm = process_module._end_with_parent()
    assert arm is not None
    monkeypatch.setattr(process_module.os, "getppid", os.getpid)
    arm()
    assert pdeathsig() == signal.SIGTERM


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the parent-death signal is Linux-only"
)
def test_launch_hook_refuses_a_launch_whose_lectern_is_gone(pdeathsig):
    """If Lectern died between the fork and the prctl, the child has a new
    parent: nobody would send the signal, so the launch is refused. Here the
    test process's parent is not the process that built the hook (itself)."""
    arm = process_module._end_with_parent()
    assert arm is not None
    with pytest.raises(ChildProcessError, match="Lectern exited"):
        arm()


@pytest.mark.integration
async def test_a_refused_parent_death_signal_does_not_block_the_start(
    tmp_path: Path, monkeypatch
):
    """The parent-death signal is a safety net: where the kernel call fails
    (here an option it rejects, in practice a strict seccomp profile), the
    server still starts."""
    monkeypatch.setattr("lectern.servers.process._PR_SET_PDEATHSIG", -1)
    proc, _ = await _started(tmp_path, _DEAF)
    try:
        assert proc.running
    finally:
        await proc.kill()


@pytest.mark.integration
async def test_force_stop_terminates_before_it_kills(tmp_path: Path, monkeypatch):
    """The fallback for a failed graceful stop sends no command but still
    terminates first (the JVM's shutdown hook saves), killing only a process
    that ignores it."""
    monkeypatch.setattr("lectern.servers.process._TERMINATE_GRACE", 0.5)
    proc, hub = await _started(tmp_path, _DEAF)
    assert await proc.force_stop() == StopOutcome.terminated
    assert proc._proc.returncode == -signal.SIGTERM
    assert "[lectern] stop failed — terminating" in hub.history("s7")

    proc, _ = await _started(tmp_path, _STUBBORN)
    assert await proc.force_stop() == StopOutcome.killed
    assert proc._proc.returncode == -signal.SIGKILL
    assert await proc.force_stop() == StopOutcome.not_running


@pytest.mark.integration
async def test_a_kill_that_does_not_take_is_reported(tmp_path: Path, monkeypatch):
    """A process that outlives its SIGKILL (stuck in uninterruptible IO) is
    reported as still running, never as killed."""
    monkeypatch.setattr("lectern.servers.process._TERMINATE_GRACE", 0.2)
    monkeypatch.setattr("lectern.servers.process._KILL_WAIT", 0.2)
    proc, _ = await _started(tmp_path, _STUBBORN)
    real_kill = psutil.Process.kill
    monkeypatch.setattr(psutil.Process, "kill", lambda self: None)  # the kill does not land
    try:
        assert await proc.stop("stop", timeout=1) == StopOutcome.survived_kill
        assert proc.running
    finally:
        real_kill(psutil.Process(proc.pid))
        await _wait_for(lambda: not proc.running)


async def test_stop_waits_for_the_final_state_report(tmp_path: Path):
    """stop() returns only after the pump has reported ``stopped``, even when
    the state callback itself yields (the manager's restart starts the next
    run as soon as stop() returns)."""
    order: list[str] = []

    async def on_state(_server_id: str, status: str, detail: str = "") -> None:
        if status == "stopped":
            await asyncio.sleep(0.1)
        order.append(status)

    proc = ServerProcess(
        "s4", [sys.executable, "-u", "-c", FAKE_SERVER], str(tmp_path), ConsoleHub(), on_state
    )
    await proc.start()
    assert await _wait_for(lambda: "running" in order)
    await proc.stop("stop", timeout=5)
    order.append("<restart resumes>")
    assert order == ["running", "stopping", "stopped", "<restart resumes>"]
