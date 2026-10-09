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
    """The indexes a rendered `upgrade head --sql` leaves behind, by name.

    The script is a history, not a schema: an index one revision creates and a later
    revision drops (directly, or by dropping its table) is not part of the result. The
    comparison against the ORM is about the state after the last statement, so the
    drops have to be taken back out - otherwise a granular revision would look like
    drift whenever it replaces an index.
    """
    out: dict[str, tuple[str, tuple[str, ...], bool, str]] = {}
    for unique, name, table, cols, where in _INDEX_RE.findall(sql):
        parsed = tuple(c.strip().strip('"') for c in cols.split(","))
        normalised = "" if where is None else " ".join(where.split())
        out[name] = (table, parsed, bool(unique), normalised)
    for name in _DROP_INDEX_RE.findall(sql):
        out.pop(name, None)
    dropped_tables = set(_DROP_TABLE_RE.findall(sql))
    return {name: value for name, value in out.items() if value[0] not in dropped_tables}


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
    upgrade_sql: str, app_metadata: MetaData, cumulative_meta: MetaData
) -> None:
    """Exact counts, tied to the model schema - not a vague 'roughly 45'.

    The three views must agree: what `alembic upgrade head --sql` renders, what the
    revision chain replays into a MetaData, and what the models declare. A number is
    still asserted, but as a floor, so a granular later revision moves the schema
    rather than this test.
    """
    tables = [t for t, _ in _TABLE_BLOCK_RE.findall(upgrade_sql) if t != "alembic_version"]
    dropped_tables = set(_DROP_TABLE_RE.findall(upgrade_sql))
    tables = [t for t in tables if t not in dropped_tables]
    indexes = _sql_indexes(upgrade_sql)
    fks = _FK_RE.findall(
        "".join(b for t, b in _TABLE_BLOCK_RE.findall(upgrade_sql) if t not in dropped_tables)
    )
    uniques = _UNIQUE_RE.findall(upgrade_sql)
    pks = _PK_RE.findall(upgrade_sql)

    orm_uniques = sum(
        1
        for t in app_metadata.tables.values()
        for c in t.constraints
        if type(c).__name__ == "UniqueConstraint"
    )
    assert len(tables) == len(app_metadata.tables) == len(cumulative_meta.tables) >= 45
    assert (
        len(indexes)
        == sum(len(t.indexes) for t in app_metadata.tables.values())
        == len(index_tuples(cumulative_meta))
        >= 90
    )
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
    rendered_unique = {name for name, value in _sql_indexes(upgrade_sql).items() if value[2]}
    orm_unique = {
        idx.name for t in app_metadata.tables.values() for idx in t.indexes if idx.unique
    }
    assert rendered_unique == orm_unique
    assert len(rendered_unique) >= 5


def test_downgrade_renders_every_drop(
    downgrade_sql: str, app_metadata: MetaData
) -> None:
    assert set(_DROP_TABLE_RE.findall(downgrade_sql)) == set(app_metadata.tables)
    # An index a downgrade *rebuilds* (0003 puts bootstrap's plain checksum index back
    # before bootstrap's own downgrade drops it) is not a drop the reversal is
    # responsible for, so the two statements cancel out. What must not cancel out is an
    # index the schema still declares: every one of those has to be dropped.
    rebuilt = {name for _u, name, _t, _c, _w in _INDEX_RE.findall(downgrade_sql)}
    assert set(_DROP_INDEX_RE.findall(downgrade_sql)) - rebuilt == set(index_tuples(app_metadata))


def _statements(sql: str) -> set[str]:
    """The render's DDL, whitespace-normalised, so two renders can be compared.

    Alembic interleaves `-- Running upgrade ...` comments and its own log lines with
    the statements; both are dropped here because they describe the script rather than
    change the schema.
    """
    body = "\n".join(
        line
        for line in sql.splitlines()
        if not line.lstrip().startswith(("--", "INFO", "WARN", "BEGIN", "COMMIT"))
    )
    return {" ".join(part.split()) for part in body.split(";") if part.strip()}


def test_head_upgrades_from_its_own_previous_state(
    upgrade_sql: str, app_metadata: MetaData
) -> None:
    """`alembic upgrade <parent>:<head> --sql` is what an already-deployed database runs.

    The whole-chain render proves a fresh install reaches the current schema. It says
    nothing about whether the newest revision is a *step*: a revision that quietly
    rebuilt the schema would be invisible in it, and would fail on a server that has
    data in the tables. So the head is rendered against its own parent and checked
    three ways - it stamps only itself, it creates nothing bootstrap already created,
    and every statement it emits is one the fresh install emits too.
    """
    from migration_replay import chain_paths, replay_upgrade, revision_identifiers

    paths = chain_paths()
    assert len(paths) >= 2, "the step test needs a head with a parent"
    head_id, parent_id = revision_identifiers(paths[-1])
    assert parent_id == revision_identifiers(paths[-2])[0], "the chain is not linear"

    step = _alembic("upgrade", f"{parent_id}:{head_id}", "--sql")
    assert "CREATE TABLE alembic_version" not in step, (
        "only the base revision may create the version table"
    )
    assert f"version_num='{head_id}'" in step, f"{head_id} never stamped"

    rebuilt = {t for t, _ in _TABLE_BLOCK_RE.findall(step)}
    bootstrap = set(replay_upgrade().metadata.tables)
    assert not rebuilt & bootstrap, f"the head rebuilds tables the base already made: {sorted(rebuilt & bootstrap)}"
    assert rebuilt <= set(app_metadata.tables), f"head creates an unknown table: {sorted(rebuilt)}"

    assert _statements(step) <= _statements(upgrade_sql), (
        "the step renders DDL a fresh install does not: "
        f"{sorted(_statements(step) - _statements(upgrade_sql))}"
    )
    assert _statements(step), "the head revision renders no DDL at all"


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


