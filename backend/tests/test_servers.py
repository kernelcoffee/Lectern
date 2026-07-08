"""Smoke tests for server CRUD — proves the harness works and gives later
milestones a place to extend. Not exhaustive (CRUD is thin framework glue)."""

from __future__ import annotations


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_create_get_list(client):
    assert client.get("/api/servers").json() == []

    resp = client.post(
        "/api/servers",
        json={"name": "Test", "type": "fabric", "mc_version": "1.20.1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Test"
    assert body["type"] == "fabric"
    # Creation now kicks off the install pipeline; the record starts installing.
    assert body["status"] == "installing"
    assert body["port"] == 25565  # default

    server_id = body["id"]
    assert client.get(f"/api/servers/{server_id}").status_code == 200
    assert [s["name"] for s in client.get("/api/servers").json()] == ["Test"]


def test_delete(client):
    server_id = client.post(
        "/api/servers",
        json={"name": "Doomed", "type": "vanilla", "mc_version": "1.21"},
    ).json()["id"]

    assert client.delete(f"/api/servers/{server_id}").status_code == 204
    assert client.get(f"/api/servers/{server_id}").status_code == 404


def test_delete_missing_returns_404(client):
    assert client.delete("/api/servers/does-not-exist").status_code == 404


def test_progress_endpoint_shape(client):
    server_id = client.post(
        "/api/servers",
        json={"name": "P", "type": "vanilla", "mc_version": "1.21"},
    ).json()["id"]

    resp = client.get(f"/api/servers/{server_id}/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["server_id"] == server_id
    assert set(body) == {"server_id", "step", "message", "done", "error"}


def test_progress_missing_returns_404(client):
    assert client.get("/api/servers/nope/progress").status_code == 404


def test_detail_includes_operational_fields(client):
    server_id = client.post(
        "/api/servers",
        json={"name": "D", "type": "vanilla", "mc_version": "1.21"},
    ).json()["id"]
    body = client.get(f"/api/servers/{server_id}").json()
    # M4 detail view exposes EULA + running state alongside the record.
    assert body["eula_accepted"] is False
    assert body["running"] is False
    assert body["stop_command"] == "stop"


def test_unknown_action_returns_404(client):
    server_id = client.post(
        "/api/servers",
        json={"name": "A", "type": "vanilla", "mc_version": "1.21"},
    ).json()["id"]
    assert client.post(f"/api/servers/{server_id}/action/fly").status_code == 404


def test_action_missing_server_returns_404(client):
    assert client.post("/api/servers/nope/action/start").status_code == 404


def test_eula_before_install_returns_409(client):
    # A freshly-created record has no on-disk path yet (install is a background
    # task that doesn't run against the test engine), so EULA accept 409s.
    server_id = client.post(
        "/api/servers",
        json={"name": "E", "type": "vanilla", "mc_version": "1.21"},
    ).json()["id"]
    assert client.post(f"/api/servers/{server_id}/eula").status_code == 409


def test_console_ws_replays_history(client):
    from lectern.servers.manager import manager

    manager.hub.clear("wsid")
    manager.hub.publish("wsid", "line one")
    manager.hub.publish("wsid", "line two")
    with client.websocket_connect("/ws/servers/wsid/console") as ws:
        assert ws.receive_text() == "line one"
        assert ws.receive_text() == "line two"
        manager.hub.publish("wsid", "live line")
        assert ws.receive_text() == "live line"


def test_create_validation_error(client):
    # Missing required mc_version.
    resp = client.post("/api/servers", json={"name": "x", "type": "fabric"})
    assert resp.status_code == 422


# --- name/port conflict validation ------------------------------------------


def _create(client, name, port=25565):
    return client.post(
        "/api/servers",
        json={"name": name, "type": "vanilla", "mc_version": "1.21", "port": port},
    )


def test_create_duplicate_name_409(client):
    assert _create(client, "Alpha", 25565).status_code == 201
    resp = _create(client, "alpha ", 25600)  # case/whitespace-insensitive
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_create_duplicate_port_409(client):
    assert _create(client, "Alpha", 25565).status_code == 201
    resp = _create(client, "Beta", 25565)
    assert resp.status_code == 409
    assert "25565" in resp.json()["detail"]


def test_create_distinct_name_and_port_ok(client):
    assert _create(client, "Alpha", 25565).status_code == 201
    assert _create(client, "Beta", 25566).status_code == 201


def test_settings_patch_conflicts_409(client):
    alpha = _create(client, "Alpha", 25565).json()["id"]
    _create(client, "Beta", 25566)

    assert (
        client.patch(f"/api/servers/{alpha}", json={"name": "Beta"}).status_code
        == 409
    )
    assert (
        client.patch(f"/api/servers/{alpha}", json={"port": 25566}).status_code
        == 409
    )
    # Re-saving its own values is not a conflict.
    resp = client.patch(f"/api/servers/{alpha}", json={"name": "Alpha", "port": 25565})
    assert resp.status_code == 200


# --- create-form default suggestions -----------------------------------------


def test_suggest_defaults_empty(client):
    assert client.get("/api/servers/suggest").json() == {
        "name": "New server",
        "port": 25565,
    }


def test_suggest_defaults_iterate(client):
    _create(client, "New server", 25565)
    assert client.get("/api/servers/suggest").json() == {
        "name": "New server 2",
        "port": 25566,
    }
    _create(client, "new server 2 ", 25566)  # case/whitespace still counts
    assert client.get("/api/servers/suggest").json() == {
        "name": "New server 3",
        "port": 25567,
    }


def test_suggest_defaults_fills_port_gap(client):
    _create(client, "Alpha", 25566)  # 25565 itself is free
    suggestion = client.get("/api/servers/suggest").json()
    assert suggestion["port"] == 25565
