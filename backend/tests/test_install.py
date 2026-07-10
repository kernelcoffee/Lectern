"""Unit tests for the M3 install pipeline — pure helpers, Java extraction, and
server-type jar resolution (offline; network calls are monkeypatched)."""

from __future__ import annotations

import asyncio
import tarfile
from pathlib import Path

from sqlmodel import Session

from lectern.db import engine, init_db
from lectern.models import Server, ServerStatus
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


# --- pipeline persists provisioned fields ----------------------------------


def test_install_persists_path_after_delete_check(monkeypatch, tmp_path):
    """Regression: the mid-install delete-check must not wipe the fields
    ``provision`` sets on the record. It once used ``session.expire_all()``,
    which reverted the uncommitted path/jar/java so a freshly-installed server
    committed ``status=stopped`` with an empty ``path`` — un-startable, and
    ``/world``/``/eula`` 409'd. The check now probes a separate session."""
    init_db()
    with Session(engine) as session:
        server = Server(name="pipe", type="vanilla", mc_version="1.20.1")
        session.add(server)
        session.commit()
        server_id = server.id

    async def fake_provision(server, *, mc_version, loader_version, emit=None):
        server_dir = tmp_path / server.id
        server_dir.mkdir(parents=True, exist_ok=True)
        server.path = str(server_dir)
        server.server_jar = "server.jar"
        server.java_path = "/opt/java/bin/java"
        server.java_major = 17
        return server_dir

    monkeypatch.setattr(install, "provision", fake_provision)
    try:
        asyncio.run(install.install_server(server_id))
        with Session(engine) as session:
            row = session.get(Server, server_id)
            assert row.status == ServerStatus.stopped.value
            assert row.path == str(tmp_path / server_id)  # not wiped
            assert row.server_jar == "server.jar"
            assert row.java_path == "/opt/java/bin/java"
    finally:
        with Session(engine) as session:
            row = session.get(Server, server_id)
            if row is not None:
                session.delete(row)
                session.commit()


def _install_with_whitelist(monkeypatch, tmp_path, whitelist: bool) -> str:
    init_db()
    with Session(engine) as session:
        server = Server(name="wl", type="vanilla", mc_version="1.20.1", whitelist=whitelist)
        session.add(server)
        session.commit()
        server_id = server.id

    async def fake_provision(server, *, mc_version, loader_version, emit=None):
        d = tmp_path / server.id
        d.mkdir(parents=True, exist_ok=True)
        server.path = str(d)
        server.server_jar = "server.jar"
        server.java_path = "/j"
        return d

    monkeypatch.setattr(install, "provision", fake_provision)
    asyncio.run(install.install_server(server_id))
    return server_id


def test_install_seeds_whitelist_by_default(monkeypatch, tmp_path):
    """A server created with the whitelist on gets white-list=true seeded into
    server.properties (secure by default — nobody can join until whitelisted)."""
    server_id = _install_with_whitelist(monkeypatch, tmp_path, whitelist=True)
    try:
        props = (tmp_path / server_id / "server.properties").read_text()
        assert "white-list=true" in props
    finally:
        _delete_server_row(server_id)


def test_install_no_whitelist_when_disabled(monkeypatch, tmp_path):
    server_id = _install_with_whitelist(monkeypatch, tmp_path, whitelist=False)
    try:
        props = (tmp_path / server_id / "server.properties").read_text()
        assert "white-list=true" not in props
    finally:
        _delete_server_row(server_id)


def _delete_server_row(server_id: str) -> None:
    with Session(engine) as session:
        row = session.get(Server, server_id)
        if row is not None:
            session.delete(row)
            session.commit()


def test_install_does_not_resurrect_a_deleted_row(monkeypatch, tmp_path):
    """A server deleted while installing must not come back via the final
    commit. (The invariant is no-resurrection; the mid-install delete-check is a
    best-effort early exit — the UPDATE of an absent row is a no-op regardless.)"""
    init_db()
    with Session(engine) as session:
        server = Server(name="racy", type="vanilla", mc_version="1.20.1")
        session.add(server)
        session.commit()
        server_id = server.id

    async def deleting_provision(server, *, mc_version, loader_version, emit=None):
        server_dir = tmp_path / server.id
        server_dir.mkdir(parents=True, exist_ok=True)
        server.path = str(server_dir)
        server.server_jar = "server.jar"
        # Simulate a concurrent delete during the (usually slow) download.
        with Session(engine) as other:
            other.delete(other.get(Server, server.id))
            other.commit()
        return server_dir

    monkeypatch.setattr(install, "provision", deleting_provision)
    asyncio.run(install.install_server(server_id))

    with Session(engine) as session:
        assert session.get(Server, server_id) is None  # not resurrected
