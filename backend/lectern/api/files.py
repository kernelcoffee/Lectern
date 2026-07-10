"""File-manager endpoints (M12): browse/edit/upload/download inside a server.

Every path is confined to the server directory by ``servers/files.py``
(``safe_path``); this layer only maps ``FileManagerError`` to HTTP and streams
uploads/downloads. Edits are allowed while the server runs (they apply at the
next start, same as Properties) — only the delete/rename/write guards on the
root and ``.lectern/`` restrict what can change.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session

from ..db import get_session
from ..models import (
    DirListingRead,
    FileContentRead,
    FileEntryRead,
    MkdirRequest,
    RenameRequest,
    Server,
    WriteFileRequest,
)
from ..servers import files as fm

router = APIRouter(prefix="/api/servers/{server_id}/files", tags=["files"])


def _server_dir(server_id: str, session: Session) -> Path:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if not server.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not installed yet")
    return Path(server.path)


def _handle(exc: fm.FileManagerError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


@router.get("", response_model=DirListingRead)
def list_files(
    server_id: str, path: str = "", session: Session = Depends(get_session)
) -> DirListingRead:
    server_dir = _server_dir(server_id, session)
    try:
        entries = fm.list_dir(server_dir, path)
    except fm.FileManagerError as exc:
        raise _handle(exc)
    return DirListingRead(
        path=path,
        entries=[FileEntryRead(**e.__dict__) for e in entries],
    )


@router.get("/content", response_model=FileContentRead)
def read_content(
    server_id: str, path: str, session: Session = Depends(get_session)
) -> FileContentRead:
    server_dir = _server_dir(server_id, session)
    try:
        data = fm.read_file(server_dir, path)
    except fm.FileManagerError as exc:
        raise _handle(exc)
    return FileContentRead(path=path, **data)


@router.put("/content", status_code=status.HTTP_204_NO_CONTENT)
def write_content(
    server_id: str,
    path: str,
    payload: WriteFileRequest,
    session: Session = Depends(get_session),
) -> None:
    server_dir = _server_dir(server_id, session)
    try:
        fm.write_file(server_dir, path, payload.content)
    except fm.FileManagerError as exc:
        raise _handle(exc)


@router.post("/dir", status_code=status.HTTP_201_CREATED)
def make_dir(
    server_id: str, payload: MkdirRequest, session: Session = Depends(get_session)
) -> dict:
    server_dir = _server_dir(server_id, session)
    try:
        fm.make_dir(server_dir, payload.path)
    except fm.FileManagerError as exc:
        raise _handle(exc)
    return {"path": payload.path}


@router.post("/rename", status_code=status.HTTP_204_NO_CONTENT)
def rename(
    server_id: str, payload: RenameRequest, session: Session = Depends(get_session)
) -> None:
    server_dir = _server_dir(server_id, session)
    try:
        fm.rename(server_dir, payload.path, payload.to)
    except fm.FileManagerError as exc:
        raise _handle(exc)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    server_id: str, path: str, session: Session = Depends(get_session)
) -> None:
    server_dir = _server_dir(server_id, session)
    try:
        fm.delete(server_dir, path)
    except fm.FileManagerError as exc:
        raise _handle(exc)


@router.get("/download")
def download(
    server_id: str, path: str, session: Session = Depends(get_session)
) -> FileResponse:
    server_dir = _server_dir(server_id, session)
    try:
        target = fm.safe_path(server_dir, path)
    except fm.FileManagerError as exc:
        raise _handle(exc)
    if not target.is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a file")
    return FileResponse(target, filename=target.name)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(
    server_id: str,
    file: UploadFile = File(...),
    path: str = Form(""),
    session: Session = Depends(get_session),
) -> dict:
    """Upload a file into directory ``path`` (default the server root)."""
    server_dir = _server_dir(server_id, session)
    try:
        dest = fm.upload_dest(server_dir, path, file.filename or "upload")
    except fm.FileManagerError as exc:
        raise _handle(exc)

    received = 0
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, delete=False, suffix=".part"
    ) as tmp:
        tmp_path = Path(tmp.name)
        while chunk := await file.read(1 << 20):
            received += len(chunk)
            if received > fm.UPLOAD_MAX:
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is too large"
                )
            tmp.write(chunk)
    tmp_path.replace(dest)
    return {"name": dest.name, "size": received}
