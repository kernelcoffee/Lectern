"""Stats history + size (M11 monitoring): the sampler records/prunes, sizes are
computed off-request, and the endpoints serve the window / cached size."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from lectern.db import engine, init_db
from lectern.models import Server, ServerStat
from lectern.servers import stats_sampler as ss
from lectern.servers.stats_sampler import SizeInfo, StatsSampler, _dir_size

# --- dir size ---------------------------------------------------------------


def test_dir_size(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 50)
    assert _dir_size(tmp_path) == 150


# --- sampler recording ------------------------------------------------------


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.running = True


def _make_server(tmp_path, **overrides) -> str:
    """Create a server on the *module* engine (the sampler owns its own
    sessions there, same as test_manager.py)."""
    init_db()
    with Session(engine) as s:
        server = Server(name="s", mc_version="1.21", path=str(tmp_path), **overrides)
        s.add(server)
        s.commit()
        return server.id


def _cleanup(server_id: str) -> None:
    with Session(engine) as s:
        for row in s.exec(
            select(ServerStat).where(ServerStat.server_id == server_id)
        ).all():
            s.delete(row)
        srv = s.get(Server, server_id)
        if srv is not None:
            s.delete(srv)
        s.commit()


def test_sample_once_records_running_servers(tmp_path, monkeypatch):
    (tmp_path / "server.properties").write_text("server-port=25599\n")
    server_id = _make_server(tmp_path)
    sampler = StatsSampler()

    from lectern.servers.manager import manager

    monkeypatch.setattr(
        manager, "running_processes", lambda: [(server_id, _FakeProc(4242))]
    )
    monkeypatch.setattr(
        ss.stats_mod, "resource_usage",
        lambda pid: ss.stats_mod.ResourceUsage(cpu_percent=12.5, memory_mb=900.0),
    )

    async def fake_ping(host, port):
        assert port == 25599  # read from server.properties, not the DB column
        return ss.stats_mod.PingResult(online=3, max=20)

    monkeypatch.setattr(ss.stats_mod, "server_list_ping", fake_ping)

    try:
        asyncio.run(sampler.sample_once(compute_sizes=False))
        with Session(engine) as s:
            rows = s.exec(
                select(ServerStat).where(ServerStat.server_id == server_id)
            ).all()
        assert len(rows) == 1
        assert rows[0].cpu_percent == 12.5
        assert rows[0].memory_mb == 900.0
        assert rows[0].players_online == 3
    finally:
        _cleanup(server_id)


def test_sample_once_computes_sizes(tmp_path, monkeypatch):
    (tmp_path / "server.properties").write_text("level-name=world\n")
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"x" * 200)
    (tmp_path / "server.jar").write_bytes(b"y" * 300)
    server_id = _make_server(tmp_path)
    sampler = StatsSampler()

    from lectern.servers.manager import manager

    monkeypatch.setattr(manager, "running_processes", list)
    try:
        asyncio.run(sampler.sample_once(compute_sizes=True))
        info = sampler.size_of(server_id)
        assert info is not None
        assert info.world_bytes == 200
        # world (200) + jar (300) + server.properties (17 bytes)
        assert info.server_bytes == 517
    finally:
        _cleanup(server_id)


def test_prune_drops_old_samples(tmp_path):
    server_id = _make_server(tmp_path)
    old = datetime.now(UTC) - timedelta(hours=48)
    with Session(engine) as s:
        s.add(ServerStat(server_id=server_id, created_at=old))
        s.add(ServerStat(server_id=server_id))  # now
        s.commit()
    try:
        StatsSampler()._prune()
        with Session(engine) as s:
            remaining = s.exec(
                select(ServerStat).where(ServerStat.server_id == server_id)
            ).all()
        assert len(remaining) == 1  # the 48h-old one pruned
    finally:
        _cleanup(server_id)


# --- endpoints --------------------------------------------------------------


def test_history_endpoint_windows(client, engine, tmp_path):
    server_id = client.post(
        "/api/servers", json={"name": "H", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(ServerStat(server_id=server_id, created_at=now - timedelta(minutes=90), cpu_percent=1))
        s.add(ServerStat(server_id=server_id, created_at=now - timedelta(minutes=30), cpu_percent=2))
        s.add(ServerStat(server_id=server_id, created_at=now - timedelta(minutes=5), cpu_percent=3))
        s.commit()

    # Default 60 min → only the last two, oldest→newest.
    hist = client.get(f"/api/servers/{server_id}/stats/history").json()
    assert [h["cpu_percent"] for h in hist] == [2, 3]
    # Wider window includes all three.
    hist = client.get(f"/api/servers/{server_id}/stats/history?minutes=120").json()
    assert len(hist) == 3


def test_size_endpoint_reads_cache(client, engine, tmp_path, monkeypatch):
    server_id = client.post(
        "/api/servers", json={"name": "Z", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    # No measurement yet → empty.
    empty = client.get(f"/api/servers/{server_id}/size").json()
    assert empty == {"world_bytes": None, "server_bytes": None, "computed_at": None}

    from lectern.api.servers import stats_sampler

    monkeypatch.setitem(
        stats_sampler._sizes,
        server_id,
        SizeInfo(world_bytes=1024, server_bytes=4096, computed_at=datetime.now(UTC)),
    )
    got = client.get(f"/api/servers/{server_id}/size").json()
    assert got["world_bytes"] == 1024 and got["server_bytes"] == 4096
