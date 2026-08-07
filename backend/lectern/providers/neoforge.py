"""NeoForge — versions from their maven, installed via the installer jar.

There is no meta API: the maven-metadata.xml lists every NeoForge build and
the Minecraft version is encoded in the build number:

- legacy era (MC 1.x):  MC 1.21.1 → ``21.1.<build>``, MC 1.21 → ``21.0.<build>``
- modern era (MC 26+):  MC 26.2   → ``26.2.0.<build>``, MC 26.1.2 → ``26.1.2.<build>``

The installer (``neoforge-<v>-installer.jar``) is run with ``--install-server``;
it lays down ``libraries/`` and an ``@args`` file the server launches from.
"""

from __future__ import annotations

import re

from .base import get_text

METADATA_URL = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"


def installer_jar_url(neoforge_version: str) -> str:
    return f"{MAVEN}/{neoforge_version}/neoforge-{neoforge_version}-installer.jar"


# --- pure parsing (unit-tested) -------------------------------------------


def parse_metadata_versions(xml: str) -> list[str]:
    """All build ids from maven-metadata.xml, oldest→newest (maven order)."""
    return re.findall(r"<version>([^<]+)</version>", xml)


def neoforge_prefix(mc_version: str) -> str:
    """The NeoForge build prefix for a Minecraft version (see module doc)."""
    parts = mc_version.split(".")
    if parts[0] == "1":  # legacy era: drop the "1.", pad the patch
        minor = parts[1]
        patch = parts[2] if len(parts) > 2 else "0"
        return f"{minor}.{patch}."
    if len(parts) == 2:  # modern era, no patch: 26.2 → 26.2.0.
        return f"{mc_version}.0."
    return f"{mc_version}."


def builds_for_mc(all_versions: list[str], mc_version: str) -> list[str]:
    """NeoForge builds for one MC version, newest first."""
    prefix = neoforge_prefix(mc_version)
    return [v for v in reversed(all_versions) if v.startswith(prefix)]


def supported_mc_versions(all_versions: list[str], mojang_releases: list[str]) -> list[str]:
    """Mojang's release list filtered to versions NeoForge has builds for
    (keeps Mojang's newest-first ordering — the wizard preselects [0])."""
    return [mc for mc in mojang_releases if builds_for_mc(all_versions, mc)]


# --- network ---------------------------------------------------------------


async def list_all_versions() -> list[str]:
    return parse_metadata_versions(await get_text(METADATA_URL, ttl=3600))
