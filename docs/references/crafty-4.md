# Reference analysis — Crafty Controller 4

> Source: `references/crafty-4/` (git-ignored checkout). Python/Tornado web panel for managing
> game servers (Minecraft Java/Bedrock, Steam, Hytale). Peewee ORM on SQLite, thread-per-server,
> server-rendered templates + WebSocket. The closest existing product to Lectern — most of our
> functional scope (console, backups, schedules, stats) has a battle-tested counterpart here.

## Architecture in one paragraph

A singleton `TasksManager` (APScheduler) plus one `ServerInstance` object per server
(`app/classes/shared/server.py`, ~2000 lines) that owns *everything* for that server: the
`subprocess.Popen` handle, a stdout-reader thread feeding a class-level ring buffer
(`ServerOutBuf`), **two** per-server `BackgroundScheduler`s (stats polling, dir-size calc, crash
watcher, update watcher), backup threads, webhook dispatch, and Prometheus registries. The web
layer (Tornado handlers under `app/classes/web/routes/api/`) is a REST-ish JSON API with
users/roles/per-server-permissions and JSON-schema request validation.

## Functional points to adopt

| Feature | How Crafty does it | Lectern milestone |
|---|---|---|
| **Stats only when watched** | `realtime_stats` polls psutil/ping every 5 s **only if WebSocket clients are connected** ("no point in burning cpu"); a separate job persists stats every 30 s for history graphs | **M5** — gate stats polling on active WS/HTTP consumers |
| **Server List Ping** | Hand-rolled wiki.vg status ping (`remote_stats/ping.py`): varint framing, handshake+status, 5 s socket timeout; parses all MOTD `description` variants (plain string, `translate`, `extra[]` with format codes), `favicon` base64 icon, player sample list, version/protocol | **M5** — our `servers/stats.py`; support at least string + `extra[]` descriptions, treat icon as optional |
| **True game port** | Reads `server-port` from `server.properties` at ping time instead of trusting the DB column (cached per start) | **M5** — ping the port from `server.properties`, not only `Server.port` |
| **Crash-restart cap** | Watcher increments `restart_count`; after 3 restarts it gives up, marks crashed, stops watching. Also a per-server `ignored_exits` list (default `"0"`) — exit codes treated as clean | **M5/M11** — our `crash_restart` currently retries forever; add a restart cap (and consider ignored exit codes) |
| **Auto-start with delay** | `auto_start` + `auto_start_delay` (default 10 s) per server, scheduled at boot — staggers JVM startups | **M11** — add a delay to our planned auto-start |
| **Backup configs, not one backup** | Multiple named backup configs per server: own `backup_location`, `excluded_dirs`, `max_backups` (retention prune), `compress`, `shutdown` (stop server first), `before`/`after` command hooks, `enabled`, live `status` JSON on the row | **M9** — at minimum: exclusions, retention, compress flag, optional stop-before-backup; `before`/`after` hooks are cheap wins |
| **Backup location guard** | `validate_backup_location`: `resolve()` both paths, reject if server path == target or is any parent of it (recursive-archive protection) — the guard our technical.md §8 already cites | **M9** — implement exactly this check |
| **Restore flow** | Stop server → validate archive/path traversal → replace dir (optional `in_place`) → notify all users of success/failure | **M9** |
| **Richer schedules** | `Schedules` row: `interval`+`interval_type` *or* `cron_string`, `one_time` flag, `action` + optional raw console `command` action, **chained tasks** (`parent` id + `delay` seconds after parent fires), computed `next_run` shown in UI | **M10** — adopt "send command" as a schedulable action, `one_time`, and surface `next_run`; chaining is post-v1 |
| **EULA prompt on start** | Start detects `eula.txt` not accepted → pushes a prompt over WS instead of failing silently; auto-start logs an explicit error | **done (M4)** — matches our gate; keep the auto-start-vs-EULA error path in mind for M11 |
| **Graceful stop countdown** | Sends `stop_command`, polls every 2 s broadcasting "N seconds until force close" to the console, then kills the psutil process tree | **M5 polish** — we already escalate; broadcasting the countdown into the console is a nice UX touch |
| **Player cache** | Persists seen players (`players_cache.json`) with last-seen/online, shown alongside live ping data | post-v1 |
| **Webhooks** | `@callback` decorator on lifecycle methods fires provider webhooks (Discord/Slack/Mattermost/Teams) on start/stop/crash/backup with per-event triggers | post-v1 — good shape: provider interface + per-server webhook rows with `trigger` list |
| **Dir-size as background job** | Server directory size is expensive → computed on a scheduled job and cached, never inline in a request | **M5** — if we show world/server size, compute it off-request |
| **Import existing server + jar catalog** | `import_helper` (zip/dir import with traversal checks) and "Big Bucket" curated jar catalog (remote manifest + healthcheck + local cache) | post-v1 |
| **Auth model** | Users/roles/per-server permission enums, API tokens, TOTP, passkeys | out of scope (LAN trust), but the per-server permission enum is the shape to copy if auth ever lands |

