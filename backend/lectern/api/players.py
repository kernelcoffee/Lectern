"""Players: a global registry + per-server whitelist/ops/banned management.

The registry (``Player`` table) is the "friends" list — add someone by username
or UUID and Lectern validates them against Mojang. Per-server, you add those
registered players to the whitelist / ops / banned lists. On a **running**
server the change takes effect immediately: through the Server Management
Protocol when the process has a connected client (``minecraft:allowlist/add``,
``operators/add``, ``bans/add`` and their ``remove``), else through a console
command (``whitelist add``, ``op``, ``ban``, …). Either way Minecraft rewrites
its own JSON file, keeping the files the source of truth, and every read comes
from them. When neither lands, or the server is stopped (or starting or
stopping), Lectern edits the JSON files directly, which applies at the next
start. Bans may carry a reason and an expiry: the protocol honours both, the
console ``ban`` only the reason, the file edit stores both.

The online roster comes from the Server Management Protocol when the process
has a connected client (1.21.9+) and from console parsing otherwise; kicks
follow the same order (see ``kick_player``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
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
from ..servers import msmp, playerlists
from ..servers import properties as props
from ..servers.manager import ManagerError, manager
from ..servers.process import ServerProcess

log = logging.getLogger(__name__)

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

# kind → (add, remove) management-protocol methods for a running server.
_MSMP_METHODS = {
    "whitelist": (msmp.M_ALLOWLIST_ADD, msmp.M_ALLOWLIST_REMOVE),
    "ops": (msmp.M_OPERATORS_ADD, msmp.M_OPERATORS_REMOVE),
    "banned": (msmp.M_BANS_ADD, msmp.M_BANS_REMOVE),
}
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


# A ban reason travels as one console line (``ban <name> <reason>``), which
# Minecraft caps at 256 characters; the UI caps its field at the same 200.
BAN_REASON_MAX = 200


def _sanitize_reason(text: str | None) -> str | None:
    """A reason as one console line: newlines and carriage returns become
    spaces, surrounding blanks go, and nothing left means no reason."""
    if text is None:
        return None
    cleaned = text.replace("\r", " ").replace("\n", " ").strip()
    return cleaned or None


def _live(server: Server) -> bool:
    """A change can go through the console only once the server is fully up —
    during starting/stopping we edit the files instead (the booting server
    reads them when it loads its lists)."""
    return (
        server.status == ServerStatus.running.value and manager.is_running(server.id)
    )


def _list_player(entry: object) -> dict | None:
    """The ``player`` component of one protocol list entry: the entry itself
    for the allowlist, its nested ``player`` for operators and bans."""
    if not isinstance(entry, dict):
        return None
    nested = entry.get("player")
    return nested if isinstance(nested, dict) else entry


def _lists_contain(result: list, name: str, uuid: str) -> bool:
    """Whether a protocol list holds the added player, by UUID or by name (an
    offline-mode server lists its own offline UUID for the name)."""
    target = mojang.undash_uuid(uuid)
    wanted = name.lower()
    for entry in result:
        player = _list_player(entry)
        if player is None:
            continue
        if str(player.get("name", "")).lower() == wanted:
            return True
        if mojang.undash_uuid(str(player.get("id", ""))) == target:
            return True
    return False


def _lists_still_hold(result: list, name: str, uuid: str) -> bool:
    """Whether a protocol list still holds the removed player. The files are
    keyed by UUID, so an entry with an id matches by id only: another row
    with the same name (an offline UUID the server added for that name) is a
    different entry and must not make the removal look unconfirmed. The name
    is the match only for an entry that carries no id."""
    target = mojang.undash_uuid(uuid)
    wanted = name.lower()
    for entry in result:
        player = _list_player(entry)
        if player is None:
            continue
        player_id = player.get("id")
        if player_id:
            if mojang.undash_uuid(str(player_id)) == target:
                return True
        elif str(player.get("name", "")).lower() == wanted:
            return True
    return False


def _msmp_add_entry(
    kind: str, name: str, *, reason: str | None, expires: datetime | None, op_level: int
) -> dict:
    """The component to add: a name only, so the server resolves the UUID per
    its own online mode, exactly like the console command."""
    player = {"name": name}
    if kind == "ops":
        return {"player": player, "permissionLevel": op_level, "bypassesPlayerLimit": False}
    if kind == "banned":
        ban: dict = {"player": player, "source": "Lectern"}
        if reason is not None:
            ban["reason"] = reason
        if expires is not None:
            ban["expires"] = expires.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ban
    return player


async def _operator_level(client: msmp.MsmpClient, fallback: int) -> int:
    """The level the console ``op`` would grant on the running server: its
    ``op-permission-level`` as it started with, read through the protocol.
    ``fallback`` (the file's value) when the server cannot say."""
    try:
        level = await client.call(msmp.M_OP_LEVEL, [])
    except msmp.MsmpError as exc:
        log.info("msmp read of the operator level failed (%s), using the file's", exc)
        return fallback
    if isinstance(level, bool) or not isinstance(level, int):
        log.info("msmp operator level %r is not a level, using the file's", level)
        return fallback
    return props.clamp_op_level(level)


async def _apply_msmp(
    proc: ServerProcess | None,
    kind: str,
    name: str,
    uuid: str,
    add: bool,
    *,
    reason: str | None = None,
    expires: datetime | None = None,
    op_level: int = props.DEFAULT_OP_LEVEL,
) -> bool:
    """Apply a list change through the management protocol. The server
    rewrites the list file before it answers; the answer is the resulting
    list, which must (add) or must no longer (remove) hold the player.
    False → the caller uses the console: no client, a failed call, or a reply
    that does not confirm the change (e.g. a name the server cannot resolve).
    A new operator gets the server's ``op-permission-level``, as ``op``
    grants; ``op_level`` (the file's) when the server cannot say."""
    client = proc.msmp if proc is not None else None
    if client is None or not client.connected:
        return False
    add_method, remove_method = _MSMP_METHODS[kind]
    if add:
        if kind == "ops":
            op_level = await _operator_level(client, op_level)
        entry = _msmp_add_entry(kind, name, reason=reason, expires=expires, op_level=op_level)
        method = add_method
    else:
        # Remove by the file's UUID as well: a name alone makes an online-mode
        # server look it up again, which may hit whoever holds the name now.
        entry = {"name": name, "id": mojang.dash_uuid(uuid)}
        method = remove_method
    try:
        result = await client.call(method, [[entry]])
    except msmp.MsmpError as exc:
        log.info("msmp %s of %s failed (%s), using the console", method, name, exc)
        return False
    if isinstance(result, list):
        confirmed = (
            _lists_contain(result, name, uuid)
            if add
            else not _lists_still_hold(result, name, uuid)
        )
        if confirmed:
            return True
    log.info("msmp %s did not confirm %s, using the console", method, name)
    return False


async def _apply_live(
    server: Server,
    server_dir: Path,
    kind: str,
    name: str,
    uuid: str,
    add: bool,
    *,
    reason: str | None = None,
) -> bool:
    """Send the console command for a list change and wait for the server's own
    rewrite of the JSON file to reflect it. False → the caller falls back to
    editing the file directly (command raced a stop, or the server couldn't
    resolve the name). The add check also matches by name: an offline-mode
    server records its own (offline) UUID, not the registry's Mojang one.
    ``reason`` (already one line) follows the ``ban`` command; the console
    has no way to express an expiry."""
    add_cmd, remove_cmd = _LIVE_COMMANDS[kind]
    command = (add_cmd if add else remove_cmd).format(name=name)
    if add and kind == "banned" and reason is not None:
        command = f"{command} {reason}"
    try:
        await manager.send(server.id, command)
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


def _file_op_level(server_dir: Path) -> int:
    """The ``op-permission-level`` in the server's own file, Minecraft's
    default when the file cannot be read (or is symlinked out of the server
    directory): a list change never fails over it."""
    try:
        props.properties_file(server_dir)
        file_props = props.read_properties(server_dir)
    except OSError as exc:
        log.info("server.properties unreadable (%s), operators get level %d",
                 exc, props.DEFAULT_OP_LEVEL)
        return props.DEFAULT_OP_LEVEL
    return props.op_permission_level(file_props)


def _ban_expiry(expires: datetime | None) -> datetime | None:
    """The requested ban expiry in UTC, or 422 when it has no UTC equivalent
    (``9999-12-31T23:59:59-01:00`` lies past the last representable instant)."""
    if expires is None:
        return None
    try:
        return playerlists.as_utc(expires)
    except OverflowError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "expires is out of range"
        ) from None


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
    # A reason and an expiry only mean something on the ban list.
    banned = kind == "banned"
    reason = _sanitize_reason(payload.reason) if banned else None
    if reason is not None and len(reason) > BAN_REASON_MAX:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"reason is longer than {BAN_REASON_MAX} characters",
        )
    expires = _ban_expiry(payload.expires) if banned else None
    if expires is not None and expires <= datetime.now(UTC):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "expires must be in the future"
        )
    player = session.get(Player, mojang.undash_uuid(payload.uuid))
    if player is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Add the player to the registry first"
        )
    # The level ``op`` grants by this file; the protocol asks the server.
    op_level = _file_op_level(server_dir) if kind == "ops" else props.DEFAULT_OP_LEVEL
    applied = False
    if _live(server):
        # The live paths wait on the server: do not pin a pooled connection.
        session.close()
        applied = await _apply_msmp(
            manager.get_process(server.id), kind, player.name, player.uuid, add=True,
            reason=reason, expires=expires, op_level=op_level,
        )
        if not applied:
            applied = await _apply_live(
                server, server_dir, kind, player.name, player.uuid, add=True, reason=reason
            )
    if not applied:
        playerlists.add(
            server_dir, kind, player.uuid, player.name, reason=reason, expires=expires,
            level=op_level,
        )
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
        session.close()  # the live paths wait on the server
        applied = await _apply_msmp(
            manager.get_process(server.id), kind, entry["name"], target, add=False
        )
        if not applied:
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
    """Who's on right now — from the management protocol when connected, the
    console otherwise; includes artificial players (Carpet bots etc., flagged
    ``bot`` from their console login line). Empty when not running."""
    if session.get(Server, server_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    proc = manager.get_process(server_id)
    return [] if proc is None else _roster_read(proc)


async def _kick_msmp(proc, name: str) -> bool:
    """Kick through the management protocol; False → use the console."""
    client = proc.msmp
    if client is None or not client.connected:
        return False
    try:
        kicked = await client.call(msmp.M_KICK, [[{"name": name}]])
    except msmp.MsmpError as exc:
        log.info("msmp kick of %s failed (%s), using the console", name, exc)
        return False
    # The reply lists the players actually kicked; anything else (an empty
    # list when the server resolved nobody) means the console must do it.
    wanted = name.lower()
    if isinstance(kicked, list) and any(
        isinstance(p, dict) and str(p.get("name", "")).lower() == wanted for p in kicked
    ):
        return True
    log.info("msmp kick of %s kicked nobody, using the console", name)
    return False


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
    """Remove an online player. Carpet-style bots have no real connection to
    sever, so they get Carpet's ``player … kill``. Real players go through the
    management protocol's ``minecraft:players/kick`` when the client is
    connected and no reason is given, else the console ``kick`` (which carries
    the reason). The name must match a roster entry, which also keeps
    arbitrary text out of the console command: roster keys from either
    source match the console's name pattern (no selectors like ``@a``)."""
    if session.get(Server, server_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    proc = manager.get_process(server_id)
    if proc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Server is not running")
    player = proc.roster.players.get(name)
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} is not online")
    reason = _sanitize_reason(payload.reason) if payload else None
    if player.bot:
        command: str | None = f"player {name} kill"
    elif reason:
        command = f"kick {name} {reason}"
    else:
        command = None if await _kick_msmp(proc, name) else f"kick {name}"
    if command is not None:
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
