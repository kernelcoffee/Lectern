"""Velocity proxy support: toml scaffold/round-trip, PaperMC parsing, and the
link/unlink endpoints with their backend side effects."""

from __future__ import annotations

import tomllib
from pathlib import Path

from sqlmodel import Session

from lectern.models import Server
from lectern.providers import papermc
from lectern.servers import properties as props
from lectern.servers import velocity

# --- minimal TOML writer ------------------------------------------------------


def test_toml_dumps_round_trips_velocity_shape():
    config = {
        "config-version": "2.7",
        "bind": "0.0.0.0:25577",
        "online-mode": True,
        "show-max-players": 500,
        "servers": {
            "main": "127.0.0.1:25601",
            "creative-2": "127.0.0.1:25602",
            "try": ["main", "creative-2"],
        },
        "forced-hosts": {"lobby.example.com": ["main"]},
    }
    parsed = tomllib.loads(velocity.toml_dumps(config))
    assert parsed == config


def test_toml_dumps_escapes_strings_and_keys():
    out = velocity.toml_dumps({'we"ird': 'va"lue\\path'})
    assert tomllib.loads(out) == {'we"ird': 'va"lue\\path'}


# --- scaffold + config edits ----------------------------------------------------


def test_initial_config_scaffold(tmp_path: Path):
    velocity.write_initial_config(tmp_path, port=25599)
    config = velocity.read_config(tmp_path)
    assert config["bind"] == "0.0.0.0:25599"
    assert config["player-info-forwarding-mode"] == "modern"
    assert config["servers"] == {"try": []}
    secret = (tmp_path / velocity.SECRET_NAME).read_text()
    assert len(secret) >= 24
    # Idempotent secret: a re-scaffold must not rotate it.
    velocity.write_initial_config(tmp_path, port=25599)
    assert (tmp_path / velocity.SECRET_NAME).read_text() == secret


def test_bind_port_edit(tmp_path: Path):
    velocity.write_initial_config(tmp_path, port=25565)
    velocity.set_bind_port(tmp_path, 30000)
    assert velocity.get_bind_port(tmp_path) == 30000


def test_links_round_trip(tmp_path: Path):
    velocity.write_initial_config(tmp_path, port=25565)
    velocity.write_links(
        tmp_path,
        {"main": "127.0.0.1:25601", "mini": "127.0.0.1:25602"},
        ["mini", "main", "ghost"],  # unknown names are dropped from try
    )
    assert velocity.linked_servers(tmp_path) == {
        "main": "127.0.0.1:25601",
        "mini": "127.0.0.1:25602",
    }
    assert velocity.read_config(tmp_path)["servers"]["try"] == ["mini", "main"]


def test_link_name_sanitizes():
    assert velocity.link_name("Main Server") == "main-server"
    assert velocity.link_name("épic! lobby") == "pic-lobby"
    assert velocity.link_name("???") == "server"


# --- PaperMC fill v3 parsing -----------------------------------------------------


def test_papermc_version_parsing():
    payload = {
        "versions": {
            "4.0.0": ["4.1.0-SNAPSHOT", "4.0.0", "4.0.0-SNAPSHOT"],
            "3.0.0": ["3.5.1", "3.5.0"],
        }
    }
    assert papermc.parse_release_versions(payload) == ["4.0.0", "3.5.1", "3.5.0"]


def test_papermc_build_download():
    build = {
        "downloads": {
            "server:default": {
                "name": "velocity-4.0.0-6.jar",
                "url": "https://example/velocity.jar",
                "checksums": {"sha256": "abc123"},
            }
        }
    }
    parsed = papermc.parse_build_download(build)
    assert parsed == {
        "url": "https://example/velocity.jar",
        "name": "velocity-4.0.0-6.jar",
        "sha256": "abc123",
    }


# --- proxy endpoints ---------------------------------------------------------------


def _mk_server(client, engine, tmp_path: Path, name, type_, port, scaffold=None) -> str:
    server_id = client.post(
        "/api/servers",
        json={"name": name, "type": type_, "mc_version": "26.2", "port": port},
    ).json()["id"]
    server_dir = tmp_path / server_id
    server_dir.mkdir()
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(server_dir)
        server.status = "stopped"
        session.add(server)
        session.commit()
    if scaffold == "proxy":
        velocity.write_initial_config(server_dir, port=port)
    elif scaffold == "mc":
        (server_dir / "server.properties").write_text(
            f"online-mode=true\nserver-port={port}\n"
        )
    return server_id


