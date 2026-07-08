"""Database engine and session management (SQLModel over SQLite)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_settings = get_settings()

# check_same_thread=False lets FastAPI's threadpool-run endpoints share the
# engine; SQLite is fine for this single-instance, LAN-scale workload.
engine = create_engine(
    f"sqlite:///{_settings.db_path}",
    connect_args={"check_same_thread": False},
)


def _add_missing_columns(target_engine=None) -> None:
    """Bring existing tables up to the current models by adding any column
    the model declares but the table lacks (SQLite ``ALTER TABLE … ADD COLUMN``).

    This is the whole migration story: columns are only ever *added* between
    milestones (schema is otherwise append-only), and new columns always have
    defaults — so a database created by an older build keeps working instead
    of 500ing with "no such column" until someone deletes it (bit us when M6
    added ``contentitem.channel``/``version_number`` and older compose-volume
    DBs broke).
    """
    target_engine = target_engine if target_engine is not None else engine
    inspector = inspect(target_engine)
    existing_tables = set(inspector.get_table_names())
    with target_engine.begin() as conn:
        for table in SQLModel.metadata.tables.values():
            if table.name not in existing_tables:
                continue  # create_all handles brand-new tables
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(target_engine.dialect)}'
                default = column.default
                if default is not None and default.is_scalar:
                    value = default.arg
                    if isinstance(value, bool):
                        value = int(value)
                    quoted = f"'{value}'" if isinstance(value, str) else str(value)
                    ddl += f" DEFAULT {quoted}"
                conn.execute(text(ddl))


def init_db() -> None:
    """Create the data directory tree and all tables (idempotent), then add
    any columns newer models declare that an older database lacks."""
    _settings.ensure_dirs()
    # Import models for side-effect registration on SQLModel.metadata.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
