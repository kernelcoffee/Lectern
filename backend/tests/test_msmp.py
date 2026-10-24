"""Server Management Protocol: version gate, enablement, the client against an
in-process fake management server, and how ServerProcess merges it with the
console (roster, running/stopping, fallback)."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session
from websockets.asyncio.server import serve

from lectern.models import Server
from lectern.servers import msmp
from lectern.servers.msmp import (
    M_ALLOWLIST_ADD,
    M_ALLOWLIST_REMOVE,
    M_AUTOSAVE,
    M_BANS_ADD,
    M_BANS_REMOVE,
    M_KICK,
    M_OP_LEVEL,
    M_OPERATORS_ADD,
    M_OPERATORS_REMOVE,
    M_PLAYERS,
    M_SAVE,
    N_JOINED,
    N_LEFT,
    N_STARTED,
    N_STOPPING,
    MsmpClient,
    MsmpError,
    MsmpTimeout,
)
from lectern.servers.process import ServerProcess
from lectern.servers.properties import (
    LIVE_SETTINGS,
    MANAGED_KEYS,
    read_properties,
    write_properties,
)
from lectern.servers.roster import Roster
from lectern.ws import ConsoleHub

SECRET = "a" * 40
PREFIX = "[12:00:00] [Server thread/INFO]: "
AUTH = "[12:00:00] [User Authenticator #1/INFO]: "
NOTCH = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
NOTCH_UNDASHED = NOTCH.replace("-", "")
OFFLINE = "5b6b3b8e-0000-3000-8000-000000000001"
SILENT = "test:never-answers"
LATE = "test:answers-late"
N_SAVING = "minecraft:notification/server/saving"
N_SAVED = "minecraft:notification/server/saved"
SETTING_PREFIX = "minecraft:serversettings/"
# serversettings name -> server.properties key (the inverse of LIVE_SETTINGS).
SETTING_KEYS = {name: key for key, name in LIVE_SETTINGS.items()}
# Protocol list -> (file, add method, remove method, notification stem).
LISTS = {
    "allowlist": ("whitelist.json", M_ALLOWLIST_ADD, M_ALLOWLIST_REMOVE),
    "operators": ("ops.json", M_OPERATORS_ADD, M_OPERATORS_REMOVE),
    "bans": ("banned-players.json", M_BANS_ADD, M_BANS_REMOVE),
}


def _resolve(player: dict) -> dict:
    """What the server does with a name-only player: give it a UUID (here a
    deterministic stand-in for the Mojang / offline lookup)."""
    name = player["name"]
    return {"id": player.get("id") or str(uuid.uuid5(uuid.NAMESPACE_URL, name)), "name": name}


class FakeMsmp:
    """A minimal MSMP endpoint: Bearer check, rpc.discover, players, kick,
    server/status, pushed notifications; the allowlist / operators / bans
    mutations, server/save, serversettings get/set, autosave and the
    operator level (``op_level``, the server's ``op-permission-level``).

    With ``server_dir`` set it behaves like the server on disk: list changes
    rewrite whitelist.json / ops.json / banned-players.json in Minecraft's
    shapes before replying, and a settings set rewrites server.properties from
    its in-memory copy (``props``, loaded from the file at the first set, or
    set by a test to model the server's startup snapshot). Methods named in
    ``fail`` answer -32603; a method in ``delays`` is handled that many seconds
    late, holding up the calls behind it like the server's main thread."""

    def __init__(self, secret: str = SECRET) -> None:
        self.secret = secret
        self.port = 0
        self.calls: list[tuple[str, object]] = []
        self.players: list[dict] = []
        self.started = False
        self.rejected = 0
        self.lists: dict[str, list[dict]] = {name: [] for name in LISTS}
        self.settings: dict[str, object] = {}
        self.saved = 0
        self.autosave = True
        self.op_level = 4
        self.server_dir: Path | None = None
        self.props: dict[str, str] | None = None
        self.fail: set[str] = set()
        self.answers: dict[str, object] = {}
        self.delays: dict[str, float] = {}
        self._conns: set = set()
        self._server = None

    async def start(self, port: int = 0) -> FakeMsmp:
        self._server = await serve(
            self._handler, "127.0.0.1", port, process_request=self._auth
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            await server.wait_closed()

    def _auth(self, connection, request):
        if request.headers.get("Authorization") != f"Bearer {self.secret}":
            self.rejected += 1
            return connection.respond(401, "unauthorized")
        return None

    async def push(self, method: str, params: list) -> None:
        message = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        for ws in list(self._conns):
            await ws.send(message)

    async def _handler(self, ws) -> None:
        self._conns.add(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                method, params = msg["method"], msg.get("params")
                self.calls.append((method, params))
                await asyncio.sleep(self.delays.get(method, 0))
                if not isinstance(params, list):
                    await self._reply(ws, msg, error=(-32602, "Not a json array"))
                elif method == "rpc.discover":
                    await self._reply(ws, msg, result={
                        "info": {"title": "Minecraft Server JSON-RPC", "version": "3.1.0"},
                        "methods": [
                            {"name": m} for m in (
                                M_PLAYERS, M_KICK, M_SAVE, M_AUTOSAVE, M_OP_LEVEL,
                                *(m for _f, *pair in LISTS.values() for m in pair),
                                *(f"{SETTING_PREFIX}{n}/set" for n in SETTING_KEYS),
                                *(f"{SETTING_PREFIX}{n}" for n in SETTING_KEYS),
                            )
                        ],
                    })
                elif method in self.fail:
                    await self._reply(ws, msg, error=(-32603, "boom"))
                elif method in self.answers:
                    await self._reply(ws, msg, result=self.answers[method])
                elif await self._mutate(ws, msg, method, params):
                    continue
                elif method == M_PLAYERS:
                    await self._reply(ws, msg, result=list(self.players))
                elif method == "minecraft:server/status":
                    await self._reply(ws, msg, result={
                        "started": self.started,
                        "version": {"name": "26.3", "protocol": 775},
                    })
                elif method == M_KICK:
                    names = {p["name"] for p in params[0]}
                    gone = [p for p in self.players if p["name"] in names]
                    self.players = [p for p in self.players if p["name"] not in names]
                    await self._reply(ws, msg, result=gone)
                    for player in gone:
                        await self.push(N_LEFT, [player])
                elif method == SILENT:
                    continue
                elif method == LATE:
                    await asyncio.sleep(0.3)
                    await self._reply(ws, msg, result="late")
                else:
                    await self._reply(ws, msg, error=(-32601, "Method not found"))
        finally:
            self._conns.discard(ws)

    async def _mutate(self, ws, msg, method: str, params: list) -> bool:
        """Lists, save, autosave and settings; False for any other method."""
        for list_name, (_file, add_method, remove_method) in LISTS.items():
            if method == add_method:
                await self._list_add(ws, msg, list_name, params[0])
                return True
            if method == remove_method:
                await self._list_remove(ws, msg, list_name, params[0])
                return True
        if method == M_SAVE:
            await self.push(N_SAVING, [])
            self.saved += 1
            await self._reply(ws, msg, result=True)
            await self.push(N_SAVED, [])
            return True
        if method == M_AUTOSAVE:
            await self._reply(ws, msg, result=self.autosave)
            return True
        if method == M_OP_LEVEL:
            await self._reply(ws, msg, result=self.op_level)
            return True
        name = method.removeprefix(SETTING_PREFIX).removesuffix("/set")
        if method.startswith(SETTING_PREFIX) and method.endswith("/set") and name in SETTING_KEYS:
            value = params[0]
            self.settings[name] = value
            self._write_setting(SETTING_KEYS[name], value)
            await self._reply(ws, msg, result=value)
            return True
        if name in SETTING_KEYS:  # the get: no params, the current value
            await self._reply(ws, msg, result=self.settings.get(name))
            return True
        return False

    @staticmethod
    def _player_of(entry: dict) -> dict:
        return entry.get("player", entry)

    async def _list_add(self, ws, msg, list_name: str, entries: list[dict]) -> None:
        current = self.lists[list_name]
        for entry in entries:
            player = _resolve(self._player_of(entry))
            if any(self._player_of(e)["id"] == player["id"] for e in current):
                continue
            if list_name == "allowlist":
                added: dict = player
            elif list_name == "operators":
                added = {**entry, "player": player}
            else:
                added = {
                    "player": player,
                    "reason": entry.get("reason", "Banned by an operator."),
                    "source": entry.get("source", "Management server"),
                    **({"expires": entry["expires"]} if "expires" in entry else {}),
                }
            current.append(added)
            self._write_list(list_name)
            await self.push(f"minecraft:notification/{list_name}/added", [added])
        await self._reply(ws, msg, result=list(current))

    async def _list_remove(self, ws, msg, list_name: str, entries: list[dict]) -> None:
        for entry in entries:
            gone = [
                e for e in self.lists[list_name]
                if self._player_of(e)["id"] == entry.get("id")
                or self._player_of(e)["name"].lower() == entry["name"].lower()
            ]
            self.lists[list_name] = [e for e in self.lists[list_name] if e not in gone]
            self._write_list(list_name)
            for removed in gone:
                await self.push(f"minecraft:notification/{list_name}/removed", [removed])
        await self._reply(ws, msg, result=list(self.lists[list_name]))

    def _write_list(self, list_name: str) -> None:
        if self.server_dir is None:
            return
        rows = []
        for entry in self.lists[list_name]:
            player = self._player_of(entry)
            row: dict = {"uuid": player["id"], "name": player["name"]}
            if list_name == "operators":
                row["level"] = entry["permissionLevel"]
                row["bypassesPlayerLimit"] = entry["bypassesPlayerLimit"]
            elif list_name == "bans":
                expires = entry.get("expires")
                row.update(
                    created="2026-10-24 12:00:00 +0000",
                    source=entry["source"],
                    expires=(
                        datetime.fromisoformat(expires).strftime("%Y-%m-%d %H:%M:%S +0000")
                        if expires else "forever"
                    ),
                    reason=entry["reason"],
                )
            rows.append(row)
        (self.server_dir / LISTS[list_name][0]).write_text(json.dumps(rows, indent=2))

    def _write_setting(self, key: str, value: object) -> None:
        if self.server_dir is None:
            return
        if self.props is None:
            self.props = read_properties(self.server_dir)
        if isinstance(value, bool):
            self.props[key] = "true" if value else "false"
        else:
            self.props[key] = str(value)
        write_properties(self.server_dir, self.props)

    @staticmethod
    async def _reply(ws, msg, result=None, error=None) -> None:
        body: dict = {"jsonrpc": "2.0", "id": msg["id"]}
        if error is not None:
            body["error"] = {"code": error[0], "message": error[1]}
        else:
            body["result"] = result
        await ws.send(json.dumps(body))


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(msmp, "_BACKOFF_START", 0.05)
    monkeypatch.setattr(msmp, "_BACKOFF_MAX", 0.2)


@pytest.fixture
async def fake_msmp():
    server = await FakeMsmp().start()
    yield server
    await server.stop()


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _connected_client(fake: FakeMsmp) -> MsmpClient:
    client = MsmpClient("s", fake.port, fake.secret)
    await client.start()
    assert await _wait_for(lambda: client.connected)
    return client


class _States:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, server_id: str, status: str, detail: str) -> None:
        self.seen.append(status)


async def _process_with(fake: FakeMsmp, tmp_path: Path) -> tuple[ServerProcess, _States]:
    states = _States()
    client = MsmpClient("s", fake.port, fake.secret)
    proc = ServerProcess("s", [], str(tmp_path), ConsoleHub(), states, msmp=client)
    await proc._start_msmp()
    assert await _wait_for(lambda: client.connected)
    return proc, states


# --- version gate ----------------------------------------------------------------


def test_supports_msmp_version_gate():
    cases = {
        "1.21.8": False, "1.21.9": True, "1.21.11": True, "26.1": True, "26.3": True,
        "1.21": False, "25w35a": False, "": False, "1.20.1": False,
    }
    assert {v: msmp.supports_msmp(v) for v in cases} == cases
    assert msmp.parse_mc_version("26.1") > msmp.parse_mc_version("1.21.11")


# --- enablement --------------------------------------------------------------------


def test_enable_writes_exactly_managed_keys(tmp_path):
    write_properties(tmp_path, {"motd": "hi", "server-port": "25565"})
    endpoint = msmp.enable(tmp_path, game_port=25565, reserved=set())
    props = read_properties(tmp_path)
    assert set(props) == {
        "motd", "server-port", "management-server-enabled", "management-server-host",
        "management-server-port", "management-server-tls-enabled",
        "management-server-secret",
    }
    assert props["motd"] == "hi" and props["server-port"] == "25565"
    assert props["management-server-enabled"] == "true"
    assert props["management-server-host"] == "127.0.0.1"
    assert props["management-server-tls-enabled"] == "false"
    secret = props["management-server-secret"]
    assert len(secret) == 40 and secret.isalnum() and secret == endpoint.secret
    assert props["management-server-port"] == str(endpoint.port)
    assert endpoint.port != 25565


def test_enable_keeps_existing_secret_and_port(tmp_path):
    port = _free_port()
    write_properties(tmp_path, {
        "management-server-secret": SECRET, "management-server-port": str(port),
    })
    endpoint = msmp.enable(tmp_path, game_port=25565, reserved=set())
    assert endpoint == msmp.Endpoint(port=port, secret=SECRET)
    assert read_properties(tmp_path)["management-server-secret"] == SECRET


def test_enable_replaces_reserved_port_and_empty_secret(tmp_path):
    write_properties(tmp_path, {
        "management-server-secret": "", "management-server-port": "25565",
    })
    endpoint = msmp.enable(tmp_path, game_port=25565, reserved=set())
    assert endpoint.port != 25565
    assert len(endpoint.secret) == 40


def test_pick_management_port_skips_reserved_and_bound(monkeypatch):
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        busy = held.getsockname()[1]
        free = _free_port()
        # Model the range as [busy-1 (reserved), busy (bound), free].
        monkeypatch.setattr(msmp, "MSMP_PORT_BASE", busy - 1)
        monkeypatch.setattr(msmp, "_PORT_SPAN", 2)
        assert msmp._port_free(busy) is False
        with pytest.raises(RuntimeError, match="no free management port"):
            msmp.pick_management_port(None, {busy - 1})
        assert msmp.pick_management_port(free, {busy - 1}) == free
        with pytest.raises(RuntimeError):
            msmp.pick_management_port(busy, {busy - 1})


def test_pick_management_port_scans_from_base():
    port = msmp.pick_management_port(None, set())
    assert msmp.MSMP_PORT_BASE <= port < msmp.MSMP_PORT_BASE + msmp._PORT_SPAN
    assert msmp.pick_management_port(None, {port}) != port
    assert msmp.pick_management_port(0, set()) >= msmp.MSMP_PORT_BASE


# --- client ------------------------------------------------------------------------


async def test_client_connects_when_port_opens_late():
    port = _free_port()
    client = MsmpClient("s", port, SECRET)
    await client.start()
    await asyncio.sleep(0.3)
    assert client.connected is False
    fake = await FakeMsmp().start(port)
    try:
        assert await _wait_for(lambda: client.connected)
        assert client.schema_version == "3.1.0"
        assert fake.calls[0] == ("rpc.discover", [])
    finally:
        await client.close()
        await fake.stop()


async def test_client_wrong_secret_is_rejected_and_keeps_retrying(fake_msmp):
    client = MsmpClient("s", fake_msmp.port, "b" * 40)
    await client.start()
    assert await _wait_for(lambda: fake_msmp.rejected >= 2)
    assert client.connected is False
    assert client.schema_version is None
    await client.close()
    await client.close()  # idempotent
    rejected = fake_msmp.rejected
    await asyncio.sleep(0.3)
    assert fake_msmp.rejected == rejected  # closed: no more attempts


async def test_client_never_logs_the_secret(fake_msmp, caplog):
    caplog.set_level(logging.DEBUG)
    client = await _connected_client(fake_msmp)
    await client.close()
    client = MsmpClient("s", fake_msmp.port, "c" * 40)
    await client.start()
    assert await _wait_for(lambda: fake_msmp.rejected >= 1)
    await client.close()
    client_records = [r for r in caplog.records if not r.name.startswith("websockets.server")]
    assert any("connected on port" in r.getMessage() for r in client_records)
    assert any("HTTP 401" in r.getMessage() for r in client_records)
    assert not any(
        SECRET in r.getMessage() or "c" * 40 in r.getMessage() for r in client_records
    )


async def test_client_call_errors(fake_msmp):
    client = MsmpClient("s", fake_msmp.port, SECRET)
    with pytest.raises(MsmpError, match="not connected"):
        await client.call(M_PLAYERS)
    await client.start()
    assert await _wait_for(lambda: client.connected)
    try:
        with pytest.raises(MsmpError) as missing:
            await client.call("minecraft:players/")
        assert missing.value.code == -32601
        with pytest.raises(MsmpTimeout, match="timed out"):
            await client.call(SILENT, timeout=0.2)
        assert client._pending == {}
        assert await client.call(M_PLAYERS) == []
    finally:
        await client.close()
    with pytest.raises(MsmpError, match="not connected"):
        await client.call(M_PLAYERS)


async def test_client_ignores_late_reply_and_closed_socket(fake_msmp):
    client = await _connected_client(fake_msmp)
    try:
        with pytest.raises(MsmpError, match="timed out"):
            await client.call(LATE, timeout=0.1)
        await asyncio.sleep(0.3)  # the late reply lands on no pending call
        assert client.connected and await client.call(M_PLAYERS) == []
        stale = client._ws
        await fake_msmp.stop()
        assert await _wait_for(lambda: not client.connected)
        with pytest.raises(MsmpError, match="disconnected"):
            await client._request(stale, M_PLAYERS, [], 1.0)
    finally:
        await client.close()


async def test_client_close_fails_inflight_call(fake_msmp):
    client = await _connected_client(fake_msmp)
    pending = asyncio.create_task(client.call(SILENT, timeout=5))
    await _wait_for(lambda: client._pending)
    await client.close()
    with pytest.raises(MsmpError, match="closed|disconnected"):
        await pending


async def test_client_reconnects_after_server_restart(fake_msmp):
    client = await _connected_client(fake_msmp)
    try:
        port = fake_msmp.port
        await fake_msmp.stop()
        assert await _wait_for(lambda: not client.connected)
        replacement = await FakeMsmp().start(port)
        try:
            assert await _wait_for(lambda: client.connected)
            assert client.schema_version == "3.1.0"
        finally:
            await replacement.stop()
    finally:
        await client.close()


async def test_client_ignores_malformed_messages_and_discover(fake_msmp, monkeypatch):
    async def weird_reply(ws, msg, result=None, error=None):
        if msg["method"] == "rpc.discover":
            await ws.send(json.dumps([1, 2]))  # not an object: ignored
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": "x"}))
            return
        await FakeMsmp._reply(ws, msg, result, error)

    monkeypatch.setattr(fake_msmp, "_reply", weird_reply)
    client = await _connected_client(fake_msmp)
    try:
        assert client.schema_version is None
        assert await client.call(M_PLAYERS) == []
    finally:
        await client.close()


async def test_client_survives_failing_callbacks(fake_msmp):
    client = MsmpClient("s", fake_msmp.port, SECRET)

    async def failing_connect():
        raise MsmpError("boom")

    seen: list[str] = []

    async def failing_notification(method, params):
        seen.append(method)
        raise MsmpError("boom")

    client.on_connect = failing_connect
    client.on_notification = failing_notification
    await client.start()
    assert await _wait_for(lambda: client.connected)
    try:
        await fake_msmp.push(N_STARTED, [])
        await fake_msmp.push(N_STARTED, "not a list")
        assert await _wait_for(lambda: len(seen) == 2)
        assert client.connected is True
    finally:
        await client.close()


async def test_client_reconnects_after_garbage_frame(fake_msmp):
    client = await _connected_client(fake_msmp)
    try:
        for ws in list(fake_msmp._conns):
            await ws.send("{not json")
        assert await _wait_for(lambda: len(fake_msmp.calls) >= 2)  # rediscovered
        assert await _wait_for(lambda: client.connected)
        assert [m for m, _ in fake_msmp.calls].count("rpc.discover") == 2
    finally:
        await client.close()


async def test_client_discover_lost_when_socket_drops(fake_msmp, monkeypatch):
    async def drop(ws, msg, result=None, error=None):
        await ws.close()

    monkeypatch.setattr(fake_msmp, "_reply", drop)
    client = MsmpClient("s", fake_msmp.port, SECRET)
    await client.start()
    assert await _wait_for(lambda: len(fake_msmp.calls) >= 2)
    assert client.schema_version is None
    await client.close()


# --- process: roster ----------------------------------------------------------------


async def test_process_seeds_and_updates_roster(fake_msmp, tmp_path):
    fake_msmp.players = [{"id": NOTCH, "name": "Notch"}]
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        assert await _wait_for(lambda: "Notch" in proc.roster.players)
        assert proc.roster.players["Notch"].uuid == NOTCH_UNDASHED
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Alex"}])
        assert await _wait_for(lambda: "Alex" in proc.roster.players)
        assert proc.roster.players["Alex"].uuid == OFFLINE.replace("-", "")
        await fake_msmp.push(N_LEFT, [{"id": OFFLINE, "name": "Alex"}])
        assert await _wait_for(lambda: "Alex" not in proc.roster.players)
        # Malformed notifications are ignored.
        await fake_msmp.push(N_JOINED, [])
        await fake_msmp.push(N_JOINED, [{"id": 5}])
        await fake_msmp.push("minecraft:notification/server/status", [{}])
        await fake_msmp.push(N_LEFT, [{"name": "Notch"}])
        assert await _wait_for(lambda: proc.roster.players == {})
    finally:
        await proc.msmp.close()


async def test_join_during_snapshot_is_replayed(fake_msmp, tmp_path, monkeypatch):
    original = FakeMsmp._reply

    async def reply_after_join(ws, msg, result=None, error=None):
        if msg["method"] == M_PLAYERS:
            # The server sends a join notification before the list reply.
            await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Alex"}])
        await original(ws, msg, result, error)

    monkeypatch.setattr(fake_msmp, "_reply", reply_after_join)
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        assert await _wait_for(lambda: proc._roster_backlog is None)
        assert "Alex" in proc.roster.players
    finally:
        await proc.msmp.close()


async def test_console_and_msmp_agree_on_one_entry(fake_msmp, tmp_path):
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        await fake_msmp.push(N_JOINED, [{"id": NOTCH, "name": "Notch"}])
        assert await _wait_for(lambda: "Notch" in proc.roster.players)
        joined_at = proc.roster.players["Notch"].joined_at
        await proc._handle_line(AUTH + f"UUID of player Notch is {NOTCH}")
        await proc._handle_line(
            PREFIX + "Notch [/192.0.2.7:52717]: logged in with entity id 1 at (0, 64, 0)"
        )
        await proc._handle_line(PREFIX + "System chat: Notch joined the game")
        [entry] = proc.roster.online()
        assert (entry.uuid, entry.bot, entry.joined_at) == (NOTCH_UNDASHED, False, joined_at)
        await proc._handle_line(PREFIX + "System chat: Notch left the game")
        assert proc.roster.online() == []
        # The protocol's own departure for the same player is then a no-op.
        await fake_msmp.push(N_LEFT, [{"id": NOTCH, "name": "Notch"}])
        await asyncio.sleep(0.05)
        assert proc.roster.online() == []
    finally:
        await proc.msmp.close()


async def test_bot_flag_from_console_login_survives_msmp_join(fake_msmp, tmp_path):
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        # Console first, then the protocol.
        await proc._handle_line(
            PREFIX + "Steve [local]: logged in with entity id 2 at ([minecraft:overworld]0, 64, 0)"
        )
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Steve"}])
        assert await _wait_for(lambda: "Steve" in proc.roster.players)
        assert proc.roster.players["Steve"].bot is True
        assert proc.roster.players["Steve"].uuid == OFFLINE.replace("-", "")
        await proc._handle_line(PREFIX + "System chat: Steve joined the game")
        assert proc.roster.players["Steve"].bot is True

        # Protocol first, then the console.
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Alex"}])
        assert await _wait_for(lambda: "Alex" in proc.roster.players)
        assert proc.roster.players["Alex"].bot is False
        await proc._handle_line(
            PREFIX + "Alex [local]: logged in with entity id 3 at ([minecraft:overworld]0, 64, 0)"
        )
        assert proc.roster.players["Alex"].bot is True
        await proc._handle_line(PREFIX + "System chat: Alex joined the game")
        assert proc.roster.players["Alex"].bot is True
        assert len(proc.roster.online()) == 2
    finally:
        await proc.msmp.close()


async def test_disconnect_keeps_roster_and_console_fallback(fake_msmp, tmp_path):
    fake_msmp.players = [{"id": NOTCH, "name": "Notch"}]
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        assert await _wait_for(lambda: "Notch" in proc.roster.players)
        await fake_msmp.stop()
        assert await _wait_for(lambda: not proc.msmp.connected)
        assert [p.name for p in proc.roster.online()] == ["Notch"]
        await proc._handle_line(PREFIX + "System chat: Alex joined the game")
        assert [p.name for p in proc.roster.online()] == ["Notch", "Alex"]
    finally:
        await proc.msmp.close()


async def test_snapshot_failure_keeps_console_roster(fake_msmp, tmp_path, monkeypatch):
    original = FakeMsmp._reply

    async def no_list(ws, msg, result=None, error=None):
        if msg["method"] == M_PLAYERS:
            error = (-32603, "Internal error")
        await original(ws, msg, result, error)

    monkeypatch.setattr(fake_msmp, "_reply", no_list)
    states = _States()
    client = MsmpClient("s", fake_msmp.port, SECRET)
    proc = ServerProcess("s", [], str(tmp_path), ConsoleHub(), states, msmp=client)
    await proc._handle_line(PREFIX + "System chat: Alex joined the game")
    await proc._start_msmp()
    try:
        assert await _wait_for(lambda: client.connected and proc._roster_backlog is None)
        assert [p.name for p in proc.roster.online()] == ["Alex"]
    finally:
        await client.close()


# --- process: status ------------------------------------------------------------------


async def test_server_started_reports_running_once(fake_msmp, tmp_path):
    proc, states = await _process_with(fake_msmp, tmp_path)
    try:
        await fake_msmp.push(N_STARTED, [])
        assert await _wait_for(lambda: states.seen == ["running"])
        await proc._handle_line(PREFIX + 'Done (1.0s)! For help, type "help"')
        await fake_msmp.push(N_STARTED, [])
        await asyncio.sleep(0.05)
        assert states.seen == ["running"]
    finally:
        await proc.msmp.close()


async def test_status_started_on_connect_reports_running_once(fake_msmp, tmp_path):
    fake_msmp.started = True  # a 1.21.9–26.1 server: port opens after startup
    proc, states = await _process_with(fake_msmp, tmp_path)
    try:
        assert await _wait_for(lambda: states.seen == ["running"])
        await proc._handle_line(PREFIX + 'Done (1.0s)! For help, type "help"')
        assert states.seen == ["running"]
    finally:
        await proc.msmp.close()


async def test_marker_reports_running_without_msmp(tmp_path):
    states = _States()
    proc = ServerProcess("s", [], str(tmp_path), ConsoleHub(), states)
    await proc._start_msmp()  # no client: no-op
    await proc._handle_line(PREFIX + 'Done (1.0s)! For help, type "help"')
    await proc._handle_line(PREFIX + "<Notch> Done (with this)")
    assert states.seen == ["running"]


async def test_server_stopping_notification_reports_stopping(fake_msmp, tmp_path):
    proc, states = await _process_with(fake_msmp, tmp_path)
    try:
        await fake_msmp.push(N_STOPPING, [])
        assert await _wait_for(lambda: states.seen == ["stopping"])
        assert proc._stopping is False  # exit classification stays exit-code based
        proc._stopping = True  # a Lectern-initiated stop already reported it
        await fake_msmp.push(N_STOPPING, [])
        await fake_msmp.push(N_STARTED, [])
        await asyncio.sleep(0.05)
        assert states.seen == ["stopping"]
    finally:
        await proc.msmp.close()


async def test_status_failure_on_connect_is_logged_not_raised(fake_msmp, tmp_path, monkeypatch):
    original = FakeMsmp._reply

    async def no_status(ws, msg, result=None, error=None):
        if msg["method"] == "minecraft:server/status":
            error = (-32601, "Method not found")
        await original(ws, msg, result, error)

    monkeypatch.setattr(fake_msmp, "_reply", no_status)
    proc, states = await _process_with(fake_msmp, tmp_path)
    try:
        assert await _wait_for(lambda: ("minecraft:server/status", []) in fake_msmp.calls)
        await asyncio.sleep(0.05)
        assert states.seen == [] and proc.msmp.connected
    finally:
        await proc.msmp.close()


async def test_pump_closes_client_on_exit(fake_msmp, tmp_path):
    import sys

    states = _States()
    client = MsmpClient("s", fake_msmp.port, SECRET)
    proc = ServerProcess(
        "s", [sys.executable, "-c", "import select; select.select([], [], [], 0.5)"], str(tmp_path),
        ConsoleHub(), states, msmp=client,
    )
    await proc.start()
    assert await _wait_for(lambda: client.connected)
    assert await _wait_for(lambda: states.seen[-1:] == ["stopped"])
    assert client.connected is False and client._task is None


async def test_stop_returns_after_final_state_with_client(fake_msmp, tmp_path):
    """stop() returns only once ``stopped`` is reported, even though the pump
    still has a management client to tear down: a restart's start() must
    never run before the old run's final report (it would evict the new
    process from the manager)."""
    import sys

    order: list[str] = []

    async def on_state(_sid: str, status: str, _detail: str) -> None:
        order.append(status)

    client = MsmpClient("s", fake_msmp.port, SECRET)
    script = "import sys\nfor line in sys.stdin:\n    break\n"
    proc = ServerProcess(
        "s", [sys.executable, "-u", "-c", script], str(tmp_path), ConsoleHub(), on_state,
        msmp=client,
    )
    await proc.start()
    assert await _wait_for(lambda: client.connected)
    await proc.stop("stop", timeout=5)
    order.append("<restart resumes>")
    assert order == ["stopping", "stopped", "<restart resumes>"]
    assert await _wait_for(lambda: client._task is None)
    assert client.connected is False


async def test_client_ignores_proxy_environment(fake_msmp, monkeypatch):
    """The endpoint is always loopback: a proxy configured for downloads must
    not receive the handshake (or its Bearer secret)."""
    monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:3128")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.1:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    client = await _connected_client(fake_msmp)
    try:
        assert client.schema_version == "3.1.0"
    finally:
        await client.close()


async def test_close_after_abort_is_clean(fake_msmp):
    client = await _connected_client(fake_msmp)
    client.abort()
    assert client.connected is False
    await client.close()
    await client.close()
    assert client._task is None


# --- roster.sync --------------------------------------------------------------------------


def test_sync_drops_stale_console_entries():
    r = Roster()
    r.feed(PREFIX + "Ghost joined the game")
    r.feed(PREFIX + "bot_farm [local]: logged in with entity id 1 at (0, 0, 0)")
    r.feed(PREFIX + "System chat: bot_farm joined the game")
    r.sync([{"id": OFFLINE, "name": "bot_farm"}, {"name": 3}, "junk"])
    [entry] = r.online()
    assert (entry.name, entry.bot, entry.uuid) == ("bot_farm", True, OFFLINE.replace("-", ""))


# --- API ------------------------------------------------------------------------------------


def _server_in(engine, tmp_path, **fields) -> str:
    server = Server(name="m", mc_version="26.3", type="fabric", path=str(tmp_path), **fields)
    with Session(engine) as session:
        session.add(server)
        session.commit()
        return server.id


def test_detail_exposes_msmp(client, engine, tmp_path, monkeypatch):
    from lectern.servers.manager import manager

    sid = _server_in(engine, tmp_path)
    assert client.get(f"/api/servers/{sid}").json()["msmp"] is None

    class _Proc:
        msmp = MsmpClient("s", 1, SECRET)

    _Proc.msmp.connected = True
    _Proc.msmp.schema_version = "3.1.0"
    monkeypatch.setattr(manager, "get_process", lambda server_id: _Proc())
    body = client.get(f"/api/servers/{sid}").json()
    assert body["msmp"] == {"connected": True, "schema_version": "3.1.0"}

    _Proc.msmp = None
    assert client.get(f"/api/servers/{sid}").json()["msmp"] is None


def test_patch_properties_rejects_managed_keys(client, engine, tmp_path):
    sid = _server_in(engine, tmp_path)
    write_properties(tmp_path, {"motd": "hi", "management-server-port": "25585"})
    before = (tmp_path / "server.properties").read_text()
    resp = client.patch(
        f"/api/servers/{sid}/properties",
        json={"motd": "changed", "management-server-port": 1, "max-players": 0},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "management-server-port is managed by Lectern" in detail
    assert "max-players" in detail
    resp = client.patch(
        f"/api/servers/{sid}/properties", json={"management-server-secret": None}
    )
    assert resp.status_code == 422
    assert "management-server-secret is managed by Lectern" in resp.json()["detail"]
    assert (tmp_path / "server.properties").read_text() == before


def test_definitions_flag_managed_keys(client, engine, tmp_path):
    sid = _server_in(engine, tmp_path)
    definitions = client.get(f"/api/servers/{sid}/properties").json()["definitions"]
    managed = {k for k, d in definitions.items() if d["managed"]}
    assert managed == {
        "management-server-enabled", "management-server-host", "management-server-port",
        "management-server-tls-enabled", "management-server-secret",
    }
    assert set(MANAGED_KEYS) == managed
    assert definitions["motd"]["managed"] is False


@pytest.mark.parametrize(("type_", "version"), [("vanilla", "1.21.8"), ("velocity", "3.4.0")])
def test_management_keys_are_plain_where_lectern_does_not_manage_them(
    client, engine, tmp_path, type_, version
):
    """A server below 1.21.9 (e.g. downgraded after a 26.3 start) or a proxy:
    Lectern never writes the keys, so leftover lines are ordinary, editable
    and removable, and the editor gets no read-only definitions for them."""
    server = Server(name="m", mc_version=version, type=type_, path=str(tmp_path))
    with Session(engine) as session:
        session.add(server)
        session.commit()
        sid = server.id
    write_properties(
        tmp_path, {"management-server-port": "25585", "management-server-secret": "x" * 40}
    )
    definitions = client.get(f"/api/servers/{sid}/properties").json()["definitions"]
    assert not set(MANAGED_KEYS) & set(definitions)
    assert definitions["motd"]["managed"] is False
    resp = client.patch(
        f"/api/servers/{sid}/properties",
        json={"management-server-port": None, "management-server-secret": None},
    )
    assert resp.status_code == 200, resp.text
    assert read_properties(tmp_path) == {}


def test_settings_patch_maps_symlinked_properties_to_400(client, engine, tmp_path):
    """Every server.properties writer (here the settings PATCH's port mirror)
    answers the symlink-out refusal with a 400 and a readable detail."""
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    outside = tmp_path / "outside.conf"
    outside.write_text("precious=1\n")
    (server_dir / "server.properties").symlink_to(outside)
    sid = _server_in(engine, server_dir)
    resp = client.patch(f"/api/servers/{sid}", json={"port": 25570})
    assert resp.status_code == 400
    assert "outside the server directory" in resp.json()["detail"]
    assert outside.read_text() == "precious=1\n"


def test_enable_ignores_non_ascii_digit_port(tmp_path):
    """"²" passes str.isdigit() but not int(): a hand-edited port like that is
    treated as absent instead of failing the start with a ValueError."""
    write_properties(tmp_path, {"management-server-port": "²"})
    endpoint = msmp.enable(tmp_path, game_port=25565, reserved=set())
    assert msmp.MSMP_PORT_BASE <= endpoint.port < msmp.MSMP_PORT_BASE + msmp._PORT_SPAN


# --- challenge follow-ups ----------------------------------------------------------


def test_port_free_ignores_time_wait():
    """A quick restart keeps its management port: the previous run's closed
    connection leaves the port in TIME_WAIT, which must not count as busy."""
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as peer:
            accepted, _ = listener.accept()
            accepted.close()  # server side closes first -> TIME_WAIT on `port`
            peer.recv(1)
    assert msmp._port_free(port) is True
    assert msmp.pick_management_port(port, set()) == port


def test_patch_properties_refuses_symlink_out(client, engine, tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    outside = tmp_path / "outside.conf"
    outside.write_text("precious=1\n")
    (server_dir / "server.properties").symlink_to(outside)
    sid = _server_in(engine, server_dir)
    resp = client.patch(f"/api/servers/{sid}/properties", json={"motd": "x"})
    assert resp.status_code == 400
    assert "outside the server directory" in resp.json()["detail"]
    assert outside.read_text() == "precious=1\n"


def test_enable_refuses_symlinked_properties_pointing_out(tmp_path):
    """enable() runs unattended at every start (autostart, crash restart); a
    server.properties symlinked outside the server dir is never rewritten."""
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    outside = tmp_path / "outside.conf"
    outside.write_text("precious=1\n")
    (server_dir / "server.properties").symlink_to(outside)
    with pytest.raises(OSError):
        msmp.enable(server_dir, game_port=25565, reserved=set())
    assert outside.read_text() == "precious=1\n"


def test_enable_follows_symlink_inside_server_dir(tmp_path):
    (tmp_path / "conf").mkdir()
    real = tmp_path / "conf" / "real.properties"
    real.write_text("motd=hi\n")
    (tmp_path / "server.properties").symlink_to(real)
    msmp.enable(tmp_path, game_port=25565, reserved=set())
    assert (tmp_path / "server.properties").is_symlink()
    written = read_properties(tmp_path)
    assert written["motd"] == "hi"
    assert written["management-server-enabled"] == "true"


async def test_close_waits_for_reader_cleanup(monkeypatch):
    """_session awaits the cancelled reader: its cleanup has finished by the
    time close() returns, so no task is left pending (or destroyed pending)."""
    original = MsmpClient._reader
    cleaned: list[bool] = []

    async def slow_cleanup_reader(self, ws):
        try:
            await original(self, ws)
        finally:
            await asyncio.shield(asyncio.sleep(0.05))
            cleaned.append(True)

    monkeypatch.setattr(MsmpClient, "_reader", slow_cleanup_reader)
    fake = await FakeMsmp().start()
    client = MsmpClient("s", fake.port, SECRET)
    in_connect = asyncio.Event()

    async def on_connect():
        # close() lands while the session is still in on_connect, not yet
        # awaiting the reader: only the explicit wait covers this window.
        in_connect.set()
        await asyncio.Event().wait()

    client.on_connect = on_connect
    await client.start()
    try:
        await asyncio.wait_for(in_connect.wait(), 5)
        await client.close()
        assert cleaned == [True]
    finally:
        await fake.stop()


async def test_callback_failure_is_logged_and_session_continues(caplog):
    fake = await FakeMsmp().start()
    client = MsmpClient("s", fake.port, SECRET)
    seen: list[str] = []

    async def on_connect():
        raise ValueError("boom")

    async def on_notification(method, params):
        seen.append(method)

    client.on_connect = on_connect
    client.on_notification = on_notification
    caplog.set_level(logging.INFO, logger="lectern.servers.msmp")
    await client.start()
    try:
        assert await _wait_for(lambda: client.connected)
        await fake.push(N_STARTED, [])
        assert await _wait_for(lambda: seen == [N_STARTED])
        assert client.connected is True
        assert any("callback raised ValueError" in r.getMessage() for r in caplog.records)
    finally:
        await client.close()
        await fake.stop()


# --- test-engineer additions ---------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.21.10", True),
        ("1.21.9-rc1", True),
        ("26.1-snapshot-5", True),
        ("26.10", True),
        ("1.21.8-rc1", False),
        ("1.9", False),
    ],
)
def test_version_gate_boundaries_across_schemes(version, expected):
    """1.21.10 is numerically past 1.21.9 (not a string compare), the new
    year-based scheme always passes, and a pre-release suffix keeps its base."""
    assert msmp.supports_msmp(version) is expected


@pytest.mark.parametrize(
    ("type_", "version", "expected"),
    [
        ("vanilla", "26.3", True),
        ("fabric", "26.3", True),
        ("quilt", "1.21.9", True),
        ("forge", "1.21.10", True),
        ("neoforge", "26.1", True),
        ("velocity", "3.4.0", False),  # numerically >= 1.21.9 but a proxy
        ("fabric", "1.21.8", False),
        ("vanilla", "1.20.1", False),
    ],
)
def test_manages_java_types_only_from_1_21_9(type_, version, expected):
    assert msmp.manages(type_, version) is expected


def test_enable_avoids_game_port_inside_management_range(tmp_path):
    """A game port that happens to sit at the start of the management range
    is never handed to the management server."""
    game = msmp.MSMP_PORT_BASE
    write_properties(tmp_path, {"server-port": str(game)})
    endpoint = msmp.enable(tmp_path, game_port=game, reserved=set())
    assert endpoint.port != game
    assert read_properties(tmp_path)["management-server-port"] == str(endpoint.port)
    assert read_properties(tmp_path)["server-port"] == str(game)


def test_enable_moves_off_another_servers_management_port_keeping_secret(tmp_path):
    """The previous port is now another server's management port: the port
    moves, the secret is kept."""
    taken = _free_port()
    write_properties(tmp_path, {
        "management-server-port": str(taken), "management-server-secret": SECRET,
    })
    endpoint = msmp.enable(tmp_path, game_port=25565, reserved={taken})
    assert endpoint.port != taken
    assert endpoint.secret == SECRET
    props = read_properties(tmp_path)
    assert props["management-server-port"] == str(endpoint.port)
    assert props["management-server-secret"] == SECRET


def test_enable_is_stable_across_restarts(tmp_path):
    """Two consecutive starts write the same port and secret (nothing churns)."""
    write_properties(tmp_path, {"server-port": "25565"})
    first = msmp.enable(tmp_path, game_port=25565, reserved=set())
    text = (tmp_path / "server.properties").read_text()
    second = msmp.enable(tmp_path, game_port=25565, reserved=set())
    assert first == second
    assert read_properties(tmp_path) == {
        k: v for k, v in (
            line.split("=", 1) for line in text.splitlines()
            if "=" in line and not line.startswith("#")
        )
    }


async def test_reconnect_resyncs_roster_to_server_list(fake_msmp, tmp_path):
    """After a reconnect the server's list is authoritative: a player who left
    while the socket was down (and whose console line was missed) is dropped,
    a player who joined meanwhile is added, and a known bot stays a bot."""
    fake_msmp.players = [{"id": NOTCH, "name": "Notch"}]
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        assert await _wait_for(lambda: "Notch" in proc.roster.players)
        await proc._handle_line(
            PREFIX + "Steve [local]: logged in with entity id 2 at ([minecraft:overworld]0, 64, 0)"
        )
        await proc._handle_line(PREFIX + "System chat: Steve joined the game")
        port = fake_msmp.port
        await fake_msmp.stop()
        assert await _wait_for(lambda: not proc.msmp.connected)
        replacement = await FakeMsmp().start(port)
        replacement.players = [
            {"id": OFFLINE, "name": "Steve"},
            {"id": OFFLINE.replace("1", "2"), "name": "Alex"},
        ]
        try:
            assert await _wait_for(
                lambda: proc.msmp.connected and proc._roster_backlog is None
                and set(proc.roster.players) == {"Steve", "Alex"}
            )
            assert proc.roster.players["Steve"].bot is True
            assert proc.roster.players["Alex"].bot is False
        finally:
            await replacement.stop()
    finally:
        await proc.msmp.close()


async def test_bot_left_then_respawned_is_still_a_bot(fake_msmp, tmp_path):
    """Carpet kill then respawn: the protocol's left/joined pair must not turn
    the bot into a real player even before the new console login line lands."""
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        await proc._handle_line(
            PREFIX + "Steve [local]: logged in with entity id 2 at ([minecraft:overworld]0, 64, 0)"
        )
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Steve"}])
        assert await _wait_for(lambda: "Steve" in proc.roster.players)
        await fake_msmp.push(N_LEFT, [{"id": OFFLINE, "name": "Steve"}])
        assert await _wait_for(lambda: "Steve" not in proc.roster.players)
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Steve"}])
        assert await _wait_for(lambda: "Steve" in proc.roster.players)
        assert proc.roster.players["Steve"].bot is True
    finally:
        await proc.msmp.close()


async def test_protocol_name_with_selector_syntax_is_ignored(fake_msmp, tmp_path):
    """A roster key reaches the console as part of `kick <name>`: names the
    console could not have produced (selectors, spaces) never enter it."""
    fake_msmp.players = [{"id": NOTCH, "name": "@a"}]
    proc, _ = await _process_with(fake_msmp, tmp_path)
    try:
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "x; stop"}])
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Alex"}])
        assert await _wait_for(lambda: "Alex" in proc.roster.players)
        assert set(proc.roster.players) == {"Alex"}
    finally:
        await proc.msmp.close()