def test_every_committed_revision_appears_in_the_rendered_chain(
    upgrade_sql: str, downgrade_sql: str
) -> None:
    """`upgrade head --sql` must be the whole history, revision by revision.

    A second revision that only existed on disk but never rendered would still leave
    the bootstrap comparisons green, so each one is pinned here: its version stamp,
    and the DDL it is responsible for.
    """
    from migration_replay import chain_paths, revision_identifiers

    for path in chain_paths():
        revision, _down = revision_identifiers(path)
        assert revision in upgrade_sql, f"{path.name} never stamps alembic_version"

    assert "CREATE UNIQUE INDEX uq_vocabulary_word_language ON vocabulary_entry" in upgrade_sql
    assert "WHERE deleted_at IS NULL" in upgrade_sql
    assert "DROP INDEX uq_vocabulary_word_language" in downgrade_sql

    assert "CREATE TABLE reading_set_question" in upgrade_sql
    assert "CREATE TABLE listening_set_question" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_media_asset_checksum ON media_asset" in upgrade_sql
    assert "DROP INDEX uq_media_asset_checksum" in downgrade_sql
    assert "DROP TABLE reading_set_question" in downgrade_sql
    assert "DROP TABLE listening_set_question" in downgrade_sql

    # 0004: the reverse lookups the media library performs.
    for index, table in (
        ("ix_question_media_asset_id", "question"),
        ("ix_listening_media_asset_id", "listening"),
        ("ix_vocabulary_entry_audio_asset_id", "vocabulary_entry"),
    ):
        assert f"CREATE INDEX {index} ON {table}" in upgrade_sql, f"{index} never rendered"
        assert f"DROP INDEX {index}" in downgrade_sql, f"{index} never reversed"

    # 0005: a catalog names one piece of content once.
    assert "CREATE UNIQUE INDEX uq_catalog_item_reference ON catalog_item" in upgrade_sql
    assert "(catalog_id, kind, ref_id)" in upgrade_sql
    assert "DROP INDEX uq_catalog_item_reference" in downgrade_sql

    # 0006: assign once, attempt once, review once. The two assignment rules are partial,
    # and which half of the target each one reads is the whole design - a plain unique
    # index over a nullable column enforces nothing at all.
    for index, table, statement in (
        (
            "uq_assignment_student",
            "exam_assignment",
            "CREATE UNIQUE INDEX uq_assignment_student ON exam_assignment "
            "(exam_id, student_id) WHERE student_id IS NOT NULL",
        ),
        (
            "uq_assignment_group",
            "exam_assignment",
            "CREATE UNIQUE INDEX uq_assignment_group ON exam_assignment "
            "(exam_id, group_id) WHERE group_id IS NOT NULL",
        ),
        (
            "uq_attempt_number",
            "exam_attempt",
            "CREATE UNIQUE INDEX uq_attempt_number ON exam_attempt "
            "(exam_id, student_id, attempt_number)",
        ),
        (
            "uq_manual_review_answer",
            "manual_review",
            "CREATE UNIQUE INDEX uq_manual_review_answer ON manual_review (answer_id)",
        ),
        (
            "ix_attempt_open_expiry",
            "exam_attempt",
            "CREATE INDEX ix_attempt_open_expiry ON exam_attempt (status, expires_at)",
        ),
    ):
        assert statement in upgrade_sql, f"{index} never rendered as {statement!r}"
        assert f"DROP INDEX {index}" in downgrade_sql, f"{index} never reversed"

    # 0007: the first revision that widens a table as well as indexing one. The two
    # NOT NULL columns need their rendered default - an offline script over existing
    # rows fails without it - and `result_ref_id IS NOT NULL` is what makes the
    # one-content-per-candidate rule mean anything at all.
    assert "ALTER TABLE source_file ADD COLUMN checksum VARCHAR(128);" in upgrade_sql
    assert (
        "CREATE UNIQUE INDEX uq_source_file_checksum ON source_file (checksum) "
        "WHERE deleted_at IS NULL" in upgrade_sql
    )
    assert (
        "ALTER TABLE import_item ADD COLUMN missing JSONB DEFAULT '[]'::jsonb NOT NULL"
        in upgrade_sql
    )
    assert (
        "ALTER TABLE import_item ADD COLUMN position INTEGER DEFAULT 0 NOT NULL" in upgrade_sql
    )
    assert "CREATE INDEX ix_import_item_job_position ON import_item (job_id, position)" in upgrade_sql
    assert (
        "CREATE UNIQUE INDEX uq_import_item_result ON import_item "
        "(result_ref_type, result_ref_id) WHERE result_ref_id IS NOT NULL" in upgrade_sql
    )
    for column in ("checksum", "missing", "filing", "note", "position"):
        assert f"DROP COLUMN {column}" in downgrade_sql, f"{column} never reversed"
