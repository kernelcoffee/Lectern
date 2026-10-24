"""server.properties parsing/validation (unit) + properties/settings endpoints.

The API tests create a record through the client, then point ``Server.path`` at
a tmp dir directly through the test engine — the real install pipeline is a
background task that doesn't run against the test DB.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlmodel import Session

from lectern.models import Server
from lectern.servers import properties as props
from lectern.servers.properties import (
    normalize_value,
    parse_properties,
    read_properties,
    render_properties,
    write_properties,
)

# --- unit: parsing / rendering ---------------------------------------------


def test_parse_skips_comments_and_blanks():
    text = "#comment\n!also comment\n\nmotd=Hello World\npvp = true \nbroken-line\n"
    assert parse_properties(text) == {"motd": "Hello World", "pvp": "true"}


def test_parse_keeps_equals_in_value():
    assert parse_properties("motd=a=b=c\n") == {"motd": "a=b=c"}


def test_render_round_trip_sorted(tmp_path: Path):
    props = {"b": "2", "a": "1"}
    write_properties(tmp_path, props)
    assert (tmp_path / "server.properties").read_text() == "a=1\nb=2\n"
    assert read_properties(tmp_path) == props


def test_read_missing_file_returns_empty(tmp_path: Path):
    assert read_properties(tmp_path) == {}


# --- unit: typed validation --------------------------------------------------


def test_normalize_boolean():
    assert normalize_value("pvp", True) == "true"
    assert normalize_value("pvp", "False") == "false"
    with pytest.raises(ValueError, match="pvp"):
        normalize_value("pvp", "yes")


def test_normalize_integer_bounds():
    assert normalize_value("view-distance", "10") == "10"
    with pytest.raises(ValueError, match="≥"):
        normalize_value("view-distance", 1)
    with pytest.raises(ValueError, match="≤"):
        normalize_value("view-distance", 99)
    with pytest.raises(ValueError, match="integer"):
        normalize_value("max-players", "lots")


def test_normalize_enum():
    assert normalize_value("difficulty", "hard") == "hard"
    with pytest.raises(ValueError, match="one of"):
        normalize_value("difficulty", "impossible")


def test_unknown_key_is_free_form():
    # Modded servers add their own keys — accepted verbatim.
    assert normalize_value("my-mod-option", "whatever") == "whatever"
    assert normalize_value("my-mod-flag", True) == "true"


# --- API helpers --------------------------------------------------------------


def _installed_server(client, engine, tmp_path: Path) -> str:
    """Create a record and mark it installed at ``tmp_path`` (bypassing the
    real install pipeline, which doesn't run against the test engine)."""
    server_id = client.post(
        "/api/servers", json={"name": "P", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(tmp_path)
        server.status = "stopped"
        session.add(server)
        session.commit()
    write_properties(tmp_path, {"motd": "hi", "server-port": "25565"})
    return server_id


# --- API: properties -----------------------------------------------------------


def test_properties_before_install_409(client):
    server_id = client.post(
        "/api/servers", json={"name": "X", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    assert client.get(f"/api/servers/{server_id}/properties").status_code == 409
    assert (
        client.patch(f"/api/servers/{server_id}/properties", json={"pvp": True}).status_code
        == 409
    )


def test_get_properties_returns_file_and_definitions(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    body = client.get(f"/api/servers/{server_id}/properties").json()
    assert body["properties"]["motd"] == "hi"
    # Definitions drive the UI widgets; spot-check the shape.
    assert body["definitions"]["difficulty"]["type"] == "enum"
    assert "hard" in body["definitions"]["difficulty"]["values"]
    assert body["definitions"]["view-distance"]["min"] == 3


def test_patch_properties_updates_file(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    resp = client.patch(
        f"/api/servers/{server_id}/properties",
        json={"pvp": False, "max-players": 10, "motd": None},
    )
    assert resp.status_code == 200
    on_disk = read_properties(tmp_path)
    assert on_disk["pvp"] == "false"
    assert on_disk["max-players"] == "10"
    assert "motd" not in on_disk  # null removes


def test_patch_properties_validation_lists_all_errors(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    resp = client.patch(
        f"/api/servers/{server_id}/properties",
        json={"difficulty": "impossible", "view-distance": 999},
    )
    assert resp.status_code == 422
    assert "difficulty" in resp.json()["detail"]
    assert "view-distance" in resp.json()["detail"]
    # Nothing was written.
    assert read_properties(tmp_path) == {"motd": "hi", "server-port": "25565"}


def test_patch_server_port_syncs_record(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    client.patch(f"/api/servers/{server_id}/properties", json={"server-port": 25570})
    assert client.get(f"/api/servers/{server_id}").json()["port"] == 25570


# --- API: Lectern-owned settings ------------------------------------------------


def test_patch_settings_partial_update(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    resp = client.patch(
        f"/api/servers/{server_id}",
        json={"memory_mb": 4096, "crash_restart": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["memory_mb"] == 4096
    assert body["crash_restart"] is True
    assert body["stop_command"] == "stop"  # untouched


def test_patch_settings_port_writes_properties_file(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    client.patch(f"/api/servers/{server_id}", json={"port": 25999})
    assert read_properties(tmp_path)["server-port"] == "25999"


def test_patch_settings_bounds(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    assert (
        client.patch(f"/api/servers/{server_id}", json={"memory_mb": 1}).status_code == 422
    )
    assert (
        client.patch(f"/api/servers/{server_id}", json={"port": 99999}).status_code == 422
    )


def test_patch_settings_missing_server_404(client):
    assert client.patch("/api/servers/nope", json={"memory_mb": 2048}).status_code == 404


def test_patch_server_port_to_used_port_allowed(client, engine, tmp_path):
    # Two servers may share a port (only one can run at a time); the properties
    # edit goes through and is written to the file.
    server_id = _installed_server(client, engine, tmp_path)
    client.post(
        "/api/servers", json={"name": "Other", "type": "vanilla", "mc_version": "1.21", "port": 25600}
    )
    resp = client.patch(
        f"/api/servers/{server_id}/properties", json={"server-port": 25600}
    )
    assert resp.status_code == 200
    from lectern.servers.properties import read_properties

    assert read_properties(tmp_path)["server-port"] == "25600"


# --- Java-properties escaping (MC writes via Properties.store()) --------------


def test_parse_unescapes_java_properties():
    text = "level-type=minecraft\\:normal\nmotd=caf\\u00E9 \\n2nd\nweird\\=key=v\n"
    props = parse_properties(text)
    assert props["level-type"] == "minecraft:normal"
    assert props["motd"] == "café \n2nd"
    assert props["weird=key"] == "v"


def test_parse_accepts_colon_separator():
    # Java properties allow ':' as separator; MC writes '=' but be liberal.
    assert parse_properties("key:value\n") == {"key": "value"}


def test_render_escapes_and_round_trips():
    props = {"level-type": "minecraft:normal", "motd": "café \n2nd", "a=b": "x"}
    rendered = render_properties(props)
    assert "level-type=minecraft\\:normal" in rendered
    assert "motd=caf\\u00E9 \\n2nd" in rendered
    assert "a\\=b=x" in rendered
    assert parse_properties(rendered) == props


def test_definitions_carry_defaults():
    from lectern.servers.properties import definitions_payload

    payload = definitions_payload()
    assert payload["allow-nether"]["default"] == "true"
    assert payload["allow-flight"]["default"] == "false"
    assert payload["level-seed"]["default"] is None


# --- live settings through the management protocol (stubbed client) -----------


class _Settings:
    """A management client stub: records ``serversettings`` sets and answers
    with the value sent, or fails the methods in ``fail``."""

    def __init__(self, connected: bool = True, fail: tuple[str, ...] = ()) -> None:
        self.connected = connected
        self.fail = fail
        self.calls: list = []

    async def call(self, method, params=None, timeout=5.0):
        from lectern.servers.msmp import MsmpError

        self.calls.append((method, params))
        if method in self.fail:
            raise MsmpError("boom", -32603)
        return params[0]


def _live_server(client, engine, tmp_path, monkeypatch, stub, status="running") -> str:
    from types import SimpleNamespace

    from lectern.servers.manager import manager

    server_id = _installed_server(client, engine, tmp_path)
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.status = status
        session.add(server)
        session.commit()
    monkeypatch.setattr(manager, "get_process", lambda sid: SimpleNamespace(msmp=stub))
    return server_id


def test_definitions_mark_live_keys(client, engine, tmp_path):
    server_id = _installed_server(client, engine, tmp_path)
    body = client.get(f"/api/servers/{server_id}/properties").json()
    assert body["applied_live"] == []
    live = {k for k, d in body["definitions"].items() if d["live"]}
    assert live == {
        "motd", "difficulty", "max-players", "view-distance", "white-list",
        "enforce-whitelist", "gamemode",
    }
    assert body["definitions"]["pvp"]["live"] is False


@pytest.mark.parametrize(
    ("status", "connected"), [("stopped", True), ("starting", True), ("running", False)]
)
def test_patch_without_live_client_writes_file(
    client, engine, tmp_path, monkeypatch, status, connected
):
    stub = _Settings(connected=connected)
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub, status=status)
    resp = client.patch(f"/api/servers/{server_id}/properties", json={"motd": "new"})
    assert resp.status_code == 200
    assert resp.json()["applied_live"] == []
    assert stub.calls == []
    assert read_properties(tmp_path)["motd"] == "new"


def test_patch_live_keys_are_not_written_by_lectern(client, engine, tmp_path, monkeypatch):
    """The stub does not rewrite the file the way the server does, so the file
    keeps the old live values: proof Lectern did not write them. The non-live
    key and the null removal are written."""
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    write_properties(tmp_path, {"motd": "hi", "difficulty": "easy", "pvp": "true"})
    resp = client.patch(
        f"/api/servers/{server_id}/properties",
        json={"motd": "x", "pvp": False, "difficulty": None, "max-players": "25"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied_live"] == ["motd", "max-players"]
    assert stub.calls == [
        ("minecraft:serversettings/motd/set", ["x"]),
        ("minecraft:serversettings/max_players/set", [25]),
    ]
    on_disk = read_properties(tmp_path)
    assert on_disk == {"motd": "hi", "pvp": "false"}
    assert body["properties"] == on_disk


def test_patch_failing_live_set_writes_the_file(client, engine, tmp_path, monkeypatch):
    stub = _Settings(fail=("minecraft:serversettings/difficulty/set",))
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    resp = client.patch(
        f"/api/servers/{server_id}/properties",
        json={"white-list": "true", "difficulty": "hard"},
    )
    assert resp.json()["applied_live"] == ["white-list"]
    assert stub.calls == [
        ("minecraft:serversettings/use_allowlist/set", [True]),
        ("minecraft:serversettings/difficulty/set", ["hard"]),
    ]
    assert read_properties(tmp_path)["difficulty"] == "hard"
    assert "white-list" not in read_properties(tmp_path)


def test_patch_live_validation_error_calls_nothing(client, engine, tmp_path, monkeypatch):
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    before = (tmp_path / "server.properties").read_text()
    resp = client.patch(
        f"/api/servers/{server_id}/properties", json={"motd": "x", "difficulty": "insane"}
    )
    assert resp.status_code == 422
    assert "difficulty must be one of" in resp.json()["detail"]
    assert stub.calls == []
    assert (tmp_path / "server.properties").read_text() == before


def test_patch_all_live_writes_nothing(client, engine, tmp_path, monkeypatch):
    from lectern.api import servers as api

    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    writes: list = []
    monkeypatch.setattr(api.props, "write_properties", lambda d, p: writes.append(p))
    resp = client.patch(
        f"/api/servers/{server_id}/properties",
        json={"gamemode": "creative", "view-distance": 12, "enforce-whitelist": False},
    )
    assert resp.json()["applied_live"] == ["gamemode", "view-distance", "enforce-whitelist"]
    assert [params for _m, params in stub.calls] == [["creative"], [12], [False]]
    assert writes == []


def test_live_value_types():
    from lectern.servers.properties import live_value

    assert live_value("max-players", "25") == 25
    assert live_value("white-list", "false") is False
    assert live_value("gamemode", "creative") == "creative"
    assert live_value("some-mod-key", "1") == "1"


def test_patch_properties_lock_is_not_kept(client, engine, tmp_path, monkeypatch):
    """Writers of one server's file take turns through a lock that lives only
    while someone holds or waits on it: nothing is left behind."""
    import gc

    server_id = _live_server(client, engine, tmp_path, monkeypatch, _Settings())
    resp = client.patch(f"/api/servers/{server_id}/properties", json={"motd": "x"})
    assert resp.status_code == 200
    assert resp.json()["applied_live"] == ["motd"]
    gc.collect()
    assert len(props._edit_locks) == 0


class _Gated(_Settings):
    """A set that holds until ``gate`` opens, like a lagging server."""

    schema_version = "3.1.0"

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def call(self, method, params=None, timeout=5.0):
        self.calls.append((method, params))
        self.entered.set()
        await self.gate.wait()
        return params[0]


async def _while_a_live_patch_waits(client, engine, tmp_path, monkeypatch, writer):
    """Run ``writer()`` while a live properties PATCH of the same server sits
    in its set; it must wait for that PATCH, then land on top of its write."""
    from lectern.api.servers import patch_properties

    stub = _Gated()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    props.write_properties(tmp_path, {"motd": "old", "server-port": "25565"})
    with Session(engine) as session:
        patch = asyncio.create_task(patch_properties(server_id, {"motd": "x"}, session))
        await stub.entered.wait()
        other = asyncio.create_task(writer(server_id))
        await asyncio.sleep(0.05)
        assert not other.done()  # waits on the lock the PATCH holds
        stub.gate.set()
        await patch
        await other
    return server_id


async def test_settings_port_waits_for_a_live_properties_patch(
    client, engine, tmp_path, monkeypatch
):
    """Without the shared lock the port write lands during the PATCH's set
    and the PATCH's merge (from its earlier read) puts the old port back."""
    from lectern.api.servers import update_server_settings
    from lectern.models import ServerSettingsUpdate

    async def change_port(server_id):
        with Session(engine) as session:
            await update_server_settings(server_id, ServerSettingsUpdate(port=25570), session)

    server_id = await _while_a_live_patch_waits(
        client, engine, tmp_path, monkeypatch, change_port
    )
    assert props.read_properties(tmp_path)["server-port"] == "25570"
    with Session(engine) as session:
        assert session.get(Server, server_id).port == 25570


async def test_resource_pack_prompt_waits_for_a_live_properties_patch(
    client, engine, tmp_path, monkeypatch
):
    from lectern.content import resourcepacks as rp

    async def set_pack(_server_id):
        await rp.set_server_resource_pack(tmp_path, url="http://lan/pack.zip", sha1="abc")

    await _while_a_live_patch_waits(client, engine, tmp_path, monkeypatch, set_pack)
    on_disk = props.read_properties(tmp_path)
    assert on_disk["resource-pack"] == "http://lan/pack.zip"
    assert on_disk["resource-pack-sha1"] == "abc"
    await rp.set_server_resource_pack(tmp_path, url="", sha1="")
    assert "resource-pack" not in props.read_properties(tmp_path)


async def test_edit_properties_sets_and_removes(tmp_path):
    props.write_properties(tmp_path, {"a": "1", "b": "2"})
    written = await props.edit_properties(tmp_path, {"a": "3", "b": None, "c": "4"})
    assert written == {"a": "3", "c": "4"}
    assert props.read_properties(tmp_path) == written


@pytest.mark.parametrize(
    ("file_props", "expected"),
    [({}, 4), ({"op-permission-level": "2"}, 2), ({"op-permission-level": "9"}, 4),
     ({"op-permission-level": "-3"}, 0), ({"op-permission-level": "x"}, 4)],
)
def test_op_permission_level(file_props, expected):
    assert props.op_permission_level(file_props) == expected


# --- QA: reads, removals, managed keys, symlinks, missing client, port mirror ---


def test_qa_get_on_connected_server_calls_nothing(client, engine, tmp_path, monkeypatch):
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    body = client.get(f"/api/servers/{server_id}/properties").json()
    assert body["applied_live"] == []
    assert body["definitions"]["motd"]["live"] is True
    assert stub.calls == []


def test_qa_null_on_a_live_key_removes_it_without_a_call(client, engine, tmp_path, monkeypatch):
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    resp = client.patch(f"/api/servers/{server_id}/properties", json={"motd": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_live"] == []
    assert stub.calls == []
    assert "motd" not in read_properties(tmp_path)
    assert "motd" not in resp.json()["properties"]


def test_qa_managed_key_with_live_key_is_422_without_call(client, engine, tmp_path, monkeypatch):
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.type, server.mc_version = "fabric", "26.3"
        session.add(server)
        session.commit()
    before = (tmp_path / "server.properties").read_text()
    resp = client.patch(
        f"/api/servers/{server_id}/properties",
        json={"motd": "x", "management-server-port": 1},
    )
    assert resp.status_code == 422
    assert "management-server-port is managed by Lectern" in resp.json()["detail"]
    assert stub.calls == []
    assert (tmp_path / "server.properties").read_text() == before


@pytest.mark.parametrize("body", [{"motd": "x"}, {"motd": "x", "pvp": False}])
def test_qa_symlinked_properties_is_400_before_any_call(
    client, engine, tmp_path, monkeypatch, body
):
    stub = _Settings()
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    server_id = _live_server(client, engine, server_dir, monkeypatch, stub)
    outside = tmp_path / "outside.properties"
    outside.write_text("motd=outside\n")
    (server_dir / "server.properties").unlink()
    (server_dir / "server.properties").symlink_to(outside)
    resp = client.patch(f"/api/servers/{server_id}/properties", json=body)
    assert resp.status_code == 400
    assert stub.calls == []
    assert outside.read_text() == "motd=outside\n"


@pytest.mark.parametrize("process", ["none", "no-client", "stopping"])
def test_qa_no_live_path_writes_the_file(client, engine, tmp_path, monkeypatch, process):
    from types import SimpleNamespace

    from lectern.servers.manager import manager

    stub = _Settings()
    status = "stopping" if process == "stopping" else "running"
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub, status=status)
    if process == "none":
        monkeypatch.setattr(manager, "get_process", lambda sid: None)
    elif process == "no-client":
        monkeypatch.setattr(manager, "get_process", lambda sid: SimpleNamespace(msmp=None))
    resp = client.patch(
        f"/api/servers/{server_id}/properties", json={"motd": "new", "difficulty": "hard"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_live"] == []
    assert stub.calls == []
    on_disk = read_properties(tmp_path)
    assert on_disk["motd"] == "new" and on_disk["difficulty"] == "hard"


def test_qa_non_live_keys_only_make_no_call(client, engine, tmp_path, monkeypatch):
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    resp = client.patch(
        f"/api/servers/{server_id}/properties", json={"pvp": False, "spawn-protection": 3}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_live"] == []
    assert stub.calls == []
    on_disk = read_properties(tmp_path)
    assert on_disk["pvp"] == "false" and on_disk["spawn-protection"] == "3"


def test_qa_live_patch_still_mirrors_the_port(client, engine, tmp_path, monkeypatch):
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    resp = client.patch(
        f"/api/servers/{server_id}/properties", json={"motd": "x", "server-port": 25571}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_live"] == ["motd"]
    assert read_properties(tmp_path)["server-port"] == "25571"
    with Session(engine) as session:
        assert session.get(Server, server_id).port == 25571


def test_qa_applied_live_follows_request_order(client, engine, tmp_path, monkeypatch):
    stub = _Settings()
    server_id = _live_server(client, engine, tmp_path, monkeypatch, stub)
    resp = client.patch(
        f"/api/servers/{server_id}/properties",
        json={"view-distance": 10, "pvp": True, "difficulty": "easy", "motd": "z"},
    )
    assert resp.json()["applied_live"] == ["view-distance", "difficulty", "motd"]
    assert [m for m, _ in stub.calls] == [
        "minecraft:serversettings/view_distance/set",
        "minecraft:serversettings/difficulty/set",
        "minecraft:serversettings/motd/set",
    ]
