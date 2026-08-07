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

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ..content import manager as content
from ..content import mrpack as mrpack_mod
from ..content import resourcepacks as rp
from ..content.manager import ContentError
from ..content.mrpack import MrpackError
from ..content.resourcepacks import ResourcePackError
from ..db import get_session
from ..models import (
    ContentInstallRequest,
    ContentItem,
    ContentItemRead,
    ContentItemUpdate,
    ContentUpdateRead,
    ReleaseChannel,
    Server,
    ServerResourcePackUpdate,
    VanillaTweaksInstallRequest,
)
from ..providers import modrinth
from ..providers import vanillatweaks as vt
from ..providers.base import ChecksumMismatch
from ..providers.vanillatweaks import VanillaTweaksError

router = APIRouter(prefix="/api", tags=["content"])

# Loader compatibility chain per server type: which Modrinth loader facets
# the server can actually run, most-native first. Vanilla has no entry —
# mods are refused for it (by the manager), while loaderless kinds (resource
# packs) work on every type. Quilt's chain includes fabric because Quilt
# loads Fabric mods — without it, dependency closures dead-end on projects
# with no quilt-tagged builds (Fabric API is the canonical case).
_LOADERS: dict[str, list[str]] = {
    "fabric": ["fabric"],
    "quilt": ["quilt", "fabric"],
    "neoforge": ["neoforge"],
    "forge": ["forge"],
    "paper": ["paper"],
}

_MAX_UPLOAD = 100 * 1024 * 1024  # 100 MB — generous for a resource pack
_MAX_MRPACK = 500 * 1024 * 1024  # .mrpack archives can bundle big overrides


def _get_server(server_id: str, session: Session) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    return server


def _content_context(server: Server) -> tuple[list[str] | None, Path]:
    """(loader-chain-or-None, server_dir) for content operations. The chain
    is ``None`` for loaderless server types — installing a *mod* then fails
    in the manager; resource packs go through fine."""
    if not server.path:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Server is not installed yet"
        )
    return _LOADERS.get(server.type), Path(server.path)


# --- catalog-ish (server-agnostic) -----------------------------------------


