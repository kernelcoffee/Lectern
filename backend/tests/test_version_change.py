"""Server version change (M9.5, F-SM-9).

Covers the ordering helper and the full change flow against fakes: real jar/
Java provisioning is stubbed (``install.provision``), Modrinth version lists are
faked so the content migration runs offline, and Mojang's release order is
supplied directly. Exercises: upgrade re-resolves compatible content and
disables the rest, uploads/modpack files are kept, a downgrade is refused
without the override and allowed with it, a running server is rejected, and
``backup_first`` runs a backup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from lectern.models import ContentItem, Server
from lectern.servers import install
from lectern.servers import version_change as vc
from lectern.servers.version_change import is_downgrade

RELEASES = ["1.21", "1.20.4", "1.20.1", "1.19.4"]  # newest-first (Mojang order)


# --- pure ordering ----------------------------------------------------------


def test_is_downgrade_ordering():
    assert is_downgrade("1.20.1", "1.19.4", RELEASES) is True
    assert is_downgrade("1.20.1", "1.21", RELEASES) is False
    assert is_downgrade("1.20.1", "1.20.1", RELEASES) is False
    # Unknown versions can't be ordered — not treated as a downgrade.
    assert is_downgrade("1.20.1", "snapshot-x", RELEASES) is False


# --- fixtures ---------------------------------------------------------------


def _item(pid, name, filename, *, loader="fabric", enabled=True, source="modrinth",
          project=True):
    return {
        "id": pid,
        "kind": "mod",
        "source": source,
        "project_id": pid if project else None,
        "version_id": f"{pid}-v0",
        "version_number": "1.0.0",
        "slug": name,
        "name": name,
        "filename": filename,
        "sha512": "old-hash",
        "game_version": "1.20.1",
        "loader": loader,
        "channel": "release",
        "enabled": enabled,
    }


def _version(vid, pid, number):
    return {
        "id": vid,
        "project_id": pid,
        "version_number": number,
        "version_type": "release",
        "files": [
            {
                "url": f"http://fake/{pid}/{vid}.jar",
                "filename": f"{pid}-{number}.jar",
                "primary": True,
                "hashes": {"sha512": f"hash-{vid}"},
            }
        ],
        "dependencies": [],
    }


@pytest.fixture
def changeable(monkeypatch):
    """Stub provisioning + Mojang order + Modrinth version lists.

    ``P_up`` has a 1.21 build; ``P_bad`` has none. ``download_file`` (in the
    version_change module) writes a placeholder so file swaps are observable."""

    async def fake_provision(server, *, mc_version, loader_version, emit=None):
        server.mc_version = mc_version
        if loader_version is not None:
            server.loader_version = loader_version
        return Path(server.path)

    async def fake_releases():
        return RELEASES

    from lectern.providers import modrinth

    async def list_versions(pid, *, loader=None, mc_version=None):
        if pid == "P_up" and mc_version == "1.21":
            return [_version("V_up_new", "P_up", "2.0.0")]
        return []

    async def fake_download(url, dest: Path, *, expected_hash=None, hash_algo="sha512"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"JAR:" + url.encode())
        return dest

    monkeypatch.setattr(install, "provision", fake_provision)
    monkeypatch.setattr(vc.mojang, "list_release_versions", fake_releases)
    monkeypatch.setattr(modrinth, "list_versions", list_versions)
    monkeypatch.setattr(vc, "download_file", fake_download)


def _server_with_content(engine, tmp_path: Path, items: list[dict]) -> str:
    """Create a stopped, installed Fabric server whose manifest holds ``items``
    (their files placed on disk)."""
    server_id = "srv-vc"
    with Session(engine) as session:
        session.add(Server(
            id=server_id, name="VC", type="fabric", mc_version="1.20.1",
            loader_version="0.15.0", path=str(tmp_path), server_jar="fabric.jar",
            java_path="/j", status="stopped",
        ))
        session.commit()
    (tmp_path / ".lectern").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".lectern/manifest.json").write_text(json.dumps({"items": items}))
    mods = tmp_path / "mods"
    mods.mkdir(exist_ok=True)
    for item in items:
        name = item["filename"] + ("" if item.get("enabled", True) else ".disabled")
        (mods / name).write_bytes(b"old")
    return server_id


def _manifest(tmp_path: Path) -> dict[str, dict]:
    items = json.loads((tmp_path / ".lectern/manifest.json").read_text())["items"]
    return {i["id"]: i for i in items}


# --- flow -------------------------------------------------------------------


def test_upgrade_migrates_content(client, engine, changeable, tmp_path):
    items = [
        _item("P_up", "Upgradable", "P_up-1.0.0.jar"),
        _item("P_bad", "NoBuild", "P_bad-1.0.0.jar"),
        _item("U1", "MyPack", "my.zip", loader=None, source="upload", project=False),
        _item("M1", "Modpack file", "packmod.jar", source="mrpack", project=False),
    ]
    server_id = _server_with_content(engine, tmp_path, items)

    resp = client.post(
        f"/api/servers/{server_id}/version",
        json={"mc_version": "1.21", "backup_first": False},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert report["updated"] == ["Upgradable"]
    assert report["incompatible"] == ["NoBuild"]
    assert sorted(report["kept"]) == ["Modpack file", "MyPack"]
    assert resp.json()["server"]["mc_version"] == "1.21"

    # Files: upgraded swapped, incompatible disabled on disk.
    mods = tmp_path / "mods"
    assert (mods / "P_up-2.0.0.jar").exists()
    assert not (mods / "P_up-1.0.0.jar").exists()
    assert (mods / "P_bad-1.0.0.jar.disabled").exists()
    assert not (mods / "P_bad-1.0.0.jar").exists()

    manifest = _manifest(tmp_path)
    assert manifest["P_up"]["version_id"] == "V_up_new"
    assert manifest["P_up"]["game_version"] == "1.21"
    assert manifest["P_bad"]["enabled"] is False

    # Rows mirror the manifest.
    with Session(engine) as session:
        bad = session.get(ContentItem, "P_bad")
        assert bad.enabled is False
        up = session.get(ContentItem, "P_up")
        assert up.version_id == "V_up_new"


def test_preview_classifies_without_touching_anything(
    client, engine, changeable, tmp_path
):
    items = [
        _item("P_up", "Upgradable", "P_up-1.0.0.jar"),
        _item("P_bad", "NoBuild", "P_bad-1.0.0.jar"),
        _item("U1", "MyPack", "my.zip", loader=None, source="upload", project=False),
    ]
    server_id = _server_with_content(engine, tmp_path, items)
    before = (tmp_path / ".lectern/manifest.json").read_text()

    resp = client.get(
        f"/api/servers/{server_id}/version/preview", params={"mc_version": "1.21"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "updated": ["Upgradable"],
        "incompatible": ["NoBuild"],
        "regenerated": [],
        "kept": ["MyPack"],
    }

    # Pure dry-run: manifest, files, and the record are untouched.
    assert (tmp_path / ".lectern/manifest.json").read_text() == before
    assert (tmp_path / "mods/P_up-1.0.0.jar").exists()
    assert (tmp_path / "mods/P_bad-1.0.0.jar").exists()
    with Session(engine) as session:
        assert session.get(Server, server_id).mc_version == "1.20.1"


def test_downgrade_refused_without_override(client, engine, changeable, tmp_path):
    server_id = _server_with_content(engine, tmp_path, [])
    resp = client.post(
        f"/api/servers/{server_id}/version",
        json={"mc_version": "1.19.4", "backup_first": False},
    )
    assert resp.status_code == 400
    assert "older" in resp.json()["detail"]
    # Version unchanged.
    with Session(engine) as session:
        assert session.get(Server, server_id).mc_version == "1.20.1"


def test_downgrade_allowed_with_override(client, engine, changeable, tmp_path):
    server_id = _server_with_content(engine, tmp_path, [])
    resp = client.post(
        f"/api/servers/{server_id}/version",
        json={"mc_version": "1.19.4", "allow_downgrade": True, "backup_first": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["server"]["mc_version"] == "1.19.4"


def test_running_server_refused(client, engine, changeable, tmp_path, monkeypatch):
    server_id = _server_with_content(engine, tmp_path, [])
    monkeypatch.setattr(vc.manager, "is_running", lambda sid: True)
    resp = client.post(
        f"/api/servers/{server_id}/version",
        json={"mc_version": "1.21", "backup_first": False},
    )
    assert resp.status_code == 409


def test_backup_first_runs_a_backup(client, engine, changeable, tmp_path, monkeypatch):
    server_id = _server_with_content(engine, tmp_path, [])
    calls: list[str] = []

    async def fake_backup(session, server, *, trigger="manual"):
        calls.append(server.id)

    from lectern import backups
    monkeypatch.setattr(backups, "create_backup", fake_backup)

    resp = client.post(
        f"/api/servers/{server_id}/version",
        json={"mc_version": "1.21", "backup_first": True},
    )
    assert resp.status_code == 200, resp.text
    assert calls == [server_id]