# --- kick through the real client (same event loop as the fake server) -----------


async def _kick_env(fake: FakeMsmp, engine, tmp_path, monkeypatch):
    from lectern.servers.manager import manager

    with Session(engine) as session:
        server = Server(name="k", mc_version="26.3", type="fabric", path=str(tmp_path))
        session.add(server)
        session.commit()
        sid = server.id
    proc, _ = await _process_with(fake, tmp_path)
    monkeypatch.setattr(manager, "get_process", lambda server_id: proc)
    sent: list[str] = []

    async def send(server_id, command):
        sent.append(command)
        name = command.split()[1]
        await proc._handle_line(PREFIX + f"System chat: {name} left the game")

    monkeypatch.setattr(manager, "send", send)
    return sid, proc, sent


async def test_kick_player_goes_through_real_msmp_client(fake_msmp, engine, tmp_path, monkeypatch):
    """End to end: the endpoint sends minecraft:players/kick with positional
    params over the socket, the server's players/left notification removes
    the player, and nothing is typed into the console."""
    from lectern.api.players import kick_player
    from lectern.models import KickRequest

    fake_msmp.players = [{"id": NOTCH, "name": "Notch"}]
    sid, proc, sent = await _kick_env(fake_msmp, engine, tmp_path, monkeypatch)
    assert await _wait_for(lambda: "Notch" in proc.roster.players)
    try:
        with Session(engine) as session:
            body = await kick_player(sid, "Notch", KickRequest(), session)
        assert body == []
        assert (M_KICK, [[{"name": "Notch"}]]) in fake_msmp.calls
        assert sent == []
        assert fake_msmp.players == []
    finally:
        await proc.msmp.close()


