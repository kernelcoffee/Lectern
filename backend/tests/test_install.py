"""Unit tests for the M3 install pipeline — pure helpers, Java extraction, and
server-type jar resolution (offline; network calls are monkeypatched)."""

from __future__ import annotations

import asyncio
import tarfile
from pathlib import Path

from lectern.providers import adoptium, fabric, mojang
from lectern.servers import install, types


# --- pure helpers ----------------------------------------------------------


def test_render_server_properties_pins_port():
    text = install.render_server_properties(25570)
    assert "server-port=25570\n" in text
    assert text.endswith("\n")
    # deterministic (sorted) output
    assert text == install.render_server_properties(25570)


def test_build_launch_command_vanilla():
    cmd = install.build_launch_command("/j/bin/java", 2048, "server.jar")
    assert cmd == ["/j/bin/java", "-Xmx2048M", "-Xms2048M", "-jar", "server.jar", "nogui"]


def test_build_launch_command_includes_extra_jvm_args():
    cmd = install.build_launch_command("/j/bin/java", 1024, "server.jar", jvm_args="-XX:+UseG1GC -Dfoo=bar")
    assert "-XX:+UseG1GC" in cmd and "-Dfoo=bar" in cmd
    # extra args come before -jar
    assert cmd.index("-Dfoo=bar") < cmd.index("-jar")


# --- Java runtime extraction ----------------------------------------------


def test_find_java_exe_locates_bin_java(tmp_path: Path):
    exe = tmp_path / "jdk-21-jre" / "bin" / "java"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    found = adoptium.find_java_exe(tmp_path, "linux")
    assert found == exe


def test_extract_tar_gz_then_find(tmp_path: Path):
    # Build a minimal .tar.gz that looks like a Temurin JRE archive.
    src = tmp_path / "src" / "jdk-21-jre" / "bin"
    src.mkdir(parents=True)
    (src / "java").write_text("bin\n")
    archive = tmp_path / "jre.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(tmp_path / "src" / "jdk-21-jre", arcname="jdk-21-jre")

    dest = tmp_path / "temurin-21"
    adoptium._extract(archive, dest)
    assert adoptium.find_java_exe(dest, "linux") == dest / "jdk-21-jre" / "bin" / "java"


# --- server-type jar resolution -------------------------------------------


def test_vanilla_resolve_jar(monkeypatch):
    async def fake_url(version):
        assert version == "1.20.1"
        return "https://example/server.jar"

    monkeypatch.setattr(mojang, "get_server_jar_url", fake_url)
    spec = asyncio.run(types.VanillaType().resolve_jar("1.20.1"))
    assert spec.url == "https://example/server.jar"
    assert spec.jar_name == "server.jar"
    assert spec.loader_version is None


def test_fabric_resolve_jar_picks_newest_loader(monkeypatch):
    async def fake_loaders(mc):
        return ["0.16.0", "0.15.11"]  # newest first

    async def fake_installer():
        return "1.0.1"

    monkeypatch.setattr(fabric, "list_loader_versions", fake_loaders)
    monkeypatch.setattr(fabric, "latest_installer_version", fake_installer)

    spec = asyncio.run(types.FabricType().resolve_jar("1.20.1"))
    assert spec.loader_version == "0.16.0"
    assert spec.jar_name == "fabric-server-launch.jar"
    assert "1.20.1/0.16.0/1.0.1/server/jar" in spec.url


def test_fabric_resolve_jar_respects_explicit_loader(monkeypatch):
    async def fake_installer():
        return "1.0.1"

    monkeypatch.setattr(fabric, "latest_installer_version", fake_installer)
    spec = asyncio.run(types.FabricType().resolve_jar("1.20.1", loader_version="0.15.7"))
    assert spec.loader_version == "0.15.7"
