"""Modrinth — mods, plugins, resource packs, modpacks (docs/technical.md §6.5).

Base: https://api.modrinth.com/v2 (descriptive User-Agent set in base.py).
- search:           GET /search?query=…&facets=[["project_type:mod"],…]
- project versions: GET /project/{id|slug}/version?loaders=…&game_versions=…
- bulk projects:    GET /projects?ids=["a","b"]  (dependency names in one call)

Update checks list the project's qualifying versions and compare ids rather
than using ``GET /version_file/{sha512}`` — the manifest already knows each
file's project, and going through the project honours the per-item release
channel (the hash endpoint can only say "newest", not "newest release").

Version dicts pass through unparsed; the pure helpers below extract what the
content manager needs (channel selection, primary file, dependencies).
"""

from __future__ import annotations

import json
from typing import Any

from .base import get_json

key = "modrinth"

BASE = "https://api.modrinth.com/v2"

# Search results and version lists move fast (new uploads); cache briefly.
_SEARCH_TTL = 300
_VERSIONS_TTL = 300
_PROJECT_TTL = 3600

# Release channels, most→least stable. An item's channel admits its own tier
# and everything more stable: "beta" accepts beta+release, "alpha" everything.
CHANNELS = ("release", "beta", "alpha")


# --- pure helpers (unit-tested) --------------------------------------------


def as_loader_list(loader: str | list[str] | None) -> list[str]:
    """Normalize a loader spec: None → [], str → [str], list passes through.
    A LIST is a compatibility chain (e.g. Quilt servers accept ["quilt",
    "fabric"] because Quilt loads Fabric mods) — facets OR them together."""
    if loader is None:
        return []
    return [loader] if isinstance(loader, str) else list(loader)


def build_facets(
    *,
    project_type: str,
    loader: str | list[str] | None,
    mc_version: str | None,
    categories: list[str] | None = None,
) -> str:
    """Modrinth facets: a JSON array of AND-ed OR-groups.

    Resource packs have no loader; mods filter by loader category. Selected
    categories are AND-ed (each in its own group), matching the site's
    filter behaviour. Loaders and content categories share the same facet
    key ("categories:…") on Modrinth.
    """
    facets: list[list[str]] = [[f"project_type:{project_type}"]]
    loaders = as_loader_list(loader)
    if loaders:
        # One OR-group: any loader in the chain qualifies.
        facets.append([f"categories:{ld}" for ld in loaders])
    for category in categories or []:
        facets.append([f"categories:{category}"])
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    return json.dumps(facets)


def channel_allows(channel: str, version_type: str) -> bool:
    """True if a version of ``version_type`` qualifies under ``channel``."""
    if channel not in CHANNELS:
        channel = "release"
    if version_type not in CHANNELS:  # unknown type — treat as least stable
        version_type = "alpha"
    return CHANNELS.index(version_type) <= CHANNELS.index(channel)


def select_version(
    versions: list[dict[str, Any]], channel: str = "release"
) -> dict[str, Any] | None:
    """Newest version whose release type qualifies under ``channel``.

    Modrinth returns versions newest-first; falls back to the newest of any
    type when nothing qualifies (a mod that only ever shipped betas).
    """
    for version in versions:
        if channel_allows(channel, version.get("version_type", "release")):
            return version
    return versions[0] if versions else None


def primary_file(version: dict[str, Any]) -> dict[str, Any] | None:
    """The file to install from a version: the one marked primary, else first."""
    files = version.get("files") or []
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


def dependency_ids(version: dict[str, Any], *, include_optional: bool) -> list[str]:
    """Project ids this version depends on, per the install policy
    (required always; optional only when opted in). Entries without a
    project_id (file-only deps) are skipped — rare and not resolvable."""
    wanted = {"required", "optional"} if include_optional else {"required"}
    return [
        d["project_id"]
        for d in version.get("dependencies") or []
        if d.get("dependency_type") in wanted and d.get("project_id")
    ]


# --- network ---------------------------------------------------------------


# Sort orders Modrinth supports for search (the "index" parameter).
SORT_INDEXES = ("relevance", "downloads", "follows", "newest", "updated")


async def search(
    query: str,
    *,
    project_type: str = "mod",
    loader: str | list[str] | None = None,
    mc_version: str | None = None,
    categories: list[str] | None = None,
    index: str = "relevance",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Faceted search. Returns the raw Modrinth payload: ``hits`` +
    ``total_hits`` (each hit: project_id, slug, title, description,
    downloads, icon_url, …)."""
    params = {
        "query": query,
        "facets": build_facets(
            project_type=project_type,
            loader=loader,
            mc_version=mc_version,
            categories=categories,
        ),
        "index": index if index in SORT_INDEXES else "relevance",
        "limit": limit,
        "offset": offset,
    }
    return await get_json(f"{BASE}/search", params=params, ttl=_SEARCH_TTL)


async def list_categories() -> list[dict[str, Any]]:
    """Modrinth's category taxonomy (``GET /tag/category``): each entry has
    ``name``, ``project_type`` and ``header`` (e.g. "categories",
    "resolutions"). Loaders are NOT in here — they're a separate tag list."""
    return await get_json(f"{BASE}/tag/category", ttl=86400)


async def list_versions(
    project_id: str, *, loader: str | list[str] | None = None, mc_version: str | None = None
) -> list[dict[str, Any]]:
    """Versions of a project compatible with ``loader`` (a single loader or
    a compatibility chain — any match qualifies) and ``mc_version``, newest
    first. ``project_id`` may be an id or a slug."""
    params: dict[str, str] = {}
    loaders = as_loader_list(loader)
    if loaders:
        params["loaders"] = json.dumps(loaders)
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    return await get_json(
        f"{BASE}/project/{project_id}/version", params=params, ttl=_VERSIONS_TTL
    )


async def get_project(project_id: str) -> dict[str, Any]:
    """One project (id or slug) — title, slug, project_type, …"""
    return await get_json(f"{BASE}/project/{project_id}", ttl=_PROJECT_TTL)


async def get_projects(project_ids: list[str]) -> list[dict[str, Any]]:
    """Bulk project lookup — one call for a whole dependency set."""
    if not project_ids:
        return []
    return await get_json(
        f"{BASE}/projects", params={"ids": json.dumps(project_ids)}, ttl=_PROJECT_TTL
    )


async def check_update(
    project_id: str,
    current_version_id: str,
    *,
    loader: str | list[str] | None,
    mc_version: str | None,
    channel: str = "release",
) -> dict[str, Any] | None:
    """Newest qualifying version if it differs from the installed one."""
    versions = await list_versions(project_id, loader=loader, mc_version=mc_version)
    newest = select_version(versions, channel)
    if newest is not None and newest.get("id") != current_version_id:
        return newest
    return None