async def test_kick_bot_uses_carpet_even_with_real_client(
    fake_msmp, engine, tmp_path, monkeypatch
):
    from lectern.api.players import kick_player

    sid, proc, sent = await _kick_env(fake_msmp, engine, tmp_path, monkeypatch)
    try:
        await proc._handle_line(
            PREFIX + "Steve [local]: logged in with entity id 2 at ([minecraft:overworld]0, 64, 0)"
        )
        await fake_msmp.push(N_JOINED, [{"id": OFFLINE, "name": "Steve"}])
        assert await _wait_for(lambda: "Steve" in proc.roster.players)
        with Session(engine) as session:
            body = await kick_player(sid, "Steve", None, session)
        assert sent == ["player Steve kill"]
        assert not any(m == M_KICK for m, _ in fake_msmp.calls)
        assert body == []
    finally:
        await proc.msmp.close()


async def test_kick_unknown_name_is_404_without_any_call(
    fake_msmp, engine, tmp_path, monkeypatch
):
    from fastapi import HTTPException

    from lectern.api.players import kick_player

    sid, proc, sent = await _kick_env(fake_msmp, engine, tmp_path, monkeypatch)
    try:
        with Session(engine) as session, pytest.raises(HTTPException) as exc:
            await kick_player(sid, "@a", None, session)
        assert exc.value.status_code == 404
        assert sent == []
        assert not any(m == M_KICK for m, _ in fake_msmp.calls)
    finally:
        await proc.msmp.close()


