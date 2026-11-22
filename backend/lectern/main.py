"""FastAPI application factory and entrypoint.

For now this only wires up the app, CORS (for the Vite dev server), the data
directory, and a health check. Routers and services are added in later
milestones (see docs/implementation.md).
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from types import FrameType

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from uvicorn.logging import DefaultFormatter

from . import __version__, backups
from .api import backups as backups_api
from .api import catalog as catalog_api
from .api import content as content_api
from .api import events as events_api
from .api import files as files_api
from .api import players as players_api
from .api import proxy as proxy_api
from .api import schedules as schedules_api
from .api import servers as servers_api
from .api import settings as settings_api
from .config import get_settings
from .db import engine, init_db
from .scheduler import scheduler_service
from .servers.manager import manager
from .servers.properties import PropertiesPathError
from .servers.stats_sampler import stats_sampler

logger = logging.getLogger(__name__)


def _sweep_stale_parts() -> None:
    """Remove ``.part`` files orphaned by a killed upload or backup. Best effort:
    a failure here must never block startup."""
    try:
        with Session(engine) as session:
            backups.sweep_stale_parts(session)
    except Exception:
        logger.exception("Could not sweep stale .part files")


@contextmanager
def _lectern_log_output() -> Iterator[None]:
    """Print Lectern's own INFO lines (how each server ended at shutdown, the
    management protocol and backup notes) next to uvicorn's. uvicorn only
    configures its ``uvicorn.*`` loggers, so without a handler these records
    reach logging's last resort, which prints WARNING and above only. Left
    alone when a logging config already has a handler for them (a
    ``--log-config``, pytest), so no line is printed twice."""
    lectern_logger = logging.getLogger("lectern")
    if lectern_logger.handlers or logging.getLogger().handlers:
        yield
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        DefaultFormatter("%(levelprefix)s %(name)s: %(message)s", use_colors=False)
    )
    previous_level = lectern_logger.level
    lectern_logger.addHandler(handler)
    if previous_level == logging.NOTSET:
        # Follow ``uvicorn --log-level`` when it was given one.
        lectern_logger.setLevel(logging.getLogger("uvicorn.error").level or logging.INFO)
    try:
        yield
    finally:
        lectern_logger.removeHandler(handler)
        lectern_logger.setLevel(previous_level)


def _hangup_as_terminate(_signum: int, _frame: FrameType | None) -> None:
    # Hand over to whatever handles SIGTERM (uvicorn: a graceful shutdown).
    signal.raise_signal(signal.SIGTERM)


@contextmanager
def _hangup_shuts_down() -> Iterator[None]:
    """Make a terminal hangup (the terminal closed, an SSH session dropped)
    shut Lectern down the way SIGTERM does. uvicorn handles only SIGINT and
    SIGTERM, so SIGHUP's default action would kill Lectern before its
    lifespan shutdown, and the servers, which run in their own session and
    never see the hangup, would get no stop command.

    Left alone when SIGHUP is already ignored (``nohup``: a hangup must keep
    Lectern running) or handled, where the platform has no SIGHUP, and when
    the lifespan does not run on the main thread (the test client), the only
    thread Python lets set a signal handler."""
    if (
        not hasattr(signal, "SIGHUP")
        or threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGHUP) is not signal.SIG_DFL
    ):
        yield
        return
    signal.signal(signal.SIGHUP, _hangup_as_terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with _lectern_log_output(), _hangup_shuts_down():
        # Startup: ensure the data directory tree exists, create tables, and
        # reconcile any stale "running" statuses left by a previous process.
        get_settings().ensure_dirs()
        init_db()
        manager.reconcile()
        _sweep_stale_parts()
        # Auto-start flagged servers (each after its own stagger delay).
        manager.autostart()
        # Cron schedules tick for the whole app lifetime (M10).
        scheduler_service.start()
        # Persist resource samples for CPU/mem/player history graphs (M11).
        stats_sampler.start()
        yield
        # Shutdown: stop firing schedules, then stop every server we own the
        # way the Stop button does, all at once (each with its own stop
        # command and graceful-stop timeout); from here on no server can be
        # started. Docker's stop grace period must outlast the longest of
        # those timeouts.
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

    @app.exception_handler(PropertiesPathError)
    async def properties_path_error(_request: Request, exc: PropertiesPathError) -> JSONResponse:
        # Every endpoint that writes server.properties (settings, properties,
        # clone, proxy linking, resource packs) refuses a symlink leading out
        # of the server directory the same way: 400 with a readable detail.
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)

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
    app.include_router(players_api.router)
    app.include_router(proxy_api.router)
    app.include_router(events_api.router)

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
