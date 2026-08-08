"""Schedules (M10): cron validation, CRUD + next_run, job sync, and firing.

CRUD goes through the API (client fixture, in-memory DB). Firing tests use the
module-level engine (``_fire`` owns its sessions, same pattern as
test_manager.py) with the manager's methods monkeypatched to record calls.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session

from lectern.db import engine, init_db
from lectern.models import Schedule, Server
from lectern.scheduler import (
    SchedulerService,
    next_run_time,
    scheduler_service,
    validate_cron,
)
from lectern.servers.manager import manager

# --- pure helpers ------------------------------------------------------------


def test_validate_cron():
    validate_cron("0 4 * * *")
    validate_cron("*/5 * * * sun")
    with pytest.raises(ValueError):
        validate_cron("not a cron")
    with pytest.raises(ValueError):
        validate_cron("61 * * * *")


def test_next_run_time():
    every_minute = Schedule(server_id="s", action="restart", cron="* * * * *")
    assert next_run_time(every_minute) is not None
    disabled = Schedule(
        server_id="s", action="restart", cron="* * * * *", enabled=False
    )
    assert next_run_time(disabled) is None
    invalid = Schedule(server_id="s", action="restart", cron="bad")
    assert next_run_time(invalid) is None


# --- job sync ------------------------------------------------------------------


def test_sync_mirrors_enabled_state():
    service = SchedulerService()
    row = Schedule(server_id="s", action="restart", cron="0 4 * * *")
    service.sync(row)
    assert service._scheduler.get_job(row.id) is not None

    row.enabled = False
    service.sync(row)
    assert service._scheduler.get_job(row.id) is None

    # Invalid cron rows are skipped, not fatal (API validates writes).
    bad = Schedule(server_id="s", action="restart", cron="nope")
    service.sync(bad)
    assert service._scheduler.get_job(bad.id) is None


# --- API CRUD -------------------------------------------------------------------


def _server(client, name: str = "S", port: int = 25565) -> str:
    return client.post(
        "/api/servers",
        json={"name": name, "type": "vanilla", "mc_version": "1.21", "port": port},
    ).json()["id"]


def test_schedule_crud(client):
    server_id = _server(client)

    resp = client.post(
        f"/api/servers/{server_id}/schedules",
        json={"action": "restart", "cron": "0 4 * * *"},
    )
    assert resp.status_code == 201, resp.text
    schedule = resp.json()
    assert schedule["next_run"] is not None
    assert schedule["one_time"] is False

    # List shows it.
    listed = client.get(f"/api/servers/{server_id}/schedules").json()
    assert [s["id"] for s in listed] == [schedule["id"]]

    # Disable → next_run null.
    resp = client.patch(
        f"/api/servers/{server_id}/schedules/{schedule['id']}",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["next_run"] is None

    # Bad cron on update → 422, row unchanged.
    resp = client.patch(
        f"/api/servers/{server_id}/schedules/{schedule['id']}",
        json={"cron": "junk"},
    )
    assert resp.status_code == 422
    assert (
        client.get(f"/api/servers/{server_id}/schedules").json()[0]["cron"]
        == "0 4 * * *"
    )

    resp = client.delete(f"/api/servers/{server_id}/schedules/{schedule['id']}")
    assert resp.status_code == 204
    assert client.get(f"/api/servers/{server_id}/schedules").json() == []


def test_schedule_validation(client):
    server_id = _server(client)
    base = f"/api/servers/{server_id}/schedules"

    assert (
        client.post(base, json={"action": "restart", "cron": "junk"}).status_code
        == 422
    )
    # command action requires a command…
    assert (
        client.post(base, json={"action": "command", "cron": "0 4 * * *"}).status_code
        == 422
    )
    # …and is accepted with one.
    resp = client.post(
        base, json={"action": "command", "cron": "0 4 * * *", "command": "say hi"}
    )
    assert resp.status_code == 201

    # Unknown server / cross-server ids → 404.
    assert client.get("/api/servers/nope/schedules").status_code == 404
    other = _server(client, name="S2", port=25566)
    schedule_id = resp.json()["id"]
    assert (
        client.delete(f"/api/servers/{other}/schedules/{schedule_id}").status_code
        == 404
    )


# --- firing ---------------------------------------------------------------------


def _make_row(**overrides) -> tuple[str, str]:
    """Server + schedule rows on the module-level engine (what _fire reads)."""
    init_db()
    with Session(engine) as session:
        server = Server(name="sched-t", mc_version="1.21")
        session.add(server)
        session.commit()
        schedule = Schedule(server_id=server.id, cron="* * * * *", **overrides)
        session.add(schedule)
        session.commit()
        return server.id, schedule.id


def _cleanup(server_id: str) -> None:
    with Session(engine) as session:
        from sqlmodel import select

        for schedule in session.exec(
            select(Schedule).where(Schedule.server_id == server_id)
        ).all():
            session.delete(schedule)
        server = session.get(Server, server_id)
        if server is not None:
            session.delete(server)
        session.commit()


def test_fire_dispatches_restart(monkeypatch):
    server_id, schedule_id = _make_row(action="restart")
    calls: list[str] = []

    async def fake_restart(sid):
        calls.append(sid)

    monkeypatch.setattr(manager, "restart", fake_restart)
    try:
        asyncio.run(scheduler_service._fire(schedule_id))
        assert calls == [server_id]
        # Not one_time — the row survives.
        with Session(engine) as session:
            assert session.get(Schedule, schedule_id) is not None
    finally:
        _cleanup(server_id)


def test_fire_command_sends_to_console(monkeypatch):
    server_id, schedule_id = _make_row(action="command", command="say scheduled hi")
    sent: list[tuple[str, str]] = []

    async def fake_send(sid, cmd):
        sent.append((sid, cmd))

    monkeypatch.setattr(manager, "send", fake_send)
    try:
        asyncio.run(scheduler_service._fire(schedule_id))
        assert sent == [(server_id, "say scheduled hi")]
    finally:
        _cleanup(server_id)


def test_fire_backup_uses_scheduled_trigger(monkeypatch):
    server_id, schedule_id = _make_row(action="backup")
    triggers: list[str] = []

    async def fake_backup(session, server, *, trigger="manual"):
        assert server.id == server_id
        triggers.append(trigger)

    from lectern import backups

    monkeypatch.setattr(backups, "create_backup", fake_backup)
    try:
        asyncio.run(scheduler_service._fire(schedule_id))
        assert triggers == ["scheduled"]
    finally:
        _cleanup(server_id)


def test_fire_one_time_deletes_itself(monkeypatch):
    server_id, schedule_id = _make_row(action="stop", one_time=True)

    async def fake_stop(sid):
        pass

    monkeypatch.setattr(manager, "stop", fake_stop)
    try:
        asyncio.run(scheduler_service._fire(schedule_id))
        with Session(engine) as session:
            assert session.get(Schedule, schedule_id) is None
    finally:
        _cleanup(server_id)


def test_fire_failure_lands_in_console_hub(monkeypatch):
    server_id, schedule_id = _make_row(action="stop")

    async def failing_stop(sid):
        raise RuntimeError("boom")

    monkeypatch.setattr(manager, "stop", failing_stop)
    try:
        asyncio.run(scheduler_service._fire(schedule_id))
        history = list(manager.hub.history(server_id))
        assert any("scheduled stop failed" in line for line in history)
        # A failed ordinary schedule stays for the next occurrence.
        with Session(engine) as session:
            assert session.get(Schedule, schedule_id) is not None
    finally:
        _cleanup(server_id)


def test_fire_stale_or_disabled_is_a_noop(monkeypatch):
    server_id, schedule_id = _make_row(action="stop", enabled=False)
    called = False

    async def fake_stop(sid):
        nonlocal called
        called = True

    monkeypatch.setattr(manager, "stop", fake_stop)
    try:
        asyncio.run(scheduler_service._fire(schedule_id))  # disabled row
        asyncio.run(scheduler_service._fire("gone"))  # deleted row
        assert called is False
    finally:
        _cleanup(server_id)


def test_fire_update_mods_applies_and_records(monkeypatch, tmp_path):
    server_id, schedule_id = _make_row(action="update_mods")
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(tmp_path)
        session.add(server)
        session.commit()

    calls: list[tuple] = []

    async def fake_apply(session, sid, server_dir, *, mc_version):
        calls.append((sid, str(server_dir), mc_version))
        return ["Sodium 0.9.0 → 0.9.1"]

    recorded: list[tuple] = []
    from lectern import events
    from lectern.content import manager as content_manager

    monkeypatch.setattr(content_manager, "apply_updates_all", fake_apply)
    monkeypatch.setattr(
        events, "record", lambda sid, kind, message="": recorded.append((sid, kind, message))
    )
    try:
        asyncio.run(scheduler_service._fire(schedule_id))
        assert calls == [(server_id, str(tmp_path), "1.21")]
        assert (server_id, "mods_updated", "Sodium 0.9.0 → 0.9.1") in recorded
    finally:
        _cleanup(server_id)


def test_fire_update_mods_requires_installed_server(monkeypatch):
    # No path on the server → the failure lands in the console hub, not raised.
    server_id, schedule_id = _make_row(action="update_mods")
    try:
        manager.hub.clear(server_id)
        asyncio.run(scheduler_service._fire(schedule_id))
        assert any(
            "scheduled update_mods failed" in line
            for line in manager.hub.history(server_id)
        )
    finally:
        _cleanup(server_id)
