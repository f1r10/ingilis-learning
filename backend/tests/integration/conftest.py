"""Integration fixtures that require a REAL Postgres, Redis and MinIO/S3.

These are deliberately NOT mocked: they exercise the actual DB round-trips
(migrations/schema, persistence, restart behaviour, session termination, rate
limiting) exactly as the task spec requires. When a service is unreachable the
affected tests skip with a clear reason rather than report a false pass.

The schema for every test is rebuilt by running the REAL bootstrap migration
(`alembic upgrade head`), not `Base.metadata.create_all()`, so the committed DDL
itself is exercised end to end.

`bash scripts/verify_phase12.sh` (repo root, also available as
`make verify-phase12`) starts an isolated Postgres/Redis/MinIO, points this suite at
a throwaway `*_verify_phase12` database, and treats any skip here as a failure. To
run the suite by hand against your own dev stack:

    docker compose -f ../docker-compose.yml up -d postgres redis minio
    cd backend
    DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/app_test \
    REDIS_URL=redis://localhost:6379/0 \
    OBJECT_STORAGE_ENDPOINT=http://localhost:9000 \
    OBJECT_STORAGE_ACCESS_KEY=... OBJECT_STORAGE_SECRET_KEY=... \
    BOOTSTRAP_ADMIN_PASSWORD=... pytest tests/integration -v
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core import constants, security
from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.redis_client import close_redis, get_redis
from app.main import create_app
from app.models import identity

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ADMIN_USERNAME = os.getenv("TEST_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("TEST_ADMIN_PASSWORD", "integration-admin-pw-123")


def _services_available() -> tuple[bool, str]:
    """Blocking probe used at collection time to decide skip vs run."""

    async def _probe() -> None:
        conn = await engine.connect()
        try:
            await conn.execute(text("SELECT 1"))
        finally:
            await conn.close()
        # Drop the pooled connection: it belongs to this short-lived loop and must
        # not be handed to a later test loop.
        await engine.dispose()

    try:
        asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001  (probe: any failure => unavailable)
        return False, f"Postgres unavailable: {type(exc).__name__}: {exc}"
    return True, ""


DB_OK, DB_REASON = _services_available()


def _guard_disposable_database(url: str) -> str:
    """Return the target database name, refusing one that could hold real data.

    The schema reset below is a `DROP SCHEMA ... CASCADE`, so pointing DATABASE_URL
    at an application database would be unrecoverable. A name has to declare itself
    disposable; anything else is a configuration error and fails immediately rather
    than being skipped away.
    """
    from sqlalchemy.engine import make_url

    name = make_url(url).database or ""
    lowered = name.lower()
    if not (
        lowered.endswith("_test") or lowered.startswith("test_") or "_verify" in lowered
    ):
        raise RuntimeError(
            f"refusing to reset the schema of database {name!r}: integration tests only "
            "run against a disposable database whose name ends in '_test', starts with "
            "'test_' or contains '_verify'. Point DATABASE_URL at such a database."
        )
    return name


def _rebuild_schema_with_migrations() -> None:
    """Drop the whole schema, then recreate it by running the committed migration.

    Called via `asyncio.to_thread`: alembic's env.py drives its own event loop, so
    it must not run inside the pytest-asyncio loop.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    settings = get_settings()
    _guard_disposable_database(settings.database_url)

    async def _drop() -> None:
        # throwaway NullPool engine: never share pooled connections across loops
        disposer = create_async_engine(
            settings.database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with disposer.begin() as conn:
                # DDL-level drop, not `Base.metadata.drop_all`: the ORM only knows the
                # tables the models still declare, so objects left over from an earlier
                # schema would survive and could satisfy a test that the committed
                # migration itself does not create.
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await disposer.dispose()

    asyncio.run(_drop())

    cfg = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    command.upgrade(cfg, "head")

    async def _verify() -> None:
        verifier = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with verifier.connect() as conn:
                return await conn.scalar(text("select version_num from alembic_version"))
        finally:
            await verifier.dispose()

    # Compared with the revision the committed scripts actually define as head, so a
    # later 0002_* is covered the same way without hardcoding a number here. What must
    # never change is that the history is linear and the database sits on its tip -
    # that is the proof the schema came from the migrations and not from the ORM.
    heads = ScriptDirectory.from_config(cfg).get_heads()
    version = asyncio.run(_verify())
    assert len(heads) == 1, f"migration history has {len(heads)} heads: {heads}"
    assert version == heads[0], (
        f"alembic upgrade head left alembic_version at {version!r}, expected {heads[0]!r}"
    )


def _truncate_app_tables() -> None:
    """Delete every row while keeping every DDL object exactly as the migration made it.

    Per-test data isolation must not rebuild the schema: if it fell back to
    `drop_all/create_all` a broken migration could go unnoticed, because the test
    schema would come from the ORM instead of from `0001_bootstrap`.
    """
    quoted = ", ".join(f'"{name}"' for name in sorted(Base.metadata.tables))

    async def _run() -> None:
        disposer = create_async_engine(
            get_settings().database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with disposer.connect() as conn:
                await conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        finally:
            await disposer.dispose()

    asyncio.run(_run())


@pytest.fixture(scope="session")
def migrated_schema():
    """The schema produced by `alembic upgrade head`, built once per test session.

    Synchronous on purpose: alembic's `env.py` drives its own event loop, and this
    fixture is shared by tests that each own a different loop.
    """
    if not DB_OK:
        pytest.skip(DB_REASON)
    _rebuild_schema_with_migrations()


@pytest_asyncio.fixture
async def clean_db(migrated_schema):
    await asyncio.to_thread(_truncate_app_tables)
    # Seed one admin directly (mirrors scripts/seed bootstrap).
    from app.core.database import SessionLocal

    async with SessionLocal() as db:
        db.add(
            identity.AdminUser(
                username=ADMIN_USERNAME,
                password_hash=security.hash_secret(ADMIN_PASSWORD),
            )
        )
        await db.commit()
    yield


def _redis_ok() -> bool:
    async def _p() -> bool:
        try:
            return await get_redis().ping()
        except Exception:  # noqa: BLE001  (probe: any failure => unavailable)
            return False
        finally:
            # release the connection: it is bound to this short-lived loop
            with contextlib.suppress(Exception):
                await close_redis()

    return asyncio.run(_p())


REDIS_OK = _redis_ok()


def _minio_probe() -> tuple[bool, str]:
    """Reachability probe for the object storage the app itself talks to.

    Deliberately read-only (and not a bucket check): creating the bucket is the
    tested behaviour (`ensure_bucket`), so an absent bucket must not skip tests.
    """
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        from app.core.config import get_settings

        s = get_settings()
        client = boto3.client(
            "s3",
            endpoint_url=s.object_storage_endpoint,
            region_name=s.object_storage_region,
            aws_access_key_id=s.object_storage_access_key,
            aws_secret_access_key=s.object_storage_secret_key,
            use_ssl=s.object_storage_secure,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}, connect_timeout=3, read_timeout=5),
        )
        client.list_buckets()
        return True, ""
    except Exception as exc:  # noqa: BLE001  (probe: any failure => unavailable)
        return False, f"MinIO/S3 unavailable: {type(exc).__name__}: {exc}"