@router.get("/content/search")
async def search_content(
    query: str = "",
    project_type: str = "mod",
    loader: str | None = None,  # a server TYPE — expanded to its compat chain
    mc_version: str | None = None,
    categories: str | None = None,  # comma-separated Modrinth category slugs
    index: str = "relevance",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Modrinth search, passed through (hits + total_hits)."""
    return await modrinth.search(
        query,
        project_type=project_type,
        loader=_LOADERS.get(loader, [loader]) if loader else None,
        mc_version=mc_version,
        categories=[c for c in (categories or "").split(",") if c],
        index=index,
        limit=min(limit, 50),
        offset=offset,
    )


@router.get("/content/categories")
async def content_categories(project_type: str = "mod") -> list[dict]:
    """Modrinth category tags for one project type (name + header)."""
    tags = await modrinth.list_categories()
    return [
        {"name": t["name"], "header": t.get("header", "categories")}
        for t in tags
        if t.get("project_type") == project_type
    ]


@router.get("/content/projects/{project_id}/versions")
async def project_versions(
    project_id: str,
    loader: str | None = None,  # a server TYPE — expanded to its compat chain
    mc_version: str | None = None,
) -> list[dict]:
    """Compatible versions of a project, newest first (id, version_number,
    version_type, dependencies, files)."""
    return await modrinth.list_versions(
        project_id,
        loader=_LOADERS.get(loader, [loader]) if loader else None,
        mc_version=mc_version,
    )


# --- per-server ------------------------------------------------------------


@router.get("/servers/{server_id}/content", response_model=list[ContentItemRead])
def list_content(
    server_id: str, session: Session = Depends(get_session)
) -> list[ContentItem]:
    server = _get_server(server_id, session)
    if server.path:
        # Manifest is the source of truth — repair the row mirror if a past
        # sync failed or the server dir was changed out of band.
        content.ensure_synced(session, server_id, Path(server.path))
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
            kind=body.kind,
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
    _, server_dir = _content_context(server)
    found = await content.check_updates(server_dir, mc_version=server.mc_version)
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
    _, server_dir = _content_context(server)
    try:
        return await content.apply_update(
            session,
            server_id,
            server_dir,
            item_id,
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


# --- modpacks (M8) -----------------------------------------------------------


@router.post("/servers/{server_id}/modpack")
async def import_modpack(
    server_id: str,
    file: UploadFile,
    include_client_only: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """Import a Modrinth ``.mrpack`` onto this server: pins the pack's
    loader version, downloads its server-side files (sha512-verified),
    applies overrides, and reconciles against a previous import (an upgrade
    removes files the new pack version dropped). Client-only files are
    skipped unless ``include_client_only`` (for mislabeled mods)."""
    server = _get_server(server_id, session)
    _, server_dir = _content_context(server)
    data = await file.read(_MAX_MRPACK + 1)
    if len(data) > _MAX_MRPACK:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, "Modpack exceeds 500 MB"
        )
    try:
        return await mrpack_mod.import_mrpack(
            session,
            server_id,
            server_dir,
            data,
            include_client_only=include_client_only,
        )
    except MrpackError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ChecksumMismatch as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# --- resource packs (M7) -----------------------------------------------------


@router.get("/servers/{server_id}/vanillatweaks/categories")
async def vanillatweaks_categories(
    server_id: str, session: Session = Depends(get_session)
) -> dict:
    """VT resource-pack categories for this server's Minecraft version
    (raw Vanilla Tweaks payload; 502 when their unofficial API is down)."""
    server = _get_server(server_id, session)
    try:
        return await vt.categories(server.mc_version)
    except VanillaTweaksError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/servers/{server_id}/vanillatweaks/categories/{pack_type}")
async def vanillatweaks_typed_categories(
    server_id: str, pack_type: str, session: Session = Depends(get_session)
) -> dict:
    """VT categories for one pack type (resourcepacks|datapacks|craftingtweaks)."""
    server = _get_server(server_id, session)
    try:
        return await vt.categories(server.mc_version, pack_type)
    except VanillaTweaksError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post(
    "/servers/{server_id}/vanillatweaks",
    response_model=list[ContentItemRead],
    status_code=status.HTTP_201_CREATED,
)
async def install_vanillatweaks(
    server_id: str,
    body: VanillaTweaksInstallRequest,
    session: Session = Depends(get_session),
) -> list[ContentItem]:
    """Generate + install a VT selection (share code or explicit packs).
    Replaces the server's previous VT set of the same type; an unchanged
    selection is a no-op (fingerprint match). A share code carries its own
    pack type, which wins over ``pack_type``. Datapack sets return one item
    per extracted pack."""
    server = _get_server(server_id, session)
    _, server_dir = _content_context(server)
    packs = body.packs
    pack_type = body.pack_type
    try:
        if body.share_code:
            definition = await vt.resolve_share_code(body.share_code.strip())
            packs = definition.get("packs") or {}
            pack_type = definition.get("type") or pack_type
        if not packs:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "No packs selected"
            )
        return await rp.install_vanillatweaks(
            session,
            server_id,
            server_dir,
            packs=packs,
            mc_version=server.mc_version,
            pack_type=pack_type,
        )
    except VanillaTweaksError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except ResourcePackError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post(
    "/servers/{server_id}/content/upload",
    response_model=ContentItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_content(
    server_id: str,
    file: UploadFile,
    kind: str = "resourcepack",
    session: Session = Depends(get_session),
) -> ContentItem:
    """Upload a resource-pack or datapack zip (validated via pack.mcmeta)."""
    server = _get_server(server_id, session)
    _, server_dir = _content_context(server)
    data = await file.read(_MAX_UPLOAD + 1)
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, "Pack exceeds 100 MB"
        )
    try:
        return await rp.install_upload(
            session,
            server_id,
            server_dir,
            filename=file.filename or "pack.zip",
            data=data,
            kind=kind,
        )
    except ResourcePackError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/servers/{server_id}/content/{item_id}/file")
def download_content_file(
    server_id: str,
    item_id: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Serve an item's file — this is the URL ``resource-pack`` points at so
    Minecraft clients can fetch the pack."""
    server = _get_server(server_id, session)
    _, server_dir = _content_context(server)
    item = session.get(ContentItem, item_id)
    if item is None or item.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Content item not found")
    path = content.kind_dir(server_dir, item.kind) / item.filename
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not on disk")
    return FileResponse(path, filename=item.filename)


@router.post("/servers/{server_id}/content/{item_id}/serve")
def set_server_pack(
    server_id: str,
    item_id: str,
    body: ServerResourcePackUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Set (or clear) this pack as the server's ``resource-pack`` prompt.

    The URL is derived from this request's origin — it must be reachable by
    the *players'* machines (works on a LAN; a proxied/dev origin follows
    whatever host the browser used). Applies at next server start.
    """
    server = _get_server(server_id, session)
    _, server_dir = _content_context(server)
    item = session.get(ContentItem, item_id)
    if item is None or item.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Content item not found")
    if item.kind != "resourcepack":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a resource pack")

    if body.enabled:
        path = content.kind_dir(server_dir, "resourcepack") / item.filename
        if not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "File not on disk")
        url = f"{str(request.base_url).rstrip('/')}/api/servers/{server_id}/content/{item_id}/file"
        sha1 = rp.sha1_of(path)
        rp.set_server_resource_pack(server_dir, url=url, sha1=sha1)
        return {"resource_pack": url, "resource_pack_sha1": sha1}
    rp.set_server_resource_pack(server_dir, url="", sha1="")
    return {"resource_pack": None, "resource_pack_sha1": None}
