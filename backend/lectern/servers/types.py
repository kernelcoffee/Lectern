"""Server-type registry — the ``ServerTypeProvider`` seam.

Each type knows how to enumerate its own Minecraft versions (and loader builds,
if any). Vanilla and Fabric are the first-version implementations; Quilt, Paper,
Forge, etc. are added later by registering more entries here without touching
callers. The install/launch methods land in M3.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..providers import fabric, mojang


@dataclass
class JarSpec:
    """What the install pipeline needs to fetch a runnable server jar."""

    url: str
    jar_name: str
    # Effective loader build (Fabric); ``None`` for loaderless types. Resolved
    # here so the record stores exactly what was installed.
    loader_version: str | None = None
    # Publisher-declared jar hash, verified on download when available.
    # Mojang publishes SHA1; Fabric's meta endpoint publishes none.
    sha1: str | None = None


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


REGISTRY: dict[str, object] = {t.key: t for t in (VanillaType(), FabricType())}


def get_server_type(key: str):
    """Return the provider for ``key`` or raise ``KeyError`` if unknown."""
    return REGISTRY[key]


def list_server_types() -> list[dict]:
    return [{"key": t.key, "needs_loader": t.needs_loader} for t in REGISTRY.values()]
