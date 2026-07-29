"""Event timeline: record/prune, lifecycle hooks, and the read endpoints."""

from __future__ import annotations

import asyncio

from sqlmodel import Session, select

from lectern import events
from lectern.db import engine, init_db
from lectern.models import Server, ServerEvent, ServerStatus
from lectern.servers.manager import ServerManager


# --- record / prune (module-level engine, like test_manager) -----------------


def _make_server(**overrides) -> str:
    init_db()
    server = Server(name="ev", mc_version="1.21", **overrides)
    with Session(engine) as session:
        session.add(server)
        session.commit()
        session.refresh(server)
        return server.id


def _events_for(server_id: str) -> list[ServerEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(ServerEvent)
                .where(ServerEvent.server_id == server_id)
                .order_by(ServerEvent.id)
            ).all()
        )


def _cleanup(server_id: str) -> None:
    with Session(engine) as session:
        for row in _events_for(server_id):
            session.delete(session.get(ServerEvent, row.id))
        server = session.get(Server, server_id)
        if server is not None:
            session.delete(server)
        session.commit()


def test_record_prunes_to_cap(monkeypatch):
    monkeypatch.setattr(events, "_KEEP_PER_SERVER", 5)
    sid = _make_server()
    try:
        for i in range(8):
            events.record(sid, "started", f"n{i}")
        rows = _events_for(sid)
        assert [r.message for r in rows] == ["n3", "n4", "n5", "n6", "n7"]
    finally:
        _cleanup(sid)


def test_record_never_raises(monkeypatch):
    # Telemetry must not break the manager/backup/scheduler paths that call it.
    monkeypatch.setattr(events, "engine", None)
    events.record("whatever", "started")  # no exception


def test_on_state_records_lifecycle():
    sid = _make_server(status=ServerStatus.starting.value, crash_restart=False)
    try:
        m = ServerManager()
        asyncio.run(m._on_state(sid, ServerStatus.running.value))
        asyncio.run(m._on_state(sid, ServerStatus.crashed.value))
        asyncio.run(m._on_state(sid, ServerStatus.stopped.value))
        assert [e.kind for e in _events_for(sid)] == ["started", "crashed", "stopped"]
    finally:
        _cleanup(sid)


def test_crash_restart_and_give_up_recorded():
    sid = _make_server(status=ServerStatus.starting.value, crash_restart=True)
    try:
        m = ServerManager()
        for _ in range(4):  # _MAX_CRASH_RESTARTS = 3, the 4th gives up
            asyncio.run(m._on_state(sid, ServerStatus.crashed.value))
        kinds = [e.kind for e in _events_for(sid)]
        assert kinds.count("crash_restart") == 3
        assert kinds[-1] == "crash_gave_up"
    finally:
        _cleanup(sid)


# --- endpoints (fixture engine via the client override) ----------------------


def _server_with_events(client, engine_, name, *kinds) -> str:
    sid = client.post(
        "/api/servers", json={"name": name, "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    with Session(engine_) as s:
        for kind in kinds:
            s.add(ServerEvent(server_id=sid, kind=kind, message=f"m-{kind}"))
        s.commit()
    return sid


def test_server_events_newest_first(client, engine):
    sid = _server_with_events(
        client, engine, "EA", "started", "crashed", "crash_restart"
    )

    body = client.get(f"/api/servers/{sid}/events").json()
    assert [e["kind"] for e in body] == ["crash_restart", "crashed", "started"]
    assert body[0]["message"] == "m-crash_restart"

    assert client.get(f"/api/servers/{sid}/events?limit=2").json()[-1]["kind"] == "crashed"
    assert client.get("/api/servers/nope/events").status_code == 404


def test_recent_events_carry_server_name(client, engine):
    a = _server_with_events(client, engine, "EA", "started")
    b = _server_with_events(client, engine, "EB", "backup_failed")

    body = client.get("/api/events").json()
    assert [(e["server_id"], e["kind"], e["server_name"]) for e in body[:2]] == [
        (b, "backup_failed", "EB"),
        (a, "started", "EA"),
    ]


def test_backup_records_event(client, engine, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(events, "record", lambda *a: calls.append(a))

    sid = client.post(
        "/api/servers", json={"name": "B", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.path = str(tmp_path)
        srv.status = "stopped"
        s.add(srv)
        s.commit()
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"x")

    assert client.post(f"/api/servers/{sid}/backups").status_code == 201
    assert calls and calls[-1][0] == sid and calls[-1][1] == "backup_created"
