"""M8 — .mrpack import: index parsing, env.server filtering, overrides,
loader pinning, and re-import reconciliation. Downloads and the loader-jar
resolution are faked; the zip handling and manifest work run for real."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session

from lectern.content import manager as content_manager
from lectern.content import mrpack
from lectern.models import Server
from lectern.servers.types import JarSpec


def _mrpack(
    files: list[dict],
    *,
    name="Test Pack",
    version_id="1.0.0",
    mc="1.20.1",
    loader=("fabric-loader", "0.15.0"),
    overrides: dict[str, bytes] | None = None,
    server_overrides: dict[str, bytes] | None = None,
) -> bytes:
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "name": name,
        "versionId": version_id,
        "dependencies": {"minecraft": mc, loader[0]: loader[1]},
        "files": files,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        for rel, data in (overrides or {}).items():
            zf.writestr(f"overrides/{rel}", data)
        for rel, data in (server_overrides or {}).items():
            zf.writestr(f"server-overrides/{rel}", data)
    return buf.getvalue()


def _pack_file(path: str, *, server_env="required") -> dict:
    return {
        "path": path,
        "hashes": {"sha512": f"hash-{path}"},
        "env": {"client": "required", "server": server_env},
        "downloads": [f"http://fake/{path}"],
        "fileSize": 1,
    }


# --- pure parsing --------------------------------------------------------------


def test_parse_index_rejects_junk():
    with pytest.raises(mrpack.MrpackError):
        mrpack.parse_index(b"not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("whatever.txt", "x")
    with pytest.raises(mrpack.MrpackError):
        mrpack.parse_index(buf.getvalue())


def test_pack_loader_and_env_filter():
    index = mrpack.parse_index(
        _mrpack(
            [
                _pack_file("mods/server-ok.jar"),
                _pack_file("mods/client-only.jar", server_env="unsupported"),
                {"path": "mods/no-env.jar", "hashes": {}, "downloads": ["http://x"]},
            ]
        )
    )
    assert mrpack.pack_loader(index) == ("fabric", "0.15.0")
    wanted, skipped = mrpack.server_files(index)
    assert [f["path"] for f in wanted] == ["mods/server-ok.jar", "mods/no-env.jar"]
    assert [f["path"] for f in skipped] == ["mods/client-only.jar"]
    wanted, skipped = mrpack.server_files(index, include_client_only=True)
    assert len(wanted) == 3 and skipped == []


def test_unsafe_paths_rejected():
    for bad in ("../evil.jar", "/abs/evil.jar", "world/evil.jar"):
        with pytest.raises(mrpack.MrpackError):
            mrpack._safe_relpath(bad)
    assert str(mrpack._safe_relpath("mods/ok.jar")) == "mods/ok.jar"


# --- import flow -----------------------------------------------------------------


def _fabric_server(client, engine, tmp_path: Path) -> str:
    server_id = client.post(
        "/api/servers",
        json={"name": "MP", "type": "fabric", "mc_version": "1.20.1"},
    ).json()["id"]
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(tmp_path)
        server.status = "stopped"
        server.loader_version = "0.14.0"  # older than the pack pins
        server.server_jar = "fabric-server-launch.jar"
        session.add(server)
        session.commit()
    return server_id


@pytest.fixture
def fake_downloads(monkeypatch):
    calls: list[tuple[str, str, str | None]] = []

    async def fake_download(url, dest: Path, *, expected_hash=None, hash_algo="sha512"):
        calls.append((url, str(dest), expected_hash))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"DATA:" + url.encode())
        return dest

    async def fake_resolve_jar(self, mc_version, loader_version=None):
        return JarSpec(
            url=f"http://fake/loader/{loader_version}.jar",
            jar_name="fabric-server-launch.jar",
            loader_version=loader_version,
        )

    monkeypatch.setattr(mrpack, "download_file", fake_download)
    from lectern.servers.types import FabricType

    monkeypatch.setattr(FabricType, "resolve_jar", fake_resolve_jar)
    return calls


def test_import_full_flow(client, engine, tmp_path, fake_downloads):
    sid = _fabric_server(client, engine, tmp_path)
    pack = _mrpack(
        [
            _pack_file("mods/alpha.jar"),
            _pack_file("mods/client.jar", server_env="unsupported"),
        ],
        overrides={"config/mod.toml": b"base", "server.properties": b"motd=pack\n"},
        server_overrides={"config/mod.toml": b"server-wins"},
    )
    resp = client.post(
        f"/api/servers/{sid}/modpack",
        files={"file": ("pack.mrpack", pack, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["installed"] == 1
    assert summary["skipped_client_only"] == ["mods/client.jar"]
    assert summary["loader_changed"] is True
    assert summary["loader_version"] == "0.15.0"

    # Files + overrides landed; server-overrides beat overrides.
    assert (tmp_path / "mods/alpha.jar").exists()
    assert not (tmp_path / "mods/client.jar").exists()
    assert (tmp_path / "config/mod.toml").read_bytes() == b"server-wins"
    # sha512 was passed through to the verified downloader.
    mod_calls = [c for c in fake_downloads if "alpha" in c[0]]
    assert mod_calls[0][2] == "hash-mods/alpha.jar"

    # Items are tracked with source=mrpack and appear in the content list.
    listed = client.get(f"/api/servers/{sid}/content").json()
    assert [(i["source"], i["name"]) for i in listed] == [("mrpack", "alpha.jar")]

    # Loader was pinned on the record.
    detail = client.get(f"/api/servers/{sid}").json()
    assert detail["loader_version"] == "0.15.0"


def test_reimport_reconciles(client, engine, tmp_path, fake_downloads):
    sid = _fabric_server(client, engine, tmp_path)
    v1 = _mrpack([_pack_file("mods/keep.jar"), _pack_file("mods/dropped.jar")])
    client.post(
        f"/api/servers/{sid}/modpack",
        files={"file": ("pack.mrpack", v1, "application/zip")},
    )
    ids_before = {
        i["name"]: i["id"] for i in client.get(f"/api/servers/{sid}/content").json()
    }
    assert (tmp_path / "mods/dropped.jar").exists()

    # v2 drops one file, adds another; also a user-installed item must survive.
    (tmp_path / "mods").mkdir(exist_ok=True)
    items = content_manager.read_manifest(tmp_path)
    items.append(
        {
            "id": "usermod00000000000000000000000000",
            "kind": "mod",
            "source": "modrinth",
            "name": "User Mod",
            "filename": "user-mod.jar",
            "enabled": True,
        }
    )
    content_manager.write_manifest(tmp_path, items)
    (tmp_path / "mods/user-mod.jar").write_bytes(b"user")

    v2 = _mrpack(
        [_pack_file("mods/keep.jar"), _pack_file("mods/new.jar")], version_id="2.0.0"
    )
    resp = client.post(
        f"/api/servers/{sid}/modpack",
        files={"file": ("pack.mrpack", v2, "application/zip")},
    )
    assert resp.status_code == 200

    assert not (tmp_path / "mods/dropped.jar").exists()  # reconciled away
    assert (tmp_path / "mods/new.jar").exists()
    assert (tmp_path / "mods/user-mod.jar").exists()  # untouched

    listed = client.get(f"/api/servers/{sid}/content").json()
    by_name = {i["name"]: i for i in listed}
    assert set(by_name) == {"keep.jar", "new.jar", "User Mod"}
    # Persisting file keeps its stable id across the upgrade.
    assert by_name["keep.jar"]["id"] == ids_before["keep.jar"]


def test_import_mismatches_rejected(client, engine, tmp_path, fake_downloads):
    sid = _fabric_server(client, engine, tmp_path)
    wrong_mc = _mrpack([_pack_file("mods/a.jar")], mc="1.19.2")
    resp = client.post(
        f"/api/servers/{sid}/modpack",
        files={"file": ("pack.mrpack", wrong_mc, "application/zip")},
    )
    assert resp.status_code == 400
    assert "1.19.2" in resp.json()["detail"]

    not_a_pack = client.post(
        f"/api/servers/{sid}/modpack",
        files={"file": ("junk.mrpack", b"junk", "application/zip")},
    )
    assert not_a_pack.status_code == 400
