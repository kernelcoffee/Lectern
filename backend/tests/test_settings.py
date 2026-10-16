"""App settings (runtime tunables): GET/PATCH, validation, and that the values
actually drive the upload caps + create-form default."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from lectern.models import Server


def _by_key(items: list[dict]) -> dict[str, dict]:
    return {i["key"]: i for i in items}


def test_list_settings_defaults(client):
    items = _by_key(client.get("/api/settings").json())
    assert items["max_file_upload_mb"]["value"] == 2048
    assert items["max_world_upload_mb"]["value"] == 20480
    assert items["max_backup_upload_mb"]["value"] == 16384
    assert items["max_backup_upload_mb"]["category"] == "Uploads"
    assert items["default_memory_mb"]["value"] == 2048
    reserve = items["restore_disk_reserve_mb"]
    assert reserve["value"] == 1024
    assert reserve["min"] == 0 and reserve["max"] == 1048576
    assert reserve["unit"] == "MB" and reserve["category"] == "Backups"
    # metadata present for the UI.
    assert items["max_file_upload_mb"]["unit"] == "MB"
    assert items["max_file_upload_mb"]["min"] >= 1


def test_patch_persists_and_reflects(client):
    resp = client.patch("/api/settings", json={"max_file_upload_mb": 512})
    assert resp.status_code == 200
    assert _by_key(resp.json())["max_file_upload_mb"]["value"] == 512
    # A fresh GET still shows the override.
    assert _by_key(client.get("/api/settings").json())["max_file_upload_mb"]["value"] == 512


def test_restore_reserve_accepts_zero(client):
    resp = client.patch("/api/settings", json={"restore_disk_reserve_mb": 0})
    assert resp.status_code == 200
    assert _by_key(client.get("/api/settings").json())["restore_disk_reserve_mb"]["value"] == 0


def test_patch_out_of_bounds_422(client):
    resp = client.patch("/api/settings", json={"default_memory_mb": 1})  # min 256
    assert resp.status_code == 422
    assert "between" in resp.json()["detail"]
    # Nothing changed.
    assert _by_key(client.get("/api/settings").json())["default_memory_mb"]["value"] == 2048


def test_patch_unknown_key_422(client):
    resp = client.patch("/api/settings", json={"nope": 5})
    assert resp.status_code == 422
    assert "Unknown setting" in resp.json()["detail"]


def test_default_memory_flows_into_suggest(client):
    assert client.get("/api/servers/suggest").json()["memory_mb"] == 2048
    client.patch("/api/settings", json={"default_memory_mb": 4096})
    assert client.get("/api/servers/suggest").json()["memory_mb"] == 4096


def test_file_upload_cap_honours_setting(client, engine, tmp_path):
    # Lower the file-manager cap to 1 MB, then a 2 MB upload is rejected.
    sid = client.post(
        "/api/servers", json={"name": "S", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.path = str(tmp_path)
        srv.status = "stopped"
        s.add(srv)
        s.commit()

    client.patch("/api/settings", json={"max_file_upload_mb": 1})
    big = b"x" * (2 * 1024 * 1024)  # 2 MB
    resp = client.post(
        f"/api/servers/{sid}/files/upload",
        files={"file": ("big.bin", big, "application/octet-stream")},
        data={"path": ""},
    )
    assert resp.status_code == 413
    assert not (Path(tmp_path) / "big.bin").exists()  # partial cleaned up
    assert list((Path(tmp_path) / ".lectern").glob("*.part")) == []


def test_restore_reserve_default_from_env(client, monkeypatch):
    # LECTERN_RESTORE_DISK_RESERVE_MB changes the default when nothing is stored.
    from lectern import config

    monkeypatch.setenv("LECTERN_RESTORE_DISK_RESERVE_MB", "5")
    config.get_settings.cache_clear()
    try:
        items = _by_key(client.get("/api/settings").json())
        assert items["restore_disk_reserve_mb"]["value"] == 5
        assert items["restore_disk_reserve_mb"]["category"] == "Backups"
    finally:
        monkeypatch.delenv("LECTERN_RESTORE_DISK_RESERVE_MB")
        config.get_settings.cache_clear()
