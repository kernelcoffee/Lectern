"""Players: a global registry + per-server whitelist/ops/banned management.

The registry (``Player`` table) is the "friends" list — add someone by username
or UUID and Lectern validates them against Mojang. Per-server, you add those
registered players to the whitelist / ops / banned lists, which edits the
server's own JSON files (applied at the next start / list reload).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlmodel import Session, select

from ..db import get_session
from ..providers import avatars
from ..models import (
    Player,
    PlayerAddRequest,
    PlayerEntry,
    PlayerListAddRequest,
    PlayerListsRead,
    PlayerRead,
    Server,
)
from ..providers import mojang
from ..servers import playerlists

router = APIRouter(tags=["players"])


# --- global registry --------------------------------------------------------


@router.get("/api/players", response_model=list[PlayerRead])
def list_players(session: Session = Depends(get_session)) -> list[Player]:
    return list(session.exec(select(Player).order_by(Player.name)).all())


@router.post("/api/players", response_model=PlayerRead, status_code=status.HTTP_201_CREATED)
async def add_player(
    payload: PlayerAddRequest, session: Session = Depends(get_session)
) -> Player:
    profile = await mojang.resolve_profile(payload.query)
    if profile is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f'No Minecraft player found for "{payload.query.strip()}"',
        )
    player = session.get(Player, profile["uuid"])
    if player is None:
        player = Player(uuid=profile["uuid"], name=profile["name"])
    else:
        player.name = profile["name"]  # refresh a possibly-renamed player
    session.add(player)
    session.commit()
    session.refresh(player)
    return player


@router.get("/api/players/{uuid}/avatar")
async def player_avatar(uuid: str, size: int = 32) -> Response:
    """Self-hosted player face avatar (PNG), rendered from the Mojang skin.
    404 when the player has no resolvable skin — the UI shows a fallback tile."""
    size = max(8, min(size, 512))
    png = await avatars.face_png(mojang.undash_uuid(uuid), size)
    if png is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No avatar available")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.delete("/api/players/{uuid}", status_code=status.HTTP_204_NO_CONTENT)
def remove_player(uuid: str, session: Session = Depends(get_session)) -> None:
    player = session.get(Player, mojang.undash_uuid(uuid))
    if player is not None:
        session.delete(player)
        session.commit()


# --- per-server lists -------------------------------------------------------


def _server_dir(server_id: str, session: Session) -> Path:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if not server.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not installed yet")
    return Path(server.path)


def _lists(server_dir: Path) -> PlayerListsRead:
    return PlayerListsRead(
        **{
            kind: [PlayerEntry(**e) for e in playerlists.read_list(server_dir, kind)]
            for kind in playerlists.FILES
        }
    )


@router.get("/api/servers/{server_id}/playerlists", response_model=PlayerListsRead)
def get_player_lists(
    server_id: str, session: Session = Depends(get_session)
) -> PlayerListsRead:
    return _lists(_server_dir(server_id, session))


@router.post(
    "/api/servers/{server_id}/playerlists/{kind}", response_model=PlayerListsRead
)
def add_to_list(
    server_id: str,
    kind: str,
    payload: PlayerListAddRequest,
    session: Session = Depends(get_session),
) -> PlayerListsRead:
    server_dir = _server_dir(server_id, session)
    if kind not in playerlists.FILES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown list: {kind}")
    player = session.get(Player, mojang.undash_uuid(payload.uuid))
    if player is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Add the player to the registry first"
        )
    playerlists.add(server_dir, kind, player.uuid, player.name)
    return _lists(server_dir)


@router.delete(
    "/api/servers/{server_id}/playerlists/{kind}/{uuid}", response_model=PlayerListsRead
)
def remove_from_list(
    server_id: str,
    kind: str,
    uuid: str,
    session: Session = Depends(get_session),
) -> PlayerListsRead:
    server_dir = _server_dir(server_id, session)
    if kind not in playerlists.FILES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown list: {kind}")
    playerlists.remove(server_dir, kind, uuid)
    return _lists(server_dir)
