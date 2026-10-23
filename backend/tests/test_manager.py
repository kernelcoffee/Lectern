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


class _FakeClient:
    def __init__(self, port: int) -> None:
        self.port = port


class _FakeProc:
    """Just what ``start``'s port checks read from a registered process."""

    def __init__(self, *, running: bool, msmp_port: int | None = None) -> None:
        self.running = running
        self.msmp = _FakeClient(msmp_port) if msmp_port is not None else None


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
    manager._procs[holder] = _FakeProc(running=True)
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


# --- Server Management Protocol enablement -------------------------------------------


def _installed_server(tmp_path, *, type="fabric", mc_version="26.3", props="server-port=25565\n"):
    server_dir = tmp_path / "srv"
    server_dir.mkdir(parents=True)
    java = tmp_path / "jre" / "bin" / "java"  # OUTSIDE the server dir
    java.parent.mkdir(parents=True)
    java.write_bytes(b"#!/bin/sh\n")
    (server_dir / "eula.txt").write_text("eula=true\n")
    (server_dir / "server.jar").write_bytes(b"JAR")
    (server_dir / "server.properties").write_text(props)
    server_id = _make_server(
        status=ServerStatus.stopped.value, path=str(server_dir), type=type,
        server_jar="server.jar", java_path=str(java), port=25565,
    )
    with Session(engine) as session:
        row = session.get(Server, server_id)
        row.mc_version = mc_version
        session.add(row)
        session.commit()
    return server_id, server_dir


async def _noop_start(self) -> None:
    return None


_MANAGED = {
    "management-server-enabled", "management-server-host", "management-server-port",
    "management-server-tls-enabled", "management-server-secret",
}


def test_start_enables_msmp_for_263(tmp_path, monkeypatch):
    from lectern.servers.msmp import MsmpClient
    from lectern.servers.properties import read_properties

    monkeypatch.setattr("lectern.servers.manager.ServerProcess.start", _noop_start)
    server_id, server_dir = _installed_server(tmp_path)
    manager = ServerManager()
    try:
        asyncio.run(manager.start(server_id))
        props = read_properties(server_dir)
        assert set(props) == _MANAGED | {"server-port"}
        assert props["management-server-enabled"] == "true"
        assert props["management-server-tls-enabled"] == "false"
        assert props["management-server-port"] != "25565"
        client = manager._procs[server_id].msmp
        assert isinstance(client, MsmpClient)
        assert client.port == int(props["management-server-port"])
        assert client.secret == props["management-server-secret"]
        history = manager.hub.history(server_id)
        assert f"[lectern] management server on 127.0.0.1:{client.port}" in history
        assert not any(client.secret in line for line in history)
    finally:
        _cleanup(server_id)


def test_reserved_ports_include_running_management_ports(tmp_path, monkeypatch):
    """Every registered client's port is reserved, including a start still
    awaiting its subprocess (not ``running`` yet, nothing bound, maybe no
    record any more): two servers auto-starting in the same tick must not
    pick the same management port. The starting server's own are free."""
    from lectern.servers import msmp

    monkeypatch.setattr("lectern.servers.manager.ServerProcess.start", _noop_start)
    seen: dict = {}

    def _enable(server_dir, *, game_port, reserved):
        seen.update(game_port=game_port, reserved=reserved)
        return msmp.Endpoint(port=25700, secret="s" * 40)

    monkeypatch.setattr("lectern.servers.manager.msmp.enable", _enable)
    server_id, _ = _installed_server(tmp_path)
    other_id = _make_server(status=ServerStatus.running.value, port=25570)
    manager = ServerManager()
    manager._procs.update({
        other_id: _FakeProc(running=True, msmp_port=25590),
        "in-flight": _FakeProc(running=False, msmp_port=25591),
        "no-client": _FakeProc(running=True),
        server_id: _FakeProc(running=False, msmp_port=25599),  # stale entry of its own
    })
    try:
        asyncio.run(manager.start(server_id))
        assert seen["game_port"] == 25565
        assert {25570, 25590, 25591} <= seen["reserved"]
        assert 25599 not in seen["reserved"]
        assert 25565 not in seen["reserved"]  # passed separately as game_port
    finally:
        _cleanup(server_id)
        _cleanup(other_id)


