"""World import (creation-only): level.dat detection, extraction, guards.

The pure extractor is tested directly against zips built in a tmp dir; the
endpoint is exercised through the client with a stopped, installed server
(same trick as test_content.py — mark the row installed at a tmp path).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session

from lectern.models import Server
from lectern.servers.world_import import (
    WorldImportError,
    extract_world,
    find_world_root,
)


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- find_world_root --------------------------------------------------------


def test_find_world_root_at_archive_root():
    assert find_world_root(["level.dat", "region/r.0.0.mca"]) == ""


def test_find_world_root_nested_wrapper():
    assert find_world_root(["MyMap/level.dat", "MyMap/region/r.mca"]) == "MyMap/"


def test_find_world_root_prefers_shallowest():
    # A backup level.dat buried deeper must not beat the real top-level one.
    names = ["world/level.dat", "world/DIM1/data/level.dat"]
    assert find_world_root(names) == "world/"


def test_find_world_root_none_when_absent():
    assert find_world_root(["readme.txt", "region/r.mca"]) is None


# --- extract_world ----------------------------------------------------------


def test_extract_root_world(tmp_path: Path):
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(_zip({"level.dat": b"LEVEL", "region/r.0.0.mca": b"REGION"}))
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    written, skipped = extract_world(zip_path, server_dir, level_name="world")

    assert (written, skipped) == (2, 0)
    assert (server_dir / "world" / "level.dat").read_bytes() == b"LEVEL"
    assert (server_dir / "world" / "region" / "r.0.0.mca").read_bytes() == b"REGION"


def test_extract_strips_wrapper_folder(tmp_path: Path):
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(_zip({"CoolMap/level.dat": b"L", "CoolMap/data/x": b"X"}))
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    extract_world(zip_path, server_dir)

    assert (server_dir / "world" / "level.dat").read_bytes() == b"L"
    assert (server_dir / "world" / "data" / "x").read_bytes() == b"X"
    assert not (server_dir / "world" / "CoolMap").exists()


def test_extract_replaces_existing_world(tmp_path: Path):
    server_dir = tmp_path / "server"
    (server_dir / "world").mkdir(parents=True)
    (server_dir / "world" / "stale.dat").write_bytes(b"OLD")

    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(_zip({"level.dat": b"NEW"}))
    extract_world(zip_path, server_dir)

    assert (server_dir / "world" / "level.dat").read_bytes() == b"NEW"
    assert not (server_dir / "world" / "stale.dat").exists()  # old world gone


def test_extract_honours_level_name(tmp_path: Path):
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(_zip({"level.dat": b"L"}))
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    extract_world(zip_path, server_dir, level_name="survival")
    assert (server_dir / "survival" / "level.dat").exists()


def test_extract_excludes_distant_horizons_by_default(tmp_path: Path):
    # A Distant Horizons LOD cache embedded in the world is skipped by default.
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(
        _zip({
            "level.dat": b"L",
            "region/r.0.0.mca": b"R",
            "data/DistantHorizons.sqlite": b"X" * 100,
            "data/DistantHorizons.sqlite-wal": b"X" * 50,
        })
    )
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    written, skipped = extract_world(zip_path, server_dir)

    assert (written, skipped) == (2, 2)
    assert (server_dir / "world" / "level.dat").exists()
    assert not (server_dir / "world" / "data" / "DistantHorizons.sqlite").exists()


def test_extract_custom_exclude_patterns(tmp_path: Path):
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(
        _zip({"level.dat": b"L", "region/r.mca": b"R", "junk/big.tmp": b"T"})
    )
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    written, skipped = extract_world(zip_path, server_dir, exclude=["*.tmp"])
    assert (written, skipped) == (2, 1)
    assert not (server_dir / "world" / "junk" / "big.tmp").exists()


def test_extract_empty_exclude_imports_everything(tmp_path: Path):
    # An explicit empty list disables the default DH filter.
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(
        _zip({"level.dat": b"L", "data/DistantHorizons.sqlite": b"X"})
    )
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    written, skipped = extract_world(zip_path, server_dir, exclude=[])
    assert (written, skipped) == (2, 0)
    assert (server_dir / "world" / "data" / "DistantHorizons.sqlite").exists()


def test_extract_rejects_no_level_dat(tmp_path: Path):
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(_zip({"readme.txt": b"hi"}))
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    with pytest.raises(WorldImportError, match="no level.dat"):
        extract_world(zip_path, server_dir)


def test_extract_rejects_bad_zip(tmp_path: Path):
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(b"not a zip at all")
    server_dir = tmp_path / "server"
    server_dir.mkdir()

    with pytest.raises(WorldImportError, match="valid .zip"):
        extract_world(zip_path, server_dir)


def test_extract_rejects_zip_slip(tmp_path: Path):
    # A member that would escape the world dir must be refused, and the existing
    # world left untouched (swap only happens after a clean extraction).
    server_dir = tmp_path / "server"
    (server_dir / "world").mkdir(parents=True)
    (server_dir / "world" / "keep.dat").write_bytes(b"KEEP")

    zip_path = tmp_path / "evil.zip"
    zip_path.write_bytes(_zip({"level.dat": b"L", "../../escape.txt": b"PWNED"}))

    with pytest.raises(WorldImportError, match="Unsafe path"):
        extract_world(zip_path, server_dir)
    assert (server_dir / "world" / "keep.dat").read_bytes() == b"KEEP"
    assert not (tmp_path / "escape.txt").exists()


# --- endpoint ---------------------------------------------------------------


def _installed_server(client, engine, tmp_path: Path) -> str:
    server_id = client.post(
        "/api/servers", json={"name": "W", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(tmp_path)
        server.status = "stopped"
        session.add(server)
        session.commit()
    return server_id


def test_endpoint_imports_uploaded_world(client, engine, tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    server_id = _installed_server(client, engine, server_dir)
    data = _zip({"MyMap/level.dat": b"L", "MyMap/region/r.mca": b"R"})

    resp = client.post(
        f"/api/servers/{server_id}/world",
        files={"file": ("map.zip", data, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["written"] == 2 and body["skipped"] == 0
    assert body["server"]["id"] == server_id
    assert (server_dir / "world" / "level.dat").read_bytes() == b"L"


def test_endpoint_excludes_via_form_field(client, engine, tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    server_id = _installed_server(client, engine, server_dir)
    data = _zip({"level.dat": b"L", "data/DistantHorizons.sqlite": b"X" * 10})

    resp = client.post(
        f"/api/servers/{server_id}/world",
        files={"file": ("map.zip", data, "application/zip")},
        data={"exclude": "*DistantHorizons*"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] == 1
    assert not (server_dir / "world" / "data" / "DistantHorizons.sqlite").exists()


def test_endpoint_empty_exclude_imports_everything(client, engine, tmp_path):
    # An explicit empty exclude field (what the UI sends when the box is
    # cleared) turns filtering off — the default DH filter must not apply.
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    server_id = _installed_server(client, engine, server_dir)
    data = _zip({"level.dat": b"L", "data/DistantHorizons.sqlite": b"X" * 10})

    resp = client.post(
        f"/api/servers/{server_id}/world",
        files={"file": ("map.zip", data, "application/zip")},
        data={"exclude": ""},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] == 0
    assert (server_dir / "world" / "data" / "DistantHorizons.sqlite").exists()


def test_endpoint_requires_a_source(client, engine, tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    server_id = _installed_server(client, engine, server_dir)
    resp = client.post(f"/api/servers/{server_id}/world")
    assert resp.status_code == 400


def test_endpoint_rejects_non_world_zip(client, engine, tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    server_id = _installed_server(client, engine, server_dir)
    resp = client.post(
        f"/api/servers/{server_id}/world",
        files={"file": ("x.zip", _zip({"notes.txt": b"hi"}), "application/zip")},
    )
    assert resp.status_code == 400
    assert "level.dat" in resp.json()["detail"]


def test_endpoint_409_while_installing(client, engine, tmp_path):
    # A server with no path yet (still installing) can't take a world.
    server_id = client.post(
        "/api/servers", json={"name": "Inst", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    resp = client.post(
        f"/api/servers/{server_id}/world",
        files={"file": ("x.zip", _zip({"level.dat": b"L"}), "application/zip")},
    )
    assert resp.status_code == 409
