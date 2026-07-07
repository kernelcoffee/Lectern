"""Catalog endpoints feeding the create-server wizard.

Version lists are scoped to the chosen server type, so the wizard's Minecraft
version choice always matches what that type actually supports.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..providers import fabric
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


@router.get("/loaders/fabric/{mc_version}")
async def fabric_loaders(mc_version: str) -> list[str]:
    return await fabric.list_loader_versions(mc_version)
