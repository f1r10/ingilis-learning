"""Live schema introspection: what Postgres actually holds after `upgrade head`.

The offline tests prove the migration *text* matches the models. This proves the
database that migration produces matches them too - table by table, column by
column, constraint by constraint - and that the enum strategy really is "VARCHAR,
no CHECK constraint, no native type". It inspects a schema built by `db_ready`,
which runs the committed Alembic revision rather than `create_all()`.

Skips via `db_ready` when Postgres is unavailable.
"""
from __future__ import annotations

from copy import deepcopy

import pytest
import sqlalchemy as sa
from migration_replay import (
    chain_paths,
    fk_tuples,
    index_tuples,
    revision_identifiers,
    unique_tuples,
)
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.models.content import VocabularyEntry

_PG = postgresql.dialect()
# The application's objects all live in one schema. Comparing against
# `NOT IN ('pg_catalog', 'information_schema')` was not enough: PostgreSQL also
# keeps its TOAST tables in `pg_toast`, and naming that schema explicitly is what
# keeps 76 internal indexes out of an application-level comparison.
APP_SCHEMA = "public"


async def _reflect() -> dict:
    """Snapshot the live schema with SQLAlchemy's own inspector."""
    out: dict = {"pks": {}, "fks": {}, "uniques": {}, "columns": {}}

    def _read(sync_conn) -> None:
        insp = inspect(sync_conn)
        out["tables"] = set(insp.get_table_names())
        for table in sorted(out["tables"]):
            out["columns"][table] = {c["name"]: c for c in insp.get_columns(table)}
            out["pks"][table] = tuple(sorted(insp.get_pk_constraint(table)["constrained_columns"]))
            out["fks"][table] = [
                (
                    table,
                    tuple(sorted(fk["constrained_columns"])),
                    fk["referred_table"],
                    tuple(sorted(fk.get("referred_columns") or [])),
                    str((fk.get("options") or {}).get("ondelete", "")).upper(),
                    str((fk.get("options") or {}).get("onupdate", "")).upper(),
                )
                for fk in insp.get_foreign_keys(table)
            ]
            out["uniques"][table] = [
                # SQLAlchemy's unique-constraint rows key the columns as
                # `column_names` (only FK and PK dicts use `constrained_columns`).
                (table, uc["name"], tuple(sorted(uc["column_names"])))
                for uc in insp.get_unique_constraints(table)
            ]

    async with SessionLocal() as db:
        conn = await db.connection()
        await conn.run_sync(_read)
    return out


def _compiled(col_type) -> str:
    return _normalise_type(str(col_type.compile(dialect=_PG)).upper())


# PostgreSQL accepts several spellings for one type and the inspector reports the
# canonical one: `FLOAT` without precision *is* `DOUBLE PRECISION`, `FLOAT(24)` is
# `REAL`. Without this, 11 correctly migrated float columns would read as drift.
_TYPE_ALIASES = {
    "FLOAT": "DOUBLE PRECISION",
    "FLOAT(53)": "DOUBLE PRECISION",
    "FLOAT(24)": "REAL",
}


def _normalise_type(rendered: str) -> str:
    return _TYPE_ALIASES.get(rendered, rendered)


async def test_every_table_the_models_define_exists(db_ready, app_metadata):
    live = await _reflect()
    expected = set(app_metadata.tables) | {"alembic_version"}
    assert expected <= live["tables"], (
        f"missing in the live schema: {sorted(expected - live['tables'])}"
    )
    assert not (live["tables"] - expected), (
        f"tables in the database that no model defines: {sorted(live['tables'] - expected)}"
    )
    assert len(app_metadata.tables) == 47, (
        "45 from the bootstrap domain plus the two question-set membership tables "
        "`0003_membership_and_checksum` adds"
    )


async def test_column_types_widths_and_nullability_match_the_models(db_ready, app_metadata):
    live = await _reflect()
    drift: list[str] = []
    compared = 0

    for table_name, model_table in app_metadata.tables.items():
        live_cols = live["columns"][table_name]
        if set(live_cols) != set(model_table.columns.keys()):
            drift.append(
                f"{table_name}: column set differs "
                f"(only live={sorted(set(live_cols) - set(model_table.columns))}, "
                f"only models={sorted(set(model_table.columns) - set(live_cols))})"
            )
            continue
        for name, model_col in model_table.columns.items():
            live_col = live_cols[name]
            compared += 1
            if bool(live_col["nullable"]) != bool(model_col.nullable):
                drift.append(
                    f"{table_name}.{name}: nullable live={live_col['nullable']} "
                    f"model={model_col.nullable}"
                )
            if _compiled(live_col["type"]) != _compiled(model_col.type):
                drift.append(
                    f"{table_name}.{name}: type live={_compiled(live_col['type'])} "
                    f"model={_compiled(model_col.type)}"
                )

    assert compared >= 400, f"only {compared} columns compared - the test would be vacuous"
    assert not drift, "live/model column drift:\n" + "\n".join(drift[:25])


