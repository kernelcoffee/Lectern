"""The single writer of an existing row's ``Server.status``.

Every transition (process lifecycle, install, version change, startup
reconcile) goes through ``set_status`` so the change is both persisted and
published to ``StatusHub`` (and from there to ``/ws/status``). Deliberately
imports no manager code so ``install``/``version_change`` can use it without
an import cycle.
"""

from __future__ import annotations

from sqlmodel import Session

from ..models import Server
from ..ws import status_hub


def set_status(session: Session, server: Server, status: str) -> bool:
    """Persist and publish a status change. No-op (returns ``False``) when the
    status is unchanged, so double writes never produce duplicate feed events.
    Publishes strictly after the commit so a client refetching on the event
    never reads the old row."""
    if server.status == status:
        return False
    server.status = status
    session.add(server)
    session.commit()
    status_hub.publish({"server_id": server.id, "status": status})
    return True
