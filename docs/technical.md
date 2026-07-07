# Lectern — Technical Documentation

> Architecture and integration reference. For *what* Lectern does and why, see
> [`functional.md`](./functional.md).

---

## 1. Technology Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | **Python 3.11+ / FastAPI** | Async, native WebSocket support |
| ASGI server | **uvicorn** | |
| HTTP client | **httpx** (async) | All external service calls |
| ORM / DB | **SQLModel** on **SQLite** | Pydantic + SQLAlchemy; single-file DB |
| Scheduling | **APScheduler** (`AsyncIOScheduler`) | Cron actions |
| Process mgmt | **asyncio** subprocess + **psutil** | One async task per server, no thread-per-server |
| Frontend | **React + TypeScript (Vite)** | SPA, JSON API + WebSocket |
| Styling | **Tailwind** (minimal) | Simplicity over polish |
| Packaging | **Docker / docker-compose** | One-command LAN deploy |

**Design principle — async-first.** Unlike Crafty's thread-per-server model, each Minecraft
process is driven by an `asyncio` subprocess with a background task pumping stdout directly to
WebSocket subscribers. This is the natural fit for FastAPI and keeps concurrency simple.

---

## 2. Architecture Overview

```
┌────────────────────┐   HTTP JSON    ┌─────────────────────────┐
│  React SPA (Vite)   │ ─────────────► │   FastAPI routers        │
│  - dashboard        │   WebSocket    │   (api/*)                │
│  - create wizard    │ ◄────────────► │                         │
│  - server detail    │  console       └───────────┬─────────────┘
└────────────────────┘                             │
                                                    ▼
        ┌───────────────────────────── Services ──────────────────────────────┐
        │ ServerManager   ContentManager   Installers   BackupManager          │
        │ (asyncio procs) (mods/packs/     (vanilla/    (zip/restore)          │
        │  + ConsoleHub)   updates)         fabric/quilt/paper + Java)          │
        │ Scheduler (APScheduler)          Providers (httpx clients, cached)    │
        └───────────────┬───────────────────────────────┬──────────────────────┘
                        ▼                                ▼
                 SQLite (SQLModel)                External services
                                                 (Modrinth, Vanilla Tweaks,
                                                  Mojang, Fabric, Quilt,
                                                  Paper, Adoptium)
```

### Backend module layout (`backend/lectern/`)

```
main.py            App factory; lifespan: init DB, start scheduler,
                   reconcile statuses, auto-start flagged servers.
config.py          Settings: LECTERN_DATA dir, host/port, defaults.
db.py              SQLModel engine + session dependency.
models.py          ORM tables (see §4).
ws.py              ConsoleHub: per-server subscriber sets + broadcast.
servers/
  process.py       ServerProcess: asyncio subprocess, stdout pump, stdin.
  manager.py       Registry {server_id: ServerProcess}; lifecycle ops.
  install.py       Create-server pipeline (jar + Java + files + command).
content/
  manager.py       Install/remove/toggle/update; manifest; .mrpack import.
resourcepacks.py   Vanilla Tweaks generation + pack.mcmeta parsing.
backups.py         BackupManager: zip/list/restore/prune.
scheduler.py       APScheduler wiring from Schedule rows.
providers/         httpx clients (see §6): mojang, fabric, quilt, paper,
                   modrinth, vanillatweaks, adoptium.
api/               Routers: servers, content, catalog, backups, schedules.
```

### Frontend layout (`frontend/`)

- Typed API client (`fetch` wrapper) + shared model types.
- `useConsoleSocket(serverId)` hook for the live console WebSocket.
- Pages: **Server list**, **Create wizard**, **Server detail** with tabs — Console, Properties,
  Mods/Plugins, Resource Packs, Backups, Schedule.

### Extensibility — provider abstractions (built in from day one)

