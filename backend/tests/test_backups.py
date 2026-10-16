"""M9 — backups: create (exclusions, traversal guard, prune), restore
round-trip with the move-aside safety, and the API guards."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

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


def test_download_backup(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    backup = client.post(f"/api/servers/{sid}/backups").json()

    resp = client.get(f"/api/servers/{sid}/backups/{backup['id']}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert backup["filename"] in resp.headers.get("content-disposition", "")
    assert resp.content[:2] == b"PK"  # a real zip archive

    # Unknown id → 404.
    assert client.get(f"/api/servers/{sid}/backups/nope/download").status_code == 404


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


# --- upload ----------------------------------------------------------------------


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _upload(client, sid: str, body: bytes, name: str = "edited.zip"):
    return client.post(
        f"/api/servers/{sid}/backups/upload",
        files={"file": (name, body, "application/zip")},
    )


def _backup_files(sid: str) -> list[Path]:
    d = get_settings().backups_dir / sid
    return sorted(d.iterdir()) if d.exists() else []


def test_upload_backup_lists_and_restores(client, engine, tmp_path, monkeypatch):
    sid = _installed_server(client, engine, tmp_path)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bk.events, "record", lambda _sid, kind, msg="": recorded.append((kind, msg))
    )
    server_dir = tmp_path / "srv"
    body = _zip_bytes(
        {"world/level.dat": b"WORLD-PRUNED", "server.properties": b"motd=edited\n"}
    )
    resp = _upload(client, sid, body)
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["trigger"] == "uploaded"
    assert row["size_bytes"] == len(body)
    assert row["filename"].endswith(".zip") and row["filename"] != "edited.zip"

    archive = get_settings().backups_dir / sid / row["filename"]
    assert archive.read_bytes() == body
    assert not archive.resolve().is_relative_to(server_dir.resolve())
    assert [p.name for p in _backup_files(sid)] == [row["filename"]]  # no .part left
    assert [b["id"] for b in client.get(f"/api/servers/{sid}/backups").json()] == [row["id"]]
    assert recorded == [("backup_created", f"{row['filename']} (uploaded)")]

    resp = client.post(f"/api/servers/{sid}/backups/{row['id']}/restore")
    assert resp.status_code == 204, resp.text
    assert (server_dir / "world/level.dat").read_bytes() == b"WORLD-PRUNED"
    assert (server_dir / "server.properties").read_text() == "motd=edited\n"
    assert not (server_dir / "logs").exists()  # full replace


def test_upload_downloaded_backup_round_trips(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    server_dir = tmp_path / "srv"
    created = client.post(f"/api/servers/{sid}/backups").json()
    body = client.get(f"/api/servers/{sid}/backups/{created['id']}/download").content
    uploaded = _upload(client, sid, body).json()
    assert uploaded["trigger"] == "uploaded"

    (server_dir / "world/level.dat").write_bytes(b"CHANGED")
    resp = client.post(f"/api/servers/{sid}/backups/{uploaded['id']}/restore")
    assert resp.status_code == 204, resp.text
    assert (server_dir / "world/level.dat").read_bytes() == b"WORLD-V1"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"definitely not a zip", "not a zip"),
        (_zip_bytes({"../evil": b"boom"}), "unsafe path"),
        (_zip_bytes({}), "empty"),
        (_zip_bytes({"world/": b""}), "empty"),
        (_zip_bytes({"world/level.dat": b"W", "world/region/r.0.0.mca": b"R"}), "whole-server"),
        (_zip_bytes({"level.dat": b"W", "region/r.0.0.mca": b"R"}), "whole-server"),
        (
            _zip_bytes({"myserver/server.properties": b"", "myserver/world/level.dat": b"W"}),
            "wrapped in a single folder (myserver/)",
        ),
        (_zip_bytes({"world/server.properties": b""}), "wrapped in a single folder (world/)"),
        (
            _zip_bytes({
                "myserver/server.properties": b"",
                "__MACOSX/myserver/._server.properties": b"",
            }),
            "wrapped in a single folder (myserver/)",
        ),
    ],
    ids=[
        "not-zip", "traversal", "empty", "dirs-only", "world-only", "world-contents",
        "wrapped", "nested-marker", "macos-compress",
    ],
)
def test_upload_rejects_bad_archive(client, engine, tmp_path, monkeypatch, body, match):
    sid = _installed_server(client, engine, tmp_path)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bk.events, "record", lambda _sid, kind, msg="": recorded.append((kind, msg))
    )
    resp = _upload(client, sid, body)
    assert resp.status_code == 400
    assert [k for k, _ in recorded] == ["backup_failed"]
    assert match in resp.json()["detail"]
    assert _backup_files(sid) == []
    assert client.get(f"/api/servers/{sid}/backups").json() == []
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize(
    ("payload_mb", "early"),
    [(2.0, True), (1.5, False)],
    ids=["content-length", "streamed-count"],
)
def test_upload_over_cap_is_413(client, engine, tmp_path, payload_mb, early):
    sid = _installed_server(client, engine, tmp_path)
    assert client.patch("/api/settings", json={"max_backup_upload_mb": 1}).status_code == 200
    body = _zip_bytes({"big.bin": b"x" * int(payload_mb * 1024 * 1024)})  # stored
    resp = _upload(client, sid, body)
    assert resp.status_code == 413
    assert _backup_files(sid) == []  # no .part left behind
    # Way over the cap: refused from Content-Length before anything is staged.
    assert (get_settings().backups_dir / sid).exists() is not early


def test_upload_not_multipart_or_missing_field_is_400(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    url = f"/api/servers/{sid}/backups/upload"
    resp = client.post(url, content=b"raw", headers={"content-type": "application/zip"})
    assert resp.status_code == 400
    resp = client.post(url, files={"other": ("a.zip", _zip_bytes({"a.txt": b"a"}))})
    assert resp.status_code == 400
    assert "file" in resp.json()["detail"]
    assert _backup_files(sid) == []


def test_upload_ignores_extra_fields(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    body = _zip_bytes({"world/level.dat": b"W", "server.properties": b""})
    resp = client.post(
        f"/api/servers/{sid}/backups/upload",
        data={"note": "hello"},
        files={"file": ("edited.zip", body, "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["size_bytes"] == len(body)
    archive = get_settings().backups_dir / sid / resp.json()["filename"]
    assert archive.read_bytes() == body


def test_upload_accepts_archive_too_big_to_restore_now(client, engine, tmp_path, monkeypatch):
    """Upload checks structure only: an archive that would not fit on the disk
    once unpacked is still stored (the admin may free space before restoring),
    and restore is what refuses it."""
    sid = _installed_server(client, engine, tmp_path)
    body = _zip_bytes({"world/level.dat": b"x" * 4096, "server.properties": b""})
    assert client.patch("/api/settings", json={"restore_disk_reserve_mb": 1}).status_code == 200
    free = 1024 * 1024 + 1024  # 4096 bytes unpacked won't fit beside a 1 MiB reserve
    monkeypatch.setattr(bk.shutil, "disk_usage", lambda _p: SimpleNamespace(free=free))
    resp = _upload(client, sid, body)
    assert resp.status_code == 201, resp.text
    assert len(_backup_files(sid)) == 1
    resp = client.post(f"/api/servers/{sid}/backups/{resp.json()['id']}/restore")
    assert resp.status_code in (400, 409)
    assert "more than the disk can hold" in resp.json()["detail"]


def test_restore_refuses_archive_too_big_for_disk(client, engine, tmp_path, monkeypatch):
    sid = _installed_server(client, engine, tmp_path)
    backup = client.post(f"/api/servers/{sid}/backups").json()
    assert client.patch("/api/settings", json={"restore_disk_reserve_mb": 4}).status_code == 200
    reserve = 4 * 1024 * 1024
    monkeypatch.setattr(bk.shutil, "disk_usage", lambda _p: SimpleNamespace(free=reserve))
    resp = client.post(f"/api/servers/{sid}/backups/{backup['id']}/restore")
    assert resp.status_code == 409
    assert "more than the disk can hold" in resp.json()["detail"]
    assert f"{reserve} kept in reserve" in resp.json()["detail"]
    assert (tmp_path / "srv/world/level.dat").read_bytes() == b"WORLD-V1"


def test_restore_reserve_honours_setting(client, engine, tmp_path, monkeypatch):
    """With 3 MiB free and an archive unpacking to 2 MiB: a 2 MB reserve refuses
    the restore, a 0 reserve lets it through."""
    sid = _installed_server(client, engine, tmp_path)
    body = _zip_bytes({"world/level.dat": b"N" * (2 * 1024 * 1024), "eula.txt": b""})
    backup = _upload(client, sid, body).json()
    monkeypatch.setattr(
        bk.shutil, "disk_usage", lambda _p: SimpleNamespace(free=3 * 1024 * 1024)
    )
    url = f"/api/servers/{sid}/backups/{backup['id']}/restore"

    assert client.patch("/api/settings", json={"restore_disk_reserve_mb": 2}).status_code == 200
    resp = client.post(url)
    assert resp.status_code == 409
    assert "more than the disk can hold" in resp.json()["detail"]
    assert (tmp_path / "srv/world/level.dat").read_bytes() == b"WORLD-V1"

    assert client.patch("/api/settings", json={"restore_disk_reserve_mb": 0}).status_code == 200
    resp = client.post(url)
    assert resp.status_code == 204, resp.text
    assert (tmp_path / "srv/world/level.dat").read_bytes() == b"N" * (2 * 1024 * 1024)


def test_upload_requires_installed_server(client):
    sid = client.post(
        "/api/servers", json={"name": "NI", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    resp = _upload(client, sid, _zip_bytes({"a.txt": b"a"}))
    assert resp.status_code == 400
    assert _backup_files(sid) == []
    resp = client.post("/api/servers/nope/backups/upload", files={"file": ("a.zip", b"x")})
    assert resp.status_code == 404


def test_upload_prunes_to_retention(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path, backup_max=1)
    first = client.post(f"/api/servers/{sid}/backups").json()
    uploaded = _upload(
        client, sid, _zip_bytes({"world/level.dat": b"W", ".lectern/manifest.json": b"{}"})
    ).json()
    listed = client.get(f"/api/servers/{sid}/backups").json()
    assert [b["id"] for b in listed] == [uploaded["id"]]
    assert first["id"] != uploaded["id"]
    assert [p.name for p in _backup_files(sid)] == [uploaded["filename"]]


@pytest.mark.parametrize(
    "marker",
    ["server.properties", "eula.txt", "velocity.toml", ".lectern/manifest.json"],
)
def test_upload_accepts_any_root_marker(client, engine, tmp_path, marker):
    sid = _installed_server(client, engine, tmp_path)
    resp = _upload(client, sid, _zip_bytes({marker: b"x", "world/level.dat": b"W"}))
    assert resp.status_code == 201, resp.text


def test_create_backup_skips_staged_upload_parts(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    lectern_dir = tmp_path / "srv/.lectern"
    lectern_dir.mkdir()
    (lectern_dir / "tmpabc.part").write_bytes(b"orphan")
    (lectern_dir / "manifest.json").write_text("{}")
    created = client.post(f"/api/servers/{sid}/backups").json()
    archive = get_settings().backups_dir / sid / created["filename"]
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert ".lectern/manifest.json" in names
    assert ".lectern/tmpabc.part" not in names


def test_sweep_stale_parts(client, engine, tmp_path):
    sid = _installed_server(client, engine, tmp_path)
    lectern_dir = tmp_path / "srv/.lectern"
    lectern_dir.mkdir()
    (lectern_dir / "tmpabc.part").write_bytes(b"orphan")
    (lectern_dir / "manifest.json").write_text("{}")
    (tmp_path / "srv/world/keep.part").write_bytes(b"user file")  # outside .lectern: kept
    backups_dir = get_settings().backups_dir / sid
    backups_dir.mkdir(parents=True, exist_ok=True)
    (backups_dir / "tmpxyz.part").write_bytes(b"orphan")
    (backups_dir / "20260101-000000-000000.zip").write_bytes(b"zip")
    with Session(engine) as session:
        assert bk.sweep_stale_parts(session) >= 2  # backups root is shared across tests
    assert not (lectern_dir / "tmpabc.part").exists()
    assert (lectern_dir / "manifest.json").exists()
    assert (tmp_path / "srv/world/keep.part").exists()
    assert not (backups_dir / "tmpxyz.part").exists()
    assert (backups_dir / "20260101-000000-000000.zip").exists()
