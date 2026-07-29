# Lectern — Functional Specification

> What Lectern is expected to **do**. This document tracks scope and behaviour from a
> product/user perspective. Technical design lives in [`technical.md`](./technical.md).

---

## 1. Vision

Lectern is a **self-hosted web application for creating and managing modded Minecraft
servers**, built with **simplicity** as the primary design value.

It should let a single admin, from a browser on their home/private network:

- Spin up a Minecraft server (vanilla or modded) in a few clicks, without touching a terminal.
- Add mods, plugins, and resource packs from the web, and keep them up to date automatically.
- Generate Vanilla Tweaks resource packs directly.
- Back up and restore worlds, and schedule routine tasks (restarts, backups).

### Why it exists

The two closest existing tools each solve only half the problem:

| Tool | Strength | Gap Lectern fills |
|------|----------|-------------------|
| **Crafty Controller 4** | Web-based server run/manage | No mod support; heavy auth/RBAC/2FA we don't need |
| **PrismLauncher** | Excellent mod/modpack/loader management | Desktop-only; no Vanilla Tweaks support |

Lectern = *Crafty's "server runs in the background, managed over the web" model* +
*Prism's mod management* − *the auth weight* + *Vanilla Tweaks*.

---

## 2. Usage Context & Audience

- **Deployment:** Self-hosted on a trusted LAN (a home server, NAS, or spare PC).
- **User:** A single technical hobbyist admin (server owner). Not multi-tenant.
- **Security posture:** Trusted network — **no login or role-based access at launch**. A single
  optional password may be added later. No per-user permissions, no 2FA.
- **Scale:** One Lectern instance managing a handful of servers on one host.

### Deferred — not in the first version, but the design must keep the door open

These are intentionally **out of the initial scope** to keep the first version small, but Lectern's
architecture must be built so they can be added later **without a rewrite** (see the extensibility
notes in [`technical.md`](./technical.md)):

- **CurseForge** as an additional content source alongside Modrinth (pluggable content providers).
- **Bedrock edition** servers (pluggable server editions/runtimes).
- **Other loaders** — Quilt, Paper (plugins), Forge/NeoForge (pluggable server types).
- An optional **single-password gate** for when the host is less trusted.

### True non-goals (not planned)

- Multi-user accounts, roles/permissions, audit logs, 2FA/WebAuthn.
- Public/hosted SaaS, billing, or hardening for exposure to the open internet.
- In-browser world editing or map rendering.

---

## 3. Scope & Roadmap

Lectern is built in an extensible way, but the **first version deliberately targets a narrow,
end-to-end slice**:

### Initial focus (first version)

- **Fabric** mod loader (plus the **Vanilla** base server it builds on).
- **Modrinth** as the content source (mods + resource packs + `.mrpack` modpacks).
- **Vanilla Tweaks** resource packs.
- The full server lifecycle around them: create, run, console, config, Java auto-provisioning,
  backups, scheduling.

### Roadmap (designed for, added later)

| Area | Later additions |
|------|-----------------|
| Server types | **Quilt**, **Paper** (plugins), then **Forge/NeoForge**, **Bedrock** |
| Content sources | **CurseForge** alongside Modrinth |
| Access | Optional single-password gate |

**Design implication:** server types/editions and content sources are treated as **pluggable
providers** from day one (see [`technical.md`](./technical.md) §§ 2, 6). Fabric and Modrinth are
the first concrete implementations of those abstractions — not hard-coded assumptions.

Note on terminology: "mods" (Fabric/Quilt) and "plugins" (Paper) are both sourced from Modrinth
and behave the same way from the user's point of view — install, enable/disable, update, remove —
which is why one content pipeline serves them all.

---

## 4. Functional Requirements

### 4.1 Server Management

