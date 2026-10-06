"""Replay a frozen Alembic revision's *literal* ops into a synthetic MetaData.

This is what lets the test suite compare the historical schema encoded by
``0001_bootstrap_schema`` against the live ORM schema without a PostgreSQL
server: the revision is executed with a recording ``op`` stand-in, so the result
is a plain SQLAlchemy ``MetaData`` built only from what the migration spells out
(no ``Base.metadata``, no ORM import).

``fk_tuples`` / ``index_tuples`` normalise a MetaData into comparable tuples and
are shared by both the replay-vs-ORM test and the rendered-offline-SQL test.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import MetaData, UniqueConstraint
from sqlalchemy.dialects import postgresql

BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_ROOT / "migrations" / "versions"
BOOTSTRAP_REVISION = VERSIONS_DIR / "0001_bootstrap_schema.py"
_PG = postgresql.dialect()


class _RecordingOp:
    """Minimal Alembic ``op`` surface: records DDL into ``metadata``."""

    def __init__(self, metadata: MetaData) -> None:
        self.metadata = metadata
        self.created_tables: list[str] = []
        self.created_indexes: list[tuple[str, str]] = []
        self.unexpected: list[str] = []

    def create_table(self, table_name: str, *elements: Any, **_kw: Any) -> None:
        sa.Table(table_name, self.metadata, *elements)
        self.created_tables.append(table_name)

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: list[Any],
        unique: bool = False,
        **kw: Any,
    ) -> None:
        table = self.metadata.tables[table_name]
        cols = [table.columns[c if isinstance(c, str) else str(c)] for c in columns]
        where = kw.pop("postgresql_where", None)
        if kw:
            raise AssertionError(f"unexpected create_index kwargs for {index_name}: {sorted(kw)}")
        sa.Index(index_name, *cols, unique=unique, postgresql_where=where)
        self.created_indexes.append((index_name, table_name))

    def drop_table(self, table_name: str, **_kw: Any) -> None:
        self.unexpected.append(f"drop_table({table_name}) inside upgrade()")

    def drop_index(self, index_name: str, **_kw: Any) -> None:
        self.unexpected.append(f"drop_index({index_name}) inside upgrade()")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"bootstrap revision uses unsupported op {name!r}; the schema must be built from "
            "literal create_table/create_index ops only"
        )


@dataclass
class ReplayResult:
    metadata: MetaData
    module: Any
    created_tables: list[str] = field(default_factory=list)
    created_indexes: list[tuple[str, str]] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)


def _load(path: Path, suffix: str) -> tuple[Any, Any]:
    """Build (module, loader) without executing: the caller execs while the
    recording `alembic.op` is installed, since the revision binds `op` at import."""
    spec = importlib.util.spec_from_file_location(f"rev_{suffix}_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    return importlib.util.module_from_spec(spec), spec.loader


def replay_upgrade(path: Path = BOOTSTRAP_REVISION) -> ReplayResult:
    """Import ``path`` and run its ``upgrade()`` against a recording op."""
    import alembic

    metadata = MetaData()
    recorder = _RecordingOp(metadata)
    module, loader = _load(path, "up")
    original = getattr(alembic, "op", None)
    alembic.op = recorder  # type: ignore[assignment]
    try:
        loader.exec_module(module)
        module.upgrade()
    finally:
        if original is not None:
            alembic.op = original  # type: ignore[assignment]
    return ReplayResult(
        metadata=metadata,
        module=module,
        created_tables=list(recorder.created_tables),
        created_indexes=list(recorder.created_indexes),
        unexpected=list(recorder.unexpected),
    )


def downgrade_table_names(path: Path = BOOTSTRAP_REVISION) -> list[str]:
    """Table names dropped by ``downgrade()``, captured with a throwaway recorder."""
    import alembic

    dropped: list[str] = []
    unexpected: list[str] = []

    class _DropOp:
        def drop_table(self, table_name: str, **_kw: Any) -> None:
            dropped.append(table_name)

        def drop_index(self, index_name: str, **_kw: Any) -> None:
            pass

        def __getattr__(self, name: str) -> Any:
            unexpected.append(name)
            raise AssertionError(f"downgrade uses unsupported op {name!r}")

    module, loader = _load(path, "down")
    original = getattr(alembic, "op", None)
    alembic.op = _DropOp()  # type: ignore[assignment]
    try:
        loader.exec_module(module)
        module.downgrade()
    finally:
        if original is not None:
            alembic.op = original  # type: ignore[assignment]
    assert not unexpected
    return dropped


# --------------------------------------------------------------------------- #
# Normalisation helpers shared by the schema-comparison tests
# --------------------------------------------------------------------------- #
def compile_type(column: Any) -> str:
    return str(column.type.compile(dialect=_PG)).upper()


def compile_server_default(column: Any) -> str | None:
    if column.server_default is None:
        return None
    arg = getattr(column.server_default, "arg", None)
    if arg is None:
        return "FetchedValue"
    return str(arg.compile(dialect=_PG, compile_kwargs={"literal_binds": True})).strip()


def fk_tuples(meta: MetaData, table_name: str | None = None) -> set[tuple]:
    """(local cols, referred table, referred cols, ON DELETE, ON UPDATE) per FK."""
    tables = {table_name: meta.tables[table_name]} if table_name else meta.tables
    return {
        (
            tname,
            tuple(sorted(c.name for c in fk.columns)),
            fk.referred_table.name,
            tuple(sorted(e.target_fullname.rsplit(".", 1)[-1] for e in fk.elements)),
            (fk.ondelete or "").upper(),
            (fk.onupdate or "").upper(),
        )
        for tname, table in tables.items()
        for fk in table.foreign_key_constraints
    }


def _index_where(idx: Any) -> str:
    """Normalised partial-index predicate ('' when the index is not partial)."""
    where = idx.dialect_options["postgresql"].get("where")
    return "" if where is None else " ".join(str(where).split())


def index_tuples(meta: MetaData) -> dict[str, tuple[str, tuple[str, ...], bool, str]]:
    """index name -> (table, columns in order, unique, partial predicate)."""
    out: dict[str, tuple[str, tuple[str, ...], bool, str]] = {}
    for table in meta.tables.values():
        for idx in table.indexes:
            out[idx.name] = (
                table.name,
                tuple(c.name for c in idx.columns),
                bool(idx.unique),
                _index_where(idx),
            )
    return out


def unique_tuples(meta: MetaData) -> set[tuple]:
    return {
        (tname, uc.name, tuple(c.name for c in uc.columns))
        for tname, table in meta.tables.items()
        for uc in table.constraints
        if isinstance(uc, UniqueConstraint)
    }


def column_specs(meta: MetaData) -> dict[str, dict[str, tuple[str, bool, str | None, int | None]]]:
    """table -> column -> (compiled type, nullable, server default, string width)."""
    return {
        tname: {
            c.name: (
                compile_type(c),
                bool(c.nullable),
                compile_server_default(c),
                getattr(c.type, "length", None),
            )
            for c in table.columns
        }
        for tname, table in meta.tables.items()
    }


def pk_tuples(meta: MetaData) -> dict[str, set[str]]:
    return {tname: {c.name for c in table.primary_key.columns} for tname, table in meta.tables.items()}
