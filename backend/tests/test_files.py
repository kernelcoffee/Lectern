"""File manager (M12): path confinement (the security core) + operations.

The pure ``servers/files`` functions are tested directly against a tmp server
dir; the endpoints go through the client with a server marked installed at a
tmp path (same trick as the other suites).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session

from lectern.models import Server
from lectern.servers import files as fm

# --- safe_path confinement (the guard everything relies on) ----------------


def test_safe_path_allows_within(tmp_path):
    base = tmp_path / "srv"
    (base / "config").mkdir(parents=True)
    assert fm.safe_path(base, "config") == (base / "config").resolve()
    assert fm.safe_path(base, "") == base.resolve()
    assert fm.safe_path(base, ".") == base.resolve()


@pytest.mark.parametrize(
    "evil",
    ["../secret", "../../etc/passwd", "/etc/passwd", "config/../../out", "a/../../b"],
)
def test_safe_path_rejects_escapes(tmp_path, evil):
    base = tmp_path / "srv"
    base.mkdir()
    with pytest.raises(fm.FileManagerError, match="outside the server"):
        fm.safe_path(base, evil)


def test_safe_path_rejects_symlink_escape(tmp_path):
    base = tmp_path / "srv"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET")
    (base / "link").symlink_to(outside)  # a symlink pointing out of the tree
    with pytest.raises(fm.FileManagerError, match="outside the server"):
        fm.safe_path(base, "link/secret.txt")


# --- listing ----------------------------------------------------------------


def test_list_dir_dirs_first_and_hides_lectern(tmp_path):
    base = tmp_path / "srv"
    (base / "mods").mkdir(parents=True)
    (base / ".lectern").mkdir()
    (base / "server.properties").write_text("x")
    (base / "eula.txt").write_text("eula=true")
    entries = fm.list_dir(base, "")
    names = [e.name for e in entries]
    assert names == ["mods", "eula.txt", "server.properties"]  # dir first, no .lectern
    by_name = {e.name: e for e in entries}
    assert by_name["mods"].mode.startswith("d")  # dir flagged in the mode string
    assert by_name["eula.txt"].mode.startswith("-")


# --- read / write text ------------------------------------------------------


def test_read_write_text_round_trip(tmp_path):
    base = tmp_path / "srv"
    (base / "config").mkdir(parents=True)
    fm.write_file(base, "config/mod.toml", "a = 1\n")
    got = fm.read_file(base, "config/mod.toml")
    assert got["content"] == "a = 1\n"
    assert got["binary"] is False and got["too_large"] is False


def test_read_flags_binary(tmp_path):
    base = tmp_path / "srv"
    base.mkdir()
    (base / "server.jar").write_bytes(b"PK\x03\x04\x00\x01binary")
    got = fm.read_file(base, "server.jar")
    assert got["binary"] is True and got["content"] is None


def test_read_flags_too_large(tmp_path, monkeypatch):
    base = tmp_path / "srv"
    base.mkdir()
    monkeypatch.setattr(fm, "TEXT_EDIT_MAX", 4)
    (base / "big.txt").write_text("hello world")
    got = fm.read_file(base, "big.txt")
    assert got["too_large"] is True and got["content"] is None


def test_write_refuses_lectern(tmp_path):
    base = tmp_path / "srv"
    (base / ".lectern").mkdir(parents=True)
    with pytest.raises(fm.FileManagerError, match="managed by Lectern"):
        fm.write_file(base, ".lectern/manifest.json", "{}")


def test_write_needs_existing_parent(tmp_path):
    base = tmp_path / "srv"
    base.mkdir()
    with pytest.raises(fm.FileManagerError, match="Parent directory"):
        fm.write_file(base, "nope/deep/file.txt", "x")


# --- mkdir / rename / delete ------------------------------------------------


def test_mkdir_rename_delete(tmp_path):
    base = tmp_path / "srv"
    base.mkdir()
    fm.make_dir(base, "datapacks")
    assert (base / "datapacks").is_dir()
    fm.rename(base, "datapacks", "packs")
    assert (base / "packs").is_dir() and not (base / "datapacks").exists()
    fm.delete(base, "packs")
    assert not (base / "packs").exists()


def test_delete_refuses_root_and_lectern(tmp_path):
    base = tmp_path / "srv"
    (base / ".lectern").mkdir(parents=True)
    with pytest.raises(fm.FileManagerError, match="server root"):
        fm.delete(base, "")
    with pytest.raises(fm.FileManagerError, match="managed by Lectern"):
        fm.delete(base, ".lectern")


def test_delete_recursive_dir(tmp_path):
    base = tmp_path / "srv"
    (base / "world" / "region").mkdir(parents=True)
    (base / "world" / "level.dat").write_bytes(b"L")
    fm.delete(base, "world")
    assert not (base / "world").exists()


# --- unzip ------------------------------------------------------------------


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_unzip_in_place(tmp_path):
    base = tmp_path / "srv"
    (base / "packs").mkdir(parents=True)
    (base / "packs" / "map.zip").write_bytes(
        _zip_bytes({"World/level.dat": b"L", "World/region/r.mca": b"R"})
    )
    count = fm.unzip(base, "packs/map.zip")
    assert count == 2
    # Members land in the archive's own directory (in place).
    assert (base / "packs" / "World" / "level.dat").read_bytes() == b"L"
    assert (base / "packs" / "World" / "region" / "r.mca").read_bytes() == b"R"


def test_unzip_rejects_non_zip(tmp_path):
    base = tmp_path / "srv"
    base.mkdir()
    (base / "notes.txt").write_text("hi")
    with pytest.raises(fm.FileManagerError, match="Not a zip"):
        fm.unzip(base, "notes.txt")


def test_unzip_rejects_zip_slip(tmp_path):
    base = tmp_path / "srv"
    base.mkdir()
    (base / "evil.zip").write_bytes(_zip_bytes({"../escape.txt": b"PWNED"}))
    with pytest.raises(fm.FileManagerError, match="Unsafe path"):
        fm.unzip(base, "evil.zip")
    assert not (tmp_path / "escape.txt").exists()


def test_unzip_refuses_writing_into_lectern(tmp_path):
    base = tmp_path / "srv"
    base.mkdir()
    (base / "sneaky.zip").write_bytes(_zip_bytes({".lectern/manifest.json": b"{}"}))
    with pytest.raises(fm.FileManagerError, match="managed by Lectern"):
        fm.unzip(base, "sneaky.zip")


# --- endpoints --------------------------------------------------------------


def _installed(client, engine, tmp_path) -> tuple[str, Path]:
    sid = client.post(
        "/api/servers", json={"name": "F", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    sdir = tmp_path / "srv"
    (sdir / "config").mkdir(parents=True)
    (sdir / "server.properties").write_text("motd=hi\n")
    (sdir / "config" / "a.toml").write_text("x = 1\n")
    with Session(engine) as s:
        srv = s.get(Server, sid)
        srv.path = str(sdir)
        srv.status = "stopped"
        s.add(srv)
        s.commit()
    return sid, sdir


def test_endpoint_list_and_read(client, engine, tmp_path):
    sid, _ = _installed(client, engine, tmp_path)
    listing = client.get(f"/api/servers/{sid}/files").json()
    assert listing["path"] == ""
    assert [e["name"] for e in listing["entries"]] == ["config", "server.properties"]

    content = client.get(
        f"/api/servers/{sid}/files/content", params={"path": "config/a.toml"}
    ).json()
    assert content["content"] == "x = 1\n"


def test_endpoint_write_and_delete(client, engine, tmp_path):
    sid, sdir = _installed(client, engine, tmp_path)
    assert client.put(
        f"/api/servers/{sid}/files/content",
        params={"path": "config/new.txt"},
        json={"content": "hello"},
    ).status_code == 204
    assert (sdir / "config" / "new.txt").read_text() == "hello"

    assert client.request(
        "DELETE", f"/api/servers/{sid}/files", params={"path": "config/new.txt"}
    ).status_code == 204
    assert not (sdir / "config" / "new.txt").exists()


def test_endpoint_rejects_traversal(client, engine, tmp_path):
    sid, _ = _installed(client, engine, tmp_path)
    resp = client.get(
        f"/api/servers/{sid}/files/content", params={"path": "../../../etc/passwd"}
    )
    assert resp.status_code == 400
    assert "outside the server" in resp.json()["detail"]


def test_endpoint_upload_and_download(client, engine, tmp_path):
    sid, sdir = _installed(client, engine, tmp_path)
    resp = client.post(
        f"/api/servers/{sid}/files/upload",
        files={"file": ("note.txt", b"UPLOADED", "text/plain")},
        data={"path": "config"},
    )
    assert resp.status_code == 201
    assert (sdir / "config" / "note.txt").read_bytes() == b"UPLOADED"

    dl = client.get(f"/api/servers/{sid}/files/download", params={"path": "config/note.txt"})
    assert dl.status_code == 200 and dl.content == b"UPLOADED"


def test_endpoint_unzip(client, engine, tmp_path):
    sid, sdir = _installed(client, engine, tmp_path)
    (sdir / "bundle.zip").write_bytes(_zip_bytes({"a.txt": b"A", "sub/b.txt": b"B"}))
    resp = client.post(
        f"/api/servers/{sid}/files/unzip", json={"path": "bundle.zip"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["extracted"] == 2
    assert (sdir / "a.txt").read_bytes() == b"A"
    assert (sdir / "sub" / "b.txt").read_bytes() == b"B"


def test_endpoint_installed_guard(client):
    sid = client.post(
        "/api/servers", json={"name": "NI", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    assert client.get(f"/api/servers/{sid}/files").status_code == 409
