"""Players: registry (Mojang-validated) + per-server whitelist/ops/banned files."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from lectern.models import Player, Server
from lectern.providers import mojang
from lectern.servers import playerlists as pl

NOTCH = "069a79f444e94726a5befca90e38aaf5"
NOTCH_DASHED = "069a79f4-44e9-4726-a5be-fca90e38aaf5"


# --- uuid helpers -----------------------------------------------------------


def test_uuid_dash_roundtrip():
    assert mojang.undash_uuid(NOTCH_DASHED) == NOTCH
    assert mojang.dash_uuid(NOTCH) == NOTCH_DASHED
    assert mojang.dash_uuid(NOTCH_DASHED) == NOTCH_DASHED  # idempotent


# --- playerlists file ops ---------------------------------------------------


def test_add_remove_whitelist(tmp_path):
    pl.add(tmp_path, "whitelist", NOTCH, "Notch")
    entries = json.loads((tmp_path / "whitelist.json").read_text())
    assert entries == [{"uuid": NOTCH_DASHED, "name": "Notch"}]
    # read_list normalises uuid to undashed.
    assert pl.read_list(tmp_path, "whitelist") == [{"uuid": NOTCH, "name": "Notch"}]
    # idempotent add.
    pl.add(tmp_path, "whitelist", NOTCH, "Notch")
    assert len(json.loads((tmp_path / "whitelist.json").read_text())) == 1
    # remove by either dashed or undashed uuid.
    pl.remove(tmp_path, "whitelist", NOTCH_DASHED)
    assert pl.read_list(tmp_path, "whitelist") == []


def test_ops_entry_shape(tmp_path):
    pl.add(tmp_path, "ops", NOTCH, "Notch")
    entry = json.loads((tmp_path / "ops.json").read_text())[0]
    assert entry["uuid"] == NOTCH_DASHED
    assert entry["level"] == 4 and entry["bypassesPlayerLimit"] is False


def test_banned_entry_shape(tmp_path):
    pl.add(tmp_path, "banned", NOTCH, "Notch")
    entry = json.loads((tmp_path / "banned-players.json").read_text())[0]
    assert entry["expires"] == "forever" and entry["source"] == "Lectern"
    assert entry["reason"] == "Banned by an operator."


def test_banned_entry_stores_reason_and_utc_expiry(tmp_path):
    """Minecraft's file formats: a naive expiry is UTC, an aware one is
    converted; reason and expiry are ignored on the other lists."""
    naive = datetime.fromisoformat("2030-01-01T05:00:00")
    pl.add(tmp_path, "banned", NOTCH, "Notch", reason="grief", expires=naive)
    other = "f" * 32
    pl.add(
        tmp_path, "banned", other, "Jeb",
        expires=datetime(2030, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))),
    )
    pl.add(
        tmp_path, "whitelist", NOTCH, "Notch",
        reason="grief", expires=datetime(2030, 1, 1, tzinfo=UTC),
    )
    rows = json.loads((tmp_path / "banned-players.json").read_text())
    assert [(r["reason"], r["expires"]) for r in rows] == [
        ("grief", "2030-01-01 05:00:00 +0000"),
        ("Banned by an operator.", "2030-01-01 00:00:00 +0000"),
    ]
    assert json.loads((tmp_path / "whitelist.json").read_text()) == [
        {"uuid": NOTCH_DASHED, "name": "Notch"}
    ]


def test_unknown_kind_raises(tmp_path):
    with pytest.raises(pl.PlayerListError):
        pl.add(tmp_path, "nope", NOTCH, "x")


# --- endpoints --------------------------------------------------------------


def _fake_resolve(monkeypatch, mapping: dict[str, dict | None]):
    async def resolve(query):
        return mapping.get(query.strip().lower())

    monkeypatch.setattr(mojang, "resolve_profile", resolve)
    # api.players imported the name into its own namespace? No — it calls
    # mojang.resolve_profile, so patching the module attribute is enough.


def test_registry_add_validates_and_lists(client, monkeypatch):
    _fake_resolve(monkeypatch, {"notch": {"uuid": NOTCH, "name": "Notch"}})

    resp = client.post("/api/players", json={"query": "Notch"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["uuid"] == NOTCH and body["name"] == "Notch"
    assert [p["name"] for p in client.get("/api/players").json()] == ["Notch"]

    # Unknown player → 404.
    assert client.post("/api/players", json={"query": "ghost"}).status_code == 404

    # Delete.
    assert client.delete(f"/api/players/{NOTCH}").status_code == 204
    assert client.get("/api/players").json() == []


def _installed(client, engine, tmp_path) -> str:
    sid = client.post(
        "/api/servers", json={"name": "P", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.path = str(tmp_path)
        srv.status = "stopped"
        s.add(srv)
        s.commit()
    return sid


def test_server_list_add_requires_registry(client, engine, tmp_path):
    sid = _installed(client, engine, tmp_path)
    # Not registered → 404.
    assert (
        client.post(
            f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH}
        ).status_code
        == 404
    )


def test_server_list_add_remove(client, engine, tmp_path):
    sid = _installed(client, engine, tmp_path)
    with Session(engine) as s:
        s.add(Player(uuid=NOTCH, name="Notch"))
        s.commit()

    resp = client.post(
        f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [p["name"] for p in body["ops"]] == ["Notch"]
    assert body["whitelist"] == [] and body["banned"] == []
    # File written with the op shape.
    assert (tmp_path / "ops.json").exists()

    # Remove.
    resp = client.delete(f"/api/servers/{sid}/playerlists/ops/{NOTCH}")
    assert resp.json()["ops"] == []


# --- live path (running server) ---------------------------------------------


def _running(client, engine, tmp_path) -> str:
    sid = _installed(client, engine, tmp_path)
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.status = "running"
        s.add(srv)
        s.commit()
    return sid


def _register_notch(engine):
    with Session(engine) as s:
        s.add(Player(uuid=NOTCH, name="Notch"))
        s.commit()


def _patch_live(monkeypatch, sent: list, on_send=None):
    """Pretend the server is up: record console commands, and let ``on_send``
    mimic Minecraft applying the command by rewriting the list file."""
    from lectern.servers.manager import manager

    monkeypatch.setattr(manager, "is_running", lambda sid: True)

    async def send(sid, command):
        sent.append(command)
        if on_send is not None:
            on_send(command)

    monkeypatch.setattr(manager, "send", send)


def test_live_add_sends_command(client, engine, tmp_path, monkeypatch):
    sid = _running(client, engine, tmp_path)
    _register_notch(engine)
    sent: list = []
    _patch_live(
        monkeypatch, sent, lambda cmd: pl.add(tmp_path, "whitelist", NOTCH, "Notch")
    )

    resp = client.post(
        f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH}
    )
    assert resp.status_code == 200, resp.text
    assert sent == ["whitelist add Notch"]
    assert [p["name"] for p in resp.json()["whitelist"]] == ["Notch"]


def test_live_commands_per_kind(client, engine, tmp_path, monkeypatch):
    sid = _running(client, engine, tmp_path)
    _register_notch(engine)
    sent: list = []

    def apply(cmd):
        kind = {"whitelist": "whitelist", "op": "ops", "ban": "banned"}[cmd.split()[0]]
        pl.add(tmp_path, kind, NOTCH, "Notch")

    _patch_live(monkeypatch, sent, apply)
    for kind in ("whitelist", "ops", "banned"):
        assert (
            client.post(
                f"/api/servers/{sid}/playerlists/{kind}", json={"uuid": NOTCH}
            ).status_code
            == 200
        )
    assert sent == ["whitelist add Notch", "op Notch", "ban Notch"]


def test_live_remove_uses_file_name(client, engine, tmp_path, monkeypatch):
    sid = _running(client, engine, tmp_path)
    pl.add(tmp_path, "ops", NOTCH, "Notch")  # in the file, not the registry
    sent: list = []
    _patch_live(monkeypatch, sent, lambda cmd: pl.remove(tmp_path, "ops", NOTCH))

    resp = client.delete(f"/api/servers/{sid}/playerlists/ops/{NOTCH}")
    assert sent == ["deop Notch"]
    assert resp.json()["ops"] == []


def test_live_add_offline_uuid_confirms_by_name(client, engine, tmp_path, monkeypatch):
    # An offline-mode server records its own (offline) UUID — confirmation
    # matches by name, and the file is left as the server wrote it (no
    # duplicate entry from a fallback file edit).
    sid = _running(client, engine, tmp_path)
    _register_notch(engine)
    offline = "f" * 32
    sent: list = []
    _patch_live(
        monkeypatch, sent, lambda cmd: pl.add(tmp_path, "whitelist", offline, "Notch")
    )

    resp = client.post(
        f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH}
    )
    assert [p["uuid"] for p in resp.json()["whitelist"]] == [offline]


def test_live_add_falls_back_to_file_edit(client, engine, tmp_path, monkeypatch):
    # The command never lands (e.g. the server can't resolve the name) → after
    # the confirmation timeout the file is edited directly, same as when the
    # server is stopped.
    sid = _running(client, engine, tmp_path)
    _register_notch(engine)
    monkeypatch.setattr("lectern.api.players._LIVE_APPLY_TIMEOUT", 0.2)
    sent: list = []
    _patch_live(monkeypatch, sent)  # commands recorded, nothing applied

    resp = client.post(
        f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH}
    )
    assert sent == ["whitelist add Notch"]
    assert [p["name"] for p in resp.json()["whitelist"]] == ["Notch"]


def test_playerlists_installed_guard(client):
    sid = client.post(
        "/api/servers", json={"name": "NI", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    assert client.get(f"/api/servers/{sid}/playerlists").status_code == 409


# --- online roster endpoints -------------------------------------------------


class _FakeProc:
    def __init__(self):
        from lectern.servers.roster import Roster

        self.roster = Roster()
        self.msmp = None  # no management client (pre-1.21.9 / proxy)


class _FakeMsmp:
    """Records management calls; a kick removes the player like the server's
    ``players/left`` notification would."""

    def __init__(self, proc: _FakeProc, connected: bool, fail: bool = False):
        self.proc = proc
        self.connected = connected
        self.fail = fail
        self.calls: list = []
        self.on_call = None  # (method, params) -> None: the server's file rewrite
        self.op_level: object = 4  # the server's op-permission-level

    async def call(self, method, params=None, timeout=5.0):
        from lectern.servers.msmp import M_KICK, M_OP_LEVEL, MsmpError

        self.calls.append((method, params))
        if self.fail:
            raise MsmpError("Method not found", -32601)
        if method == M_OP_LEVEL:
            return self.op_level
        if method != M_KICK:
            # A list mutation: answer with the resulting list, which holds
            # the added entries and no longer the removed ones.
            if self.on_call is not None:
                self.on_call(method, params)
            return [] if method.endswith("/remove") else params[0]
        for entry in params[0]:
            self.proc.roster.left(entry["name"])
        return params[0]


def _proc_with(monkeypatch, *joins) -> _FakeProc:
    """A fake running process whose roster contains the given players."""
    from lectern.servers.manager import manager

    proc = _FakeProc()
    prefix = "[12:00:00] [Server thread/INFO]: "
    for name, bot in joins:
        conn = "local" if bot else "/192.0.2.7:1"
        proc.roster.feed(f"{prefix}{name}[{conn}] logged in with entity id 1 at (0, 0, 0)")
        proc.roster.feed(f"{prefix}{name} joined the game")
    monkeypatch.setattr(manager, "get_process", lambda sid: proc)
    return proc


def test_online_players_lists_bots(client, engine, tmp_path, monkeypatch):
    sid = _installed(client, engine, tmp_path)
    _proc_with(monkeypatch, ("Notch", False), ("bot_farm", True))

    body = client.get(f"/api/servers/{sid}/players/online").json()
    assert [(p["name"], p["bot"]) for p in body] == [("Notch", False), ("bot_farm", True)]
    assert body[1]["uuid"] is None


def test_online_players_empty_when_stopped(client, engine, tmp_path):
    sid = _installed(client, engine, tmp_path)
    assert client.get(f"/api/servers/{sid}/players/online").json() == []
    assert client.get("/api/servers/nope/players/online").status_code == 404


def test_kick_sends_command_and_updates_roster(client, engine, tmp_path, monkeypatch):
    from lectern.servers.manager import manager

    sid = _installed(client, engine, tmp_path)
    proc = _proc_with(monkeypatch, ("Notch", False), ("bot_farm", True))
    sent: list = []

    async def send(server_id, command):
        sent.append(command)
        name = command.split()[1]
        proc.roster.feed(f"[12:00:00] [Server thread/INFO]: {name} left the game")

    monkeypatch.setattr(manager, "send", send)

    # Bots can't be kicked (no real connection) — Carpet's kill command is used.
    body = client.post(
        f"/api/servers/{sid}/players/online/bot_farm/kick", json={}
    ).json()
    assert sent == ["player bot_farm kill"]
    assert [p["name"] for p in body] == ["Notch"]

    # Reason is passed through, newlines stripped (no command injection).
    client.post(
        f"/api/servers/{sid}/players/online/Notch/kick",
        json={"reason": "bye\nstop evil"},
    )
    assert sent[-1] == "kick Notch bye stop evil"


def _kick_setup(client, engine, tmp_path, monkeypatch, connected=True, fail=False):
    from lectern.servers.manager import manager

    sid = _installed(client, engine, tmp_path)
    proc = _proc_with(monkeypatch, ("Notch", False), ("bot_farm", True))
    proc.msmp = _FakeMsmp(proc, connected, fail)
    sent: list = []

    async def send(server_id, command):
        sent.append(command)
        name = command.split()[1]
        proc.roster.feed(f"[12:00:00] [Server thread/INFO]: {name} left the game")

    monkeypatch.setattr(manager, "send", send)
    return sid, proc, sent


def test_kick_routes_to_msmp_for_players_and_carpet_for_bots(
    client, engine, tmp_path, monkeypatch
):
    sid, proc, sent = _kick_setup(client, engine, tmp_path, monkeypatch)
    body = client.post(f"/api/servers/{sid}/players/online/bot_farm/kick", json={}).json()
    assert sent == ["player bot_farm kill"]
    assert proc.msmp.calls == []
    assert [p["name"] for p in body] == ["Notch"]

    body = client.post(f"/api/servers/{sid}/players/online/Notch/kick").json()
    assert proc.msmp.calls == [("minecraft:players/kick", [[{"name": "Notch"}]])]
    assert sent == ["player bot_farm kill"]  # nothing more through the console
    assert body == []


def test_kick_with_reason_uses_console(client, engine, tmp_path, monkeypatch):
    sid, proc, sent = _kick_setup(client, engine, tmp_path, monkeypatch)
    client.post(f"/api/servers/{sid}/players/online/Notch/kick", json={"reason": "bye"})
    assert sent == ["kick Notch bye"]
    assert proc.msmp.calls == []


def test_kick_falls_back_to_console_when_msmp_disconnected(
    client, engine, tmp_path, monkeypatch
):
    sid, proc, sent = _kick_setup(client, engine, tmp_path, monkeypatch, connected=False)
    body = client.post(f"/api/servers/{sid}/players/online/Notch/kick", json={}).json()
    assert sent == ["kick Notch"]
    assert proc.msmp.calls == []
    assert [p["name"] for p in body] == ["bot_farm"]


def test_kick_falls_back_when_msmp_call_fails(client, engine, tmp_path, monkeypatch):
    sid, proc, sent = _kick_setup(client, engine, tmp_path, monkeypatch, fail=True)
    body = client.post(f"/api/servers/{sid}/players/online/Notch/kick", json={}).json()
    assert proc.msmp.calls == [("minecraft:players/kick", [[{"name": "Notch"}]])]
    assert sent == ["kick Notch"]
    assert [p["name"] for p in body] == ["bot_farm"]


def test_kick_reports_send_failure(client, engine, tmp_path, monkeypatch):
    from lectern.servers.manager import NotRunning, manager

    sid, _proc, _sent = _kick_setup(client, engine, tmp_path, monkeypatch, connected=False)

    async def send(server_id, command):
        raise NotRunning("Server is not running")

    monkeypatch.setattr(manager, "send", send)
    resp = client.post(f"/api/servers/{sid}/players/online/Notch/kick", json={})
    assert resp.status_code == 409


def test_kick_guards(client, engine, tmp_path, monkeypatch):
    sid = _installed(client, engine, tmp_path)
    # Not running → 409.
    assert (
        client.post(f"/api/servers/{sid}/players/online/Notch/kick", json={}).status_code
        == 409
    )
    _proc_with(monkeypatch, ("Notch", False))
    # Not online → 404 (also keeps arbitrary names out of the console).
    assert (
        client.post(f"/api/servers/{sid}/players/online/ghost/kick", json={}).status_code
        == 404
    )


# --- avatars ----------------------------------------------------------------


def _skin_png(face_rgba=(255, 0, 0, 255)) -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for x in range(8, 16):
        for y in range(8, 16):
            img.putpixel((x, y), face_rgba)  # the face region
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_render_face_crops_and_scales():
    import io

    from PIL import Image

    from lectern.providers.avatars import _render_face

    png = _render_face(_skin_png((10, 200, 30, 255)), size=32)
    out = Image.open(io.BytesIO(png))
    assert out.size == (32, 32)
    assert out.convert("RGBA").getpixel((0, 0))[:3] == (10, 200, 30)  # face color


def test_avatar_endpoint(client, monkeypatch):
    from lectern.providers import avatars

    async def fake_face(uuid, size):
        return b"PNGBYTES" if uuid == NOTCH else None

    monkeypatch.setattr(avatars, "face_png", fake_face)

    resp = client.get(f"/api/players/{NOTCH}/avatar?size=64")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"PNGBYTES"

    # A player with no skin → 404 (UI falls back to a tile).
    assert client.get("/api/players/ffffffffffffffffffffffffffffffff/avatar").status_code == 404


def test_kick_accepts_msmp_reply_with_other_case(client, engine, tmp_path, monkeypatch):
    """The kick reply names the kicked players; Minecraft names are
    case-insensitive, so ``notch`` in the reply confirms kicking ``Notch``."""
    sid, proc, sent = _kick_setup(client, engine, tmp_path, monkeypatch)

    async def call(method, params=None, timeout=5.0):
        proc.msmp.calls.append((method, params))
        proc.roster.left("Notch")
        return [{"id": "x", "name": "notch"}, "junk"]

    proc.msmp.call = call
    client.post(f"/api/servers/{sid}/players/online/Notch/kick", json={})
    assert sent == []
    assert len(proc.msmp.calls) == 1


def test_kick_falls_back_when_msmp_kicks_nobody(client, engine, tmp_path, monkeypatch):
    """``minecraft:players/kick`` answers with the players it kicked; an empty
    reply is not success, so the console ``kick`` runs and the player goes."""
    sid, proc, sent = _kick_setup(client, engine, tmp_path, monkeypatch)

    async def call(method, params=None, timeout=5.0):
        proc.msmp.calls.append((method, params))
        return []

    proc.msmp.call = call
    body = client.post(f"/api/servers/{sid}/players/online/Notch/kick", json={}).json()
    assert proc.msmp.calls == [("minecraft:players/kick", [[{"name": "Notch"}]])]
    assert sent == ["kick Notch"]
    assert [p["name"] for p in body] == ["bot_farm"]


# --- player lists through the management protocol (routing, fallbacks) ---------


def _list_setup(client, engine, tmp_path, monkeypatch, *, connected=True, fail=False,
                on_send=None, status="running"):
    """A server with a fake process whose management client is a stub; the
    console is recorded (and applied with ``on_send``)."""
    from lectern.servers.manager import manager

    sid = _running(client, engine, tmp_path)
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.status = status
        s.add(srv)
        s.commit()
    _register_notch(engine)
    proc = _FakeProc()
    proc.msmp = _FakeMsmp(proc, connected, fail)
    monkeypatch.setattr(manager, "get_process", lambda server_id: proc)
    sent: list = []
    _patch_live(monkeypatch, sent, on_send)
    return sid, proc.msmp, sent


def test_list_add_prefers_protocol_when_connected(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    stub.on_call = lambda method, params: pl.add(tmp_path, "whitelist", NOTCH, "Notch")
    resp = client.post(f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH})
    assert resp.status_code == 200, resp.text
    assert stub.calls == [("minecraft:allowlist/add", [[{"name": "Notch"}]])]
    assert sent == []
    assert [p["name"] for p in resp.json()["whitelist"]] == ["Notch"]


def _op_add(level: int) -> tuple:
    return ("minecraft:operators/add", [[{
        "player": {"name": "Notch"}, "permissionLevel": level, "bypassesPlayerLimit": False,
    }]])


def test_list_add_ops_grants_the_servers_op_level(client, engine, tmp_path, monkeypatch):
    """A protocol operator gets the level the console ``op`` would grant: the
    running server's ``op-permission-level``, read through the protocol, not
    a fixed 4 nor the file (which may have been edited since it started)."""
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    (tmp_path / "server.properties").write_text("op-permission-level=3\n")
    stub.op_level = 2
    resp = client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert resp.status_code == 200, resp.text
    assert stub.calls == [("minecraft:serversettings/operator_user_permission_level", []), _op_add(2)]
    assert sent == []


@pytest.mark.parametrize(("answer", "expected"), [(9, 4), (-1, 0), (True, 3), ("2", 3)])
def test_list_add_ops_level_is_clamped_or_taken_from_the_file(
    client, engine, tmp_path, monkeypatch, answer, expected
):
    """An out-of-range level is clamped to 0..4; an answer that is not a level
    (a bool, a string) falls back to the file's ``op-permission-level``."""
    sid, stub, _sent = _list_setup(client, engine, tmp_path, monkeypatch)
    (tmp_path / "server.properties").write_text("op-permission-level=3\n")
    stub.op_level = answer
    client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert stub.calls[-1] == _op_add(expected)