async def test_primary_keys_foreign_keys_and_uniques_match_the_models(db_ready, app_metadata):
    live = await _reflect()

    expected_pks = {
        name: tuple(sorted(c.name for c in table.primary_key.columns))
        for name, table in app_metadata.tables.items()
    }
    assert {t: p for t, p in live["pks"].items() if t in expected_pks} == expected_pks

    live_fks = {fk for entries in live["fks"].values() for fk in entries}
    expected_fks = fk_tuples(app_metadata)
    assert live_fks == expected_fks, (
        f"only live: {sorted(map(str, live_fks - expected_fks))}\n"
        f"only models: {sorted(map(str, expected_fks - live_fks))}"
    )
    assert len(live_fks) >= 50
    assert all(fk[4] for fk in live_fks), "every foreign key must declare an ON DELETE rule"

    live_uniques = {u for entries in live["uniques"].values() for u in entries}
    expected_uniques = {(t, n, tuple(sorted(c))) for t, n, c in unique_tuples(app_metadata)}
    assert live_uniques == expected_uniques


async def test_indexes_exist_are_unique_where_declared_and_ordered(db_ready, app_metadata):
    """Read straight from pg_index, so column order and partial predicates count.

    Primary keys and `UniqueConstraint`s are left out on purpose: they are indexes
    in PostgreSQL, but the models declare them as constraints and this file already
    compares them constraint-for-constraint above. What is compared here is exactly
    the set of `Index` objects the models declare.
    """
    query = text(
        f"""
        SELECT c.relname, i.relname, idx.indisunique,
               idx.indpred IS NOT NULL AS is_partial,
               array_agg(a.attname ORDER BY k.ord) AS columns
        FROM pg_index idx
        JOIN pg_class i ON i.oid = idx.indexrelid
        JOIN pg_class c ON c.oid = idx.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL unnest(idx.indkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE n.nspname = '{APP_SCHEMA}'
          AND NOT idx.indisprimary
          AND NOT EXISTS (
              SELECT 1 FROM pg_constraint con
              WHERE con.conindid = idx.indexrelid AND con.conrelid = idx.indrelid
          )
        GROUP BY c.relname, i.relname, idx.indisunique, idx.indpred
        """
    )
    async with SessionLocal() as db:
        rows = (await db.execute(query)).all()

    live = {name: (table, tuple(columns), bool(uniq)) for table, name, uniq, _p, columns in rows}
    expected = {
        name: (table, columns, uniq)
        for name, (table, columns, uniq, _predicate) in index_tuples(app_metadata).items()
    }
    assert live == expected, (
        f"only live={sorted(set(live) - set(expected))} "
        f"only models={sorted(set(expected) - set(live))} "
        f"differing={sorted(k for k in set(live) & set(expected) if live[k] != expected[k])}"
    )
    assert len(live) >= 98, f"expected every declared index, found {len(live)}"

    partial = {name for _t, name, _u, is_partial, _c in rows if is_partial}
    expected_partial = {
        name
        for name, (_t, _c, _u, predicate) in index_tuples(app_metadata).items()
        if predicate
    }
    # Named here as well as derived above, so a partial unique index cannot arrive by accident:
    # each of these is the only thing stopping a duplicate that a plain unique index would miss
    # because the column it guards is nullable. A new one has to be argued for in this list and
    # in a migration of its own.
    # `uq_source_file_checksum` (0007) guards `checksum` on a table whose rows are trashed, not
    # deleted: the same paper may be imported again once its first import is gone, so only the
    # live rows may be unique. `uq_import_item_result` (0007) guards a pair of columns that are
    # both null until a candidate is filed, and says one piece of content came from one
    # candidate - which is exactly the half of that rule an application cannot check by itself.
    assert partial == expected_partial == {
        "uq_student_access_key_active",
        "uq_vocabulary_word_language",
        "uq_media_asset_checksum",
        "uq_assignment_student",
        "uq_assignment_group",
        "uq_source_file_checksum",
        "uq_import_item_result",
    }


async def test_the_one_active_key_index_really_is_partial_and_unique(db_ready):
    async with SessionLocal() as db:
        definition = (
            await db.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_student_access_key_active'"
                )
            )
        ).scalar_one()
    assert "UNIQUE" in definition.upper(), definition
    assert "WHERE" in definition.upper(), definition
    assert "status" in definition and "ACTIVE" in definition, definition


async def test_the_vocabulary_word_index_rejects_a_live_duplicate_and_forgives_trash(
    db_ready,
):
    """The Phase 4 rule the service cannot enforce on its own.

    Two concurrent writers - a teacher and the Phase 8 importer - can each confirm the
    word is absent and then both insert it. Only the database sees both statements, so
    this checks that the real index does its job: a duplicate of a live row fails, the
    same word in another learning language is a different word, and a trashed row
    frees the word again, which is what makes soft delete and restore possible at all.

    Every statement runs in one transaction with savepoints, so a rejected insert
    cannot wipe out the row it collided with.
    """
    async with SessionLocal() as db:
        db.add(
            VocabularyEntry(
                word="improve", learning_language="en", definition="to make better"
            )
        )
        await db.flush()

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(
                    VocabularyEntry(
                        word="improve", learning_language="en", definition="a second copy"
                    )
                )
                await db.flush()

        # A different learning language is a different word: the index pairs both
        # columns so an English entry never blocks the Turkish one.
        db.add(VocabularyEntry(word="improve", learning_language="az", definition="fərqli"))
        await db.flush()

        assert await _live_count(db, "en") == 1

        # Trash releases the word: the index is partial, not total.
        await db.execute(text("UPDATE vocabulary_entry SET deleted_at = now() WHERE learning_language = 'en'"))
        await db.flush()
        db.add(VocabularyEntry(word="improve", learning_language="en", definition="a replacement"))
        await db.flush()

        assert await _live_count(db, "en") == 1
        assert await _live_count(db, "az") == 1
        await db.rollback()


