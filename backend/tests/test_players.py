"""Players: registry (Mojang-validated) + per-server whitelist/ops/banned files."""

from __future__ import annotations

import json

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
