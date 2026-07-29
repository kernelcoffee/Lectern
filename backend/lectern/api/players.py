"""Players: a global registry + per-server whitelist/ops/banned management.

The registry (``Player`` table) is the "friends" list — add someone by username
or UUID and Lectern validates them against Mojang. Per-server, you add those
registered players to the whitelist / ops / banned lists. On a **running**
server the change goes through a console command (``whitelist add``, ``op``,
``ban``, …) so it takes effect immediately — Minecraft then rewrites its own
JSON file, keeping the files the source of truth. On a stopped server (or if
the command doesn't land) Lectern edits the JSON files directly, which applies
at the next start.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlmodel import Session, select

from ..db import get_session
from ..models import (
    KickRequest,
    OnlinePlayerRead,
    Player,
    PlayerAddRequest,
    PlayerEntry,
    PlayerListAddRequest,
    PlayerListsRead,
    PlayerRead,
    Server,
    ServerStatus,
)
from ..providers import avatars, mojang
from ..servers import playerlists
from ..servers.manager import ManagerError, manager

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

# kind → (add, remove) console commands for a running server.
_LIVE_COMMANDS = {
    "whitelist": ("whitelist add {name}", "whitelist remove {name}"),
    "ops": ("op {name}", "deop {name}"),
    "banned": ("ban {name}", "pardon {name}"),
}
# Minecraft saves the list file in the same tick it executes the command, so
# the confirmation poll normally succeeds on the first try; the timeout only
# hits when the server couldn't apply it (e.g. an unresolvable name).
_LIVE_APPLY_TIMEOUT = 2.0
_LIVE_POLL = 0.1


def _get_server(server_id: str, session: Session) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if not server.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not installed yet")
    return server


def _live(server: Server) -> bool:
    """A change can go through the console only once the server is fully up —
    during starting/stopping we edit the files instead (the booting server
    reads them when it loads its lists)."""
    return (
        server.status == ServerStatus.running.value and manager.is_running(server.id)
    )


async def _apply_live(
    server: Server, server_dir: Path, kind: str, name: str, uuid: str, add: bool
) -> bool:
    """Send the console command for a list change and wait for the server's own
    rewrite of the JSON file to reflect it. False → the caller falls back to
    editing the file directly (command raced a stop, or the server couldn't
    resolve the name). The add check also matches by name: an offline-mode
    server records its own (offline) UUID, not the registry's Mojang one."""
    add_cmd, remove_cmd = _LIVE_COMMANDS[kind]
    try:
        await manager.send(server.id, (add_cmd if add else remove_cmd).format(name=name))
    except ManagerError:
        return False
    target = mojang.undash_uuid(uuid)
    for _ in range(max(1, int(_LIVE_APPLY_TIMEOUT / _LIVE_POLL))):
        await asyncio.sleep(_LIVE_POLL)
        entries = playerlists.read_list(server_dir, kind)
        if add:
            if any(
                e["uuid"] == target or e["name"].lower() == name.lower()
                for e in entries
            ):
                return True
        elif not any(e["uuid"] == target for e in entries):
            return True
    return False


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
    return _lists(Path(_get_server(server_id, session).path))


@router.post(
    "/api/servers/{server_id}/playerlists/{kind}", response_model=PlayerListsRead
)
async def add_to_list(
    server_id: str,
    kind: str,
    payload: PlayerListAddRequest,
    session: Session = Depends(get_session),
) -> PlayerListsRead:
    server = _get_server(server_id, session)
    server_dir = Path(server.path)
    if kind not in playerlists.FILES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown list: {kind}")
    player = session.get(Player, mojang.undash_uuid(payload.uuid))
    if player is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Add the player to the registry first"
        )
    applied = False
    if _live(server):
        applied = await _apply_live(
            server, server_dir, kind, player.name, player.uuid, add=True
        )
    if not applied:
        playerlists.add(server_dir, kind, player.uuid, player.name)
    return _lists(server_dir)


@router.delete(
    "/api/servers/{server_id}/playerlists/{kind}/{uuid}", response_model=PlayerListsRead
)
async def remove_from_list(
    server_id: str,
    kind: str,
    uuid: str,
    session: Session = Depends(get_session),
) -> PlayerListsRead:
    server = _get_server(server_id, session)
    server_dir = Path(server.path)
    if kind not in playerlists.FILES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown list: {kind}")
    target = mojang.undash_uuid(uuid)
    # The command needs the name the server knows this UUID by — take it from
    # the file entry, not the registry (it survives registry deletions/renames).
    entry = next(
        (e for e in playerlists.read_list(server_dir, kind) if e["uuid"] == target),
        None,
    )
    applied = False
    if entry is not None and entry["name"] and _live(server):
        applied = await _apply_live(
            server, server_dir, kind, entry["name"], uuid, add=False
        )
    if not applied:
        playerlists.remove(server_dir, kind, uuid)
    return _lists(server_dir)


# --- online roster ----------------------------------------------------------


def _roster_read(proc) -> list[OnlinePlayerRead]:
    return [OnlinePlayerRead(**vars(p)) for p in proc.roster.online()]


@router.get(
    "/api/servers/{server_id}/players/online",
    response_model=list[OnlinePlayerRead],
)
def online_players(
    server_id: str, session: Session = Depends(get_session)
) -> list[OnlinePlayerRead]:
    """Who's on right now — parsed from the console, so it includes artificial
    players (Carpet bots etc., flagged ``bot``). Empty when not running."""
    if session.get(Server, server_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    proc = manager.get_process(server_id)
    return [] if proc is None else _roster_read(proc)


@router.post(
    "/api/servers/{server_id}/players/online/{name}/kick",
    response_model=list[OnlinePlayerRead],
)
async def kick_player(
    server_id: str,
    name: str,
    payload: KickRequest | None = None,
    session: Session = Depends(get_session),
) -> list[OnlinePlayerRead]:
    """Remove an online player. Real players get ``kick``; Carpet-style bots
    have no real connection to sever, so they need Carpet's ``player … kill``
    instead. The name must match a roster entry, which also keeps arbitrary
    text out of the console command."""
    if session.get(Server, server_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    proc = manager.get_process(server_id)
    if proc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not running")
    player = proc.roster.players.get(name)
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} is not online")
    if player.bot:
        command = f"player {name} kill"
    else:
        command = f"kick {name}"
        reason = (payload.reason or "").replace("\n", " ").strip() if payload else ""
        if reason:
            command += f" {reason}"
    try:
        await manager.send(server_id, command)
    except ManagerError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    # The roster updates when the server logs the departure; wait briefly so
    # the response already reflects the kick.
    for _ in range(max(1, int(_LIVE_APPLY_TIMEOUT / _LIVE_POLL))):
        await asyncio.sleep(_LIVE_POLL)
        if name not in proc.roster.players:
            break
    return _roster_read(proc)