def test_online_players_shape_with_msmp_uuid(client, engine, tmp_path, monkeypatch):
    """The online list keeps its shape; a protocol uuid arrives undashed."""
    from lectern.servers.manager import manager

    sid = _server_in(engine, tmp_path)

    class _Proc:
        roster = Roster()
        msmp = None

    _Proc.roster.joined("Notch", NOTCH)
    monkeypatch.setattr(manager, "get_process", lambda server_id: _Proc())
    [entry] = client.get(f"/api/servers/{sid}/players/online").json()
    assert set(entry) == {"name", "uuid", "bot", "joined_at"}
    assert (entry["name"], entry["uuid"], entry["bot"]) == ("Notch", NOTCH_UNDASHED, False)


def test_settings_patch_response_carries_msmp(client, engine, tmp_path):
    """Every endpoint returning ServerDetailRead goes through _detail()."""
    sid = _server_in(engine, tmp_path)
    resp = client.patch(f"/api/servers/{sid}", json={"memory_mb": 2048})
    assert resp.status_code == 200, resp.text
    assert "msmp" in resp.json() and resp.json()["msmp"] is None


def test_patch_managed_key_alone_writes_nothing(client, engine, tmp_path):
    """A request carrying only valid keys plus one managed key is refused
    as a whole: the valid key is not written either."""
    sid = _server_in(engine, tmp_path)
    write_properties(tmp_path, {"motd": "hi"})
    resp = client.patch(
        f"/api/servers/{sid}/properties",
        json={"motd": "changed", "management-server-enabled": "false"},
    )
    assert resp.status_code == 422
    assert read_properties(tmp_path) == {"motd": "hi"}