def test_proxy_link_unlink_flow(client, engine, tmp_path: Path):
    proxy_id = _mk_server(client, engine, tmp_path, "Proxy", "velocity", 25565, "proxy")
    backend_id = _mk_server(client, engine, tmp_path, "Main World", "vanilla", 25601, "mc")

    # Candidates listed, nothing linked yet; the proxy's stop command differs.
    payload = client.get(f"/api/servers/{proxy_id}/proxy").json()
    assert payload["links"] == []
    (candidate,) = payload["candidates"]
    assert candidate["server_id"] == backend_id
    assert candidate["forwarding"] == "none"
    assert client.get(f"/api/servers/{proxy_id}").json()["stop_command"] == "shutdown"

    # Link: velocity.toml registry + try order; vanilla gets a warning and
    # online-mode=false.
    resp = client.put(
        f"/api/servers/{proxy_id}/proxy", json={"server_ids": [backend_id]}
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["links"] == [
        {"name": "main-world", "address": "127.0.0.1:25601", "server_id": backend_id}
    ]
    assert payload["try"] == ["main-world"]
    assert any("offline UUIDs" in w for w in payload["warnings"])
    with Session(engine) as session:
        backend_dir = Path(session.get(Server, backend_id).path)
    assert props.read_properties(backend_dir)["online-mode"] == "false"

    # Unlink: registry emptied, online-mode restored.
    resp = client.put(f"/api/servers/{proxy_id}/proxy", json={"server_ids": []})
    assert resp.status_code == 200
    assert resp.json()["links"] == []
    assert props.read_properties(backend_dir)["online-mode"] == "true"


def test_proxy_endpoints_reject_non_proxy(client, engine, tmp_path: Path):
    backend_id = _mk_server(client, engine, tmp_path, "Plain", "vanilla", 25604, "mc")
    assert client.get(f"/api/servers/{backend_id}/proxy").status_code == 400


def test_proxy_link_installs_forwarding_mod(client, engine, tmp_path: Path, monkeypatch):
    from lectern.api import proxy as proxy_api

    proxy_id = _mk_server(client, engine, tmp_path, "P2", "velocity", 25566, "proxy")
    backend_id = _mk_server(client, engine, tmp_path, "Fab", "fabric", 25605, "mc")

    calls: list[dict] = []

    async def fake_install(session, server_id, server_dir, **kwargs):
        calls.append({"server_id": server_id, **kwargs})
        return []

    monkeypatch.setattr(proxy_api.content, "install", fake_install)
    resp = client.put(
        f"/api/servers/{proxy_id}/proxy", json={"server_ids": [backend_id]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []
    (call,) = calls
    assert call["project_id"] == "fabricproxy-lite"
    assert call["server_id"] == backend_id
    # The mod's config carries the proxy's forwarding secret.
    with Session(engine) as session:
        backend_dir = Path(session.get(Server, backend_id).path)
        proxy_dir = Path(session.get(Server, proxy_id).path)
    mod_config = tomllib.loads(
        (backend_dir / "config" / "FabricProxy-Lite.toml").read_text()
    )
    assert mod_config["secret"] == velocity.read_secret(proxy_dir)
    assert mod_config["hackOnlineMode"] is True


# --- proxy-first port policy ---------------------------------------------------


def test_suggest_is_kind_aware(client, engine, tmp_path: Path):
    # A game server on the public port first (the common pre-proxy state).
    _mk_server(client, engine, tmp_path, "World", "vanilla", 25565, "mc")

    # Proxies claim the public port regardless — linking moves the backend.
    proxy_suggestion = client.get("/api/servers/suggest?kind=proxy").json()
    assert proxy_suggestion["name"] == "New proxy"
    assert proxy_suggestion["port"] == 25565
    # Proxies are lightweight — they get their own (small) memory default.
    assert proxy_suggestion["memory_mb"] == 512
    assert client.get("/api/servers/suggest").json()["memory_mb"] == 2048

    # Without a proxy, game suggestions keep the historical behavior.
    assert client.get("/api/servers/suggest").json()["port"] == 25566

    # Once a proxy exists, game servers get clearly-internal ports and never
    # a proxy's port.
    _mk_server(client, engine, tmp_path, "Gate", "velocity", 25565, "proxy")
    game_suggestion = client.get("/api/servers/suggest?kind=game").json()
    assert game_suggestion["port"] == 25600
    # A second proxy skips the first proxy's port.
    assert client.get("/api/servers/suggest?kind=proxy").json()["port"] == 25566


def test_link_moves_backend_off_proxy_port(client, engine, tmp_path: Path):
    proxy_id = _mk_server(client, engine, tmp_path, "Front", "velocity", 25565, "proxy")
    backend_id = _mk_server(client, engine, tmp_path, "Old World", "vanilla", 25565, "mc")

    resp = client.put(
        f"/api/servers/{proxy_id}/proxy", json={"server_ids": [backend_id]}
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    (move,) = payload["moved"]
    assert move["server_id"] == backend_id
    assert move["from"] == 25565
    assert move["to"] == 25600
    # The link targets the NEW port, and both configs moved with it.
    assert payload["links"][0]["address"] == "127.0.0.1:25600"
    with Session(engine) as session:
        backend = session.get(Server, backend_id)
        assert backend.port == 25600
        assert (
            props.read_properties(Path(backend.path))["server-port"] == "25600"
        )

    # Re-saving the same links is a no-op — no second move.
    resp = client.put(
        f"/api/servers/{proxy_id}/proxy", json={"server_ids": [backend_id]}
    )
    assert resp.json()["moved"] == []


def test_link_refuses_moving_a_running_backend(
    client, engine, tmp_path: Path, monkeypatch
):
    from lectern.api import proxy as proxy_api

    proxy_id = _mk_server(client, engine, tmp_path, "Front 2", "velocity", 25567, "proxy")
    backend_id = _mk_server(client, engine, tmp_path, "Busy", "vanilla", 25567, "mc")
    monkeypatch.setattr(proxy_api.manager, "is_running", lambda sid: sid == backend_id)

    resp = client.put(
        f"/api/servers/{proxy_id}/proxy", json={"server_ids": [backend_id]}
    )
    assert resp.status_code == 409
    assert "Stop it first" in resp.json()["detail"]
