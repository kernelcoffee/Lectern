# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What Lectern is

A self-hosted web app for creating and running modded Minecraft servers on a trusted LAN.
One Docker image serves a FastAPI backend and a built React SPA on one port. No auth by design.
Core mission is done (v0.3.0); current work is the improvement phase.

- `docs/functional.md` — what it does. **The roadmap table in §3 is the single source of truth
  for the todo list.** When asked to "add to the list", edit that table.
- `docs/technical.md` — architecture, on-disk layout, provider integrations, API surface.
- `docs/references/` — notes on Crafty 4 and mc-image-helper (patterns borrowed from them).

## Layout

```
backend/lectern/        Python package (FastAPI)
  main.py               app factory + lifespan (init DB, scheduler, reconcile, auto-start)
  config.py             env settings: LECTERN_DATA, LECTERN_HOST/PORT, LECTERN_STATIC_DIR
  app_settings.py       runtime tunables stored in the Setting table, layered over env
  db.py                 engine + get_session; add-column-only migrations (see below)
  models.py             SQLModel tables
  events.py             persisted per-server event timeline
  scheduler.py          APScheduler wiring from Schedule rows
  ws.py                 ConsoleHub: WebSocket fan-out per server
  api/                  one router per area (servers, content, catalog, backups, schedules,
                        files, settings, players, proxy, events); all under /api
  servers/              process lifecycle, install pipeline, types registry, properties,
                        stats, roster, playerlists, files, logs, velocity, version_change,
                        world_import
  content/              Modrinth install/update/remove, .mrpack import, resource packs
  providers/            thin httpx clients: mojang, fabric, quilt, forge, neoforge, papermc,
                        modrinth, vanillatweaks, adoptium, avatars
backend/tests/          pytest; conftest provides `engine` (in-memory) and `client` fixtures
frontend/src/           React + TS (Vite), Tailwind
  api/                  typed fetch wrappers, one file per backend router
  pages/                Dashboard, CreateServer, Players, Settings, ServerDetail/ (tabs)
  components/, hooks/   shared UI; useConsoleSocket, useInstallProgress
frontend/e2e/           Playwright specs against the real stack
```

## Commands

Backend (from `backend/`, needs a venv with `pip install -e ".[dev]"`):

```bash
LECTERN_DATA=./data uvicorn lectern.main:app --reload   # dev server on :8000
python -m pytest -q                                     # unit tests
ruff check .                                            # lint (CI fails on warnings)
```

Frontend (from `frontend/`, Node 24 per `.nvmrc`):

```bash
npm run dev          # Vite on :5173, proxies /api and /ws to :8000
npm run lint         # tsc --noEmit
npm test             # vitest, src/**/*.test.{ts,tsx} only
npm run build        # tsc -b && vite build
npm run test:e2e     # Playwright smoke tier; needs backend/.venv to exist
npm run test:e2e:full  # also runs @full specs: real JRE download + server install
```

E2E uses its own ports (:8010 backend, :5174 Vite) and `.e2e-data/` at the repo root, so a
running dev stack is never touched. Docker: `docker compose up -d` (prod image) or
`docker compose -f docker-compose.dev.yml up` (hot reload).

CI (`.github/workflows/ci.yml`) runs ruff + pytest on Python 3.14, vitest + build on Node 24,
the Playwright smoke tier, and a Docker build. Pushes to `main` publish the `master` image tag;
`v*` tags publish releases. Keep all four green before considering a change done.

## Conventions

- **Python:** 3.11+ syntax (ruff target py311), line length 100, async-first. One asyncio
  subprocess per Minecraft server; never threads per server. `Depends(...)` in signatures is
  fine (B008 ignored). Tests use `asyncio_mode = "auto"`; mark anything that starts a real
  process or hits the network with `@pytest.mark.integration`.
- **DB migrations:** there is no migration framework. `db._add_missing_columns` adds columns
  the model declares but the table lacks. Only ever *add* nullable/defaulted columns; never
  rename, drop, or change types. Add a case to `test_db_migration.py` when adding a column.
- **Content manifest:** `.lectern/manifest.json` per server is a reconciliation input, not a
  log. Content operations compute the desired file set, diff against the manifest, delete
  stale files, then write the new manifest. Keep install/update/remove/import convergent.
- **Path safety:** everything under a server directory must be confined to it (reject
  absolute paths, `..`, symlinks pointing out; zip-slip guard on every extraction). Backups
  live outside server dirs. Follow the existing guards in `servers/files.py` and `backups.py`.
- **Events:** record user-visible outcomes (crash, restart, backup result, failed schedule)
  via `events.py`. Recording failures must never break the action being recorded.
- **Providers:** new server types go through the registry in `servers/types.py`; new content
  sources implement the `providers/base.py` interface. Do not special-case in core logic.
  Modrinth is deliberately the only content source (CurseForge rejected; see README).
- **Frontend:** keep API types in `src/api/*` mirroring backend schemas. Use the shared
  `Modal` and `Toasts` components rather than new ad-hoc ones. Layout must work on phones.
- **Docs:** when a feature ships, update `docs/functional.md` (requirements + roadmap table)
  and `docs/technical.md` if the API or layout changed. Bump the version in both
  `backend/pyproject.toml` and `frontend/package.json` together for a release.

## Git

- Conventional commit subjects with a scope: `feat(servers): …`, `fix(content): …`,
  `docs(readme): …`, `chore: …`, `ci: …`, `test(frontend): …`.
- Do not add a `Co-Authored-By` trailer to commits.
- Git identity is configured per repo. Never run `git config --global`.
- Feature work goes on `dev/<topic>` branches merged into `main` via PR.
