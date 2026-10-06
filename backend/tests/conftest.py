"""Pytest configuration.

Environment is fixed here, BEFORE any app module is imported, so that
`get_settings()` (which reads `.env` / env vars at import time) is deterministic
and never depends on a developer's local `.env`.

These tests are the runnable, dependency-free half of the suite: they exercise
pure crypto/config logic and the CSRF + error-envelope middleware against the
real app object. The lifespan in `main.py` does not open a DB/Redis connection,
and CSRF/short-circuit paths return before any `Depends(get_db)` resolves, so no
Postgres/Redis is required to run them.

Integration tests that DO need Postgres/Redis live in `tests/integration/` and
skip themselves cleanly when those services are unreachable (see that package).
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-0123456789abcdef")
os.environ.setdefault("CSRF_SECRET", "test-csrf-secret-0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("AI_PROVIDER", "none")
os.environ.setdefault("OCR_PROVIDER", "none")
os.environ.setdefault("BOOTSTRAP_ADMIN_USERNAME", "admin")


@pytest.fixture(scope="session")
def app_metadata():
    """The live ORM schema (all 45 tables registered by `app.models`)."""
    import app.models  # noqa: F401  - registers every table on Base.metadata
    from app.core.database import Base

    return Base.metadata


@pytest.fixture(scope="session")
def migration_meta():
    """Schema reconstructed by executing the frozen bootstrap revision's literal
    `op.create_table` / `op.create_index` calls (no ORM metadata involved)."""
    from migration_replay import replay_upgrade

    return replay_upgrade().metadata


@pytest.fixture(scope="session")
def cumulative_meta():
    """Schema after the *whole* committed revision chain.

    The ORM comparison has to be made against this, not against bootstrap alone:
    once `0002` exists, a model that matches the migrations would look like drift in
    the bootstrap-only view, and the honest fix is to replay further, not to delete
    the guard.
    """
    from migration_replay import replay_chain

    result = replay_chain()
    assert not result.unexpected, f"a revision built schema the recorder cannot follow: {result.unexpected}"
    return result.metadata
