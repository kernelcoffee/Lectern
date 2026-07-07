# Reference analysis — mc-image-helper (itzg)

> Source: `references/mc-image-helper/` (git-ignored checkout). Java CLI (picocli +
> reactor-netty) that powers `itzg/docker-minecraft-server`: at container start it *declaratively
> provisions* a server — install loaders (Fabric/Quilt/Forge/NeoForge/Paper/Purpur), mods
> (Modrinth/CurseForge), modpacks (`.mrpack`), Vanilla Tweaks packs, patch configs, set
> `server.properties` from env vars, manage whitelist/ops. It has no UI and no process
> management; it is the best-in-class reference for Lectern's **install/content pipeline**
> (M6–M8) precisely because it must be **idempotent** — the same command re-runs on every
> container start and must converge, not duplicate.

## The one big idea: per-module manifests + diff cleanup

Every installer command writes a manifest `.{module}-manifest.json` into the output directory
(`files/Manifests.java`, `BaseManifest`): a timestamp, the command's inputs (project refs, share
codes, modpack version…), and the **relative paths of every file it wrote**. On the next run:

1. load the old manifest (if any);
2. compute the new desired file set;
3. `Manifests.cleanup(dir, old, new)` → delete files in *old − new*;
4. write the new manifest (or remove it when the file list is empty).

Consequences: removing a mod from the desired list removes its jar; a modpack upgrade deletes
files the new version no longer ships; `allFilesPresent(dir, manifest)` lets a command skip all
work when nothing changed (with glob-based `ignoreMissingFiles` exceptions). **This is exactly
what Lectern's `.lectern/manifest.json` (technical.md §3) should do in M6/M8** — not just record
installs, but be the reconciliation input that makes install/update/remove and `.mrpack`
re-import convergent operations.

## Technical points to adopt

- **Idempotent downloads** (`http/`): `skipExisting` (file already there) and `skipUpToDate`
  (conditional GET via `If-Modified-Since` / `NotModifiedHandler`) on every fetch; filename
  derivation from `Content-Disposition`; content-type validation so an HTML error page never gets
  saved as a `.jar`. → Lectern's `download_file` (M6, and the M6 checksum follow-up already in
  the plan).
- **Streaming checksum validation** (`files/Checksums.java`, `ChecksumAlgo`): digest while
  reading, algorithm chosen per provider (Modrinth SHA512, CurseForge SHA1, Mojang SHA1) behind
  one enum. Matches the plan's "algorithm differences stay inside the provider".
- **Structured API cache** (`cache/ApiCachingImpl`): `.cache/{namespace}/{operation}/` content
  files + a `cache-index.json`, per-operation TTL overrides (default 48 h), expired entries pruned
  at startup, corrupt index → wipe and rebuild. Our `providers/base.get_json` hash-blob cache
  works, but adopt the ideas when it grows: namespace per provider, TTL per endpoint, and an
  index that can be pruned/inspected.
- **Dependency expansion with a cycle guard** (`modrinth/ModrinthCommand`): recursive resolution
  of Modrinth version `dependencies[]`, filtered by policy `NONE | REQUIRED | OPTIONAL`
  (OPTIONAL implies REQUIRED), with a `projectsProcessed` set preventing loops and duplicate
  installs, and **bulk** project lookup (one API call for all ids/slugs). → M6's "install (+ deps)".
- **Version selection by release channel**: pick the newest version whose type is *sufficient*
  for the allowed `release | beta | alpha`, with per-project overrides of the global default. →
  M6 update checks ("newest release, unless this mod is pinned to beta").
- **`.mrpack` install done right** (`modrinth/ModrinthPackInstaller`):
  - filter `files[]` by `env.server` (`required`/`optional` install, `unsupported` skip) with a
    user-visible `--include-files` escape hatch for mislabeled mods, plus `--exclude-files`;
  - extract `overrides/` then `server-overrides/` (server wins), with ant-style exclusion
    patterns;
  - read `dependencies` from the index and **auto-install the pinned modloader** (fabric →
    `FabricLauncherInstaller`, etc.) — a Lectern `.mrpack` import (M8) must set/verify the
    server's loader version, not assume the wizard's;
  - bounded concurrent downloads (default 10);
  - a machine-readable results file (e.g. resolved `SERVER` jar path) for the caller — analogous
    to us persisting `server_jar`/`loader_version` on the record.
