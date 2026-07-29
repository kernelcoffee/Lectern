"""server.properties parsing/validation (unit) + properties/settings endpoints.

The API tests create a record through the client, then point ``Server.path`` at
a tmp dir directly through the test engine — the real install pipeline is a
background task that doesn't run against the test DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from lectern.models import Server
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