# --- phase 2 through the real client: lists, backups, live settings ---------------

JEB = "853c80ef-3c37-49fd-aa49-938b674adae6"


def _fake_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


async def _running_env(fake: FakeMsmp, engine, tmp_path, monkeypatch, *, apply=None, **fields):
    """A running server whose process has a real client connected to ``fake``
    (which writes its files into ``tmp_path``); console commands are recorded
    and, with ``apply``, acted on like the server would."""
    from lectern.models import Player
    from lectern.servers.manager import manager

    fake.server_dir = tmp_path
    with Session(engine) as session:
        server = Server(
            name="p", mc_version="26.3", type="fabric", path=str(tmp_path),
            status="running", **fields,
        )
        session.add(server)
        session.add(Player(uuid=NOTCH_UNDASHED, name="Notch"))
        session.add(Player(uuid=JEB.replace("-", ""), name="jeb_"))
        session.commit()
        sid = server.id
    proc, _ = await _process_with(fake, tmp_path)
    monkeypatch.setattr(manager, "get_process", lambda server_id: proc)
    monkeypatch.setattr(manager, "is_running", lambda server_id: True)
    sent: list[str] = []

    async def send(server_id, command):
        sent.append(command)
        if apply is not None:
            apply(command)

    monkeypatch.setattr(manager, "send", send)
    return sid, proc, sent


