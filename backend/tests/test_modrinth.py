"""Modrinth provider pure helpers + download checksum verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from lectern.providers import base
from lectern.providers.modrinth import (
    build_facets,
    channel_allows,
    dependency_ids,
    primary_file,
    select_version,
)

# --- facets ------------------------------------------------------------------


def test_build_facets_full():
    facets = json.loads(
        build_facets(project_type="mod", loader="fabric", mc_version="1.20.1")
    )
    assert facets == [
        ["project_type:mod"],
        ["categories:fabric"],
        ["versions:1.20.1"],
    ]


def test_build_facets_no_loader():
    # Resource packs are loaderless — no categories group.
    facets = json.loads(
        build_facets(project_type="resourcepack", loader=None, mc_version="1.20.1")
    )
    assert facets == [["project_type:resourcepack"], ["versions:1.20.1"]]


# --- channels ----------------------------------------------------------------


def test_channel_allows_matrix():
    assert channel_allows("release", "release")
    assert not channel_allows("release", "beta")
    assert channel_allows("beta", "release")
    assert channel_allows("beta", "beta")
    assert not channel_allows("beta", "alpha")
    assert channel_allows("alpha", "alpha")
    # Unknown channel falls back to release; unknown type treated as alpha.
    assert channel_allows("bogus", "release")
    assert not channel_allows("release", "bogus")
    assert channel_allows("alpha", "bogus")


def _v(vid: str, vtype: str) -> dict:
    return {"id": vid, "version_type": vtype}


def test_select_version_by_channel():
    versions = [_v("v3", "alpha"), _v("v2", "beta"), _v("v1", "release")]
    assert select_version(versions, "release")["id"] == "v1"
    assert select_version(versions, "beta")["id"] == "v2"
    assert select_version(versions, "alpha")["id"] == "v3"


def test_select_version_falls_back_to_newest():
    # A project that only ever shipped betas still installs under "release".
    versions = [_v("v2", "beta"), _v("v1", "beta")]
    assert select_version(versions, "release")["id"] == "v2"
    assert select_version([], "release") is None


# --- files / dependencies ------------------------------------------------------


def test_primary_file_prefers_primary_flag():
    version = {
        "files": [
            {"filename": "sources.jar", "primary": False},
            {"filename": "mod.jar", "primary": True},
        ]
    }
    assert primary_file(version)["filename"] == "mod.jar"
    assert primary_file({"files": [{"filename": "only.jar"}]})["filename"] == "only.jar"
    assert primary_file({"files": []}) is None


def test_dependency_ids_policy():
    version = {
        "dependencies": [
            {"project_id": "req1", "dependency_type": "required"},
            {"project_id": "opt1", "dependency_type": "optional"},
            {"project_id": None, "dependency_type": "required"},  # file-only dep
            {"project_id": "inc1", "dependency_type": "incompatible"},
        ]
    }
    assert dependency_ids(version, include_optional=False) == ["req1"]
    assert dependency_ids(version, include_optional=True) == ["req1", "opt1"]


# --- download_file checksum (M6 architecture-review follow-up) ---------------


def _mock_http(monkeypatch, payload: bytes):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=payload))
    real = httpx.AsyncClient
    monkeypatch.setattr(
        base.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
    )


def test_download_file_checksum_ok(tmp_path: Path, monkeypatch):
    _mock_http(monkeypatch, b"hello")
    good = hashlib.sha512(b"hello").hexdigest()
    dest = tmp_path / "f.jar"
    asyncio.run(base.download_file("http://x/f.jar", dest, expected_hash=good))
    assert dest.read_bytes() == b"hello"


def test_download_file_checksum_mismatch_removes_file(tmp_path: Path, monkeypatch):
    _mock_http(monkeypatch, b"hello")
    dest = tmp_path / "g.jar"
    with pytest.raises(base.ChecksumMismatch):
        asyncio.run(
            base.download_file("http://x/g.jar", dest, expected_hash="0" * 128)
        )
    assert not dest.exists()


def test_download_file_sha1(tmp_path: Path, monkeypatch):
    # Mojang publishes SHA1 (vanilla server jars).
    _mock_http(monkeypatch, b"jar")
    good = hashlib.sha1(b"jar").hexdigest()
    dest = tmp_path / "server.jar"
    asyncio.run(
        base.download_file("http://x/s.jar", dest, expected_hash=good, hash_algo="sha1")
    )
    assert dest.exists()
