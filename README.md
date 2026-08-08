# Lectern

[![CI](https://github.com/kernelcoffee/Lectern/actions/workflows/ci.yml/badge.svg)](https://github.com/kernelcoffee/Lectern/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/tag/kernelcoffee/Lectern?label=release&color=2ea44f)](https://github.com/kernelcoffee/Lectern/tags)
[![GHCR](https://img.shields.io/badge/ghcr.io-kernelcoffee%2Flectern-1f6feb?logo=docker&logoColor=white)](https://github.com/kernelcoffee/Lectern/pkgs/container/lectern)
![Python](https://img.shields.io/badge/python-3.14-3776ab?logo=python&logoColor=white)
![React](https://img.shields.io/badge/react-18-58c4dc?logo=react&logoColor=white)
[![Built with Claude](https://img.shields.io/badge/built%20with-Claude-D97757?logo=claude&logoColor=white)](https://claude.com/claude-code)
[![License: MIT](https://img.shields.io/badge/license-MIT-8b949e)](LICENSE)

Lectern is a simple-to-install, self-hosted **web app for creating and managing modded Minecraft servers**.
Your servers run in the background on your own hardware; everything — process control, live console, mods, backups, schedules, even a Velocity proxy in front of your instances — is managed from the browser. No accounts, no heavy auth: built for a trusted LAN.

Choose a server and Lectern takes care of everything else — it downloads what that version
needs (including the right Java) and runs it for you.

![Dashboard — servers, players, and the recent-events feed](docs/screenshots/dashboard.png)

| Live console | Resource metrics |
|---|---|
| ![Live console](docs/screenshots/console.png) | ![CPU / memory / player history](docs/screenshots/metrics.png) |

| Modrinth mods | Player registry |
|---|---|
| ![Installed mods from Modrinth](docs/screenshots/mods.png) | ![Player cards with self-rendered avatars](docs/screenshots/players.png) |

## Features

- **Servers** — Vanilla, **Fabric**, **Quilt**, **NeoForge** and **Forge**, with a live
  console, stats, an online player roster, and easy settings editing.
- **Proxy** — put your servers behind **one address** with a **Velocity** proxy: players
  connect once and hop between servers. Pick the servers to put behind it — Lectern does
  all the wiring for you.
- **Mods & packs** — search and install mods from **Modrinth** (dependencies come along
  automatically, updates are one click), add **Vanilla Tweaks** packs, or import a whole
  modpack (`.mrpack`).
- **Version changes** — move a server to a new Minecraft version. Compatible mods get
  updated, the rest are safely disabled — and you see what will happen before you commit.
- **Worlds** — start a new server from an existing world: upload a zip or paste a link.
- **Backups** — create, restore, download; old ones are cleaned up automatically.
- **Scheduling** — nightly restarts, daily backups, timed commands — set up in plain
  words, no cron knowledge needed.
- **Event timeline** — what happened while you were away: crashes, restarts and backup
  results, so a 3 a.m. incident is still visible in the morning.
- **File manager** — browse, edit, upload and unzip server files right in the browser.
- **Settings** — app-level limits and defaults, editable from the UI.

Paper and Bedrock support may come later.

> **Why no CurseForge?** The official CurseForge API is key-gated per application. Desktop
> launchers can embed an approved key in their compiled binaries, but Lectern is open source with
> a public image — an embedded key would be public, which the key terms forbid — so every
> deployment would need to apply for its own key just to search mods. Modrinth needs no key and
> covers the actively-maintained modding ecosystem; for CurseForge-only mods, download the jar in
> a browser and drop it onto the server with the file manager (mods land in `mods/`).

## Quick start (production, one command)

Requires Docker (or Podman) with the Compose plugin.

```bash
docker compose up -d
```

Then open **http://localhost:8000** (or `http://<host-ip>:8000` on your LAN). Minecraft clients
connect on **25565**.

This pulls the pre-built image `ghcr.io/kernelcoffee/lectern` — a single container that serves the
API and the built web UI on one port. All state (database, downloaded JREs, caches, backups,
server files) lives in the `lectern_data` volume.

Images are published on two channels, selected with `LECTERN_TAG`:

| Tag | Tracks |
|---|---|
| `latest` / `stable` | The newest tagged release (default). |
| `master` | Every push to the `main` branch that passes CI — bleeding edge. |
| `1.2.3` / `1.2` | An exact release / its patch line. |

```bash
LECTERN_TAG=1.2.3 docker compose up -d          # pin a version
LECTERN_TAG=master docker compose up -d         # ride the main branch
docker compose pull && docker compose up -d     # upgrade in place
```

> **Multiple servers / custom ports:** the compose file publishes a single `25565`. To run
> several servers at once, or use custom per-server ports, publish a range instead — e.g.
> `"25565-25575:25565-25575"` in `docker-compose.yml`.
>
> **Build from source instead of pulling:** run `docker build -t lectern .` against the
> `Dockerfile`, or use the hot-reload stack in `docker-compose.dev.yml` (see Development below).

## Configuration

Everything is optional. Deployment settings are environment variables; the operational tunables
below them are also editable at runtime from the in-app **Settings** page (stored in the database).

| Variable | Default | Purpose |
|---|---|---|
| `LECTERN_DATA` | `/data` (image) | Directory for the DB, JREs, caches, backups, and server files. |
| `LECTERN_HOST` | `0.0.0.0` | API/web bind address. |
| `LECTERN_PORT` | `8000` | API/web port. |

Lectern is built for a **trusted LAN** — there is no user auth. Don't expose it directly to the
internet; put it behind a VPN or a reverse proxy with access control if you need remote access.

## Development

### With Docker/Podman (hot-reload)

```bash
docker compose -f docker-compose.dev.yml up
```

- Backend: http://localhost:8000 (health at `/api/health`)
- Frontend (Vite, proxies `/api`): http://localhost:5173

### Locally, without containers

Backend:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
LECTERN_DATA=./data uvicorn lectern.main:app --reload
python -m pytest -q            # tests
```

Frontend:

```bash
cd frontend
npm install
npm run dev                    # http://localhost:5173
npm run build                  # type-check + production build
```

## Documentation

- [`docs/functional.md`](docs/functional.md) — what Lectern does (functional spec).
- [`docs/technical.md`](docs/technical.md) — architecture + external service integration.

## Tech stack

- **Backend:** Python + FastAPI (async, WebSockets), SQLModel/SQLite, APScheduler, httpx.
- **Frontend:** React + TypeScript (Vite), Tailwind.

## License

[MIT](LICENSE)
