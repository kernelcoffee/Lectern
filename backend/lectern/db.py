"""Database engine and session management (SQLModel over SQLite)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_settings = get_settings()

# check_same_thread=False lets FastAPI's threadpool-run endpoints share the
# engine; SQLite is fine for this single-instance, LAN-scale workload.
engine = create_engine(
    f"sqlite:///{_settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """Create the data directory tree and all tables (idempotent)."""
    _settings.ensure_dirs()
    # Import models for side-effect registration on SQLModel.metadata.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