def test_list_add_ops_level_read_failing_uses_the_file(client, engine, tmp_path, monkeypatch):
    sid, stub, _sent = _list_setup(client, engine, tmp_path, monkeypatch)
    (tmp_path / "server.properties").write_text("op-permission-level=1\n")

    async def call(method, params=None, timeout=5.0):
        from lectern.servers.msmp import M_OP_LEVEL, MsmpError

        stub.calls.append((method, params))
        if method == M_OP_LEVEL:
            raise MsmpError("Method not found", -32601)
        return params[0]

    stub.call = call
    client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert stub.calls[-1] == _op_add(1)


@pytest.mark.parametrize(("line", "expected"), [
    ("op-permission-level=2\n", 2),
    ("op-permission-level=7\n", 4),
    ("op-permission-level=high\n", 4),
    ("", 4),
])
def test_ops_file_fallback_writes_the_files_op_level(
    client, engine, tmp_path, monkeypatch, line, expected
):
    """A stopped server: the ops.json entry carries the level ``op`` would
    grant at the next start (the file's ``op-permission-level``)."""
    sid, _stub, _sent = _list_setup(client, engine, tmp_path, monkeypatch, status="stopped")
    (tmp_path / "server.properties").write_text(line)
    resp = client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert resp.status_code == 200, resp.text
    assert json.loads((tmp_path / "ops.json").read_text())[0]["level"] == expected