- **F-SM-1 — Create server.** A guided wizard collects, in order:
  1. **Server type / flavour** — Vanilla or a loader (Fabric first; Quilt/Paper/… later). This
     is a first-class choice, not a fixed default.
  2. **Minecraft version** — the admin explicitly picks the game version from the list Lectern
     fetches live for that type (e.g. Mojang's release list for Vanilla/Fabric). All supported
     versions are offered, newest first; older releases remain selectable. (Snapshots may be
     included behind a toggle later.)
  3. **Loader/build version** (only when the type needs one) — e.g. the Fabric loader version, or
     a Paper build. Lectern defaults to the recommended/latest but the admin can override.
  4. **Name, port, and allocated memory.**
  5. **Security** — an **Enable whitelist** option, **on by default** (secure by default): it seeds
     `white-list=true` into `server.properties`, so only whitelisted players can join. The wizard
     warns that the admin must then add players from the **Players** tab or nobody (including them)
     can connect.

  The **type + Minecraft version together drive everything downstream**: which loader builds are
  offered, which server jar is downloaded, and which Java runtime is auto-resolved (see 4.5). On
  completion Lectern downloads the correct jar, prepares the directory, and the server appears in
  the list.
- **F-SM-1a — Version compatibility.** The wizard only offers combinations that exist: loader
  builds are filtered to the chosen Minecraft version, and content searches later are scoped to the
  server's type + version so incompatible items aren't shown.
- **F-SM-2 — List servers.** A dashboard shows all servers with name, type, MC version, and live
  status (stopped / starting / running / stopping / crashed), plus quick start/stop controls.
- **F-SM-3 — Start / Stop / Restart / Kill.** The admin can control each server's process.
  Stop is graceful (sends the `stop` console command, waits a configurable timeout); Kill is a
  forced termination for hung processes.
- **F-SM-4 — Live console.** The admin sees real-time console output streamed to the browser and
  can type commands that are sent to the running server. Recent history is shown on open.
- **F-SM-5 — Edit configuration.** The admin can view and edit `server.properties` through a
  form, and adjust Lectern-managed settings (memory, JVM args, port, auto-start on boot,
  auto-restart on crash, graceful-stop timeout).
- **F-SM-6 — EULA.** On first start Lectern surfaces the Minecraft EULA and requires the admin to
  accept it; acceptance writes `eula=true`.
- **F-SM-7 — Delete server.** Removes the server and its files (with confirmation), optionally
  keeping backups.
- **F-SM-8 — Live stats.** For a running server, show CPU and memory usage and current player
  count / max players.
- **F-PL-1 — Players.** *(post-M12)* A global **player registry** (the "friends" list) where the
  admin adds players by **username or UUID**, validated against Mojang (name↔UUID resolved and
  stored canonical). From a server's **Players** tab those registered players are added to / removed
  from the server's **whitelist**, **operators**, and **banned** lists — backed by the server's own
  `whitelist.json` / `ops.json` / `banned-players.json` (correct per-list shape). On a **running**
  server the change is applied **immediately** via the matching console command (`whitelist
  add/remove`, `op`/`deop`, `ban`/`pardon` — the server rewrites its own files); on a stopped
  server the files are edited directly and apply at the next start. The registry has a card view
  (skin-face avatar + username) and a
  list view; **avatars are rendered by Lectern itself** from the Mojang skin (no dependency on a
  flaky community service like Crafatar).
- **F-APP-1 — App settings.** *(post-M12)* A Settings page edits app-level tunables — the file-
  manager upload limit, the world-import upload limit, and the create-form default memory — that
  would otherwise be env vars / config file. Overrides persist server-side (the `Setting` table,
  layered over the env defaults) and take effect immediately. Deployment settings (data dir, host,
  port) stay env-only by design.
- **F-SM-11 — File manager.** *(M12)* Browse and edit a server's files from the UI — the escape
  hatch for config Lectern doesn't model (`config/`, `ops.json`, reading `logs/`). A table
  (Name · Type · Modified · Size · Permissions) with multi-select; navigate the tree, edit text
  files in a **modal editor**, upload (button or **drag-and-drop**, multiple at once) / download,
  **unzip an archive in place**, and create/rename/delete (incl. bulk delete of a selection).
  **Every path is confined to the server directory** (absolute paths, `..`, and symlinks pointing
  out are refused; unzip is zip-slip guarded + size-capped); Lectern's own `.lectern/` is hidden
  and read-only; binary or oversized files offer a download instead of a garbled editor. Edits
  apply at the next start (like Properties).
- **F-SM-10 — Import a world at creation.** *(added post-M10)* When creating a server the admin
  can optionally start it on an **existing world** instead of a freshly-generated one — by
  uploading a world `.zip` or giving a download URL. Lectern locates the world folder inside the
  archive (`level.dat` at the root or one wrapper folder deep), extracts it into the new server's
  world directory (every path zip-slip guarded, swapped in atomically), so the server boots on the
  imported map. A **skip-files** field (glob patterns, default `*DistantHorizons*`) drops mod
  caches that bloat a world — Distant Horizons alone embeds gigabytes of LOD cache; clearing the
  field imports everything. Uploads show a **progress bar** (worlds run to several GB). Best when
  the world matches the server's Minecraft version. Creation-only for now (replacing a live
  server's world later is a backup-restore, not this flow).
- **F-SM-9 — Change Minecraft version.** *(gap found post-M8)* The admin can move an existing
  (stopped) server to another Minecraft version: Lectern re-provisions the server jar / loader
  build and the matching Java runtime, re-resolves installed content against the new version
  (updating what has a compatible build, disabling and reporting what doesn't), and surfaces a
  migration report. The world itself is upgraded **by Minecraft, in place and one-way**, at the
  next start — so the flow prominently offers a pre-change backup. **Downgrades are not
  supported by Minecraft**: selecting an older version requires an explicit "I understand my
  world may be unusable" override; the supported path back is restoring the pre-upgrade backup.

### 4.2 Mods & Plugins (Content)

- **F-C-1 — Browse & search.** Search Modrinth for content compatible with the selected server
  (results filtered by the server's loader and Minecraft version automatically).
- **F-C-2 — Install.** Install a chosen item; its file is downloaded, verified by checksum, and
  placed in the correct location (`mods/` for Fabric/Quilt, `plugins/` for Paper).
- **F-C-3 — List installed.** Show all installed content for a server with name, version, and
  whether an update is available.
- **F-C-4 — Enable / disable.** Toggle an item on/off without deleting it (e.g. disabled files
  are suffixed so the server ignores them).
- **F-C-5 — Update.** Detect when a newer compatible version exists and let the admin update to
  it (individually or in bulk).
- **F-C-6 — Remove.** Uninstall an item and clean up its file and metadata.
- **F-C-7 — Dependencies.** When installing content with required dependencies, Lectern surfaces
  them and offers to install them too.

### 4.3 Modpacks

- **F-MP-1 — Import `.mrpack`.** The admin can import a Modrinth modpack file. Lectern reads its
  manifest, installs the specified loader and Minecraft version, downloads and verifies every
  listed mod, and applies bundled config overrides — producing a ready-to-run server.

### 4.4 Resource Packs

- **F-RP-1 — Upload / install packs.** The admin can add resource packs (from Modrinth or by
  upload); Lectern reads each pack's metadata (`pack.mcmeta`) for name/format.
- **F-RP-2 — Vanilla Tweaks.** The admin can browse Vanilla Tweaks resource-pack categories for
  the server's Minecraft version, select the tweaks they want, and have Lectern generate the
  combined pack and install it. (This is the capability Prism lacks.)
- **F-RP-3 — Serve to clients (optional).** Lectern can set the server's `resource-pack` /
  `resource-pack-sha1` properties so players are prompted to download the pack.

### 4.5 Java Runtime Management

- **F-J-1 — Auto-provision Java.** Lectern automatically determines the Java version each
  Minecraft version requires and downloads the matching Temurin (Adoptium) JRE if not already
  present. Runtimes are shared across servers that need the same version. The admin never has to
  install Java manually.

### 4.6 Backups

- **F-B-1 — Create backup.** On demand, Lectern creates a compressed archive of a server's data,
  stored outside the server directory.
- **F-B-2 — List backups.** Show existing backups per server with timestamp and size.
- **F-B-3 — Restore.** Restore a chosen backup; the server is stopped first, the archive is
  validated, and the data is replaced.
- **F-B-4 — Retention.** Optionally prune old backups automatically (e.g. keep the last N).

### 4.7 Scheduling

- **F-SC-1 — Scheduled actions.** The admin can schedule recurring actions per server —
  **start**, **stop**, **restart**, or **backup** — using a cron-style schedule, and enable or
  disable each schedule.

---

## 5. Key User Flows

### 5.1 Create and run a modded server (happy path)

1. Admin clicks **New Server** → picks **Fabric**.
2. Selects Minecraft **1.20.1** and a Fabric loader build.
3. Names it, sets port and memory → **Create**.
4. Lectern downloads the Fabric server jar and the correct JRE, prepares the folder.
5. Admin opens the server, accepts the **EULA**, clicks **Start**.
6. The **console** streams live; the server reaches "Done".
7. Admin opens **Mods**, searches "Fabric API", clicks **Install** (dependencies handled).
8. Restarts the server; the mod loads.

### 5.2 Add Vanilla Tweaks

1. Admin opens a server → **Resource Packs** → **Vanilla Tweaks**.
2. Picks tweaks from the categories for that MC version → **Generate**.
3. Lectern builds the pack, installs it, and (optionally) sets it as the server resource pack.

### 5.3 Backup & restore

1. Admin clicks **Backup Now**; an archive is created.
2. Later, something breaks → admin picks a backup → **Restore**.
3. Lectern stops the server, validates and restores the archive, confirms success.

### 5.4 Keep content updated

1. Admin opens **Mods**; items with newer versions are flagged **Update available**.
2. Admin clicks **Update All**; Lectern fetches and verifies the new versions and swaps them in.

---

## 6. Acceptance Criteria (first-version definition of done)

- Can create, start, stop, restart, and delete **Vanilla** and **Fabric** servers from the
  browser, with Java provisioned automatically.
- Live console streaming and command input work for a running server.
- Can search, install, enable/disable, update, and remove **Modrinth** mods and resource packs.
- Can import a `.mrpack` and get a runnable server.
- Can generate and install a **Vanilla Tweaks** resource pack.
- Can create, list, and restore backups, and schedule restarts/backups.
- Runs on a LAN host via a single documented command (e.g. `docker compose up`).
- Provider abstractions (server type, content source) are in place so Quilt/Paper/Forge and
  CurseForge can be added later without reworking the core.
