"""Unit tests for provider parsing + the MC→Java mapping (offline, no network)."""

from __future__ import annotations

import pytest

from lectern.providers import adoptium, fabric, mojang


# --- Adoptium MC -> Java major --------------------------------------------


@pytest.mark.parametrize(
    ("mc", "expected"),
    [
        ("1.8.9", 8),
        ("1.12.2", 8),
        ("1.16.5", 8),
        ("1.17", 16),
        ("1.17.1", 16),
        ("1.18", 17),
        ("1.20.1", 17),
        ("1.20.4", 17),
        ("1.20.5", 21),
        ("1.20.6", 21),
        ("1.21", 21),
        ("1.21.4", 21),
        ("24w14a", 21),  # snapshot -> newest LTS fallback
    ],
)
def test_java_major_for_mc(mc: str, expected: int):
    assert adoptium.java_major_for_mc(mc) == expected


def test_adoptium_binary_url():
    url = adoptium.binary_url(21, os_token="linux", arch="x64")
    assert url == (
        "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jre/hotspot/normal/eclipse"
    )


def test_detect_os_arch_returns_known_tokens():
    os_token, arch = adoptium.detect_os_arch()
    assert os_token in {"linux", "mac", "windows"}
    assert arch in {"x64", "aarch64", "x86"}


# --- Mojang ----------------------------------------------------------------


def test_parse_release_versions_filters_to_releases():
    manifest = {
        "versions": [
            {"id": "1.21", "type": "release", "url": "u1"},
            {"id": "24w14a", "type": "snapshot", "url": "u2"},
            {"id": "1.20.6", "type": "release", "url": "u3"},
        ]
    }
    assert mojang.parse_release_versions(manifest) == ["1.21", "1.20.6"]


def test_find_version_meta_url_and_server_jar():
    manifest = {"versions": [{"id": "1.21", "type": "release", "url": "META"}]}
    assert mojang.find_version_meta_url(manifest, "1.21") == "META"
    assert mojang.find_version_meta_url(manifest, "nope") is None

    meta = {"downloads": {"server": {"url": "https://cdn/server.jar"}}}
    assert mojang.parse_server_jar_url(meta) == "https://cdn/server.jar"
    assert mojang.parse_server_jar_url({"downloads": {}}) is None


def test_parse_java_major():
    # Authoritative requirement from the manifest (e.g. MC 26.2 → 25).
    assert mojang.parse_java_major({"javaVersion": {"majorVersion": 25}}) == 25
    assert mojang.parse_java_major({"javaVersion": {"majorVersion": 21}}) == 21
    # Old versions omit it -> None (caller falls back to the heuristic).
    assert mojang.parse_java_major({}) is None


# --- Fabric ----------------------------------------------------------------


def test_parse_stable_game_versions():
    data = [
        {"version": "1.21", "stable": True},
        {"version": "24w14a", "stable": False},
        {"version": "1.20.6", "stable": True},
    ]
    assert fabric.parse_stable_game_versions(data) == ["1.21", "1.20.6"]


def test_parse_loader_versions():
    data = [
        {"loader": {"version": "0.16.0", "stable": True}, "intermediary": {}},
        {"loader": {"version": "0.15.11", "stable": True}},
    ]
    assert fabric.parse_loader_versions(data) == ["0.16.0", "0.15.11"]


def test_parse_latest_stable_installer():
    assert fabric.parse_latest_stable_installer(
        [{"version": "1.0.1", "stable": False}, {"version": "1.0.0", "stable": True}]
    ) == "1.0.0"
    # falls back to first when none stable
    assert fabric.parse_latest_stable_installer([{"version": "9.9", "stable": False}]) == "9.9"
    assert fabric.parse_latest_stable_installer([]) is None


def test_fabric_server_jar_url():
    assert fabric.server_jar_url("1.20.1", "0.16.0", "1.0.1") == (
        "https://meta.fabricmc.net/v2/versions/loader/1.20.1/0.16.0/1.0.1/server/jar"
    )
