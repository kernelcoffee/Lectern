"""Startup column auto-migration (``db._add_missing_columns``).

Reproduces the M6 field bug: a database created by an older build (the
compose-volume one had a pre-M6 ``contentitem`` without ``channel``/
``version_number``) must be upgraded in place on startup instead of 500ing
with "no such column" until someone deletes it.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from lectern.db import _add_missing_columns
from lectern.models import ContentItem

# The M3-era contentitem schema — no version_number, no channel.
_OLD_CONTENTITEM = """
CREATE TABLE contentitem (
    id VARCHAR NOT NULL PRIMARY KEY,
    server_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    project_id VARCHAR,
    version_id VARCHAR,
    slug VARCHAR,
    name VARCHAR NOT NULL,
    filename VARCHAR NOT NULL,
    sha512 VARCHAR,
    enabled BOOLEAN NOT NULL,
    installed_at DATETIME NOT NULL
)
"""


def test_old_table_gains_new_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.sqlite")
    with engine.begin() as conn:
        conn.execute(text(_OLD_CONTENTITEM))
        conn.execute(
            text(
                "INSERT INTO contentitem (id, server_id, kind, source, name, filename,"
                " enabled, installed_at) VALUES ('i1', 's1', 'mod', 'modrinth',"
                " 'Old Mod', 'old.jar', 1, '2026-01-01 00:00:00')"
            )
        )

    SQLModel.metadata.create_all(engine)  # no-op for the existing table
    _add_missing_columns(engine)

    with Session(engine) as session:
        row = session.exec(select(ContentItem)).one()
        # Pre-existing row readable through the new model, defaults applied.
        assert row.name == "Old Mod"
        assert row.channel == "release"
        assert row.version_number is None
        # And new-model writes work.
        row.channel = "beta"
        row.version_number = "1.2.3"
        session.add(row)
        session.commit()


def test_migration_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/new.sqlite")
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _add_missing_columns(engine)  # second run must not raise (duplicate column)
