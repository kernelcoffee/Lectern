"""Content endpoints (M6): Modrinth search/versions + per-server content CRUD.

Search and version listing are server-agnostic (the frontend passes the
server's loader + MC version so results are always compatible); install/
toggle/remove/update operate on one server and are guarded by its existence
and installed state. Installs run inline in the request — mod files are small
and the UI shows a busy state; the dependency closure is returned so the
frontend can surface what else was pulled in.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..content import manager as content
from ..content.manager import ContentError
from ..db import get_session
from ..models import (
    ContentInstallRequest,
    ContentItem,
    ContentItemRead,
    ContentItemUpdate,
    ContentUpdateRead,
    ReleaseChannel,
    Server,
)
from ..providers import modrinth
from ..providers.base import ChecksumMismatch

router = APIRouter(prefix="/api", tags=["content"])

# Which loader category to search Modrinth with, per server type. Vanilla has
# no entry — it can't load mods, and the API refuses content ops for it.
_LOADERS = {"fabric": "fabric", "quilt": "quilt", "paper": "paper"}


def _get_server(server_id: str, session: Session) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    return server


def _content_context(server: Server) -> tuple[str, Path]:
    """(loader, server_dir) for content operations, or the right HTTP error."""
    loader = _LOADERS.get(server.type)
    if loader is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{server.type} servers do not support mods",
        )
    if not server.path:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Server is not installed yet"
        )
    return loader, Path(server.path)


# --- catalog-ish (server-agnostic) -----------------------------------------


@router.get("/content/search")
async def search_content(
    query: str = "",
    project_type: str = "mod",
    loader: str | None = None,
    mc_version: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Modrinth search, passed through (hits + total_hits)."""
    return await modrinth.search(
        query,
        project_type=project_type,
        loader=loader,
        mc_version=mc_version,
        limit=min(limit, 50),
        offset=offset,
    )


@router.get("/content/projects/{project_id}/versions")
async def project_versions(
    project_id: str,
    loader: str | None = None,
    mc_version: str | None = None,
) -> list[dict]:
    """Compatible versions of a project, newest first (id, version_number,
    version_type, dependencies, files)."""
    return await modrinth.list_versions(
        project_id, loader=loader, mc_version=mc_version
    )


# --- per-server ------------------------------------------------------------


@router.get("/servers/{server_id}/content", response_model=list[ContentItemRead])
def list_content(
    server_id: str, session: Session = Depends(get_session)
) -> list[ContentItem]:
    _get_server(server_id, session)
    return list(
        session.exec(
            select(ContentItem)
            .where(ContentItem.server_id == server_id)
            .order_by(ContentItem.name)
        ).all()
    )


@router.post(
    "/servers/{server_id}/content",
    response_model=list[ContentItemRead],
    status_code=status.HTTP_201_CREATED,
)
async def install_content(
    server_id: str,
    body: ContentInstallRequest,
    session: Session = Depends(get_session),
) -> list[ContentItem]:
    """Install a project and its required deps (optional deps on request).
    Returns every item the operation added or replaced."""
    server = _get_server(server_id, session)
    loader, server_dir = _content_context(server)
    try:
        return await content.install(
            session,
            server_id,
            server_dir,
            project_id=body.project_id,
            source_key=body.source,
            version_id=body.version_id,
            loader=loader,
            mc_version=server.mc_version,
            channel=body.channel.value,
            include_optional_deps=body.include_optional_deps,
        )
    except ContentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ChecksumMismatch as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get(
    "/servers/{server_id}/content/updates", response_model=list[ContentUpdateRead]
)
async def content_updates(
    server_id: str, session: Session = Depends(get_session)
) -> list[ContentUpdateRead]:
    """Installed items with a newer version qualifying under their channel."""
    server = _get_server(server_id, session)
    loader, server_dir = _content_context(server)
    found = await content.check_updates(
        server_dir, loader=loader, mc_version=server.mc_version
    )
    return [
        ContentUpdateRead(
            item_id=entry["item"]["id"],
            name=entry["item"]["name"],
            installed_version=entry["item"].get("version_number")
            or entry["item"].get("version_id"),
            new_version_id=entry["new_version"]["id"],
            new_version_number=entry["new_version"].get("version_number", "?"),
        )
        for entry in found
    ]


@router.patch(
    "/servers/{server_id}/content/{item_id}", response_model=ContentItemRead
)
async def patch_content(
    server_id: str,
    item_id: str,
    body: ContentItemUpdate,
    session: Session = Depends(get_session),
) -> ContentItem:
    server = _get_server(server_id, session)
    _, server_dir = _content_context(server)
    try:
        return await content.patch_item(
            session,
            server_id,
            server_dir,
            item_id,
            enabled=body.enabled,
            channel=body.channel.value if isinstance(body.channel, ReleaseChannel) else body.channel,
        )
    except ContentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post(
    "/servers/{server_id}/content/{item_id}/update",
    response_model=ContentItemRead,
)
async def update_content(
    server_id: str,
    item_id: str,
    session: Session = Depends(get_session),
) -> ContentItem:
    """Swap the item's file for the newest version allowed by its channel."""
    server = _get_server(server_id, session)
    loader, server_dir = _content_context(server)
    try:
        return await content.apply_update(
            session,
            server_id,
            server_dir,
            item_id,
            loader=loader,
            mc_version=server.mc_version,
        )
    except ContentError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in str(exc).lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, str(exc)) from exc
    except ChecksumMismatch as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.delete(
    "/servers/{server_id}/content/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_content(
    server_id: str,
    item_id: str,
    session: Session = Depends(get_session),
) -> None:
    server = _get_server(server_id, session)
    _, server_dir = _content_context(server)
    try:
        await content.remove(session, server_id, server_dir, item_id)
    except ContentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