- **Project-ref grammar**: `[loader:]id|slug[?][:version|versionId|release-type]` — `?` marks a
  project *optional* (missing → warn, not fail). A tiny DSL worth borrowing for seeding servers
  or a power-user "batch install" box; the optional marker is a good idea for modpack-adjacent
  UX.
- **Typed property definitions** (`properties/SetPropertiesCommand` + `PropertyDefinition`): a
  JSON schema of known `server.properties` keys — type (bool/int/string), allowed values,
  placeholder interpolation, remove-when-blank — validated before writing; file rewritten only
  when something actually changed. → M5's properties editor: ship a definitions map for common
  keys so the UI can render proper widgets and validate, instead of a raw key/value grid.
- **Name→UUID resolution for whitelist/ops** (`users/ManageUsersCommand`): Mojang API with
  PlayerDB fallback, offline-mode UUID derivation (`UuidQuirks`), merge-vs-synchronize semantics
  for existing `whitelist.json`/`ops.json`. → the natural post-M5 "Players" feature for Lectern.
- **Vanilla Tweaks via share codes** (`vanillatweaks/`): accepts VT *share codes* resolved
  through their API in addition to raw pack lists, and fingerprints the sorted pack selection so
  the manifest can detect "same selection, skip re-download". → M7: support share codes — much
  friendlier than rebuilding category pickers — and hash the selection for idempotence.
- **`LATEST`/`SNAPSHOT` version tokens** (`versions/ResolveMinecraftVersionCommand`) resolved
  against the Mojang manifest; `JavaReleaseCommand` reads `javaVersion` per MC version (we
  already do this in the install pipeline).
- **WireMock fixture tests**: every provider client is tested against recorded HTTP fixtures
  (`src/test/resources/**` mappings) — the Java twin of our "recorded fixtures" strategy
  (technical.md §11); their fixtures are a handy catalog of real API response shapes.

## Functional points to adopt

| Capability | Take-away for Lectern | Milestone |
|---|---|---|
| Declarative content sets | Treat installed mods as a *desired list* reconciled by manifest diff, not a series of one-off installs | **M6** |
| Dependency policy | Expose "install required deps" (default) and "also optional deps" at install time | **M6** |
| Release-channel pins | Per-mod allowed channel (release/beta/alpha) governing updates | **M6** |
| `.mrpack` server-env filtering + overrides + loader pinning | Import must skip client-only mods (with override), apply `server-overrides`, and install the pack's pinned loader | **M8** |
| Modpack upgrade = re-reconcile | Upgrading a pack removes files the new version dropped (manifest diff) | **M8** |
| Typed `server.properties` definitions | Validating editor with real widgets per key | **M5** |
| VT share codes + selection fingerprint | Easier UX and idempotent regeneration | **M7** |
| Whitelist/ops management with UUID resolution | "Players" tab candidate | post-v1 |
| Optional-project marker (`?`) | Don't fail a whole batch because one optional item is unavailable | M6/M8 |

## What *not* to take

- It's a **one-shot CLI**, not a daemon: no process lifecycle, console, stats, or scheduling —
  nothing to learn there for M4/M5/M10.
- Reactor/Netty reactive plumbing (Mono/Flux) is Java-ecosystem ceremony; `httpx` + asyncio
  already gives us the equivalent with less machinery.
- Its configuration surface is env-var driven (container idiom); Lectern's equivalent inputs come
  from the DB/UI. Copy the *semantics* (idempotence, converge-on-desired-state), not the env-var
  interface.
- CurseForge support shows the second-`ContentSource` shape (API key, SHA1 fingerprints, distinct
  file model) but stays roadmap for us, as documented.
