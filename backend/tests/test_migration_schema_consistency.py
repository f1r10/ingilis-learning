"""Schema-drift guard: the committed migrations must equal the ORM schema.

Phase 1-2 hardening requirement: the initial migration can no longer build its
schema at runtime from ``Base.metadata``. That makes the historical schema
explicit - and therefore able to drift from the models. These tests replay the
revision's literal DDL into a synthetic MetaData and compare *structures*, not
table counts: table names, column names, compiled PostgreSQL types, nullability,
string widths, server defaults, primary keys, foreign keys (incl. ON DELETE),
unique constraints and indexes (incl. uniqueness and column order).

Two views are compared on purpose:

* ``migration_meta`` is bootstrap alone, and it stays pinned to the schema
  bootstrap was written for. Nothing after it may change that history.
* ``cumulative_meta`` is the whole revision chain, and it is what the models are
  measured against. A granular ``0002`` therefore has to be replayed, not ignored -
  the guard cannot be satisfied by leaving a migration unapplied in the comparison.
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
    cumulative_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = set(cumulative_meta.tables), set(app_metadata.tables)
    assert mig == orm, (
        f"migrations create tables the models do not have: {sorted(mig - orm)}; "
        f"models have tables the migrations never create: {sorted(orm - mig)}"
    )
    assert len(mig) == len(orm) >= 45


def test_columns_types_nullability_widths_and_defaults_match(
    cumulative_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = column_specs(cumulative_meta), column_specs(app_metadata)
    problems: list[str] = []
    for table in sorted(set(mig) | set(orm)):
        mig_cols = mig.get(table, {})
        orm_cols = orm.get(table, {})
        for missing in sorted(set(orm_cols) - set(mig_cols)):
            problems.append(f"{table}.{missing}: present in models, absent in migrations")
        for extra in sorted(set(mig_cols) - set(orm_cols)):
            problems.append(f"{table}.{extra}: created by migrations, absent in models")
        for col in sorted(set(mig_cols) & set(orm_cols)):
            if mig_cols[col] != orm_cols[col]:
                problems.append(
                    f"{table}.{col}: migration={mig_cols[col]} models={orm_cols[col]} "
                    "(type, nullable, server_default or width)"
                )
    assert not problems, "model/migration schema drift detected:\n" + "\n".join(problems)
    # the comparison above must not be vacuous
    assert sum(len(cols) for cols in mig.values()) == sum(len(cols) for cols in orm.values()) >= 400


def test_primary_keys_match(cumulative_meta: MetaData, app_metadata: MetaData) -> None:
    mig, orm = pk_tuples(cumulative_meta), pk_tuples(app_metadata)
    diff = {t: (sorted(mig[t]), sorted(orm[t])) for t in mig if mig.get(t) != orm.get(t)}
    assert not diff, f"primary key drift: {diff}"
    assert all(mig[t] for t in mig), "a table has no primary key"


def test_foreign_keys_match_including_ondelete(
    cumulative_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = fk_tuples(cumulative_meta), fk_tuples(app_metadata)
    assert mig == orm, (
        f"FKs only in models: {sorted(str(f) for f in orm - mig)}\n"
        f"FKs only in migrations: {sorted(str(f) for f in mig - orm)}"
    )
    assert len(mig) >= 50, "FK comparison looks vacuous"
    # every FK in this schema must declare an explicit delete rule
    assert all(fk[4] for fk in mig), f"FKs without ON DELETE: {[f for f in mig if not f[4]]}"


def test_unique_constraints_match(cumulative_meta: MetaData, app_metadata: MetaData) -> None:
    mig, orm = unique_tuples(cumulative_meta), unique_tuples(app_metadata)
    assert mig == orm, (
        f"unique constraint drift: only-migration={sorted(map(str, mig - orm))} "
        f"only-models={sorted(map(str, orm - mig))}"
    )


def test_indexes_match_including_uniqueness_and_order(
    cumulative_meta: MetaData, app_metadata: MetaData
) -> None:
    mig, orm = index_tuples(cumulative_meta), index_tuples(app_metadata)
    assert set(mig) == set(orm), (
        f"indexes only in migrations: {sorted(set(mig) - set(orm))}\n"
        f"indexes only in models: {sorted(set(orm) - set(mig))}"
    )
    mismatched = {k: (mig[k], orm[k]) for k in mig if mig[k] != orm[k]}
    assert not mismatched, f"index definition drift: {mismatched}"
    assert len(mig) >= 80


def test_every_extra_index_after_bootstrap_is_declared_by_a_model(
    migration_meta: MetaData, cumulative_meta: MetaData, app_metadata: MetaData
) -> None:
    """Revisions after bootstrap add exactly what the models declare.

    This is the counterpart of the guard above, aimed at the new situation: a later
    migration that silently creates something no model knows about would otherwise
    only be visible as a missing index on the *next* comparison.
    """
    added = set(index_tuples(cumulative_meta)) - set(index_tuples(migration_meta))
    orm_only = set(index_tuples(app_metadata)) - set(index_tuples(migration_meta))
    assert added == orm_only, (
        f"created by later revisions but not declared: {sorted(added - orm_only)}; "
        f"declared but never created: {sorted(orm_only - added)}"
    )
    assert "uq_vocabulary_word_language" in added, "Phase 4 must add the vocabulary word index"
    assert "uq_catalog_item_reference" in added, "Phase 6 must add the catalog reference index"
    assert "uq_import_item_result" in added, "Phase 8 must add the one-content-per-candidate index"


def test_columns_added_after_bootstrap_come_from_the_models(
    migration_meta: MetaData, cumulative_meta: MetaData, app_metadata: MetaData
) -> None:
    """A later revision may widen a table, but only with a column the model declares.

    The index guard above has no column counterpart in the cumulative comparison: a
    missing ``op.add_column`` and a stray one both show up as a difference between the
    replayed schema and the models, so this test pins the shape of the diff itself -
    which tables grew, and how.
    """

    def names(meta: MetaData) -> dict[str, set[str]]:
        return {table: set(columns) for table, columns in column_specs(meta).items()}

    baseline, current = names(migration_meta), names(cumulative_meta)
    # a table a later revision creates wholesale is not a table it *widened*
    added = {table: current[table] - baseline[table] for table in baseline}
    added = {table: columns for table, columns in added.items() if columns}

    assert added == {
        "source_file": {"checksum"},
        "import_item": {"missing", "filing", "note", "position"},
    }, "0007 is the only revision that adds columns; a new one belongs in a new revision"
    for table, columns in added.items():
        assert columns <= current[table] <= set(names(app_metadata)[table]), (
            f"{table}: {sorted(columns)} created by a revision but declared by no model"
        )


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
    # bootstrap creates the schema; it never takes an index back
    assert result.dropped_indexes == []


@pytest.mark.parametrize("drift_kind", ["column", "type", "index"])
def test_comparison_logic_detects_injected_drift(
    cumulative_meta: MetaData, drift_kind: str
) -> None:
    """Negative control: mutate a copy of the replayed schema and confirm the
    comparison helpers report a difference, so a green suite is meaningful."""
    import copy

    mutated = copy.deepcopy(cumulative_meta)
    baseline_specs = column_specs(cumulative_meta)
    baseline_indexes = index_tuples(cumulative_meta)

    if drift_kind == "column":
        mutated.tables["tag"].append_column(sa.Column("injected", sa.Text()))
        assert column_specs(mutated)["tag"] != baseline_specs["tag"]
    elif drift_kind == "type":
        mutated.tables["tag"].columns["name"].type = sa.String(length=999)
        assert column_specs(mutated)["tag"] != baseline_specs["tag"]
    else:
        mutated.tables["topic"].indexes.clear()
        assert index_tuples(mutated) != baseline_indexes