def test_ops_add_survives_an_unreadable_properties_file(client, engine, tmp_path, monkeypatch):
    """server.properties unreadable (a directory here) or symlinked out of
    the server directory: operators get Minecraft's default level and the
    change still goes through."""
    sid, _stub, _sent = _list_setup(client, engine, tmp_path, monkeypatch, status="stopped")
    (tmp_path / "server.properties").mkdir()
    resp = client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert resp.status_code == 200, resp.text
    assert json.loads((tmp_path / "ops.json").read_text())[0]["level"] == 4
    (tmp_path / "server.properties").rmdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.properties"
    outside.write_text("op-permission-level=1\n")
    (tmp_path / "server.properties").symlink_to(outside)
    (tmp_path / "ops.json").unlink()
    resp = client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert resp.status_code == 200, resp.text
    assert json.loads((tmp_path / "ops.json").read_text())[0]["level"] == 4


def test_list_add_disconnected_client_uses_console(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(
        client, engine, tmp_path, monkeypatch, connected=False,
        on_send=lambda cmd: pl.add(tmp_path, "whitelist", NOTCH, "Notch"),
    )
    resp = client.post(f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH})
    assert stub.calls == []
    assert sent == ["whitelist add Notch"]
    assert [p["name"] for p in resp.json()["whitelist"]] == ["Notch"]


