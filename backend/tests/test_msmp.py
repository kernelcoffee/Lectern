"""Server Management Protocol: version gate, enablement, the client against an
in-process fake management server, and how ServerProcess merges it with the
console (roster, running/stopping, fallback)."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path

import pytest
from sqlmodel import Session
from websockets.asyncio.server import serve

from lectern.models import Server
from lectern.servers import msmp
from lectern.servers.msmp import (
    M_KICK,
    M_PLAYERS,
    N_JOINED,
    N_LEFT,
    N_STARTED,
    N_STOPPING,
    MsmpClient,
    MsmpError,
)
from lectern.servers.process import ServerProcess
from lectern.servers.properties import MANAGED_KEYS, read_properties, write_properties
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


class FakeMsmp:
    """A minimal MSMP endpoint: Bearer check, rpc.discover, players, kick,
    server/status, pushed notifications."""

    def __init__(self, secret: str = SECRET) -> None:
        self.secret = secret
        self.port = 0
        self.calls: list[tuple[str, object]] = []
        self.players: list[dict] = []
        self.started = False
        self.rejected = 0
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
                if not isinstance(params, list):
                    await self._reply(ws, msg, error=(-32602, "Not a json array"))
                elif method == "rpc.discover":
                    await self._reply(ws, msg, result={
                        "info": {"title": "Minecraft Server JSON-RPC", "version": "3.1.0"},
                        "methods": [{"name": M_PLAYERS}, {"name": M_KICK}],
                    })
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
        with pytest.raises(MsmpError, match="timed out"):
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