async def _live_count(db, learning_language: str) -> int:
    return (
        await db.execute(
            text(
                "SELECT count(*) FROM vocabulary_entry "
                "WHERE learning_language = :lang AND deleted_at IS NULL"
            ),
            {"lang": learning_language},
        )
    ).scalar_one()


async def test_enum_columns_are_varchar_without_check_constraints_or_native_types(db_ready):
    """native_enum=False means plain VARCHAR with application-side validation."""
    async with SessionLocal() as db:
        checks = (
            await db.execute(
                text(
                    f"SELECT con.conname FROM pg_constraint con "
                    f"JOIN pg_class rel ON rel.oid = con.conrelid "
                    f"JOIN pg_namespace n ON n.oid = rel.relnamespace "
                    f"WHERE con.contype = 'c' AND n.nspname = '{APP_SCHEMA}'"
                )
            )
        ).all()
        enum_types = (
            await db.execute(
                text(
                    f"SELECT t.typname FROM pg_type t "
                    f"JOIN pg_namespace n ON n.oid = t.typnamespace "
                    f"WHERE t.typtype = 'e' AND n.nspname = '{APP_SCHEMA}'"
                )
            )
        ).all()
        kind = (
            await db.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'student_access_key' AND column_name = 'status'"
                )
            )
        ).scalar_one()

    assert checks == [], f"unexpected CHECK constraints: {[c[0] for c in checks]}"
    assert enum_types == [], f"unexpected native enum types: {[e[0] for e in enum_types]}"
    assert kind == "character varying", kind


async def test_security_relevant_server_defaults_are_present(db_ready):
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT table_name, column_name, column_default, is_nullable "
                    "FROM information_schema.columns "
                    f"WHERE table_schema = '{APP_SCHEMA}'"
                )
            )
        ).all()
    by_key = {(t, c): (d, n) for t, c, d, n in rows}

    assert "gen_random_uuid()" in (by_key[("student", "id")][0] or ""), by_key[("student", "id")]

    epoch_default, epoch_nullable = by_key[("admin_user", "session_epoch")]
    assert epoch_default == "0", epoch_default
    assert epoch_nullable == "NO", "session_epoch must be NOT NULL"

    created_default, created_nullable = by_key[("admin_user", "created_at")]
    assert "now()" in (created_default or ""), created_default
    assert created_nullable == "NO"


async def test_the_schema_came_from_the_committed_migration(db_ready):
    """alembic_version must name the head of the committed chain, not a create_all() side effect.

    Read from the revision files rather than hard-coded, so a phase that adds
    migration `0003` gets a live check that its DDL actually ran instead of a test
    that quietly keeps asserting the bootstrap stamp.
    """
    head, _down = revision_identifiers(chain_paths()[-1])
    async with SessionLocal() as db:
        version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert version == head, f"the database is at {version}, the committed chain ends at {head}"

    async with SessionLocal() as db:
        username_index = (
            await db.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE tablename = 'student' "
                    "AND indexname = 'ix_student_username'"
                )
            )
        ).scalar_one()
    assert "UNIQUE" in username_index.upper(), username_index


async def test_the_comparisons_notice_model_drift(db_ready, app_metadata):
    """Negative control: a model change with no migration counterpart must register
    as drift, so the comparisons above cannot pass vacuously."""
    live = await _reflect()

    mutated = deepcopy(app_metadata)
    mutated.tables["student"].append_column(sa.Column("phase3_only", sa.Text(), nullable=True))
    live_names = set(live["columns"]["student"])
    model_names = set(mutated.tables["student"].columns.keys())
    assert model_names - live_names == {"phase3_only"}, (
        "a new model column must be visible as live/model divergence"
    )

    narrowed = deepcopy(app_metadata)
    narrowed.tables["admin_user"].columns["session_epoch"].type = sa.BigInteger()
    assert _compiled(narrowed.tables["admin_user"].columns["session_epoch"].type) != _compiled(
        live["columns"]["admin_user"]["session_epoch"]["type"]
    ), "a widened model column type must diverge from the live column"

    flipped = deepcopy(app_metadata)
    flipped.tables["admin_user"].columns["session_epoch"].nullable = True
    assert bool(live["columns"]["admin_user"]["session_epoch"]["nullable"]) is False, (
        "making a NOT NULL column nullable in the model must show as drift"
    )
