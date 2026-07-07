"""Catalog endpoint tests. Provider network calls are monkeypatched so these
stay offline; live upstream calls are exercised separately (integration)."""

from __future__ import annotations

from lectern.providers import fabric, mojang


def test_server_types(client):
    types = client.get("/api/catalog/server-types").json()
    keys = {t["key"] for t in types}
    assert keys == {"vanilla", "fabric"}
    fabric_entry = next(t for t in types if t["key"] == "fabric")
    assert fabric_entry["needs_loader"] is True


def test_minecraft_versions_scoped_to_type(client, monkeypatch):
    async def fake_releases():
        return ["1.21", "1.20.6"]

    async def fake_game():
        return ["1.21", "1.20.1"]

    monkeypatch.setattr(mojang, "list_release_versions", fake_releases)
    monkeypatch.setattr(fabric, "list_game_versions", fake_game)

    assert client.get("/api/catalog/minecraft-versions?type=vanilla").json() == ["1.21", "1.20.6"]
    assert client.get("/api/catalog/minecraft-versions?type=fabric").json() == ["1.21", "1.20.1"]


def test_minecraft_versions_unknown_type_404(client):
    assert client.get("/api/catalog/minecraft-versions?type=bogus").status_code == 404


def test_fabric_loaders(client, monkeypatch):
    async def fake_loaders(mc_version: str):
        assert mc_version == "1.20.1"
        return ["0.16.0", "0.15.11"]

    monkeypatch.setattr(fabric, "list_loader_versions", fake_loaders)
    assert client.get("/api/catalog/loaders/fabric/1.20.1").json() == ["0.16.0", "0.15.11"]
