"""Manager status machinery (M5 fixes): startup reconcile of stale statuses
(including `installing` → `install_failed`) and the crash-restart cap.

These exercise the module-level engine (the manager owns its own sessions,
independent of the request-scoped test override), pointed at the throwaway
LECTERN_DATA dir by conftest.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session

from lectern.db import engine, init_db
from lectern.models import Server, ServerStatus
from lectern.servers.manager import (
    _MAX_CRASH_RESTARTS,
    NotInstalled,
    PortInUse,
    ServerManager,
)


def _make_server(**overrides) -> str:
    init_db()
    server = Server(name="t", mc_version="1.21", **overrides)
    with Session(engine) as session:
        session.add(server)
        session.commit()
        session.refresh(server)
        return server.id


def _status_of(server_id: str) -> str:
    with Session(engine) as session:
        return session.get(Server, server_id).status


def _cleanup(server_id: str) -> None:
    with Session(engine) as session:
        row = session.get(Server, server_id)
        if row is not None:
            session.delete(row)
            session.commit()


# --- reconcile -----------------------------------------------------------------


def test_reconcile_downgrades_live_statuses_to_stopped():
    server_id = _make_server(status=ServerStatus.running.value)
    try:
        ServerManager().reconcile()
        assert _status_of(server_id) == ServerStatus.stopped.value
    finally:
        _cleanup(server_id)


def test_reconcile_marks_stale_installing_as_failed():
    # Installs run in-process; one still `installing` after a restart can
    # never finish and must not show an eternal spinner.
    server_id = _make_server(status=ServerStatus.installing.value)
    try:
        ServerManager().reconcile()
        assert _status_of(server_id) == ServerStatus.install_failed.value
    finally:
        _cleanup(server_id)


def test_reconcile_leaves_terminal_statuses_alone():
    server_id = _make_server(status=ServerStatus.crashed.value)
    try:
        ServerManager().reconcile()
        assert _status_of(server_id) == ServerStatus.crashed.value
    finally:
        _cleanup(server_id)


# --- crash-restart cap ------------------------------------------------------------


def test_crash_restart_gives_up_after_cap():
    server_id = _make_server(status=ServerStatus.running.value, crash_restart=True)
    manager = ServerManager()
    restarts: list[int] = []

    async def fake_delayed_restart(sid: str) -> None:
        restarts.append(1)

    manager._delayed_restart = fake_delayed_restart  # bypass sleep + real start

    async def run():
        # Crash more times than the cap allows.
        for _ in range(_MAX_CRASH_RESTARTS + 2):
            await manager._on_state(server_id, ServerStatus.crashed.value)
        # Let the create_task(fake_delayed_restart) callbacks run.
        await asyncio.sleep(0)

    try:
        asyncio.run(run())
        assert len(restarts) == _MAX_CRASH_RESTARTS
        # The give-up is announced on the console.
        assert any("giving up" in line for line in manager.hub.history(server_id))
    finally:
        _cleanup(server_id)


def test_crash_count_resets_on_running():
    server_id = _make_server(status=ServerStatus.running.value, crash_restart=True)
    manager = ServerManager()
    manager._delayed_restart = lambda sid: asyncio.sleep(0)

    async def run():
        await manager._on_state(server_id, ServerStatus.crashed.value)
        await manager._on_state(server_id, ServerStatus.crashed.value)
        assert manager._crash_counts[server_id] == 2
        # A successful boot (Done marker → running) wipes the history…
        await manager._on_state(server_id, ServerStatus.running.value)
        assert server_id not in manager._crash_counts
        await asyncio.sleep(0)

    try:
        asyncio.run(run())
    finally:
        _cleanup(server_id)


def test_reset_crash_count_is_manual_override():
    manager = ServerManager()
    manager._crash_counts["x"] = 99
    manager.reset_crash_count("x")
    assert "x" not in manager._crash_counts


# --- launch guard --------------------------------------------------------------


def test_start_refuses_java_inside_server_dir(tmp_path):
    """A java binary located inside the server directory (a mod could drop one)
    is refused at launch (ref: crafty-4)."""
    server_dir = tmp_path / "srv"
    (server_dir / "java" / "bin").mkdir(parents=True)
    java = server_dir / "java" / "bin" / "java"
    java.write_bytes(b"#!/bin/sh\n")
    (server_dir / "eula.txt").write_text("eula=true\n")
    (server_dir / "server.jar").write_bytes(b"JAR")
    server_id = _make_server(
        status=ServerStatus.stopped.value, path=str(server_dir),
        server_jar="server.jar", java_path=str(java),
    )
    try:
        with pytest.raises(NotInstalled, match="inside the server"):
            asyncio.run(ServerManager().start(server_id))
    finally:
        _cleanup(server_id)


def test_start_refuses_port_held_by_running_server(tmp_path, monkeypatch):
    """Records aren't unique on port, so starting a server onto a port a
    *running* server already binds is refused (before any status change)."""
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    java = tmp_path / "jre" / "bin" / "java"  # OUTSIDE the server dir
    java.parent.mkdir(parents=True)
    java.write_bytes(b"#!/bin/sh\n")
    (server_dir / "eula.txt").write_text("eula=true\n")
    (server_dir / "server.jar").write_bytes(b"JAR")
    (server_dir / "server.properties").write_text("server-port=25565\n")

    starting = _make_server(
        status=ServerStatus.stopped.value, path=str(server_dir),
        server_jar="server.jar", java_path=str(java), port=25565,
    )
    # A second server already running on the same port (no path → its DB port
    # is the effective one).
    holder = _make_server(status=ServerStatus.running.value, port=25565)

    manager = ServerManager()
    monkeypatch.setattr(manager, "running_processes", lambda: [(holder, object())])
    try:
        with pytest.raises(PortInUse, match="25565"):
            asyncio.run(manager.start(starting))
        # Refused before the status was flipped to "starting".
        assert _status_of(starting) == ServerStatus.stopped.value
    finally:
        _cleanup(starting)
        _cleanup(holder)


# --- auto-start ----------------------------------------------------------------


def test_autostart_schedules_only_flagged_servers(monkeypatch):
    on = _make_server(
        status=ServerStatus.stopped.value, auto_start=True, auto_start_delay=0
    )
    off = _make_server(status=ServerStatus.stopped.value, auto_start=False)
    started: list[str] = []
    manager = ServerManager()

    async def fake_start(server_id: str) -> None:
        started.append(server_id)

    monkeypatch.setattr(manager, "start", fake_start)

    async def run():
        manager.autostart()
        await asyncio.sleep(0.05)  # let the delay=0 tasks fire

    try:
        asyncio.run(run())
        assert started == [on]  # only the flagged server
    finally:
        _cleanup(on)
        _cleanup(off)
