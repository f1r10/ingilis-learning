"""Schema-drift guard: the frozen bootstrap revision must equal the ORM schema.

Phase 1-2 hardening requirement: the initial migration can no longer build its
schema at runtime from ``Base.metadata``. That makes the historical schema
explicit - and therefore able to drift from the models. These tests replay the
revision's literal DDL into a synthetic MetaData and compare *structures*, not
table counts: table names, column names, compiled PostgreSQL types, nullability,
string widths, server defaults, primary keys, foreign keys (incl. ON DELETE),
unique constraints and indexes (incl. uniqueness and column order).
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from migration_replay import (
    BOOTSTRAP_REVISION,
    column_specs,
    downgrade_table_names,
    fk_tuples,
    index_tuples,
    pk_tuples,
    replay_upgrade,
    unique_tuples,
)
from sqlalchemy import MetaData


def test_revision_file_exists() -> None:
    assert BOOTSTRAP_REVISION.exists(), f"missing bootstrap revision: {BOOTSTRAP_REVISION}"


def test_table_names_match_metadata_exactly(
    migration_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = set(migration_meta.tables), set(app_metadata.tables)
    assert mig == orm, (
        f"migration creates tables the models do not have: {sorted(mig - orm)}; "
        f"models have tables the migration never creates: {sorted(orm - mig)}"
    )
    assert len(mig) == len(orm) == 45


def test_columns_types_nullability_widths_and_defaults_match(
    migration_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = column_specs(migration_meta), column_specs(app_metadata)
    problems: list[str] = []
    for table in sorted(set(mig) | set(orm)):
        mig_cols = mig.get(table, {})
        orm_cols = orm.get(table, {})
        for missing in sorted(set(orm_cols) - set(mig_cols)):
            problems.append(f"{table}.{missing}: present in models, absent in migration")
        for extra in sorted(set(mig_cols) - set(orm_cols)):
            problems.append(f"{table}.{extra}: created by migration, absent in models")
        for col in sorted(set(mig_cols) & set(orm_cols)):
            if mig_cols[col] != orm_cols[col]:
                problems.append(
                    f"{table}.{col}: migration={mig_cols[col]} models={orm_cols[col]} "
                    "(type, nullable, server_default or width)"
                )
    assert not problems, "model/migration schema drift detected:\n" + "\n".join(problems)
    # the comparison above must not be vacuous
    assert sum(len(cols) for cols in mig.values()) == sum(len(cols) for cols in orm.values()) >= 400


def test_primary_keys_match(migration_meta: MetaData, app_metadata: MetaData) -> None:
    mig, orm = pk_tuples(migration_meta), pk_tuples(app_metadata)
    diff = {t: (sorted(mig[t]), sorted(orm[t])) for t in mig if mig.get(t) != orm.get(t)}
    assert not diff, f"primary key drift: {diff}"
    assert all(mig[t] for t in mig), "a table has no primary key"


def test_foreign_keys_match_including_ondelete(
    migration_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = fk_tuples(migration_meta), fk_tuples(app_metadata)
    assert mig == orm, (
        f"FKs only in models: {sorted(str(f) for f in orm - mig)}\n"
        f"FKs only in migration: {sorted(str(f) for f in mig - orm)}"
    )
    assert len(mig) >= 50, "FK comparison looks vacuous"
    # every FK in this schema must declare an explicit delete rule
    assert all(fk[4] for fk in mig), f"FKs without ON DELETE: {[f for f in mig if not f[4]]}"


def test_unique_constraints_match(migration_meta: MetaData, app_metadata: MetaData) -> None:
    mig, orm = unique_tuples(migration_meta), unique_tuples(app_metadata)
    assert mig == orm, (
        f"unique constraint drift: only-migration={sorted(map(str, mig - orm))} "
        f"only-models={sorted(map(str, orm - mig))}"
    )


def test_indexes_match_including_uniqueness_and_order(
    migration_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = index_tuples(migration_meta), index_tuples(app_metadata)
    assert set(mig) == set(orm), (
        f"indexes only in migration: {sorted(set(mig) - set(orm))}\n"
        f"indexes only in models: {sorted(set(orm) - set(mig))}"
    )
    mismatched = {k: (mig[k], orm[k]) for k in mig if mig[k] != orm[k]}
    assert not mismatched, f"index definition drift: {mismatched}"
    assert len(mig) >= 80


def test_downgrade_drops_every_created_table(migration_meta: MetaData) -> None:
    dropped = downgrade_table_names()
    created = list(migration_meta.tables)
    assert sorted(dropped) == sorted(created), (
        f"never dropped: {sorted(set(created) - set(dropped))}; "
        f"dropped but never created: {sorted(set(dropped) - set(created))}"
    )
    assert len(dropped) == len(set(dropped))


def test_replay_is_self_contained_and_complete() -> None:
    """The replayed MetaData proves the revision is built purely from literal
    op.* calls; no application ORM metadata was consulted."""
    result = replay_upgrade()
    assert not result.unexpected
    assert set(result.created_tables) == set(result.metadata.tables)
    assert len(result.created_tables) == len(result.metadata.tables) == 45
    assert len(result.created_indexes) == 90


@pytest.mark.parametrize("drift_kind", ["column", "type", "index"])
def test_comparison_logic_detects_injected_drift(
    migration_meta: MetaData, drift_kind: str
) -> None:
    """Negative control: mutate a copy of the replayed schema and confirm the
    comparison helpers report a difference, so a green suite is meaningful."""
    import copy

    mutated = copy.deepcopy(migration_meta)
    baseline_specs = column_specs(migration_meta)
    baseline_indexes = index_tuples(migration_meta)

    if drift_kind == "column":
        mutated.tables["tag"].append_column(sa.Column("injected", sa.Text()))
        assert column_specs(mutated)["tag"] != baseline_specs["tag"]
    elif drift_kind == "type":
        mutated.tables["tag"].columns["name"].type = sa.String(length=999)
        assert column_specs(mutated)["tag"] != baseline_specs["tag"]
    else:
        mutated.tables["topic"].indexes.clear()
        assert index_tuples(mutated) != baseline_indexes
