"""Event timeline: per-server history + a cross-server recent feed.

Read-only — rows are written by the manager / backups / scheduler via
``lectern.events.record``. Newest first; ``limit`` is clamped to the
per-server retention cap so a huge value can't turn into a table scan.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..db import get_session
from ..events import _KEEP_PER_SERVER
from ..models import EventWithServerRead, Server, ServerEvent, ServerEventRead

router = APIRouter(tags=["events"])


def _clamp(limit: int) -> int:
    return max(1, min(limit, _KEEP_PER_SERVER))


@router.get(
    "/api/servers/{server_id}/events", response_model=list[ServerEventRead]
)
def server_events(
    server_id: str, limit: int = 50, session: Session = Depends(get_session)
) -> list[ServerEvent]:
    if session.get(Server, server_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    return list(
        session.exec(
            select(ServerEvent)
            .where(ServerEvent.server_id == server_id)
            # created_at (id as tiebreak): ids don't follow event time for
            # backfilled/imported rows.
            .order_by(ServerEvent.created_at.desc(), ServerEvent.id.desc())  # type: ignore[union-attr]
            .limit(_clamp(limit))
        ).all()
    )


@router.get("/api/events", response_model=list[EventWithServerRead])
def recent_events(
    limit: int = 50, session: Session = Depends(get_session)
) -> list[EventWithServerRead]:
    """Recent events across all servers — the dashboard's activity feed."""
    rows = session.exec(
        select(ServerEvent, Server.name)
        .join(Server, Server.id == ServerEvent.server_id)  # type: ignore[arg-type]
        .order_by(ServerEvent.created_at.desc(), ServerEvent.id.desc())  # type: ignore[union-attr]
        .limit(_clamp(limit))
    ).all()
    return [
        EventWithServerRead(**event.model_dump(), server_name=name)
        for event, name in rows
    ]