def test_ban_protocol_error_falls_to_console_then_file(client, engine, tmp_path, monkeypatch):
    """Every rung fails in turn: the JSON-RPC error sends the console ``ban``
    with the reason on one line, which never lands, so the file is edited
    with the reason and the expiry."""
    monkeypatch.setattr("lectern.api.players._LIVE_APPLY_TIMEOUT", 0.2)
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch, fail=True)
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned",
        json={"uuid": NOTCH, "reason": "bye\r\nstop ", "expires": "2099-01-01T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    assert stub.calls == [("minecraft:bans/add", [[{
        "player": {"name": "Notch"}, "source": "Lectern", "reason": "bye  stop",
        "expires": "2099-01-01T00:00:00Z",
    }]])]
    assert sent == ["ban Notch bye  stop"]
    [entry] = json.loads((tmp_path / "banned-players.json").read_text())
    assert (entry["reason"], entry["expires"]) == ("bye  stop", "2099-01-01 00:00:00 +0000")
    assert [p["name"] for p in resp.json()["banned"]] == ["Notch"]


def test_list_add_unconfirmed_reply_uses_console(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(
        client, engine, tmp_path, monkeypatch,
        on_send=lambda cmd: pl.add(tmp_path, "ops", NOTCH, "Notch"),
    )

    async def call(method, params=None, timeout=5.0):
        stub.calls.append((method, params))
        return [{"player": {"id": "x", "name": "someone"}}, "junk"]

    stub.call = call
    resp = client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert [method for method, _ in stub.calls] == [
        "minecraft:serversettings/operator_user_permission_level", "minecraft:operators/add",
    ]
    assert sent == ["op Notch"]
    assert [p["name"] for p in resp.json()["ops"]] == ["Notch"]


def test_list_add_confirms_by_server_uuid(client, engine, tmp_path, monkeypatch):
    """A reply listing the player under another name casing but the
    registry UUID still confirms (no console fallback)."""
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)

    async def call(method, params=None, timeout=5.0):
        stub.calls.append((method, params))
        return [{"id": NOTCH_DASHED, "name": "Renamed"}]

    stub.call = call
    client.post(f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH})
    assert sent == []


