"""Live status feed: StatusHub fan-out, the single set_status writer, the
/ws/status route, and stop/restart reporting ``stopping`` synchronously."""

from __future__ import annotations

import asyncio

from sqlmodel import Session

from lectern.models import Server
from lectern.servers.manager import manager
from lectern.servers.status import set_status
from lectern.ws import StatusHub, status_hub


def _insert(engine, name: str, status: str, port: int = 25565) -> str:
    with Session(engine) as s:
        srv = Server(name=name, mc_version="1.21", status=status, port=port)
        s.add(srv)
        s.commit()
        return srv.id


def test_status_hub_fanout_and_bounded_queue():
    hub = StatusHub()
    a, b, gone = hub.subscribe(), hub.subscribe(), hub.subscribe()
    hub.unsubscribe(gone)
    hub.publish({"server_id": "x", "status": "running"})
    assert a.get_nowait() == {"server_id": "x", "status": "running"}
    assert b.get_nowait() == {"server_id": "x", "status": "running"}
    assert gone.empty()

    # A stalled subscriber drops events instead of raising into the writer.
    for i in range(a.maxsize + 5):
        hub.publish({"server_id": str(i), "status": "running"})
    assert a.qsize() == a.maxsize


def test_set_status_publishes_once(engine):
    sid = _insert(engine, "Feed", "stopped")
    queue = status_hub.subscribe()
    try:
        with Session(engine) as s:
            srv = s.get(Server, sid)
            assert set_status(s, srv, "stopping") is True
            assert queue.get_nowait() == {"server_id": sid, "status": "stopping"}
            assert set_status(s, srv, "stopping") is False
            assert queue.empty()
        with Session(engine) as s:
            assert s.get(Server, sid).status == "stopping"
    finally:
        status_hub.unsubscribe(queue)


def test_status_ws_snapshot_then_update(client, engine):
    a = _insert(engine, "A", "stopped")
    b = _insert(engine, "B", "running", port=25570)
    with client.websocket_connect("/ws/status") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        statuses = {e["server_id"]: e["status"] for e in snap["servers"]}
        assert statuses == {a: "stopped", b: "running"}
        status_hub.publish({"server_id": a, "status": "stopping"})
        assert ws.receive_json() == {"type": "status", "server_id": a, "status": "stopping"}


class _FakeProc:
    def __init__(self) -> None:
        self.running = True
        self.stops: list[tuple[str, int]] = []

    async def stop(self, cmd: str, timeout: int) -> None:
        self.stops.append((cmd, timeout))


def _running_with_fake(engine, monkeypatch) -> tuple[str, _FakeProc]:
    monkeypatch.setattr("lectern.servers.manager.engine", engine)
    sid = _insert(engine, "Live", "running")
    fake = _FakeProc()
    manager._procs[sid] = fake  # type: ignore[assignment]
    return sid, fake


def test_stop_action_reports_stopping(client, engine, monkeypatch):
    sid, _fake = _running_with_fake(engine, monkeypatch)
    queue = status_hub.subscribe()
    try:
        resp = client.post(f"/api/servers/{sid}/action/stop")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "stopping"
        assert body["running"] is True
        assert queue.get_nowait() == {"server_id": sid, "status": "stopping"}
    finally:
        status_hub.unsubscribe(queue)
        manager._procs.pop(sid, None)


def test_restart_action_reports_stopping(client, engine, monkeypatch):
    sid, _fake = _running_with_fake(engine, monkeypatch)
    try:
        resp = client.post(f"/api/servers/{sid}/action/restart")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "stopping"
    finally:
        manager._procs.pop(sid, None)


async def test_process_stopping_callback_dedupes(engine, monkeypatch):
    """The process's own on_state("stopping") after the manager's synchronous
    write must not publish a second event."""
    sid, _fake = _running_with_fake(engine, monkeypatch)
    queue = status_hub.subscribe()
    try:
        await manager.stop(sid)
        await manager._on_state(sid, "stopping")
        await asyncio.sleep(0)
        assert queue.get_nowait()["status"] == "stopping"
        assert queue.empty()
    finally:
        status_hub.unsubscribe(queue)
        manager._procs.pop(sid, None)