def _mutations(fake: FakeMsmp) -> list[tuple[str, object]]:
    """Calls other than the ones the process makes on every connect, and the
    operator-level read an ops add makes first (see test_op_add_uses_...)."""
    reads = ("rpc.discover", M_PLAYERS, msmp.M_STATUS, M_OP_LEVEL)
    return [c for c in fake.calls if c[0] not in reads]


@pytest.mark.parametrize(
    ("kind", "add", "remove", "entry"),
    [
        ("whitelist", M_ALLOWLIST_ADD, M_ALLOWLIST_REMOVE, {"name": "Notch"}),
        ("ops", M_OPERATORS_ADD, M_OPERATORS_REMOVE, {
            "player": {"name": "Notch"}, "permissionLevel": 4, "bypassesPlayerLimit": False,
        }),
        ("banned", M_BANS_ADD, M_BANS_REMOVE, {"player": {"name": "Notch"}, "source": "Lectern"}),
    ],
)
async def test_list_add_and_remove_go_through_real_client(
    fake_msmp, engine, tmp_path, monkeypatch, kind, add, remove, entry
):
    """One protocol call per change and nothing on the console; the response
    is the file exactly as the server wrote it (its own UUID for the name)."""
    from lectern.api.players import add_to_list, remove_from_list
    from lectern.models import PlayerListAddRequest

    sid, proc, sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    server_uuid = _fake_id("Notch")
    try:
        with Session(engine) as session:
            body = await add_to_list(sid, kind, PlayerListAddRequest(uuid=NOTCH), session)
        assert _mutations(fake_msmp) == [(add, [[entry]])]
        assert sent == []
        assert [(e.uuid, e.name) for e in getattr(body, kind)] == [
            (server_uuid.replace("-", ""), "Notch")
        ]
        with Session(engine) as session:
            body = await remove_from_list(sid, kind, server_uuid.replace("-", ""), session)
        assert _mutations(fake_msmp)[1:] == [
            (remove, [[{"name": "Notch", "id": server_uuid}]])
        ]
        assert sent == []
        assert getattr(body, kind) == []
    finally:
        await proc.msmp.close()


