"""Persisted per-server event timeline (day-2 ops).

``record`` is called fire-and-forget from the manager (lifecycle, crash
restarts), backups, and the scheduler — paths that must never fail because
telemetry did, so it swallows and logs its own errors. Each write also prunes
that server's timeline to the newest ``_KEEP_PER_SERVER`` rows; events are
low-volume, so pruning inline beats another background loop.
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from .db import engine
from .models import ServerEvent

log = logging.getLogger(__name__)

_KEEP_PER_SERVER = 300


def record(server_id: str, kind: str, message: str = "") -> None:
    try:
        with Session(engine) as session:
            session.add(ServerEvent(server_id=server_id, kind=kind, message=message))
            stale = session.exec(
                select(ServerEvent)
                .where(ServerEvent.server_id == server_id)
                .order_by(ServerEvent.id.desc())  # type: ignore[union-attr]
                .offset(_KEEP_PER_SERVER)
            ).all()
            for row in stale:
                session.delete(row)
            session.commit()
    except Exception:  # noqa: BLE001 — never break the caller over telemetry
        log.exception("failed to record event %s for server %s", kind, server_id)
