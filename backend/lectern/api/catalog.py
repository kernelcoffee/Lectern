"""Catalog endpoints feeding the create-server wizard.

Version lists are scoped to the chosen server type, so the wizard's Minecraft
version choice always matches what that type actually supports.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..servers.types import get_server_type, list_server_types

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/server-types")
async def server_types() -> list[dict]:
    return list_server_types()


@router.get("/minecraft-versions")
async def minecraft_versions(type: str) -> list[str]:
    try:
        provider = get_server_type(type)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown server type: {type}")
    return await provider.list_minecraft_versions()


# NB: `/loaders/fabric/{mc}` keeps working — "fabric" is just one type key.
@router.get("/loaders/{type}/{mc_version}")
async def loader_versions(type: str, mc_version: str) -> list[str]:
    try:
        provider = get_server_type(type)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown server type: {type}")
    return await provider.list_loader_versions(mc_version)