The first version ships **Fabric + Modrinth + Vanilla Tweaks only**, but the deferred items
(CurseForge, Quilt/Paper/Forge, Bedrock — see [`functional.md`](./functional.md) §3) are added by
implementing an interface, **not** by editing core logic. Two seams matter:

- **`ServerType` provider** (`servers/install.py` → a registry keyed by `type`). Each provider
  knows how to: resolve available versions, download/prepare the server jar, and build the launch
  command. `vanilla` and `fabric` are the first implementations; `quilt`, `paper`, `forge`,
  `neoforge` slot in later. A `Server.edition` axis (java|bedrock) is reserved for Bedrock, which
  swaps the runtime/launch strategy but reuses the lifecycle/backup/schedule machinery.
- **`ContentSource` provider** (`providers/` → a registry keyed by `source`). Interface:
  `search`, `list_versions`, `resolve_download` (+ hashes), `check_update`. `modrinth` is the
  first implementation; `curseforge` is added later behind the same interface (it needs an API key
  and uses SHA1 fingerprints instead of SHA512 — differences stay inside that provider).

The DB already carries these axes (`Server.type`, `ContentItem.source` in §4), so no schema change
is needed to introduce new providers — only new provider classes and UI options.

---

## 3. On-Disk Layout

A single configurable data directory, `LECTERN_DATA`:

```
LECTERN_DATA/
├── lectern.sqlite                 # application database
├── java/
│   └── temurin-{8,16,17,21}/      # shared JREs, keyed by Java major version
├── cache/                         # provider catalogs, downloaded jars/mod files
├── backups/
│   └── {server_id}/{timestamp}.zip   # OUTSIDE server dirs (see backup guard, §7)
└── servers/
    └── {server_id}/
        ├── server.jar | paper-*.jar | fabric-server-launch.jar
        ├── eula.txt
        ├── server.properties
        ├── mods/          # Fabric/Quilt   (disabled files suffixed .disabled)
        ├── plugins/       # Paper
        ├── resourcepacks/
        ├── world/ ...
        └── .lectern/
            └── manifest.json   # per-server content manifest (see below)
```

### Content manifest (`.lectern/manifest.json`)

Source of truth for installed content and the basis for update checks. It mirrors the intent of
Prism's packwiz `.pw.toml` files (which is why content stays *updatable* rather than being opaque
jars). Per item:

```json
{
  "items": [
    {
      "kind": "mod",                       // mod | plugin | resourcepack | modpack
      "source": "modrinth",                // modrinth | vanillatweaks
      "project_id": "P7dR8mSH",
      "version_id": "abc123",
      "slug": "fabric-api",
      "name": "Fabric API",
      "filename": "fabric-api-0.92.0.jar",
      "side": "both",                      // client | server | both
      "sha512": "…",
      "game_version": "1.20.1",
      "loader": "fabric",
      "enabled": true
    }
  ]
}
```

Key rows are mirrored into SQLite (`ContentItem`) for fast querying/UI, but the manifest lets a
server be re-hydrated or inspected independently.

**The manifest is a reconciliation input, not a log** (pattern borrowed from mc-image-helper —
see [`references/mc-image-helper.md`](./references/mc-image-helper.md)). Content operations
compute the *desired* file set, diff it against the manifest, delete files present in
old-but-not-new, then write the new manifest. Install/remove/update and `.mrpack` (re-)import are
therefore convergent: re-running an import upgrades in place and removes files the new pack
version no longer ships.

---

## 4. Data Model (SQLModel / SQLite)

