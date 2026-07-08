"""Content manager + endpoints (M6) against a faked Modrinth.

The fake patches the network functions on ``lectern.providers.modrinth`` (the
pure helpers keep running for real) and ``download_file`` in the content
manager, so the full install/toggle/update/remove flow — manifest, file
placement, DB mirroring, dependency expansion — runs without touching the
network. Server records are marked installed at a tmp dir, same trick as
test_properties.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from lectern.content import manager as content
from lectern.models import ContentItem, Server

# --- fake Modrinth catalog ---------------------------------------------------
#
# fabric-api  (P_fapi) — requires P_dep, optionally P_opt
# dep         (P_dep)  — requires P_fapi back (cycle guard exercise)
# opt         (P_opt)  — no deps
# beta-mod    (P_beta) — newest version is a beta, older one a release


def _project(pid: str, slug: str, title: str) -> dict:
    return {
        "id": pid,
        "slug": slug,
        "title": title,
        "project_type": "mod",
        "client_side": "optional",
        "server_side": "optional",
    }


def _version(vid, pid, number, vtype="release", deps=()):
    return {
        "id": vid,
        "project_id": pid,
        "version_number": number,
        "version_type": vtype,
        "files": [
            {
                "url": f"http://fake/{pid}/{vid}.jar",
                "filename": f"{pid}-{number}.jar",
                "primary": True,
                "hashes": {"sha512": f"hash-{vid}"},
            }
        ],
        "dependencies": [
            {"project_id": dp, "dependency_type": dt} for dp, dt in deps
        ],
    }


@pytest.fixture
def catalog(monkeypatch):
    projects = {
        "P_fapi": _project("P_fapi", "fabric-api", "Fabric API"),
        "P_dep": _project("P_dep", "dep", "Dep"),
        "P_opt": _project("P_opt", "opt", "Opt"),
        "P_beta": _project("P_beta", "beta-mod", "Beta Mod"),
    }
    versions = {
        "P_fapi": [
            _version(
                "V_fapi_1", "P_fapi", "1.0.0",
                deps=[("P_dep", "required"), ("P_opt", "optional")],
            )
        ],
        "P_dep": [
            _version("V_dep_1", "P_dep", "2.0.0", deps=[("P_fapi", "required")])
        ],
        "P_opt": [_version("V_opt_1", "P_opt", "3.0.0")],
        "P_beta": [
            _version("V_beta_2", "P_beta", "2.0.0-b1", vtype="beta"),
            _version("V_beta_1", "P_beta", "1.0.0"),
        ],
    }
    downloads: list[str] = []

    from lectern.providers import modrinth

    async def get_project(ref):
        by_slug = {p["slug"]: p for p in projects.values()}
        project = projects.get(ref) or by_slug.get(ref)
        if project is None:
            raise Exception(f"404 project {ref}")
        return project

    async def get_projects(ids):
        return [projects[i] for i in ids if i in projects]

    async def list_versions(pid, *, loader=None, mc_version=None):
        return versions.get(pid, [])

    async def fake_download(url, dest: Path, *, expected_hash=None, hash_algo="sha512"):
        downloads.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"JAR:" + url.encode())
        return dest

    monkeypatch.setattr(modrinth, "get_project", get_project)
    monkeypatch.setattr(modrinth, "get_projects", get_projects)
    monkeypatch.setattr(modrinth, "list_versions", list_versions)
    monkeypatch.setattr(content, "download_file", fake_download)
    return {"projects": projects, "versions": versions, "downloads": downloads}


def _fabric_server(client, engine, tmp_path: Path) -> str:
    server_id = client.post(
        "/api/servers",
        json={"name": "F", "type": "fabric", "mc_version": "1.20.1"},
    ).json()["id"]
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(tmp_path)
        server.status = "stopped"
        session.add(server)
        session.commit()
    return server_id


def _manifest(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / ".lectern/manifest.json").read_text())["items"]


# --- install -----------------------------------------------------------------


def test_install_with_required_dep(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    resp = client.post(f"/api/servers/{sid}/content", json={"project_id": "fabric-api"})
    assert resp.status_code == 201, resp.text
    names = sorted(i["name"] for i in resp.json())
    # Required dep pulled in, optional one not; cycle back to fabric-api guarded.
    assert names == ["Dep", "Fabric API"]
    assert (tmp_path / "mods/P_fapi-1.0.0.jar").exists()
    assert (tmp_path / "mods/P_dep-2.0.0.jar").exists()
    assert not (tmp_path / "mods/P_opt-3.0.0.jar").exists()
    assert len(_manifest(tmp_path)) == 2

    listed = client.get(f"/api/servers/{sid}/content").json()
    assert {i["id"] for i in listed} == {i["id"] for i in _manifest(tmp_path)}


def test_install_optional_deps_opt_in(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    resp = client.post(
        f"/api/servers/{sid}/content",
        json={"project_id": "P_fapi", "include_optional_deps": True},
    )
    assert resp.status_code == 201
    assert sorted(i["name"] for i in resp.json()) == ["Dep", "Fabric API", "Opt"]


def test_reinstall_replaces_in_place(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    first = client.post(f"/api/servers/{sid}/content", json={"project_id": "P_fapi"}).json()
    ids_before = {i["id"] for i in client.get(f"/api/servers/{sid}/content").json()}

    # Publish a newer version, then reinstall the same project.
    catalog["versions"]["P_fapi"].insert(
        0, _version("V_fapi_2", "P_fapi", "1.1.0", deps=[("P_dep", "required")])
    )
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_fapi"})

    listed = client.get(f"/api/servers/{sid}/content").json()
    assert {i["id"] for i in listed} == ids_before  # row ids stable
    fapi = next(i for i in listed if i["name"] == "Fabric API")
    assert fapi["version_number"] == "1.1.0"
    assert not (tmp_path / "mods/P_fapi-1.0.0.jar").exists()  # old file reconciled away
    assert (tmp_path / "mods/P_fapi-1.1.0.jar").exists()
    assert first is not None


def test_install_specific_version_and_channel(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    # channel=release skips the newer beta…
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_beta"})
    item = client.get(f"/api/servers/{sid}/content").json()[0]
    assert item["version_number"] == "1.0.0"
    # …channel=beta takes it.
    client.post(
        f"/api/servers/{sid}/content", json={"project_id": "P_beta", "channel": "beta"}
    )
    item = client.get(f"/api/servers/{sid}/content").json()[0]
    assert item["version_number"] == "2.0.0-b1"


def test_install_unknown_version_400(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    resp = client.post(
        f"/api/servers/{sid}/content",
        json={"project_id": "P_fapi", "version_id": "nope"},
    )
    assert resp.status_code == 400


def test_mod_on_vanilla_server_400(client, engine, tmp_path, catalog):
    # Vanilla servers can hold resource packs but not mods — the refusal
    # happens at resolution time (the project's type decides, M7).
    server_id = client.post(
        "/api/servers", json={"name": "V", "type": "vanilla", "mc_version": "1.20.1"}
    ).json()["id"]
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(tmp_path)
        session.add(server)
        session.commit()
    resp = client.post(
        f"/api/servers/{server_id}/content", json={"project_id": "P_fapi"}
    )
    assert resp.status_code == 400
    assert "cannot load" in resp.json()["detail"]


# --- toggle / remove ----------------------------------------------------------


def test_toggle_renames_file(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_opt"})
    item = client.get(f"/api/servers/{sid}/content").json()[0]

    resp = client.patch(
        f"/api/servers/{sid}/content/{item['id']}", json={"enabled": False}
    )
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    assert not (tmp_path / "mods/P_opt-3.0.0.jar").exists()
    assert (tmp_path / "mods/P_opt-3.0.0.jar.disabled").exists()

    client.patch(f"/api/servers/{sid}/content/{item['id']}", json={"enabled": True})
    assert (tmp_path / "mods/P_opt-3.0.0.jar").exists()
    assert not (tmp_path / "mods/P_opt-3.0.0.jar.disabled").exists()


def test_remove_cleans_up(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_opt"})
    item = client.get(f"/api/servers/{sid}/content").json()[0]

    assert client.delete(f"/api/servers/{sid}/content/{item['id']}").status_code == 204
    assert client.get(f"/api/servers/{sid}/content").json() == []
    assert _manifest(tmp_path) == []
    assert not (tmp_path / "mods/P_opt-3.0.0.jar").exists()
    # Unknown item — 404.
    assert client.delete(f"/api/servers/{sid}/content/{item['id']}").status_code == 404


def test_list_resyncs_rows_from_manifest(client, engine, tmp_path, catalog):
    """The manifest is the source of truth: if the DB mirror is lost (e.g. a
    sync that failed mid-way, as with the pre-migration 500), listing repairs
    it instead of returning stale rows."""
    sid = _fabric_server(client, engine, tmp_path)
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_opt"})
    with Session(engine) as session:
        for row in session.exec(select(ContentItem)).all():
            session.delete(row)
        session.commit()

    listed = client.get(f"/api/servers/{sid}/content").json()
    assert [i["name"] for i in listed] == ["Opt"]
    assert listed[0]["id"] == _manifest(tmp_path)[0]["id"]


# --- updates -------------------------------------------------------------------


def test_update_check_and_apply(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_opt"})
    assert client.get(f"/api/servers/{sid}/content/updates").json() == []

    catalog["versions"]["P_opt"].insert(0, _version("V_opt_2", "P_opt", "3.1.0"))
    updates = client.get(f"/api/servers/{sid}/content/updates").json()
    assert len(updates) == 1
    assert updates[0]["new_version_number"] == "3.1.0"

    item_id = updates[0]["item_id"]
    resp = client.post(f"/api/servers/{sid}/content/{item_id}/update")
    assert resp.status_code == 200
    assert resp.json()["version_number"] == "3.1.0"
    assert (tmp_path / "mods/P_opt-3.1.0.jar").exists()
    assert not (tmp_path / "mods/P_opt-3.0.0.jar").exists()
    # Nothing further to update.
    assert client.get(f"/api/servers/{sid}/content/updates").json() == []
    assert client.post(f"/api/servers/{sid}/content/{item_id}/update").status_code == 400


def test_update_respects_channel(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_beta"})
    # Newest is a beta, item is pinned to release → no update offered…
    assert client.get(f"/api/servers/{sid}/content/updates").json() == []
    # …until the channel is widened to beta.
    item = client.get(f"/api/servers/{sid}/content").json()[0]
    client.patch(f"/api/servers/{sid}/content/{item['id']}", json={"channel": "beta"})
    updates = client.get(f"/api/servers/{sid}/content/updates").json()
    assert len(updates) == 1 and updates[0]["new_version_number"] == "2.0.0-b1"


def test_update_disabled_item_keeps_disabled(client, engine, tmp_path, catalog):
    sid = _fabric_server(client, engine, tmp_path)
    client.post(f"/api/servers/{sid}/content", json={"project_id": "P_opt"})
    item = client.get(f"/api/servers/{sid}/content").json()[0]
    client.patch(f"/api/servers/{sid}/content/{item['id']}", json={"enabled": False})

    catalog["versions"]["P_opt"].insert(0, _version("V_opt_2", "P_opt", "3.1.0"))
    client.post(f"/api/servers/{sid}/content/{item['id']}/update")
    assert (tmp_path / "mods/P_opt-3.1.0.jar.disabled").exists()
    assert not (tmp_path / "mods/P_opt-3.0.0.jar.disabled").exists()
