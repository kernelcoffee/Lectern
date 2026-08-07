"""Server-type registry — the ``ServerTypeProvider`` seam.

Each type knows how to enumerate its own Minecraft versions (and loader builds,
if any). Vanilla and Fabric are the first-version implementations; Quilt, Paper,
Forge, etc. are added later by registering more entries here without touching
callers. The install/launch methods land in M3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..providers import fabric, forge, mojang, neoforge, quilt


@dataclass
class JarSpec:
    """What the install pipeline needs to fetch a runnable server.

    Two shapes:
    - direct (vanilla, Fabric): ``url`` IS the runnable jar; download and done.
    - installer (Quilt, Forge, NeoForge): ``url`` is an installer jar to run
      with ``installer_args`` (cwd = server dir); the runnable target it lays
      down is found via ``launch_glob``.
    """

    url: str
    jar_name: str
    # Effective loader build; ``None`` for loaderless types. Resolved here so
    # the record stores exactly what was installed.
    loader_version: str | None = None
    # Publisher-declared jar hash, verified on download when available.
    # Mojang publishes SHA1; the loader metas publish none.
    sha1: str | None = None
    # Installer shape only:
    installer_args: list[str] = field(default_factory=list)
    # Glob (relative to the server dir) locating the runnable target after the
    # installer ran. A ``*.txt`` match is a JVM @args file (launched as
    # ``@<path>``); anything else is a jar launched with ``-jar``.
    launch_glob: str | None = None

    @property
    def is_installer(self) -> bool:
        return self.launch_glob is not None


class VanillaType:
    key = "vanilla"
    needs_loader = False

    async def list_minecraft_versions(self) -> list[str]:
        return await mojang.list_release_versions()

    async def list_loader_versions(self, mc_version: str) -> list[str]:
        return []

    async def resolve_jar(
        self, mc_version: str, loader_version: str | None = None
    ) -> JarSpec:
        url = await mojang.get_server_jar_url(mc_version)
        if url is None:
            raise ValueError(f"No vanilla server jar for Minecraft {mc_version}")
        sha1 = await mojang.get_server_jar_sha1(mc_version)
        return JarSpec(url=url, jar_name="server.jar", sha1=sha1)


class FabricType:
    key = "fabric"
    needs_loader = True

    async def list_minecraft_versions(self) -> list[str]:
        # Fabric's own game-version list = the MC versions Fabric supports.
        return await fabric.list_game_versions()

    async def list_loader_versions(self, mc_version: str) -> list[str]:
        return await fabric.list_loader_versions(mc_version)

    async def resolve_jar(
        self, mc_version: str, loader_version: str | None = None
    ) -> JarSpec:
        if loader_version is None:
            loaders = await fabric.list_loader_versions(mc_version)
            if not loaders:
                raise ValueError(f"No Fabric loader for Minecraft {mc_version}")
            loader_version = loaders[0]  # newest first
        installer = await fabric.latest_installer_version()
        if installer is None:
            raise ValueError("No Fabric installer available")
        url = fabric.server_jar_url(mc_version, loader_version, installer)
        # Fabric's meta endpoint serves a self-contained launcher jar.
        return JarSpec(
            url=url, jar_name="fabric-server-launch.jar", loader_version=loader_version
        )


class QuiltType:
    key = "quilt"
    needs_loader = True

    async def list_minecraft_versions(self) -> list[str]:
        return await quilt.list_game_versions()

    async def list_loader_versions(self, mc_version: str) -> list[str]:
        return await quilt.list_loader_versions(mc_version)

    async def resolve_jar(
        self, mc_version: str, loader_version: str | None = None
    ) -> JarSpec:
        if loader_version is None:
            loaders = await quilt.list_loader_versions(mc_version)
            if not loaders:
                raise ValueError(f"No Quilt loader for Minecraft {mc_version}")
            loader_version = loaders[0]  # newest first
        installer = await quilt.latest_installer_version()
        if installer is None:
            raise ValueError("No Quilt installer available")
        # No direct server-jar endpoint (unlike Fabric) — run the installer.
        return JarSpec(
            url=quilt.installer_jar_url(installer),
            jar_name="quilt-installer.jar",
            loader_version=loader_version,
            installer_args=[
                "install",
                "server",
                mc_version,
                loader_version,
                "--install-dir=.",
                "--download-server",
            ],
            launch_glob="quilt-server-launch.jar",
        )


class NeoForgeType:
    key = "neoforge"
    needs_loader = True

    async def list_minecraft_versions(self) -> list[str]:
        return neoforge.supported_mc_versions(
            await neoforge.list_all_versions(), await mojang.list_release_versions()
        )

    async def list_loader_versions(self, mc_version: str) -> list[str]:
        return neoforge.builds_for_mc(await neoforge.list_all_versions(), mc_version)

    async def resolve_jar(
        self, mc_version: str, loader_version: str | None = None
    ) -> JarSpec:
        if loader_version is None:
            builds = await self.list_loader_versions(mc_version)
            if not builds:
                raise ValueError(f"No NeoForge build for Minecraft {mc_version}")
            loader_version = builds[0]
        return JarSpec(
            url=neoforge.installer_jar_url(loader_version),
            jar_name="neoforge-installer.jar",
            loader_version=loader_version,
            installer_args=["--install-server", "."],
            launch_glob="libraries/net/neoforged/neoforge/*/unix_args.txt",
        )


class ForgeType:
    key = "forge"
    needs_loader = True

    async def list_minecraft_versions(self) -> list[str]:
        return forge.supported_mc_versions(
            await forge.list_all_versions(), await mojang.list_release_versions()
        )

    async def list_loader_versions(self, mc_version: str) -> list[str]:
        return forge.builds_for_mc(await forge.list_all_versions(), mc_version)

    async def resolve_jar(
        self, mc_version: str, loader_version: str | None = None
    ) -> JarSpec:
        if loader_version is None:
            builds = await self.list_loader_versions(mc_version)
            if not builds:
                raise ValueError(f"No Forge build for Minecraft {mc_version}")
            loader_version = builds[0]
        build = f"{mc_version}-{loader_version}"
        return JarSpec(
            url=forge.installer_jar_url(build),
            jar_name="forge-installer.jar",
            loader_version=loader_version,
            installer_args=["--installServer", "."],
            launch_glob="libraries/net/minecraftforge/forge/*/unix_args.txt",
        )


REGISTRY: dict[str, object] = {
    t.key: t
    for t in (VanillaType(), FabricType(), QuiltType(), NeoForgeType(), ForgeType())
}


def get_server_type(key: str):
    """Return the provider for ``key`` or raise ``KeyError`` if unknown."""
    return REGISTRY[key]


def list_server_types() -> list[dict]:
    return [{"key": t.key, "needs_loader": t.needs_loader} for t in REGISTRY.values()]
