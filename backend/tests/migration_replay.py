"""Replay committed Alembic revisions' *literal* ops into a synthetic MetaData.

This is what lets the test suite compare the historical schema encoded by the
migrations against the live ORM schema without a PostgreSQL server: each revision
is executed with a recording ``op`` stand-in, so the result is a plain SQLAlchemy
``MetaData`` built only from what the migrations spell out (no ``Base.metadata``,
no ORM import).

``replay_upgrade`` covers the single frozen bootstrap revision; ``replay_chain``
covers the whole committed history, which is what the ORM comparison needs once
revisions after bootstrap exist - otherwise a legitimate ``0002`` would be
reported as drift in the models.

``fk_tuples`` / ``index_tuples`` normalise a MetaData into comparable tuples and
are shared by both the replay-vs-ORM test and the rendered-offline-SQL test.
"""
from __future__ import annotations

import ast
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
        self.dropped_indexes: list[str] = []
        self.unexpected: list[str] = []

    def create_table(self, table_name: str, *elements: Any, **_kw: Any) -> None:
        sa.Table(table_name, self.metadata, *elements)
        self.created_tables.append(table_name)

    def add_column(self, table_name: str, column: Any, **_kw: Any) -> None:
        """Attach a column a later revision puts onto an existing table.

        Revisions after bootstrap widen the schema as well as index it, and the
        point of this harness is that the *models* are not the evidence. A column
        therefore reaches the replayed MetaData only through the literal
        ``op.add_column`` that a real ``alembic upgrade`` would render, defaults
        and nullability included.
        """
        self.metadata.tables[table_name].append_column(column)

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

    def drop_index(self, index_name: str, **_kw: Any) -> None:
        """Remove a recorded index, so a revision that replaces one replays truthfully.

        `upgrade()` is still expected to *create* schema; `dropped_indexes` is what a
        test asserts on when a revision is supposed to be purely additive.
        """
        for table in self.metadata.tables.values():
            for idx in list(table.indexes):
                if idx.name == index_name:
                    table.indexes.remove(idx)
        self.dropped_indexes.append(index_name)

    def drop_table(self, table_name: str, **_kw: Any) -> None:
        self.unexpected.append(f"drop_table({table_name}) inside upgrade()")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"a committed revision uses unsupported op {name!r}; the replayed schema must be "
            "built from literal create_table/add_column/create_index/drop_index calls only"
        )


@dataclass
class ReplayResult:
    metadata: MetaData
    module: Any
    created_tables: list[str] = field(default_factory=list)
    created_indexes: list[tuple[str, str]] = field(default_factory=list)
    dropped_indexes: list[str] = field(default_factory=list)
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
        dropped_indexes=list(recorder.dropped_indexes),
        unexpected=list(recorder.unexpected),
    )


def revision_identifiers(path: Path) -> tuple[str | None, str | None]:
    """(revision, down_revision) read from the module's own assignments.

    Parsed with `ast` rather than executed: the chain order is needed by tests that
    must not depend on a working `op` proxy.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in {"revision", "down_revision"}:
                try:
                    out[name] = ast.literal_eval(node.value)
                except ValueError:
                    out[name] = None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name in {"revision", "down_revision"} and node.value is not None:
                try:
                    out[name] = ast.literal_eval(node.value)
                except ValueError:
                    out[name] = None
    return out.get("revision"), out.get("down_revision")


def chain_paths() -> list[Path]:
    """Every committed revision, in apply order (base first, head last).

    Walked from the revisions' own `down_revision` values, so the guard compares
    what Alembic would actually build - not whatever alphabetical order suggests.
    """
    files = sorted(VERSIONS_DIR.glob("*.py"))
    by_down: dict[Any, Path] = {}
    revs: set[str] = set()
    for path in files:
        rev, down = revision_identifiers(path)
        assert isinstance(rev, str), f"{path.name} declares no string `revision`"
        assert rev not in revs, f"duplicate revision id {rev}"
        revs.add(rev)
        by_down[down] = path
    base = by_down.get(None)
    assert base is not None, "no base revision (down_revision = None) found"

    ordered = [base]
    cursor = base
    while True:
        rev, _down = revision_identifiers(cursor)
        nxt = by_down.get(rev)
        if nxt is None:
            break
        assert nxt not in ordered, f"revision cycle at {nxt.name}"
        ordered.append(nxt)
        cursor = nxt
    assert len(ordered) == len(files), (
        f"revisions not reachable from the base: "
        f"{sorted(p.name for p in files if p not in ordered)}"
    )
    return ordered


def replay_chain() -> ReplayResult:
    """Replay every committed `upgrade()` into one MetaData: the cumulative schema."""
    import alembic

    metadata = MetaData()
    recorder = _RecordingOp(metadata)
    last_module = None
    original = getattr(alembic, "op", None)
    alembic.op = recorder  # type: ignore[assignment]
    try:
        for path in chain_paths():
            module, loader = _load(path, "chain")
            loader.exec_module(module)
            module.upgrade()
            last_module = module
    finally:
        if original is not None:
            alembic.op = original  # type: ignore[assignment]
    return ReplayResult(
        metadata=metadata,
        module=last_module,
        created_tables=list(recorder.created_tables),
        created_indexes=list(recorder.created_indexes),
        dropped_indexes=list(recorder.dropped_indexes),
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
