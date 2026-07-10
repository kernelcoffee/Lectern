"""FastAPI application factory and entrypoint.

For now this only wires up the app, CORS (for the Vite dev server), the data
directory, and a health check. Routers and services are added in later
milestones (see docs/implementation.md).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import backups as backups_api
from .api import catalog as catalog_api
from .api import content as content_api
from .api import files as files_api
from .api import schedules as schedules_api
from .api import servers as servers_api
from .api import settings as settings_api
from .config import get_settings
from .db import init_db
from .scheduler import scheduler_service
from .servers.manager import manager
from .servers.stats_sampler import stats_sampler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure the data directory tree exists, create tables, and
    # reconcile any stale "running" statuses left by a previous process.
    get_settings().ensure_dirs()
    init_db()
    manager.reconcile()
    # Auto-start flagged servers (each after its own stagger delay).
    manager.autostart()
    # Cron schedules tick for the whole app lifetime (M10).
    scheduler_service.start()
    # Persist resource samples for CPU/mem/player history graphs (M11).
    stats_sampler.start()
    yield
    # Shutdown: stop firing schedules, then kill any server processes we own.
    await stats_sampler.stop()
    scheduler_service.shutdown()
    await manager.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Lectern", version=__version__, lifespan=lifespan)

    # Trusted-LAN posture: allow the local Vite dev server during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(servers_api.router)
    app.include_router(servers_api.console_router)
    app.include_router(catalog_api.router)
    app.include_router(content_api.router)
    app.include_router(backups_api.router)
    app.include_router(schedules_api.router)
    app.include_router(files_api.router)
    app.include_router(settings_api.router)

    # Production: serve the built SPA from the same origin (no CORS, one port).
    # Mounted last so the /api and /ws routes above take precedence; the SPA
    # uses state-based routing, so serving index.html at "/" is enough.
    static_dir = get_settings().static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount(
            "/", StaticFiles(directory=static_dir, html=True), name="spa"
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("lectern.main:app", host=settings.host, port=settings.port, reload=True)


if __name__ == "__main__":
    main()
