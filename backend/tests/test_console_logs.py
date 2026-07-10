"""Console/logs bundle (M11): graceful-stop countdown + log retention prune."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from lectern.servers.logs import prune_logs


# --- log retention ----------------------------------------------------------


def _aged(path: Path, days_old: float) -> None:
    when = time.time() - days_old * 86400
    import os

    os.utime(path, (when, when))


def test_prune_logs_removes_old_rotated(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "latest.log").write_text("live")
    (logs / "2020-01-01-1.log.gz").write_bytes(b"old")
    (logs / "2026-07-09-1.log.gz").write_bytes(b"recent")
    _aged(logs / "2020-01-01-1.log.gz", 30)
    _aged(logs / "2026-07-09-1.log.gz", 1)

    removed = prune_logs(tmp_path, retention_days=7)

    assert removed == 1
    assert not (logs / "2020-01-01-1.log.gz").exists()  # >7 days → gone
    assert (logs / "2026-07-09-1.log.gz").exists()  # <7 days → kept
    assert (logs / "latest.log").exists()  # never pruned (active file)


def test_prune_logs_zero_keeps_everything(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old = logs / "2000-01-01-1.log.gz"
    old.write_bytes(b"ancient")
    _aged(old, 9999)
    assert prune_logs(tmp_path, retention_days=0) == 0
    assert old.exists()


def test_prune_logs_no_logs_dir(tmp_path):
    assert prune_logs(tmp_path, retention_days=7) == 0


# --- graceful-stop countdown ------------------------------------------------


class _Hub:
    def __init__(self):
        self.messages: list[str] = []

    def publish(self, server_id, line):
        self.messages.append(line)


def _make_process(hub):
    from lectern.servers.process import ServerProcess

    async def _noop(server_id, status):
        pass

    return ServerProcess("s", ["true"], ".", hub, _noop)


def test_wait_countdown_announces_then_gives_up():
    """With a process that never exits, the countdown announces each step and
    returns False so stop() escalates to terminate/kill."""
    from lectern.servers import process as proc_mod

    hub = _Hub()
    p = _make_process(hub)

    class _Never:
        async def wait(self):
            await asyncio.sleep(3600)  # never exits within the test

    p._proc = _Never()  # type: ignore[assignment]

    # Shorten the announce step so the test is fast (timeout 3 → steps 1s).
    original = proc_mod._COUNTDOWN_STEP
    proc_mod._COUNTDOWN_STEP = 1
    try:
        exited = asyncio.run(p._wait_countdown(3))
    finally:
        proc_mod._COUNTDOWN_STEP = original

    assert exited is False
    # Announced at 2s and 1s remaining (not at 0).
    countdowns = [m for m in hub.messages if "until force stop" in m]
    assert len(countdowns) == 2
    assert "2s until force stop" in countdowns[0]
    assert "1s until force stop" in countdowns[1]


def test_wait_countdown_returns_early_on_exit():
    hub = _Hub()
    p = _make_process(hub)

    class _ExitsFast:
        async def wait(self):
            await asyncio.sleep(0.01)

    p._proc = _ExitsFast()  # type: ignore[assignment]
    assert asyncio.run(p._wait_countdown(60)) is True
    # Exited before the first step elapsed → no countdown spam.
    assert not any("until force stop" in m for m in hub.messages)