STORAGE_OK, STORAGE_REASON = _minio_probe()


@pytest_asyncio.fixture
async def db_ready(migrated_schema):
    """Postgres reachable AND a schema built by the real bootstrap migration.

    For tests that manage their own rows (seed, races, schema introspection) and
    do not need an HTTP client. The tables are emptied, never recreated.
    """
    await asyncio.to_thread(_truncate_app_tables)


@pytest.fixture
def redis_ready():
    if not REDIS_OK:
        pytest.skip("Redis unavailable")


@pytest.fixture
def storage_ready():
    """Depends on real MinIO/S3 through the application's own ObjectStorage."""
    if not STORAGE_OK:
        pytest.skip(STORAGE_REASON)


@pytest.fixture
def admin_credentials() -> tuple[str, str]:
    """The admin seeded by `clean_db` (same account `AuthedClient.login_admin` uses)."""
    return ADMIN_USERNAME, ADMIN_PASSWORD


@pytest_asyncio.fixture(autouse=True)
async def isolate_test_state():
    """Per-test isolation for the state that outlives a single test.

    Pool: pytest-asyncio gives each test its own event loop while the application
    engine holds pooled connections; without a dispose around every test the next
    one checks out a connection bound to a dead loop and fails with a cross-loop
    error that looks like an application bug.

    Rate limits: the limiter is a fixed 60s/300s window keyed by username, so the
    twentieth admin login of the suite would 429 for reasons unrelated to the
    test under it. Deleting only our own `rl:login:*` keys between tests is state
    isolation - the limit, window and fail-open path are untouched, and
    `test_rate_limit_live.py` proves the limiter still trips.
    """
    await engine.dispose()
    if REDIS_OK:
        redis = get_redis()
        keys = [k async for k in redis.scan_iter(match="rl:login:*", count=100)]
        if keys:
            await redis.delete(*keys)
    yield
    await engine.dispose()
    await close_redis()


