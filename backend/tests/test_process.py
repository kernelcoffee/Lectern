"""M4 process/console tests.

Drive a real ``ServerProcess`` against a tiny fake "server" (a Python script that
prints the Minecraft ``Done (`` marker, echoes stdin, and exits on ``stop``), so
start → running → console → graceful stop is exercised without a Minecraft jar.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from lectern.servers import install
from lectern.servers.process import ServerProcess
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

    async def on_state(_server_id: str, status: str) -> None:
        states.append(status)

    hub = ConsoleHub()

    async def run() -> None:
        proc = ServerProcess(
            "s1", [sys.executable, "-u", "-c", FAKE_SERVER], str(tmp_path), hub, on_state
        )
        await proc.start()
        assert await _wait_for(lambda: "running" in states), "never reached running"

        await proc.send("say hi")
        assert await _wait_for(lambda: any("echo: say hi" in l for l in hub.history("s1")))

        await proc.stop("stop", timeout=5)
        assert await _wait_for(lambda: not proc.running), "process did not exit"

    asyncio.run(run())

    assert states[-1] == "stopped"  # graceful stop, not crashed
    history = hub.history("s1")
    assert any("hello from fake server" in l for l in history)


def test_process_crash_is_reported(tmp_path: Path):
    states: list[str] = []

    async def on_state(_server_id: str, status: str) -> None:
        states.append(status)

    hub = ConsoleHub()

    async def run() -> None:
        # Exit non-zero without being asked to stop -> crashed.
        proc = ServerProcess(
            "s2", [sys.executable, "-c", "import sys; sys.exit(1)"], str(tmp_path), hub, on_state
        )
        await proc.start()
        assert await _wait_for(lambda: bool(states))

    asyncio.run(run())
    assert states[-1] == "crashed"


def test_eula_helpers(tmp_path: Path):
    assert install.eula_accepted(tmp_path) is False  # no file
    install.set_eula(tmp_path, False)
    assert install.eula_accepted(tmp_path) is False
    install.set_eula(tmp_path, True)
    assert install.eula_accepted(tmp_path) is True
