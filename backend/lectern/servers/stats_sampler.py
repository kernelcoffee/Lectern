"""Background stats sampler (M11 monitoring).

Turns the pull-based live stats (M5) into a persisted time series so the UI can
graph CPU / memory / players over time — the "feels less mature than Crafty"
gap. One asyncio loop (started in the app lifespan) that every
``SAMPLE_INTERVAL`` seconds:

1. records a ``ServerStat`` row for each **running** server (cpu/mem via psutil,
   players via a Server List Ping — the same primitives the stats endpoint
   uses), and
2. on a slower cadence computes each installed server's **directory / world
   size** (an ``os.walk`` — expensive, so kept off the request path and cached
   in memory), for both running and stopped servers.

Old samples are pruned to ``RETENTION`` so the table stays bounded. Failures in
a tick are logged and swallowed — the loop must never die.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from ..db import engine
from ..models import Server, ServerStat
from . import properties as props
from . import stats as stats_mod

log = logging.getLogger(__name__)

SAMPLE_INTERVAL = 30  # seconds between resource samples
SIZE_EVERY = 10  # compute dir sizes every Nth tick (~5 min at 30 s)
RETENTION = timedelta(hours=24)  # keep this much history per server


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SizeInfo:
    world_bytes: int | None
    server_bytes: int | None
    computed_at: datetime


def _dir_size(path: Path) -> int:
    """Total size of a directory tree in bytes (symlinks not followed)."""
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue  # file vanished mid-walk / permission — skip
    return total


def effective_port(server: Server) -> int:
    """The port the server actually bound: server.properties wins over the DB."""
    if server.path:
        file_port = props.read_properties(Path(server.path)).get("server-port")
        if file_port is not None and file_port.isdigit():
            return int(file_port)
    return server.port


class StatsSampler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # server_id -> latest computed on-disk size (in-memory cache).
        self._sizes: dict[str, SizeInfo] = {}

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def size_of(self, server_id: str) -> SizeInfo | None:
        return self._sizes.get(server_id)

    # --- loop ----------------------------------------------------------------

    async def _loop(self) -> None:
        tick = 0
        while True:
            try:
                await self.sample_once(compute_sizes=(tick % SIZE_EVERY == 0))
            except Exception:  # noqa: BLE001 — the loop must survive any tick
                log.exception("stats sampler tick failed")
            tick += 1
            await asyncio.sleep(SAMPLE_INTERVAL)

    async def sample_once(self, *, compute_sizes: bool) -> None:
        """One sampling pass: record a row per running server, optionally
        refresh sizes, then prune old rows. Exposed for tests."""
        from .manager import manager

        for server_id, proc in manager.running_processes():
            if proc.pid is None:
                continue
            usage = stats_mod.resource_usage(proc.pid)
            with Session(engine) as session:
                server = session.get(Server, server_id)
                if server is None:
                    continue
                port = effective_port(server)
            ping = await stats_mod.server_list_ping("127.0.0.1", port)
            self._record(server_id, usage, ping)

        if compute_sizes:
            await self._refresh_sizes()

        self._prune()

    def _record(self, server_id: str, usage, ping) -> None:
        with Session(engine) as session:
            session.add(
                ServerStat(
                    server_id=server_id,
                    cpu_percent=usage.cpu_percent if usage else 0.0,
                    memory_mb=usage.memory_mb if usage else 0.0,
                    players_online=ping.online if ping else 0,
                )
            )
            session.commit()

    async def _refresh_sizes(self) -> None:
        with Session(engine) as session:
            servers = [
                (s.id, s.path, props.read_properties(Path(s.path)).get("level-name") or "world")
                for s in session.exec(select(Server)).all()
                if s.path
            ]
        for server_id, path, level_name in servers:
            server_dir = Path(path)
            if not server_dir.is_dir():
                continue
            server_bytes = await asyncio.to_thread(_dir_size, server_dir)
            world_dir = server_dir / level_name
            world_bytes = (
                await asyncio.to_thread(_dir_size, world_dir)
                if world_dir.is_dir()
                else None
            )
            self._sizes[server_id] = SizeInfo(
                world_bytes=world_bytes,
                server_bytes=server_bytes,
                computed_at=_now(),
            )

    def _prune(self) -> None:
        cutoff = _now() - RETENTION
        with Session(engine) as session:
            stale = session.exec(
                select(ServerStat).where(ServerStat.created_at < cutoff)
            ).all()
            for row in stale:
                session.delete(row)
            session.commit()


# Singleton — wired into the app lifespan alongside the scheduler.
stats_sampler = StatsSampler()