def test_start_refuses_game_port_held_by_management_port(tmp_path, monkeypatch):
    """The management range overlaps the game-port suggestions (25600+), so a
    game port equal to another server's management port is refused before
    launch, whether that server is running or still starting."""
    monkeypatch.setattr("lectern.servers.manager.ServerProcess.start", _noop_start)
    server_id, _ = _installed_server(tmp_path, props="server-port=25600\n")
    holder = _make_server(status=ServerStatus.running.value, port=25570)
    manager = ServerManager()
    try:
        manager._procs[holder] = _FakeProc(running=True, msmp_port=25600)
        with pytest.raises(PortInUse, match='25600 is already in use by "t"'):
            asyncio.run(manager.start(server_id))
        manager._procs = {"gone": _FakeProc(running=False, msmp_port=25600)}
        with pytest.raises(PortInUse, match='by "gone"'):
            asyncio.run(manager.start(server_id))
        assert _status_of(server_id) == ServerStatus.stopped.value
        manager._procs = {holder: _FakeProc(running=True, msmp_port=25601)}
        asyncio.run(manager.start(server_id))  # a different management port is fine
    finally:
        _cleanup(server_id)
        _cleanup(holder)


def test_start_skips_msmp_for_velocity_and_old_versions(tmp_path, monkeypatch):
    from lectern.servers.properties import read_properties

    monkeypatch.setattr("lectern.servers.manager.ServerProcess.start", _noop_start)
    for case, (type_, version) in enumerate((("velocity", "3.4.0"), ("vanilla", "1.21.8"))):
        server_id, server_dir = _installed_server(
            tmp_path / str(case), type=type_, mc_version=version
        )
        manager = ServerManager()
        try:
            asyncio.run(manager.start(server_id))
            assert set(read_properties(server_dir)) == {"server-port"}
            assert manager._procs[server_id].msmp is None
        finally:
            _cleanup(server_id)


def test_start_keeps_existing_secret(tmp_path, monkeypatch):
    from lectern.servers.properties import read_properties

    monkeypatch.setattr("lectern.servers.manager.ServerProcess.start", _noop_start)
    secret = "Z" * 40
    server_id, server_dir = _installed_server(
        tmp_path, props=f"server-port=25565\nmanagement-server-secret={secret}\n"
    )
    manager = ServerManager()
    try:
        asyncio.run(manager.start(server_id))
        assert read_properties(server_dir)["management-server-secret"] == secret
        assert manager._procs[server_id].msmp.secret == secret
    finally:
        _cleanup(server_id)


def test_start_proceeds_when_enable_fails(tmp_path, monkeypatch):
    from lectern.servers import msmp

    def broken(*args, **kwargs):
        raise RuntimeError("no free management port")

    monkeypatch.setattr("lectern.servers.manager.ServerProcess.start", _noop_start)
    monkeypatch.setattr(msmp, "enable", broken)
    server_id, _ = _installed_server(tmp_path)
    manager = ServerManager()
    try:
        asyncio.run(manager.start(server_id))
        assert manager._procs[server_id].msmp is None
        assert _status_of(server_id) == ServerStatus.starting.value
        assert (
            "[lectern] management server not enabled — no free management port"
            in manager.hub.history(server_id)
        )
    finally:
        _cleanup(server_id)


def test_superseded_process_cannot_evict_its_successor(tmp_path, monkeypatch):
    """A restart registers the new process before the old one's late
    ``stopped`` lands: that report must neither pop the new process nor
    overwrite its status; the current process's own report still does both."""
    monkeypatch.setattr("lectern.servers.manager.ServerProcess.start", _noop_start)
    server_id, _ = _installed_server(tmp_path, mc_version="1.20.1")
    manager = ServerManager()
    try:
        asyncio.run(manager.start(server_id))
        old = manager._procs[server_id]
        asyncio.run(manager.start(server_id))  # the restart's start
        new = manager._procs[server_id]
        assert new is not old

        asyncio.run(old.on_state(server_id, ServerStatus.stopped.value, ""))
        assert manager._procs.get(server_id) is new
        assert _status_of(server_id) == ServerStatus.starting.value

        asyncio.run(new.on_state(server_id, ServerStatus.stopped.value, ""))
        assert server_id not in manager._procs
        assert _status_of(server_id) == ServerStatus.stopped.value
    finally:
        _cleanup(server_id)
