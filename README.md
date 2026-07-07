# Lectern

A self-hosted **web app for creating and managing modded Minecraft servers**, built with
simplicity in mind. Think *Crafty's "server runs in the background, managed over the web" model* +
*Prism's mod management*, minus the heavy auth, plus Vanilla Tweaks.

**First version targets:** Vanilla + **Fabric** servers, **Modrinth** content (mods, resource
packs, `.mrpack` modpacks), **Vanilla Tweaks** resource packs, with per-server Java
auto-provisioning, backups, and scheduling. CurseForge, Quilt/Paper/Forge, and Bedrock are
designed for as pluggable providers and come later.

## Documentation

- [`docs/functional.md`](docs/functional.md) — what Lectern does (functional spec).
- [`docs/technical.md`](docs/technical.md) — architecture + external service integration.
- [`docs/implementation.md`](docs/implementation.md) — step-by-step build plan.

## Tech stack

- **Backend:** Python + FastAPI (async, WebSockets), SQLModel/SQLite, APScheduler, httpx.
- **Frontend:** React + TypeScript (Vite), Tailwind.

## Development

### With Docker (podman/docker) — one command

```bash
docker compose up
```

- Backend: http://localhost:8000 (health at `/api/health`)
- Frontend: http://localhost:5173

### Locally, without containers

Backend:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn lectern.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Then open http://localhost:5173 — the page should show **backend ok** once both are running.

## Status

Early development. See [`docs/implementation.md`](docs/implementation.md) for the milestone
checklist (currently: **M0 — scaffolding**).