## Technical points to adopt

- **Bounded console history with batched broadcast.** `ServerOutBuf` caps scrollback at a
  configurable `virtual_terminal_lines` and batches stdout lines (~20 lines / 100 ms flush)
  before broadcasting, so a spamming server doesn't melt the WS layer. Our `ConsoleHub` has the
  ring buffer; if console spam becomes a problem, batch publishes rather than per-line sends.
- **ANSI stripping.** Crafty strips ANSI escape sequences from console lines server-side before
  display. Modded servers *will* emit color codes — worth doing in `ConsoleHub` or the frontend.
- **Exit-code-aware crash detection.** Not every non-zero exit is a crash and not every zero exit
  after `/stop` typed in-game is tracked state — Crafty's configurable `ignored_exits` decouples
  "clean exit" from hardcoded `code == 0`.
- **Path-traversal discipline.** `Helpers.validate_traversal(base, candidate)` is called at every
  user-influenced path (env files, restores, imports). When M9 backups/restore and file editing
  land, funnel every path through one such helper.
- **Refuse suspicious executables.** Start-up refuses a `java` binary located *inside* the server
  directory (a mod could drop one). Cheap sanity check for `build_launch_command`.
- **Snapshot (deduplicating) backups.** Besides zip backups, Crafty has a content-addressed mode:
  blake2-hash every file into a shared store + a manifest of `hash:path` lines per backup — files
  stored once across all backups, designed for future encryption/S3. Overkill for v1, but the
  natural evolution of M9 if backup storage balloons.

## Anti-patterns to avoid (deliberately)

- **God object.** `ServerInstance` mixes process, stats, backups, schedulers, webhooks, and
  Prometheus in one class. Lectern's `process.py` / `manager.py` / `ws.py` split is the fix; keep
  M5+ features (stats, properties) in their own modules rather than growing `ServerProcess`.
- **Thread sprawl.** Per server: a start thread, a stdout-reader thread, backup threads, and two
  `BackgroundScheduler`s. Backup dedup is done by *enumerating live threads by name*. Our
  async-first model (one pump task per process, one app-level scheduler in M10) is the deliberate
  counter-design — technical.md §1 already states this.
- **Polling for lifecycle events.** Crash detection polls `check_running()` every 30 s; stop polls
  every 2 s. We get the same for free by `await`ing `proc.wait()` — keep it event-driven.
- **Class-level mutable state.** `ServerOutBuf.lines` is a class attribute keyed by server id —
  hidden global. Our hub instance owned by the manager is the right scoping.
- **Presentation in the backend.** Crafty HTML-escapes and colorizes console lines server-side and
  broadcasts page-specific payloads (`/panel/server_detail` + params). Keep Lectern's WS payloads
  plain text/JSON; rendering belongs to the SPA.
- **`time.sleep()` in workflows** (3–5 s pauses "to let people read the message") — never in an
  async app; UI timing is the frontend's job.