def test_list_remove_sends_name_and_dashed_id(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    pl.add(tmp_path, "ops", NOTCH, "Notch")
    stub.on_call = lambda method, params: pl.remove(tmp_path, "ops", NOTCH)
    resp = client.delete(f"/api/servers/{sid}/playerlists/ops/{NOTCH}")
    assert stub.calls == [
        ("minecraft:operators/remove", [[{"name": "Notch", "id": NOTCH_DASHED}]])
    ]
    assert sent == []
    assert resp.json()["ops"] == []


def test_list_remove_still_listed_falls_back(client, engine, tmp_path, monkeypatch):
    """A remove reply that still lists the player is not success: the console
    ``pardon`` runs."""
    sid, stub, sent = _list_setup(
        client, engine, tmp_path, monkeypatch,
        on_send=lambda cmd: pl.remove(tmp_path, "banned", NOTCH),
    )
    pl.add(tmp_path, "banned", NOTCH, "Notch")

    async def call(method, params=None, timeout=5.0):
        stub.calls.append((method, params))
        return [{"player": {"id": NOTCH_DASHED, "name": "Notch"}}]

    stub.call = call
    resp = client.delete(f"/api/servers/{sid}/playerlists/banned/{NOTCH}")
    assert sent == ["pardon Notch"]
    assert resp.json()["banned"] == []


def test_list_remove_absent_makes_no_call(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.delete(f"/api/servers/{sid}/playerlists/whitelist/{NOTCH}")
    assert resp.status_code == 200
    assert stub.calls == [] and sent == []
    assert json.loads((tmp_path / "whitelist.json").read_text()) == []


@pytest.mark.parametrize("status", ["stopped", "starting", "stopping"])
def test_list_add_not_running_edits_file_only(client, engine, tmp_path, monkeypatch, status):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch, status=status)
    resp = client.post(f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH})
    assert resp.status_code == 200
    assert stub.calls == [] and sent == []
    assert pl.read_list(tmp_path, "whitelist") == [{"uuid": NOTCH, "name": "Notch"}]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"expires": "2000-01-01T00:00:00Z"}, "expires must be in the future"),
        ({"expires": "soon"}, None),
        # Parsed fine, but past the last instant once converted to UTC.
        ({"expires": "9999-12-31T23:59:59-01:00"}, "expires is out of range"),
    ],
)
def test_ban_bad_expiry_is_422_without_side_effects(
    client, engine, tmp_path, monkeypatch, body, detail
):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned", json={"uuid": NOTCH, **body}
    )
    assert resp.status_code == 422
    if detail is not None:
        assert resp.json()["detail"] == detail
    assert stub.calls == [] and sent == []
    assert not (tmp_path / "banned-players.json").exists()


