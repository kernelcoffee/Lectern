"""M7 — Vanilla Tweaks provider helpers + resource-pack flows (upload, VT
generate w/ fingerprint skip, Modrinth resource packs on vanilla, serve).

Network is faked: VT generate/download and Modrinth are monkeypatched; the
pure helpers (vt_version, fingerprint, pack.mcmeta parsing) run for real.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session

from lectern.content import resourcepacks as rp
from lectern.models import Server
from lectern.providers import vanillatweaks as vt

# Reuse the faked-Modrinth fixture for the Modrinth-resourcepack test.
from tests.test_content import catalog  # noqa: F401

# --- pure helpers -------------------------------------------------------------


def test_vt_version_truncates():
    assert vt.vt_version("1.20.1") == "1.20"
    assert vt.vt_version("1.20") == "1.20"
    assert vt.vt_version("26.2") == "26.2"


def test_fingerprint_order_insensitive():
    a = vt.selection_fingerprint({"b": ["y", "x"], "a": ["z"]}, "1.20.1")
    b = vt.selection_fingerprint({"a": ["z"], "b": ["x", "y"]}, "1.20.4")
    assert a == b  # same major.minor, same selection
    assert a != vt.selection_fingerprint({"a": ["z"]}, "1.20.1")
    assert a != vt.selection_fingerprint({"b": ["y", "x"], "a": ["z"]}, "1.21")
    # Empty categories don't change the selection.
    assert a == vt.selection_fingerprint({"a": ["z"], "b": ["x", "y"], "c": []}, "1.20.1")


def _pack_zip(description="A test pack", pack_format=15) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "pack.mcmeta",
            json.dumps({"pack": {"description": description, "pack_format": pack_format}}),
        )
        zf.writestr("assets/minecraft/dummy.txt", "x")
    return buf.getvalue()


def test_read_pack_meta():
    assert rp.read_pack_meta(_pack_zip()) == ("A test pack", 15)
    with pytest.raises(rp.ResourcePackError):
        rp.read_pack_meta(b"not a zip")
    # Zip without pack.mcmeta.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("whatever.txt", "x")
    with pytest.raises(rp.ResourcePackError):
        rp.read_pack_meta(buf.getvalue())


# --- flows ---------------------------------------------------------------------


def _vanilla_server(client, engine, tmp_path: Path) -> str:
    server_id = client.post(
        "/api/servers",
        json={"name": "RP", "type": "vanilla", "mc_version": "1.20.1"},
    ).json()["id"]
    with Session(engine) as session:
        server = session.get(Server, server_id)
        server.path = str(tmp_path)
        server.status = "stopped"
        session.add(server)
        session.commit()
    (tmp_path / "server.properties").write_text("motd=hi\n")
    return server_id


@pytest.fixture
def fake_vt(monkeypatch):
    """Fake VT generation + the download; counts generate calls."""
    calls = {"generate": 0}

    async def generate(packs, mc_version):
        calls["generate"] += 1
        return "http://fake-vt/download/pack.zip"

    async def fake_download(url, dest: Path, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_pack_zip("VT pack"))
        return dest

    monkeypatch.setattr(vt, "generate", generate)
    monkeypatch.setattr(rp, "download_file", fake_download)
    return calls


def test_vanillatweaks_install_and_fingerprint_skip(client, engine, tmp_path, fake_vt):
    sid = _vanilla_server(client, engine, tmp_path)
    selection = {"aesthetic": ["fancy-leaves"], "utility": []}

    resp = client.post(f"/api/servers/{sid}/vanillatweaks", json={"packs": selection})
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["kind"] == "resourcepack" and item["source"] == "vanillatweaks"
    assert item["name"] == "Vanilla Tweaks (1 packs)"
    assert (tmp_path / "resourcepacks" / item["filename"]).exists()
    assert fake_vt["generate"] == 1

    # Same selection → fingerprint match → no regeneration, same item id.
    resp = client.post(f"/api/servers/{sid}/vanillatweaks", json={"packs": selection})
    assert resp.status_code == 201
    assert resp.json()["id"] == item["id"]
    assert fake_vt["generate"] == 1

    # Changed selection → regenerates and replaces (same id, new file).
    resp = client.post(
        f"/api/servers/{sid}/vanillatweaks",
        json={"packs": {"aesthetic": ["fancy-leaves", "more-zombies"]}},
    )
    assert resp.status_code == 201
    replaced = resp.json()
    assert replaced["id"] == item["id"]
    assert replaced["filename"] != item["filename"]
    assert fake_vt["generate"] == 2
    files = list((tmp_path / "resourcepacks").iterdir())
    assert [f.name for f in files] == [replaced["filename"]]  # old zip reconciled away


def test_vanillatweaks_share_code(client, engine, tmp_path, fake_vt, monkeypatch):
    sid = _vanilla_server(client, engine, tmp_path)

    async def resolve_share_code(code):
        assert code == "abc123"
        return {"type": "resourcepacks", "version": "1.20", "packs": {"a": ["x"]}}

    monkeypatch.setattr(vt, "resolve_share_code", resolve_share_code)
    resp = client.post(
        f"/api/servers/{sid}/vanillatweaks", json={"share_code": "abc123"}
    )
    assert resp.status_code == 201
    assert resp.json()["source"] == "vanillatweaks"


def test_vanillatweaks_empty_selection_400(client, engine, tmp_path, fake_vt):
    sid = _vanilla_server(client, engine, tmp_path)
    resp = client.post(f"/api/servers/{sid}/vanillatweaks", json={"packs": {}})
    assert resp.status_code == 400


def test_upload_pack(client, engine, tmp_path):
    sid = _vanilla_server(client, engine, tmp_path)
    resp = client.post(
        f"/api/servers/{sid}/content/upload",
        files={"file": ("MyPack.zip", _pack_zip("Uploaded pack"), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["source"] == "upload" and item["name"] == "Uploaded pack"
    assert (tmp_path / "resourcepacks/MyPack.zip").exists()

    # Same filename again → 400; junk file → 400.
    assert (
        client.post(
            f"/api/servers/{sid}/content/upload",
            files={"file": ("MyPack.zip", _pack_zip(), "application/zip")},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/servers/{sid}/content/upload",
            files={"file": ("junk.zip", b"not a zip", "application/zip")},
        ).status_code
        == 400
    )

    # Uploaded packs appear in the shared content list and can be removed.
    listed = client.get(f"/api/servers/{sid}/content").json()
    assert [i["name"] for i in listed] == ["Uploaded pack"]
    assert (
        client.delete(f"/api/servers/{sid}/content/{item['id']}").status_code == 204
    )
    assert not (tmp_path / "resourcepacks/MyPack.zip").exists()


def test_serve_resource_pack_toggles_properties(client, engine, tmp_path):
    sid = _vanilla_server(client, engine, tmp_path)
    item = client.post(
        f"/api/servers/{sid}/content/upload",
        files={"file": ("Served.zip", _pack_zip(), "application/zip")},
    ).json()

    resp = client.post(
        f"/api/servers/{sid}/content/{item['id']}/serve", json={"enabled": True}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource_pack"].endswith(f"/content/{item['id']}/file")
    props = client.get(f"/api/servers/{sid}/properties").json()["properties"]
    assert props["resource-pack"] == body["resource_pack"]
    assert props["resource-pack-sha1"] == body["resource_pack_sha1"]

    # The served URL actually returns the file.
    file_resp = client.get(f"/api/servers/{sid}/content/{item['id']}/file")
    assert file_resp.status_code == 200
    assert file_resp.content == _pack_zip()

    # Disable clears both keys.
    client.post(f"/api/servers/{sid}/content/{item['id']}/serve", json={"enabled": False})
    props = client.get(f"/api/servers/{sid}/properties").json()["properties"]
    assert "resource-pack" not in props and "resource-pack-sha1" not in props


def test_modrinth_resourcepack_on_vanilla(client, engine, tmp_path, catalog):
    """A Modrinth resource pack installs on a vanilla server (loaderless)."""
    catalog["projects"]["P_rp"] = {
        "id": "P_rp",
        "slug": "pretty-pack",
        "title": "Pretty Pack",
        "project_type": "resourcepack",
        "client_side": "required",
        "server_side": "unsupported",
    }
    catalog["versions"]["P_rp"] = [
        {
            "id": "V_rp_1",
            "project_id": "P_rp",
            "version_number": "1.0.0",
            "version_type": "release",
            "files": [
                {
                    "url": "http://fake/P_rp/V_rp_1.zip",
                    "filename": "pretty-pack-1.0.0.zip",
                    "primary": True,
                    "hashes": {"sha512": "hash-V_rp_1"},
                }
            ],
            "dependencies": [],
        }
    ]
    sid = _vanilla_server(client, engine, tmp_path)
    resp = client.post(f"/api/servers/{sid}/content", json={"project_id": "P_rp"})
    assert resp.status_code == 201, resp.text
    item = resp.json()[0]
    assert item["kind"] == "resourcepack"
    assert (tmp_path / "resourcepacks/pretty-pack-1.0.0.zip").exists()
    # Loaderless: stored without a loader so update checks skip the facet.
    manifest = json.loads((tmp_path / ".lectern/manifest.json").read_text())
    assert manifest["items"][0]["loader"] is None
