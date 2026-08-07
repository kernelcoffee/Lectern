"""Quilt/Forge/NeoForge support: provider parsing, version mapping, the
installer-run flow, and the @args-file launch mode."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from lectern.providers import forge, neoforge, quilt
from lectern.servers.install import build_launch_command, run_installer
from lectern.servers.types import REGISTRY, JarSpec

# --- NeoForge version mapping ------------------------------------------------

NEO_XML = """<metadata><versioning><versions>
<version>21.0.1</version>
<version>21.1.247</version>
<version>21.1.248</version>
<version>26.1.2.93</version>
<version>26.1.2.94</version>
<version>26.2.0.48-beta</version>
<version>26.2.0.49-beta</version>
</versions></versioning></metadata>"""


def test_neoforge_prefix_maps_both_eras():
    assert neoforge.neoforge_prefix("1.21.1") == "21.1."
    assert neoforge.neoforge_prefix("1.21") == "21.0."
    assert neoforge.neoforge_prefix("26.2") == "26.2.0."
    assert neoforge.neoforge_prefix("26.1.2") == "26.1.2."


def test_neoforge_builds_and_supported_versions():
    versions = neoforge.parse_metadata_versions(NEO_XML)
    assert neoforge.builds_for_mc(versions, "26.2") == [
        "26.2.0.49-beta",
        "26.2.0.48-beta",
    ]
    assert neoforge.builds_for_mc(versions, "1.21.1") == ["21.1.248", "21.1.247"]
    # Mojang order (newest first) is preserved; unsupported versions drop out.
    mojang_releases = ["26.2", "26.1.2", "26.1", "1.21.1", "1.20.1"]
    assert neoforge.supported_mc_versions(versions, mojang_releases) == [
        "26.2",
        "26.1.2",
        "1.21.1",
    ]


# --- Forge metadata ----------------------------------------------------------

FORGE_XML = """<metadata><versioning><versions>
<version>1.20.1-47.2.0</version>
<version>1.20.1-47.3.22</version>
<version>26.2-65.0.0</version>
<version>26.2-65.1.0</version>
</versions></versioning></metadata>"""


def test_forge_builds_and_supported_versions():
    versions = forge.parse_metadata_versions(FORGE_XML)
    assert forge.builds_for_mc(versions, "26.2") == ["65.1.0", "65.0.0"]
    assert forge.builds_for_mc(versions, "1.20.1") == ["47.3.22", "47.2.0"]
    assert forge.supported_mc_versions(versions, ["26.2", "26.1", "1.20.1"]) == [
        "26.2",
        "1.20.1",
    ]


# --- Quilt loader sorting ------------------------------------------------------


def test_quilt_loader_versions_sorted_newest_first():
    data = [
        {"loader": {"version": v}}
        for v in ["0.20.0-beta.9", "0.19.2", "0.20.0-beta.11", "0.20.0", "0.20.0-beta.2"]
    ]
    assert quilt.parse_loader_versions(data) == [
        "0.20.0",  # a release outranks its own betas
        "0.20.0-beta.11",  # numeric compare, not lexicographic
        "0.20.0-beta.9",
        "0.20.0-beta.2",
        "0.19.2",
    ]


# --- registry + resolve shapes -------------------------------------------------


def test_registry_lists_all_five_types():
    assert list(REGISTRY) == ["vanilla", "fabric", "quilt", "neoforge", "forge"]
    assert all(REGISTRY[k].needs_loader for k in ("fabric", "quilt", "neoforge", "forge"))


def test_installer_specs_have_run_instructions(monkeypatch):
    async def fake_neo_versions():
        return neoforge.parse_metadata_versions(NEO_XML)

    monkeypatch.setattr(neoforge, "list_all_versions", fake_neo_versions)
    spec = asyncio.run(REGISTRY["neoforge"].resolve_jar("26.2"))
    assert spec.is_installer
    assert spec.loader_version == "26.2.0.49-beta"
    assert "--install-server" in spec.installer_args
    assert spec.launch_glob.endswith("unix_args.txt")

    async def fake_forge_versions():
        return forge.parse_metadata_versions(FORGE_XML)

    monkeypatch.setattr(forge, "list_all_versions", fake_forge_versions)
    spec = asyncio.run(REGISTRY["forge"].resolve_jar("26.2"))
    assert spec.is_installer
    assert spec.loader_version == "65.1.0"
    assert "26.2-65.1.0" in spec.url


# --- launch command ------------------------------------------------------------


def test_build_launch_command_args_file_mode():
    cmd = build_launch_command(
        "/usr/bin/java", 4096, "@libraries/net/neoforged/neoforge/26.2.0.49/unix_args.txt"
    )
    assert cmd == [
        "/usr/bin/java",
        "-Xmx4096M",
        "-Xms4096M",
        "@libraries/net/neoforged/neoforge/26.2.0.49/unix_args.txt",
        "nogui",
    ]
    # The jar mode is unchanged.
    assert build_launch_command("/j", 1024, "server.jar")[-3:] == ["-jar", "server.jar", "nogui"]


# --- run_installer (fake java shim) ---------------------------------------------


def _shim(tmp_path, body: str) -> str:
    """A stand-in ``java`` executable for run_installer tests."""
    shim = tmp_path / "fake-java"
    shim.write_text("#!/bin/sh\n" + body)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return str(shim)


def test_run_installer_finds_args_file_and_cleans_up(tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    (server_dir / "neoforge-installer.jar").write_bytes(b"fake")
    java = _shim(
        tmp_path,
        "mkdir -p libraries/net/neoforged/neoforge/26.2.0.49\n"
        "echo '-DlegacyClassPath=…' > libraries/net/neoforged/neoforge/26.2.0.49/unix_args.txt\n",
    )
    spec = JarSpec(
        url="http://x",
        jar_name="neoforge-installer.jar",
        installer_args=["--install-server", "."],
        launch_glob="libraries/net/neoforged/neoforge/*/unix_args.txt",
    )
    target = asyncio.run(run_installer(server_dir, java, spec))
    assert target == "@libraries/net/neoforged/neoforge/26.2.0.49/unix_args.txt"
    assert not (server_dir / "neoforge-installer.jar").exists()  # cleaned up
    assert not (server_dir / "installer.log").exists()  # success → no log


def test_run_installer_jar_target(tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    (server_dir / "quilt-installer.jar").write_bytes(b"fake")
    java = _shim(tmp_path, "touch quilt-server-launch.jar\n")
    spec = JarSpec(
        url="http://x",
        jar_name="quilt-installer.jar",
        installer_args=["install", "server", "26.2"],
        launch_glob="quilt-server-launch.jar",
    )
    target = asyncio.run(run_installer(server_dir, java, spec))
    assert target == "quilt-server-launch.jar"


def test_run_installer_failure_keeps_log(tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    (server_dir / "forge-installer.jar").write_bytes(b"fake")
    java = _shim(tmp_path, "echo 'boom: no such manifest'\nexit 3\n")
    spec = JarSpec(
        url="http://x",
        jar_name="forge-installer.jar",
        installer_args=["--installServer", "."],
        launch_glob="libraries/**/unix_args.txt",
    )
    with pytest.raises(RuntimeError, match="exit 3"):
        asyncio.run(run_installer(server_dir, java, spec))
    assert b"boom" in (server_dir / "installer.log").read_bytes()
    # The installer jar is kept for a retry/post-mortem on failure.
    assert (server_dir / "forge-installer.jar").exists()


def test_run_installer_missing_target_fails(tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    (server_dir / "quilt-installer.jar").write_bytes(b"fake")
    java = _shim(tmp_path, "true\n")  # exits 0, produces nothing
    spec = JarSpec(
        url="http://x",
        jar_name="quilt-installer.jar",
        installer_args=[],
        launch_glob="quilt-server-launch.jar",
    )
    with pytest.raises(RuntimeError, match="nothing matched"):
        asyncio.run(run_installer(server_dir, java, spec))


def test_run_installer_rejects_unknown_java(tmp_path):
    server_dir = tmp_path / "srv"
    server_dir.mkdir()
    spec = JarSpec(url="http://x", jar_name="i.jar", launch_glob="x.jar")
    with pytest.raises((FileNotFoundError, OSError)):
        asyncio.run(run_installer(server_dir, os.fspath(tmp_path / "nope"), spec))
