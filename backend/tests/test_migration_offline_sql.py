"""Offline DDL validation: `alembic upgrade head --sql` must render the full schema.

This is the strongest migration check available without a PostgreSQL server: it
drives the real Alembic pipeline (env.py, the PostgreSQL dialect, op rendering)
and asserts the emitted DDL structurally matches the ORM schema, statement for
statement. The live round-trip (`alembic upgrade head` against a real database)
is covered by `make verify-phase12`, not here - this test never claims to have
run against a server.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from migration_replay import fk_tuples, index_tuples
from sqlalchemy import MetaData

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# reserved words (e.g. "group") are rendered quoted by the PostgreSQL dialect
_IDENT = r'"?(\w+)"?'

_TABLE_BLOCK_RE = re.compile(rf"CREATE TABLE {_IDENT} \((.*?)\n\);", re.DOTALL)
_INDEX_RE = re.compile(
    rf"CREATE (UNIQUE )?INDEX {_IDENT} ON {_IDENT} ?\(([^)]*)\)(?: WHERE (.*?))?;", re.DOTALL
)
_FK_RE = re.compile(
    rf"CONSTRAINT (\w+) FOREIGN KEY ?\(([^)]*)\) REFERENCES {_IDENT} ?\(([^)]*)\)([^,\n]*)", re.DOTALL
)
_UNIQUE_RE = re.compile(r"CONSTRAINT (\w+) UNIQUE ?\(([^)]*)\)", re.DOTALL)
_PK_RE = re.compile(r"PRIMARY KEY ?\(([^)]*)\)", re.DOTALL)
_DROP_TABLE_RE = re.compile(rf"^DROP TABLE {_IDENT};", re.MULTILINE)
_DROP_INDEX_RE = re.compile(rf"^DROP INDEX {_IDENT};", re.MULTILINE)


def _alembic(*args: str) -> str:
    env = {
        **os.environ,
        "APP_ENV": "development",
        "SESSION_SECRET": "offline-validation-secret-0123456789",
        "CSRF_SECRET": "offline-validation-csrf-0123456789",
        "DATABASE_URL": "postgresql+asyncpg://validator:validator@localhost:5432/validator",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, (
        f"alembic {' '.join(args)} exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
    )
    return proc.stdout


def _sql_fks(sql: str) -> set[tuple]:
    out: set[tuple] = set()
    for table, body in _TABLE_BLOCK_RE.findall(sql):
        for _name, cols, ref_table, ref_cols, tail in _FK_RE.findall(body):
            ondelete = re.search(r"ON DELETE (\w+(?: NULL)?)", tail.upper())
            onupdate = re.search(r"ON UPDATE (\w+(?: NULL)?)", tail.upper())
            out.add(
                (
                    table,
                    tuple(sorted(c.strip() for c in cols.split(","))),
                    ref_table,
                    tuple(sorted(c.strip() for c in ref_cols.split(","))),
                    ondelete.group(1) if ondelete else "",
                    onupdate.group(1) if onupdate else "",
                )
            )
    return out


def _sql_indexes(sql: str) -> dict[str, tuple[str, tuple[str, ...], bool, str]]:
    out: dict[str, tuple[str, tuple[str, ...], bool, str]] = {}
    for unique, name, table, cols, where in _INDEX_RE.findall(sql):
        parsed = tuple(c.strip().strip('"') for c in cols.split(","))
        normalised = "" if where is None else " ".join(where.split())
        out[name] = (table, parsed, bool(unique), normalised)
    return out


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _alembic("upgrade", "head", "--sql")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _alembic("downgrade", "head:base", "--sql")


def test_upgrade_renders_every_table_once(upgrade_sql: str, app_metadata: MetaData) -> None:
    blocks = _TABLE_BLOCK_RE.findall(upgrade_sql)
    names = [t for t, _ in blocks]
    assert "alembic_version" in names, "version table missing"
    schema_tables = [t for t in names if t != "alembic_version"]
    assert len(schema_tables) == len(set(schema_tables)), "duplicate CREATE TABLE in rendered SQL"
    assert set(schema_tables) == set(app_metadata.tables), (
        f"only in SQL: {sorted(set(schema_tables) - set(app_metadata.tables))}; "
        f"only in models: {sorted(set(app_metadata.tables) - set(schema_tables))}"
    )


def test_rendered_statement_counts_match_the_schema(
    upgrade_sql: str, app_metadata: MetaData
) -> None:
    """Exact counts, tied to the model schema - not a vague 'roughly 45'."""
    tables = [t for t, _ in _TABLE_BLOCK_RE.findall(upgrade_sql) if t != "alembic_version"]
    indexes = _INDEX_RE.findall(upgrade_sql)
    fks = _FK_RE.findall("".join(b for _, b in _TABLE_BLOCK_RE.findall(upgrade_sql)))
    uniques = _UNIQUE_RE.findall(upgrade_sql)
    pks = _PK_RE.findall(upgrade_sql)

    orm_uniques = sum(
        1
        for t in app_metadata.tables.values()
        for c in t.constraints
        if type(c).__name__ == "UniqueConstraint"
    )
    assert len(tables) == len(app_metadata.tables) == 45
    assert len(indexes) == sum(len(t.indexes) for t in app_metadata.tables.values()) == 90
    assert len(fks) == sum(len(t.foreign_key_constraints) for t in app_metadata.tables.values())
    assert len(uniques) == orm_uniques >= 2
    assert len(pks) == len(app_metadata.tables) + 1, (
        "expected one PRIMARY KEY per table (+ alembic_version)"
    )


def test_rendered_foreign_keys_match_orm(
    upgrade_sql: str, app_metadata: MetaData
) -> None:
    rendered, orm = _sql_fks(upgrade_sql), fk_tuples(app_metadata)
    assert rendered == orm, (
        f"only in SQL: {sorted(str(f) for f in rendered - orm)}\n"
        f"only in ORM: {sorted(str(f) for f in orm - rendered)}"
    )


def test_rendered_indexes_match_orm(upgrade_sql: str, app_metadata: MetaData) -> None:
    rendered, orm = _sql_indexes(upgrade_sql), index_tuples(app_metadata)
    assert rendered == orm, (
        f"only in SQL: {sorted(set(rendered) - set(orm))}\n"
        f"only in ORM: {sorted(set(orm) - set(rendered))}\n"
        f"differing: { {k: (rendered[k], orm[k]) for k in set(rendered) & set(orm) if rendered[k] != orm[k]} }"
    )


def test_unique_columns_render_as_unique_indexes(
    upgrade_sql: str, app_metadata: MetaData
) -> None:
    rendered_unique = {m[1] for m in _INDEX_RE.findall(upgrade_sql) if m[0]}
    orm_unique = {
        idx.name for t in app_metadata.tables.values() for idx in t.indexes if idx.unique
    }
    assert rendered_unique == orm_unique
    assert len(rendered_unique) >= 5


def test_downgrade_renders_every_drop(
    downgrade_sql: str, app_metadata: MetaData
) -> None:
    assert set(_DROP_TABLE_RE.findall(downgrade_sql)) == set(app_metadata.tables)
    assert set(_DROP_INDEX_RE.findall(downgrade_sql)) == set(index_tuples(app_metadata))


def test_offline_sql_is_pure_ddl(upgrade_sql: str) -> None:
    assert upgrade_sql.startswith("BEGIN;")
    assert upgrade_sql.rstrip().endswith("COMMIT;")
    forbidden = [
        line
        for line in upgrade_sql.splitlines()
        if re.search(r"(CREATE TYPE|DO \$\$|EXECUTE |COPY )", line, re.IGNORECASE)
        or (re.search(r"INSERT INTO ", line, re.IGNORECASE) and "alembic_version" not in line)
    ]
    assert not forbidden, f"unexpected non-DDL statements: {forbidden}"
    assert "CREATE TABLE alembic_version" in upgrade_sql
    assert "0001_bootstrap" in upgrade_sql
    assert "gen_random_uuid()" in upgrade_sql, "UUID server default failed to render"
    assert "session_epoch" in upgrade_sql, "admin_user.session_epoch missing from rendered DDL"
