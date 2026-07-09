"""Backup endpoints (M9): list/create/restore/delete per server.

Create runs inline (zipping is threaded off the event loop; worlds at LAN
scale take seconds). Restore requires a stopped server and is atomic-ish:
the old directory comes back if extraction fails (see backups.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from ..backups import (
    BackupError,
    create_backup,
    delete_backup,
    list_backups,
    restore_backup,
)
from ..db import get_session
from ..models import Backup, BackupRead, Server

router = APIRouter(prefix="/api/servers/{server_id}/backups", tags=["backups"])


def _get_server(server_id: str, session: Session) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    return server


def _get_backup(server_id: str, backup_id: str, session: Session) -> Backup:
    backup = session.get(Backup, backup_id)
    if backup is None or backup.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup not found")
    return backup


@router.get("", response_model=list[BackupRead])
def list_(server_id: str, session: Session = Depends(get_session)) -> list[Backup]:
    _get_server(server_id, session)
    return list_backups(session, server_id)


@router.post("", response_model=BackupRead, status_code=status.HTTP_201_CREATED)
async def create(server_id: str, session: Session = Depends(get_session)) -> Backup:
    server = _get_server(server_id, session)
    try:
        return await create_backup(session, server)
    except BackupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{backup_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore(
    server_id: str, backup_id: str, session: Session = Depends(get_session)
) -> None:
    server = _get_server(server_id, session)
    backup = _get_backup(server_id, backup_id, session)
    try:
        await restore_backup(session, server, backup)
    except BackupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/{backup_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    server_id: str, backup_id: str, session: Session = Depends(get_session)
) -> None:
    _get_server(server_id, session)
    delete_backup(session, _get_backup(server_id, backup_id, session))
