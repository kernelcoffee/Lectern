"""M9 — backups: create (exclusions, traversal guard, prune), restore
round-trip with the move-aside safety, and the API guards."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session

from lectern import backups as bk
from lectern.config import get_settings
from lectern.models import Server


def _installed_server(client, engine, tmp_path: Path, **settings) -> str:
    server_id = client.post(
        "/api/servers",
        json={"name": settings.pop("name", "BK"), "type": "vanilla", "mc_version": "1.21"},
    ).json()["id"]
    server_dir = tmp_path / "srv"
    (server_dir / "world").mkdir(parents=True)
    (server_dir / "logs").mkdir()
    (server_dir / "world/level.dat").write_bytes(b"WORLD-V1")
    (server_dir / "server.properties").write_text("motd=hi\n")
    (server_dir / "logs/latest.log").write_text("noise\n")
    (server_dir / "session.lock").write_bytes(b"lock")
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(server_dir)
        server.status = "stopped"
        for key, value in settings.items():
            setattr(server, key, value)
        session.add(server)
        session.commit()
    return server_id


def test_create_backup_excludes_and_lands_outside(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    resp = client.post(f"/api/servers/{sid}/backups")
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["trigger"] == "manual" and row["size_bytes"] > 0

    archive = get_settings().backups_dir / sid / row["filename"]
    assert archive.exists()
    # Outside the server dir by construction.
    assert not str(archive).startswith(str(tmp_path / "srv"))
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "world/level.dat" in names
    assert "server.properties" in names
    # Default exclusions (logs) + session.lock skipped.
    assert not any(n.startswith("logs/") for n in names)
    assert "session.lock" not in names

    listed = client.get(f"/api/servers/{sid}/backups").json()
    assert [b["id"] for b in listed] == [row["id"]]


def test_prune_keeps_newest(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path, backup_max=2)
    ids = []
    for _ in range(3):
        ids.append(client.post(f"/api/servers/{sid}/backups").json()["id"])
    listed = client.get(f"/api/servers/{sid}/backups").json()
    assert len(listed) == 2
    assert ids[0] not in {b["id"] for b in listed}  # oldest pruned
    # Pruned archive removed from disk too.
    files = list((get_settings().backups_dir / sid).glob("*.zip"))
    assert len(files) == 2


def test_restore_round_trip(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    server_dir = tmp_path / "srv"
    backup = client.post(f"/api/servers/{sid}/backups").json()

    # Damage the world + add a stray file after the backup.
    (server_dir / "world/level.dat").write_bytes(b"CORRUPTED")
    (server_dir / "stray.txt").write_text("added later")

    resp = client.post(f"/api/servers/{sid}/backups/{backup['id']}/restore")
    assert resp.status_code == 204, resp.text
    assert (server_dir / "world/level.dat").read_bytes() == b"WORLD-V1"
    assert not (server_dir / "stray.txt").exists()  # full replace
    assert not server_dir.with_name("srv.pre-restore").exists()  # cleaned up


def test_restore_missing_or_corrupt_archive(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    server_dir = tmp_path / "srv"
    backup = client.post(f"/api/servers/{sid}/backups").json()
    archive = get_settings().backups_dir / sid / backup["filename"]

    # Corrupt archive → 409 and the server dir is left untouched.
    archive.write_bytes(b"garbage")
    resp = client.post(f"/api/servers/{sid}/backups/{backup['id']}/restore")
    assert resp.status_code == 409
    assert (server_dir / "world/level.dat").read_bytes() == b"WORLD-V1"

    archive.unlink()
    resp = client.post(f"/api/servers/{sid}/backups/{backup['id']}/restore")
    assert resp.status_code == 409


def test_delete_backup(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    backup = client.post(f"/api/servers/{sid}/backups").json()
    archive = get_settings().backups_dir / sid / backup["filename"]
    assert archive.exists()
    assert (
        client.delete(f"/api/servers/{sid}/backups/{backup['id']}").status_code == 204
    )
    assert not archive.exists()
    assert client.get(f"/api/servers/{sid}/backups").json() == []
    # Unknown id → 404.
    assert (
        client.delete(f"/api/servers/{sid}/backups/{backup['id']}").status_code == 404
    )


def test_unsafe_archive_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../outside.txt", "boom")
    with pytest.raises(bk.BackupError, match="unsafe path"):
        bk._validate_archive(evil)


def test_backup_settings_via_settings_patch(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    resp = client.patch(
        f"/api/servers/{sid}",
        json={"backup_max": 5, "backup_compress": False, "backup_excluded": "logs,cache"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["backup_max"] == 5
    assert body["backup_compress"] is False
    assert body["backup_excluded"] == "logs,cache"