async def test_ban_carries_reason_and_expiry_and_omits_them_when_absent(
    fake_msmp, engine, tmp_path, monkeypatch
):
    from lectern.api.players import add_to_list
    from lectern.models import PlayerListAddRequest

    sid, proc, sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    try:
        with Session(engine) as session:
            await add_to_list(sid, "banned", PlayerListAddRequest(
                uuid=NOTCH, reason=" griefing\nspawn ", expires="2099-01-01T02:00:00+02:00",
            ), session)
        with Session(engine) as session:
            await add_to_list(sid, "banned", PlayerListAddRequest(uuid=JEB), session)
        assert _mutations(fake_msmp) == [
            (M_BANS_ADD, [[{
                "player": {"name": "Notch"}, "source": "Lectern",
                "reason": "griefing spawn", "expires": "2099-01-01T00:00:00Z",
            }]]),
            (M_BANS_ADD, [[{"player": {"name": "jeb_"}, "source": "Lectern"}]]),
        ]
        assert sent == []
        rows = json.loads((tmp_path / "banned-players.json").read_text())
        assert [(r["name"], r["reason"], r["expires"], r["source"]) for r in rows] == [
            ("Notch", "griefing spawn", "2099-01-01 00:00:00 +0000", "Lectern"),
            ("jeb_", "Banned by an operator.", "forever", "Lectern"),
        ]
    finally:
        await proc.msmp.close()