| Table | Fields (summary) |
|-------|------------------|
| **Server** | `id` (uuid), `name`, `path`, `type` (vanilla\|fabric\|quilt\|paper), `mc_version`, `loader_version`, `java_major`, `java_path`, `port`, `memory_mb`, `jvm_args`, `auto_start`, `crash_restart`, `stop_command` (default `stop`), `shutdown_timeout`, `status`, `created_at` |
| **ContentItem** | `id`, `server_id` (fk), `kind`, `source`, `project_id`, `version_id`, `slug`, `name`, `filename`, `sha512`, `channel` (release\|beta\|alpha — allowed release channel for updates), `enabled`, `installed_at` |
| **Backup** | `id`, `server_id` (fk), `filename`, `size_bytes`, `created_at`, `trigger` (manual\|scheduled) |
| **Schedule** | `id`, `server_id` (fk), `action` (start\|stop\|restart\|backup), `cron`, `enabled` |
| **Setting** | `key`, `value` (data dir, default memory, optional password hash later) |

`status` ∈ `stopped | starting | running | stopping | crashed` and is reconciled at startup.

---

## 5. Process, Console & WebSocket Model

- **`ServerProcess`** launches the server via
  `asyncio.create_subprocess_exec(java, *jvm_args, "-jar", jar, "nogui",
  stdin=PIPE, stdout=PIPE, stderr=STDOUT)`.
- A background task reads stdout **line by line**, appends to a bounded **ring buffer** (recent
  history), and pushes each line to the **`ConsoleHub`**, which fans it out to all WebSocket
  subscribers for that server.
- **Commands** from the UI are written to the process **stdin** (`proc.stdin.write(cmd + "\n")`).
- **Graceful stop** sends the configured `stop_command` (`stop`), waits up to `shutdown_timeout`,
  then falls back to `terminate()`, and finally kills the process tree via `psutil` if needed.
- **Crash detection:** an unexpected process exit sets status `crashed`; if `crash_restart` is on,
  the server is restarted — **capped** (~3 consecutive attempts, counter reset once the server
  reaches `running`) so a boot-looping server doesn't restart forever (Crafty's lesson).
- **WebSocket endpoint** `GET /ws/servers/{id}/console`: on connect, replay ring-buffer history,
  then stream live lines; inbound text messages are treated as console commands.
- **Stats:** `psutil` on the process for CPU/memory; a Minecraft **Server List Ping** to
  `127.0.0.1:{port}` for online/max players and MOTD. **Pull-based only**: computed on
  `GET /servers/{id}/stats`, driven by frontend polling — no background stats loop in v1. The
  ping uses the port read from `server.properties` (the DB column can drift), and the parser must
  handle all MOTD `description` variants (plain string, `translate`, `extra[]`) with the favicon
  optional (see [`references/crafty-4.md`](./references/crafty-4.md)).

---

## 6. External Service Integration

All calls go through thin async **httpx** clients under `providers/`, with catalog responses
cached in `cache/`. Downloads are checksum-verified where a hash is available.

**First-version services:** Mojang (§6.1), Fabric (§6.2), Modrinth (§6.5), Vanilla Tweaks (§6.6),
Adoptium (§6.7). **Roadmap services** (documented now, implemented later behind the same provider
interfaces): Quilt (§6.3), Paper (§6.4), and CurseForge (noted in §6.5).

### 6.1 Mojang — vanilla server jars & version list
- Version manifest:
  `GET https://launchermeta.mojang.com/mc/game/version_manifest_v2.json`
- Per-version JSON (URL from manifest) → `downloads.server.url` = the vanilla server jar.
- Used to populate the MC-version picker and download vanilla jars.

### 6.2 Fabric — loader metadata & server jar
Base: `https://meta.fabricmc.net/v2`
- Game versions: `GET /versions/game`
- Loader versions for a MC version: `GET /versions/loader/{mc}`
- **Ready-to-run server jar:**
  `GET /versions/loader/{mc}/{loader}/{installer}/server/jar` → downloadable Fabric server launcher.

### 6.3 Quilt — loader metadata & server *(roadmap)*
Base: `https://meta.quiltmc.org/v3` (same shape as Fabric)
- `GET /versions/loader/{mc}` for loader versions.
- Server install may require running the **Quilt installer** jar rather than a direct server-jar
  endpoint — this variance is isolated in `providers/quilt.py`. *(Flagged risk, see §9.)*

