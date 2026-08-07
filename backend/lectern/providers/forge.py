"""Forge — versions from their maven, installed via the installer jar.

maven-metadata.xml lists builds as ``{mc}-{forge}`` (e.g. ``26.2-65.1.0``,
``1.20.1-47.2.0``); the MC version is the part before the first dash,
verbatim. The installer (``forge-{mc}-{forge}-installer.jar``) is run with
``--installServer``; modern versions (1.17+) lay down ``libraries/`` and an
``@args`` file the server launches from.
"""

from __future__ import annotations

import re

from .base import get_text

METADATA_URL = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"


def installer_jar_url(build: str) -> str:
    """``build`` is the full ``{mc}-{forge}`` id from the metadata."""
    return f"{MAVEN}/{build}/forge-{build}-installer.jar"


# --- pure parsing (unit-tested) -------------------------------------------


def parse_metadata_versions(xml: str) -> list[str]:
    """All ``{mc}-{forge}`` build ids, oldest→newest (maven order)."""
    return re.findall(r"<version>([^<]+)</version>", xml)


def builds_for_mc(all_versions: list[str], mc_version: str) -> list[str]:
    """Forge build numbers (the part after the dash) for one MC version,
    newest first."""
    prefix = f"{mc_version}-"
    return [v[len(prefix) :] for v in reversed(all_versions) if v.startswith(prefix)]


def supported_mc_versions(all_versions: list[str], mojang_releases: list[str]) -> list[str]:
    """Mojang's release list filtered to versions Forge has builds for."""
    with_builds = {v.split("-", 1)[0] for v in all_versions}
    return [mc for mc in mojang_releases if mc in with_builds]


# --- network ---------------------------------------------------------------


async def list_all_versions() -> list[str]:
    return parse_metadata_versions(await get_text(METADATA_URL, ttl=3600))