async def test_list_protocol_error_falls_back_to_console(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """A JSON-RPC error on add or remove: the console command runs as on main
    and the response still reflects the change."""
    from lectern.api.players import add_to_list, remove_from_list
    from lectern.models import PlayerListAddRequest
    from lectern.servers import playerlists

    def apply(command):
        verb = command.split()[0]
        if verb == "op":
            playerlists.add(tmp_path, "ops", NOTCH, "Notch")
        elif verb == "deop":
            playerlists.remove(tmp_path, "ops", NOTCH)

    sid, proc, sent = await _running_env(
        fake_msmp, engine, tmp_path, monkeypatch, apply=apply
    )
    fake_msmp.fail = {M_OPERATORS_ADD, M_OPERATORS_REMOVE}
    try:
        with Session(engine) as session:
            body = await add_to_list(sid, "ops", PlayerListAddRequest(uuid=NOTCH), session)
        assert [e.name for e in body.ops] == ["Notch"]
        with Session(engine) as session:
            body = await remove_from_list(sid, "ops", NOTCH, session)
        assert body.ops == []
        assert sent == ["op Notch", "deop Notch"]
        assert [m for m, _ in _mutations(fake_msmp)] == [M_OPERATORS_ADD, M_OPERATORS_REMOVE]
    finally:
        await proc.msmp.close()


async def test_list_unconfirmed_reply_falls_back_to_console(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """The server answered but its list lacks the player (a name it could not
    resolve): not success, so the console is tried."""
    from lectern.api.players import add_to_list
    from lectern.models import PlayerListAddRequest
    from lectern.servers import playerlists

    sid, proc, sent = await _running_env(
        fake_msmp, engine, tmp_path, monkeypatch,
        apply=lambda command: playerlists.add(tmp_path, "whitelist", NOTCH, "Notch"),
    )
    fake_msmp.answers[M_ALLOWLIST_ADD] = [{"id": JEB, "name": "jeb_"}]
    try:
        with Session(engine) as session:
            body = await add_to_list(sid, "whitelist", PlayerListAddRequest(uuid=NOTCH), session)
        assert sent == ["whitelist add Notch"]
        assert [(e.uuid, e.name) for e in body.whitelist] == [(NOTCH_UNDASHED, "Notch")]
    finally:
        await proc.msmp.close()


async def test_running_backup_flushes_through_real_client(
    fake_msmp, engine, tmp_path, monkeypatch
):
    from lectern import backups as bk

    (tmp_path / "level.dat").write_bytes(b"W")
    recorded: list = []
    monkeypatch.setattr(
        bk.events, "record", lambda _sid, kind, msg="": recorded.append((kind, msg))
    )
    sid, proc, sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    try:
        with Session(engine) as session:
            filename = (await bk.create_backup(session, session.get(Server, sid))).filename
        assert fake_msmp.saved == 1
        assert (M_SAVE, [True]) in fake_msmp.calls
        assert sent == ["save-off", "save-on"]
        assert recorded == [
            ("backup_created", f"{filename} (manual, flushed via management protocol)")
        ]
    finally:
        await proc.msmp.close()


async def test_running_backup_leaves_operator_save_off_alone(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """Saving already off (an operator's save-off): the world is still
    flushed, but neither save-off nor the save-on that would undo it is sent."""
    from lectern import backups as bk

    monkeypatch.setattr(bk.events, "record", lambda *a: None)
    sid, proc, sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.autosave = False
    try:
        with Session(engine) as session:
            await bk.create_backup(session, session.get(Server, sid), trigger="scheduled")
        assert fake_msmp.saved == 1
        assert sent == []
    finally:
        await proc.msmp.close()


async def test_running_backup_save_error_falls_back_to_console_flush(
    fake_msmp, engine, tmp_path, monkeypatch
):
    from lectern import backups as bk

    recorded: list = []
    monkeypatch.setattr(
        bk.events, "record", lambda _sid, kind, msg="": recorded.append((kind, msg))
    )
    monkeypatch.setattr(bk, "_CONSOLE_FLUSH_GRACE", 0)
    sid, proc, sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.fail = {M_SAVE, M_AUTOSAVE}
    try:
        with Session(engine) as session:
            backup = await bk.create_backup(session, session.get(Server, sid), trigger="scheduled")
            filename = backup.filename
        assert sent == ["save-off", "save-all flush", "save-on"]
        assert recorded == [
            ("backup_created", f"{filename} (scheduled, flushed via console)")
        ]
    finally:
        await proc.msmp.close()


LIVE_REQUEST = {
    "motd": "Hello \u00a7aworld", "difficulty": "hard", "max-players": "25",
    "view-distance": 12, "white-list": "true", "enforce-whitelist": False,
    "gamemode": "creative",
}


async def test_live_settings_apply_through_real_client_without_lectern_writing(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """Every live key goes out typed; the server rewrites server.properties
    itself and Lectern writes nothing when nothing else needs writing."""
    from lectern.api import servers as api
    from lectern.servers import properties as props

    write_properties(tmp_path, {"motd": "old", "pvp": "true"})
    sid, proc, _sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    writes: list = []
    real_write = props.write_properties
    monkeypatch.setattr(
        api.props, "write_properties",
        lambda d, p: (writes.append(dict(p)), real_write(d, p)),
    )
    try:
        with Session(engine) as session:
            body = await api.patch_properties(sid, dict(LIVE_REQUEST), session)
        assert body.applied_live == list(LIVE_REQUEST)
        assert fake_msmp.settings == {
            "motd": "Hello \u00a7aworld", "difficulty": "hard", "max_players": 25,
            "view_distance": 12, "use_allowlist": True, "enforce_allowlist": False,
            "game_mode": "creative",
        }
        assert [type(v) for v in fake_msmp.settings.values()] == [
            str, str, int, int, bool, bool, str,
        ]
        assert writes == []
        on_disk = read_properties(tmp_path)
        assert on_disk == fake_msmp.props
        assert on_disk["motd"] == "Hello \u00a7aworld" and on_disk["max-players"] == "25"
        assert body.properties == on_disk

        # Mixed: the live key goes out, only the other one is written, once.
        with Session(engine) as session:
            body = await api.patch_properties(sid, {"motd": "x", "pvp": False}, session)
        assert body.applied_live == ["motd"]
        assert len(writes) == 1 and writes[0]["pvp"] == "false" and writes[0]["motd"] == "x"
        assert read_properties(tmp_path)["pvp"] == "false"
    finally:
        await proc.msmp.close()


async def test_live_set_does_not_revert_earlier_file_edits(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """The server rewrites server.properties from its startup copy: a key
    Lectern wrote while it ran (pvp) would be reverted by the next live set,
    so Lectern writes it again on top."""
    from lectern.api import servers as api

    write_properties(tmp_path, {"motd": "old", "pvp": "true"})
    sid, proc, _sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.props = read_properties(tmp_path)  # the server's startup snapshot
    try:
        with Session(engine) as session:
            await api.patch_properties(sid, {"pvp": False}, session)
        with Session(engine) as session:
            body = await api.patch_properties(sid, {"motd": "new"}, session)
        assert body.applied_live == ["motd"]
        assert read_properties(tmp_path) == {"motd": "new", "pvp": "false"}
        assert body.properties == {"motd": "new", "pvp": "false"}
    finally:
        await proc.msmp.close()


async def test_live_set_failure_or_other_value_writes_the_file(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """A failing set is written to the file for the next start, and so is a
    set the server answers with another value (a hardcore world keeps hard):
    the server refused it, so it is not reported as applied now."""
    from lectern.api import servers as api

    write_properties(tmp_path, {"motd": "old"})
    sid, proc, _sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.fail = {msmp.setting_method("max-players")}
    fake_msmp.answers[msmp.setting_method("difficulty")] = "hard"
    try:
        with Session(engine) as session:
            body = await api.patch_properties(
                sid, {"motd": "m", "difficulty": "peaceful", "max-players": 5}, session
            )
        assert body.applied_live == ["motd"]
        assert read_properties(tmp_path) == {
            "motd": "m", "difficulty": "peaceful", "max-players": "5",
        }
        assert body.properties == read_properties(tmp_path)
    finally:
        await proc.msmp.close()


async def test_live_set_answered_late_is_read_back_before_the_file_write(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """A set that times out on Lectern's side still runs on the server, whose
    rewrite from its startup copy would land after Lectern's file write and
    revert pvp. Lectern reads the setting back (answered once the set ran),
    finds it applied, and writes pvp on top of the server's rewrite."""
    from lectern.api import servers as api

    write_properties(tmp_path, {"motd": "old", "pvp": "true"})
    sid, proc, _sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.props = read_properties(tmp_path)  # the server's startup snapshot
    fake_msmp.delays[msmp.setting_method("motd")] = 0.3
    monkeypatch.setattr(api, "_SET_TIMEOUT", 0.05)
    try:
        with Session(engine) as session:
            body = await api.patch_properties(sid, {"motd": "x", "pvp": False}, session)
        assert body.applied_live == ["motd"]
        assert (msmp.setting_get_method("motd"), []) in fake_msmp.calls
        assert await _wait_for(lambda: fake_msmp.settings.get("motd") == "x")
        assert read_properties(tmp_path) == {"motd": "x", "pvp": "false"}
        assert body.properties == {"motd": "x", "pvp": "false"}
    finally:
        await proc.msmp.close()


@pytest.mark.parametrize(
    ("request_body", "read_back", "read_back_timeout", "applied_first", "saved"),
    [
        # The read-back is held up behind the slow set past its own timeout.
        ({"motd": "x", "pvp": False}, None, 0.1, None, "pvp"),
        # A key applied live first, then the slow set: the reviewers' case.
        ({"difficulty": "hard", "motd": "x"}, None, 0.1, "difficulty", None),
        # The read-back answers, but not with the value sent.
        ({"difficulty": "hard", "motd": "x", "pvp": False}, "other", 5.0, "difficulty", "pvp"),
    ],
)
async def test_live_set_unconfirmed_after_timeout_is_504_after_restoring_the_file(
    fake_msmp, engine, tmp_path, monkeypatch, caplog, request_body, read_back,
    read_back_timeout, applied_first, saved,
):
    """When the read-back cannot tell whether the late set ran, the request
    still writes the file before answering 504: a set confirmed earlier in it
    made the server rewrite server.properties from its startup copy, and an
    edit saved while the server ran (spawn-protection) must survive that. The
    unconfirmed key keeps whatever is on disk; the 504 names it, what applied
    live and what was saved."""
    from fastapi import HTTPException

    from lectern.api import servers as api

    write_properties(tmp_path, {"motd": "old", "pvp": "true"})
    sid, proc, _sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.props = read_properties(tmp_path)  # the server's startup snapshot
    try:
        with Session(engine) as session:
            await api.patch_properties(sid, {"spawn-protection": 5}, session)
        fake_msmp.delays[msmp.setting_method("motd")] = 0.4
        if read_back is not None:
            fake_msmp.answers[msmp.setting_get_method("motd")] = read_back
        monkeypatch.setattr(api, "_SET_TIMEOUT", 0.05)
        monkeypatch.setattr(api, "_SETTING_READ_BACK_TIMEOUT", read_back_timeout)
        caplog.set_level(logging.INFO, logger="lectern.api.servers")

        with Session(engine) as session, pytest.raises(HTTPException) as refused:
            await api.patch_properties(sid, dict(request_body), session)
        detail = refused.value.detail
        assert refused.value.status_code == 504
        assert "did not confirm the motd change" in detail
        assert "rewrites server.properties from its startup copy" in detail
        assert ("difficulty applied live" in detail) == (applied_first is not None)
        assert (f"{saved} saved to server.properties" in detail) == (saved is not None)
        assert ("saved to server.properties" in detail) == (saved is not None)
        # Which way the read-back failed: no answer, or another value.
        assert ("read-back of motd failed" in caplog.text) == (read_back is None)
        assert ("read-back of motd: 'other'" in caplog.text) == (read_back is not None)

        on_disk = read_properties(tmp_path)
        assert on_disk["spawn-protection"] == "5"
        assert on_disk["pvp"] == ("false" if saved else "true")
        assert ("difficulty" in on_disk) == (applied_first is not None)
        if applied_first:
            assert on_disk["difficulty"] == "hard"
        # Answered late (another value), the set had already run and rewritten
        # the file: motd keeps what the server wrote. Unanswered, it has not.
        assert on_disk["motd"] == ("x" if read_back is not None else "old")
        # The slow set lands eventually; let it before the client goes away.
        assert await _wait_for(lambda: fake_msmp.settings.get("motd") == "x")
    finally:
        await proc.msmp.close()


async def test_live_set_unconfirmed_504_still_mirrors_the_port(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """The 504 comes after the file write: a server-port saved by that write
    reaches the record's port column too, so the file and the DB agree."""
    from fastapi import HTTPException

    from lectern.api import servers as api

    write_properties(tmp_path, {"motd": "old", "server-port": "25565"})
    sid, proc, _sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    try:
        fake_msmp.delays[msmp.setting_method("motd")] = 0.4
        monkeypatch.setattr(api, "_SET_TIMEOUT", 0.05)
        monkeypatch.setattr(api, "_SETTING_READ_BACK_TIMEOUT", 0.1)
        with Session(engine) as session, pytest.raises(HTTPException) as refused:
            await api.patch_properties(sid, {"motd": "x", "server-port": 25570}, session)
        assert refused.value.status_code == 504
        assert "server-port saved to server.properties" in refused.value.detail
        assert read_properties(tmp_path)["server-port"] == "25570"
        with Session(engine) as session:
            assert session.get(Server, sid).port == 25570
        assert await _wait_for(lambda: fake_msmp.settings.get("motd") == "x")
    finally:
        await proc.msmp.close()


async def test_op_add_uses_the_running_servers_op_level(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """Through the socket: the operator level is read from the server first
    (its ``op-permission-level``), and ops.json gets that level, as the
    console ``op`` would grant on this server."""
    from lectern.api.players import add_to_list
    from lectern.models import PlayerListAddRequest

    write_properties(tmp_path, {"op-permission-level": "3"})
    sid, proc, sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.op_level = 2
    try:
        with Session(engine) as session:
            await add_to_list(sid, "ops", PlayerListAddRequest(uuid=NOTCH), session)
        calls = [c for c in fake_msmp.calls if c[0] in (M_OP_LEVEL, M_OPERATORS_ADD)]
        assert calls == [(M_OP_LEVEL, []), (M_OPERATORS_ADD, [[{
            "player": {"name": "Notch"}, "permissionLevel": 2, "bypassesPlayerLimit": False,
        }]])]
        assert sent == []
        assert json.loads((tmp_path / "ops.json").read_text())[0]["level"] == 2
    finally:
        await proc.msmp.close()


async def test_concurrent_live_patches_do_not_lose_edits(
    fake_msmp, engine, tmp_path, monkeypatch
):
    """Challenger: PATCH is now async and reads the file, awaits the live
    calls, then merges onto that earlier read. Two PATCHes of one running
    server overlapping across that await each merge onto a stale snapshot, so
    the later write reverts the other's edits (a file edit, or a key the
    server just applied live and rewrote). Serialised per server, both
    survive."""
    from lectern.api import servers as api

    write_properties(tmp_path, {"motd": "old", "pvp": "true"})
    sid, proc, _sent = await _running_env(fake_msmp, engine, tmp_path, monkeypatch)
    fake_msmp.props = read_properties(tmp_path)  # the server's startup snapshot

    async def patch(updates):
        with Session(engine) as session:
            return await api.patch_properties(sid, updates, session)

    try:
        a, b = await asyncio.gather(
            patch({"motd": "a", "pvp": False}), patch({"difficulty": "hard"})
        )
        assert a.applied_live == ["motd"] and b.applied_live == ["difficulty"]
        assert read_properties(tmp_path) == {"motd": "a", "pvp": "false", "difficulty": "hard"}
    finally:
        await proc.msmp.close()