### 6.4 Paper — plugin server builds *(roadmap)*
Base: `https://api.papermc.io/v2/projects/paper`
- Versions: `GET /` → `versions[]`
- Builds for a version: `GET /versions/{version}`
- Latest build detail: `GET /versions/{version}/builds/{build}`
- Jar download:
  `GET /versions/{version}/builds/{build}/downloads/{jar}`

### 6.5 Modrinth — mods, plugins, resource packs, modpacks
Base: `https://api.modrinth.com/v2` (send a descriptive `User-Agent`)
- **Search:** `GET /search?query={q}&facets={facets}&limit=…&offset=…`
  - `facets` is a JSON array of AND-ed OR-groups, e.g.
    `[["project_type:mod"],["categories:fabric"],["versions:1.20.1"]]`
  - `project_type` = `mod` | `resourcepack` | `modpack`; Paper plugins are `project_type:mod`
    with loader categories `paper`/`bukkit`/`spigot`. Fabric/Quilt use `categories:fabric` /
    `categories:quilt`.
- **Project versions (for install/update):**
  `GET /project/{id|slug}/version?loaders=["fabric"]&game_versions=["1.20.1"]`
  → each version has `id`, `version_number`, `files[]` (`url`, `filename`, `hashes.sha512`),
  `dependencies[]` (`project_id`, `dependency_type`).
- **Update lookup by hash:** `GET /version_file/{sha512}?algorithm=sha512`.
- **Verification:** SHA512 from `files[].hashes.sha512`.
- **`.mrpack` import:** the archive contains `modrinth.index.json`
  (`dependencies` → loader + MC versions; `files[]` → `path`, `downloads[]`, `hashes.sha512`,
  `env`) plus `overrides/` (config files copied verbatim).
- **CurseForge *(roadmap)*:** a future second `ContentSource` behind the same interface. Base
  `https://api.curseforge.com/v1` (requires an API key); search `GET /mods/search?gameId=432&…`,
  files `GET /mods/{id}/files`, and SHA1-based verification/fingerprints. Its differences (key,
  hash algorithm, redistribution flags) stay contained in the provider.

### 6.6 Vanilla Tweaks — generated resource packs
Base: `https://vanillatweaks.net` (no official/stable API — isolate + fail gracefully)
- **Catalog:** `GET /assets/resources/json/{mc}/rpcategories.json` — categories and pack ids.
- **Generate:** `POST /assets/server/zipresourcepacks.php` with form field
  `packs={"category":["packId", …]}` and `version={mc}` → JSON `{ "status": "success",
  "link": "/download/…zip" }`.
- **Download:** `GET https://vanillatweaks.net{link}`.
- (Datapacks / crafting tweaks have parallel `zipdatapacks.php` / `zipcraftingtweaks.php`
  endpoints — resource packs first.)

### 6.7 Adoptium (Temurin) — Java runtimes
- **Download JRE:**
  `GET https://api.adoptium.net/v3/binary/latest/{java_major}/ga/{os}/{arch}/jre/hotspot/normal/eclipse`
  (redirects to the archive).
- **MC → Java major mapping:** `≤1.16 → 8`, `1.17 → 16`, `1.18–1.20.4 → 17`, `1.20.5+ → 21`.
- Extract into `LECTERN_DATA/java/temurin-{major}/`, shared across servers.

---

## 7. API Surface (JSON, under `/api`)

### Catalog (feeds the create wizard)
- `GET /catalog/server-types` — selectable types/flavours (initially `vanilla`, `fabric`).
- `GET /catalog/minecraft-versions?type={type}` — the Minecraft versions available **for that
  type** (Vanilla → Mojang release list; Fabric → Fabric `/versions/game`), newest first.
  Each `ServerType` provider supplies its own version list, so the wizard's version choice is
  always scoped to the chosen type.