@pytest.fixture
def storage_keys():
    """Tracks object keys so a test can never leave debris in the bucket."""
    created: list[str] = []
    yield created
    from app.core.storage import get_storage

    storage = get_storage()
    for key in created:
        try:
            storage.delete(key)
        except Exception as exc:  # noqa: BLE001  (cleanup is best-effort)
            print(f"warning: could not delete {key}: {exc}")


class AuthedClient:
    """AsyncClient that mirrors the browser double-submit contract: it replays
    the readable CSRF cookie as a header on every state-changing request."""

    def __init__(self, client: AsyncClient):
        self._c = client

    async def login(self, username: str, password: str) -> None:
        r = await self._c.post(
            "/api/v1/auth/admin/login", json={"username": username, "password": password}
        )
        assert r.status_code == 200, r.text

    async def login_admin(self) -> None:
        await self.login(ADMIN_USERNAME, ADMIN_PASSWORD)

    async def login_student(self, access_key: str) -> None:
        r = await self._c.post("/api/v1/auth/student/login", json={"access_key": access_key})
        assert r.status_code == 200, r.text

    def session_cookie(self) -> str:
        """The live session token, so a test can re-use/inspect a specific session."""
        return self._c.cookies.get(constants.SESSION_COOKIE, "")

    def _csrf(self) -> str:
        return self._c.cookies.get(constants.CSRF_COOKIE, "")

    async def post(self, path, **kw):
        headers = {constants.CSRF_HEADER: self._csrf(), **(kw.pop("headers", None) or {})}
        return await self._c.post(path, headers=headers, **kw)

    async def patch(self, path, **kw):
        headers = {constants.CSRF_HEADER: self._csrf(), **(kw.pop("headers", None) or {})}
        return await self._c.patch(path, headers=headers, **kw)

    async def put(self, path, **kw):
        headers = {constants.CSRF_HEADER: self._csrf(), **(kw.pop("headers", None) or {})}
        return await self._c.put(path, headers=headers, **kw)

    async def delete(self, path, **kw):
        headers = {constants.CSRF_HEADER: self._csrf(), **(kw.pop("headers", None) or {})}
        return await self._c.delete(path, headers=headers, **kw)

    def get(self, path, **kw):
        return self._c.get(path, **kw)


@pytest_asyncio.fixture
async def client(clean_db):
    """An ADMIN-AUTHENTICATED client, because that is what these flows test.

    Every admin-facing integration test starts from a live session; making the
    fixture log in once keeps the tests about their subject instead of about
    boilerplate. A test that needs the opposite (no session at all) builds its own
    client with `session_factory()`. Tests whose subject IS the login still call
    `login_admin()` explicitly - performing a second login is harmless here because
    admin sessions are stateless tokens (no session rows to count) and
    `isolate_test_state` clears the login rate-limit window before every test.
    """
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as raw:
        authed = AuthedClient(raw)
        await authed.login_admin()
        yield authed


@pytest_asyncio.fixture
async def session_factory():
    """Build extra isolated app sessions inside one test (each with its own cookie jar).

    Needed for multi-session behaviour (session_epoch, password change, races),
    where the single `client` session cannot observe two logins at once. Clients
    only touch the database during the test body, so this composes with
    `clean_db`/`db_ready` regardless of fixture setup order.
    """
    opened: list[AsyncClient] = []

    def _make() -> AuthedClient:
        raw = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
        opened.append(raw)
        return AuthedClient(raw)

    yield _make
    for raw in opened:
        await raw.aclose()