def test_reason_and_expiry_ignored_on_other_lists(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.post(
        f"/api/servers/{sid}/playerlists/whitelist",
        json={"uuid": NOTCH, "reason": "x", "expires": "2000-01-01T00:00:00Z"},
    )
    assert resp.status_code == 200
    assert stub.calls == [("minecraft:allowlist/add", [[{"name": "Notch"}]])]
    assert sent == []


def test_ban_reason_over_the_cap_is_422_without_side_effects(
    client, engine, tmp_path, monkeypatch
):
    """The cap is the ban list's: a 201-character reason (after trimming) on
    a ban is 422 with no call, no command and no file."""
    from lectern.api.players import BAN_REASON_MAX

    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned",
        json={"uuid": NOTCH, "reason": "r" * (BAN_REASON_MAX + 1)},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"reason is longer than {BAN_REASON_MAX} characters"
    assert stub.calls == [] and sent == []
    assert not (tmp_path / "banned-players.json").exists()

    # Surrounding blanks do not count: the trimmed reason is what is sent.
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned",
        json={"uuid": NOTCH, "reason": f"  {'r' * BAN_REASON_MAX}\n"},
    )
    assert resp.status_code == 200, resp.text
    [(method, [[ban]])] = stub.calls
    assert method == "minecraft:bans/add" and ban["reason"] == "r" * BAN_REASON_MAX