- `GET /catalog/loaders/{fabric|quilt}/{mc}` — loader builds for a given Minecraft version.
- `GET /catalog/paper/{mc}` — Paper builds *(roadmap)*.

### Servers
- `GET /servers` · `POST /servers` (create) · `GET /servers/{id}` · `DELETE /servers/{id}`
- `POST /servers/{id}/action/{start|stop|restart|kill}`
- `GET /servers/{id}/stats`
- `GET/PATCH /servers/{id}/properties` (server.properties)
- `POST /servers/{id}/eula` (accept)
- `WS /ws/servers/{id}/console`

### Content (mods / plugins / resource packs)
- `GET /content/search?server_id=…&query=…` — Modrinth search scoped to the server
- `GET /content/projects/{id}/versions?server_id=…`
- `GET /servers/{id}/content` — installed list (+ update flags)
- `POST /servers/{id}/content` — install `{project_id, version_id}` (+ deps)
- `PATCH /servers/{id}/content/{item_id}` — enable/disable/update
- `DELETE /servers/{id}/content/{item_id}`
- `GET /servers/{id}/content/updates`
- `POST /servers/{id}/modpack` — import `.mrpack`

### Resource packs / Vanilla Tweaks
- `GET /servers/{id}/vanillatweaks/categories`
- `POST /servers/{id}/vanillatweaks` — `{ packs: {category: [ids]} }` → generate + install

### Backups
- `GET /servers/{id}/backups` · `POST /servers/{id}/backups` (create)
- `POST /servers/{id}/backups/{backup_id}/restore`
- `DELETE /servers/{id}/backups/{backup_id}`

### Schedules
- `GET /servers/{id}/schedules` · `POST …` · `PATCH …/{sid}` · `DELETE …/{sid}`

---

## 8. Backups

- **Create:** zip the server directory, excluding a configurable set of paths, writing to
  `LECTERN_DATA/backups/{server_id}/{timestamp}.zip`.
- **Path-traversal guard:** the backup destination is validated to be **outside** the server
  directory before writing/restoring (borrowed from Crafty's approach) to avoid recursive or
  destructive archives.
- **Restore:** stop the server → validate the archive (name/format) → replace server data →
  report success/failure.
- **Retention:** optional prune to keep the last N backups.

---

## 9. Deployment

- **Target:** a single Linux host on a trusted LAN.
- **Docker-compose** bundles the FastAPI backend (serving the built React SPA as static files)
  and mounts `LECTERN_DATA` as a volume. One command: `docker compose up`.
- Config via environment variables (`LECTERN_DATA`, bind host/port).
- No TLS/auth assumed at launch (LAN trust); a single optional password may be added later.

---

## 10. Risks & Open Questions

- **Quilt server install** is the least standardized of the four types; may need the Quilt
  installer jar rather than a direct server-jar endpoint. Isolated in `providers/quilt.py`.
- **Vanilla Tweaks** has no official API; the `zipresourcepacks.php` contract can change without
  notice. Keep isolated and degrade gracefully.
- **Long downloads** (server jars, JREs) must run as tracked background tasks with progress
  surfaced over WebSocket, so create-server isn't a blocking spinner.
- **Modrinth rate limits / User-Agent policy** — respect their requirements; cache catalogs.
- **Java major mapping** must be revisited as new Minecraft versions raise Java requirements.

---

## 11. Testing Strategy

- **Unit (pytest):** provider clients against recorded fixtures; MC→Java mapping; manifest and
  update-diff logic; backup path-traversal guard; `.mrpack` parsing.
- **Integration (marked):** real process start/stop, console streaming, end-to-end create → run.
- **Manual per-phase verification:** create a real server of each type, install a known mod
  (e.g. Fabric API), generate a Vanilla Tweaks pack, back up and restore — observing live
  behaviour in the browser (this is a server manager; behaviour must be seen, not just asserted).
