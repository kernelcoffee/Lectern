"""Shared test fixtures.

Point ``LECTERN_DATA`` at a throwaway temp dir *before* importing the app so
that importing the module-level engine never touches the real ./data tree. Each
test then gets an isolated in-memory database via a dependency override, so no
test writes to disk and tests don't share state.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LECTERN_DATA", tempfile.mkdtemp(prefix="lectern-test-"))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from lectern.db import get_session
from lectern.main import app


@pytest.fixture(name="engine")
def engine_fixture():
    # StaticPool keeps the one in-memory DB alive across connections in the test.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(name="client")
def client_fixture(engine):
    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    # No context manager => lifespan (real init_db) is not run; the override
    # supplies the isolated engine instead.
    yield TestClient(app)
    app.dependency_overrides.clear()