def test_long_reason_ignored_on_other_lists(client, engine, tmp_path, monkeypatch):
    """Outside the ban list the reason is ignored whole, cap included."""
    from lectern.api.players import BAN_REASON_MAX

    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.post(
        f"/api/servers/{sid}/playerlists/whitelist",
        json={"uuid": NOTCH, "reason": "r" * (BAN_REASON_MAX + 1)},
    )
    assert resp.status_code == 200, resp.text
    assert stub.calls == [("minecraft:allowlist/add", [[{"name": "Notch"}]])]
    assert sent == []


def test_unknown_kind_or_player_makes_no_call(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    assert client.post(
        f"/api/servers/{sid}/playerlists/admins", json={"uuid": NOTCH}
    ).status_code == 404
    assert client.post(
        f"/api/servers/{sid}/playerlists/banned", json={"uuid": "f" * 32}
    ).status_code == 404
    assert stub.calls == [] and sent == []


def test_sanitize_reason():
    from lectern.api.players import _sanitize_reason

    assert _sanitize_reason(None) is None
    assert _sanitize_reason(" \r\n ") is None
    assert _sanitize_reason(" a\nb\rc ") == "a b c"


def test_list_remove_one_of_two_same_name_rows_keeps_the_other(
    client, engine, tmp_path, monkeypatch
):
    """Challenger: two whitelist rows named Notch, e.g. the registry UUID a
    stopped-server file edit wrote and the offline UUID the server itself
    added later. Deleting the registry row: the protocol removes it by id and
    answers with the other row, which still carries the name. That is a
    confirmed removal; treating the name match as "still listed" sends the
    console ``whitelist remove Notch``, which deletes the second row too."""
    other = "5b6b3b8e00003000800000000000000a"
    sid, stub, sent = _list_setup(
        client, engine, tmp_path, monkeypatch,
        on_send=lambda cmd: pl.remove(tmp_path, "whitelist", other),
    )
    pl.add(tmp_path, "whitelist", NOTCH, "Notch")
    pl.add(tmp_path, "whitelist", other, "Notch")

    async def call(method, params=None, timeout=5.0):
        stub.calls.append((method, params))
        pl.remove(tmp_path, "whitelist", NOTCH)  # the server removes by id
        return [{"id": mojang.dash_uuid(other), "name": "Notch"}]

    stub.call = call
    resp = client.delete(f"/api/servers/{sid}/playerlists/whitelist/{NOTCH}")
    assert resp.status_code == 200
    assert stub.calls == [
        ("minecraft:allowlist/remove", [[{"name": "Notch", "id": NOTCH_DASHED}]])
    ]
    assert sent == []
    assert resp.json()["whitelist"] == [{"uuid": other, "name": "Notch"}]


def test_lists_still_hold_matches_by_id_then_name_without_id():
    """Removal is judged by id: a same-name row with another id is a
    different entry; only an id-less entry is matched by name."""
    from lectern.api.players import _lists_still_hold

    other = mojang.dash_uuid("5b6b3b8e00003000800000000000000a")
    assert not _lists_still_hold([{"name": "Notch", "id": other}], "Notch", NOTCH)
    assert _lists_still_hold([{"name": "x", "id": NOTCH_DASHED}], "Notch", NOTCH)
    assert _lists_still_hold([{"player": {"name": "notch"}}], "Notch", NOTCH)
    assert not _lists_still_hold(["junk", {"name": "Other"}], "Notch", NOTCH)


# --- QA: reads, missing client, body variants, boundaries, remove fallbacks ----


def test_qa_get_lists_reads_files_never_the_protocol(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    pl.add(tmp_path, "banned", NOTCH, "Notch", reason="grief")
    resp = client.get(f"/api/servers/{sid}/playerlists")
    assert resp.status_code == 200
    assert resp.json()["banned"] == [{"uuid": NOTCH, "name": "Notch"}]
    assert stub.calls == [] and sent == []


@pytest.mark.parametrize("process", ["none", "no-client"])
def test_qa_list_add_without_client_uses_console(
    client, engine, tmp_path, monkeypatch, process
):
    from lectern.servers.manager import manager

    sid, stub, sent = _list_setup(
        client, engine, tmp_path, monkeypatch,
        on_send=lambda cmd: pl.add(tmp_path, "ops", NOTCH, "Notch"),
    )
    monkeypatch.setattr(
        manager, "get_process", lambda server_id: None if process == "none" else _FakeProc()
    )
    resp = client.post(f"/api/servers/{sid}/playerlists/ops", json={"uuid": NOTCH})
    assert resp.status_code == 200, resp.text
    assert stub.calls == []
    assert sent == ["op Notch"]
    assert [p["name"] for p in resp.json()["ops"]] == ["Notch"]


def test_qa_list_add_accepts_a_dashed_uuid(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    stub.on_call = lambda method, params: pl.add(tmp_path, "whitelist", NOTCH, "Notch")
    resp = client.post(
        f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH_DASHED}
    )
    assert resp.status_code == 200, resp.text
    assert stub.calls == [("minecraft:allowlist/add", [[{"name": "Notch"}]])]
    assert sent == []


def test_qa_blank_ban_reason_is_omitted_on_the_protocol(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned", json={"uuid": NOTCH, "reason": " \r\n "}
    )
    assert resp.status_code == 200, resp.text
    assert stub.calls == [
        ("minecraft:bans/add", [[{"player": {"name": "Notch"}, "source": "Lectern"}]])
    ]
    assert sent == []


def test_qa_naive_ban_expiry_is_sent_as_utc(client, engine, tmp_path, monkeypatch):
    sid, stub, _sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned",
        json={"uuid": NOTCH, "expires": "2099-06-01T12:30:45"},
    )
    assert resp.status_code == 200, resp.text
    [(_m, [[ban]])] = stub.calls
    assert ban["expires"] == "2099-06-01T12:30:45Z"


def test_qa_ban_expiry_boundary(client, engine, tmp_path, monkeypatch):
    """A second ago is the past (422, no side effect); an hour ahead is fine."""
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned", json={"uuid": NOTCH, "expires": past}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "expires must be in the future"
    assert stub.calls == [] and sent == []
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned", json={"uuid": NOTCH, "expires": future}
    )
    assert resp.status_code == 200, resp.text


def test_qa_stopped_ban_stores_reason_and_expiry(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch, status="stopped")
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned",
        json={"uuid": NOTCH, "reason": "grief\nagain", "expires": "2099-01-01T01:00:00+01:00"},
    )
    assert resp.status_code == 200, resp.text
    assert stub.calls == [] and sent == []
    [entry] = json.loads((tmp_path / "banned-players.json").read_text())
    assert entry["reason"] == "grief again"
    assert entry["expires"] == "2099-01-01 00:00:00 +0000"
    assert entry["source"] == "Lectern"


def test_qa_console_ban_carries_reason_but_no_expiry(client, engine, tmp_path, monkeypatch):
    sid, _stub, sent = _list_setup(
        client, engine, tmp_path, monkeypatch, connected=False,
        on_send=lambda cmd: pl.add(tmp_path, "banned", NOTCH, "Notch"),
    )
    resp = client.post(
        f"/api/servers/{sid}/playerlists/banned",
        json={"uuid": NOTCH, "reason": "bye\nstop", "expires": "2099-01-01T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    assert sent == ["ban Notch bye stop"]


def test_qa_protocol_reply_not_a_list_falls_back(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(
        client, engine, tmp_path, monkeypatch,
        on_send=lambda cmd: pl.add(tmp_path, "whitelist", NOTCH, "Notch"),
    )

    async def call(method, params=None, timeout=5.0):
        stub.calls.append((method, params))
        return True

    stub.call = call
    resp = client.post(f"/api/servers/{sid}/playerlists/whitelist", json={"uuid": NOTCH})
    assert resp.status_code == 200
    assert sent == ["whitelist add Notch"]


@pytest.mark.parametrize(
    ("kind", "method", "command"),
    [
        ("whitelist", "minecraft:allowlist/remove", "whitelist remove Notch"),
        ("ops", "minecraft:operators/remove", "deop Notch"),
        ("banned", "minecraft:bans/remove", "pardon Notch"),
    ],
)
def test_qa_remove_protocol_error_then_console_timeout_edits_file(
    client, engine, tmp_path, monkeypatch, kind, method, command
):
    monkeypatch.setattr("lectern.api.players._LIVE_APPLY_TIMEOUT", 0.2)
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch, fail=True)
    pl.add(tmp_path, kind, NOTCH, "Notch")
    resp = client.delete(f"/api/servers/{sid}/playerlists/{kind}/{NOTCH}")
    assert resp.status_code == 200, resp.text
    assert stub.calls == [(method, [[{"name": "Notch", "id": NOTCH_DASHED}]])]
    assert sent == [command]
    assert resp.json()[kind] == []
    assert pl.read_list(tmp_path, kind) == []


def test_qa_remove_unknown_kind_makes_no_call(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch)
    resp = client.delete(f"/api/servers/{sid}/playerlists/admins/{NOTCH}")
    assert resp.status_code == 404
    assert stub.calls == [] and sent == []


def test_qa_remove_while_stopping_edits_file_only(client, engine, tmp_path, monkeypatch):
    sid, stub, sent = _list_setup(client, engine, tmp_path, monkeypatch, status="stopping")
    pl.add(tmp_path, "whitelist", NOTCH, "Notch")
    resp = client.delete(f"/api/servers/{sid}/playerlists/whitelist/{NOTCH}")
    assert resp.status_code == 200
    assert stub.calls == [] and sent == []
    assert pl.read_list(tmp_path, "whitelist") == []
