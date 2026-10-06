"""One-off generator: introspects the CURRENT `Base.metadata` and emits an
explicit, literal Alembic migration (op.create_table/op.create_index/...).

This script is a development tool, run once by a human to produce (or
re-produce, when intentionally re-bootstrapping) `0001_bootstrap_schema.py`.
It is NOT imported by the migration itself and the generated file has NO
import of `app.models` / `Base` — it is a frozen, literal snapshot.

Usage:
    python -m scripts._gen_bootstrap_migration > migrations/versions/0001_bootstrap_schema.py
"""

from __future__ import annotations

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.schema import ForeignKeyConstraint, UniqueConstraint

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.core.database import Base

_DIALECT = postgresql.dialect()


def _render_type(col) -> str:
    t = col.type
    if isinstance(t, SAEnum):
        # native_enum=False -> plain VARCHAR, sized to fit every member; no DB
        # CHECK constraint is emitted (validation stays application-side).
        length = t.length or max(len(v) for v in t.enums)
        return f"sa.String(length={length})"
    if isinstance(t, PG_UUID):
        return "postgresql.UUID(as_uuid=True)"
    if isinstance(t, JSONB):
        return "postgresql.JSONB(astext_type=sa.Text())"
    if isinstance(t, String) and not isinstance(t, Text):
        return f"sa.String(length={t.length})"
    if isinstance(t, Text):
        return "sa.Text()"
    if isinstance(t, Boolean):
        return "sa.Boolean()"
    if isinstance(t, Integer):
        return "sa.Integer()"
    if isinstance(t, Float):
        return "sa.Float()"
    if isinstance(t, DateTime):
        return f"sa.DateTime(timezone={t.timezone!r})"
    raise TypeError(f"Unhandled column type for {col}: {t!r}")


def _render_server_default(col) -> str | None:
    if col.server_default is None:
        return None
    compiled = col.server_default.arg.compile(
        dialect=_DIALECT, compile_kwargs={"literal_binds": True}
    )
    return f"sa.text({str(compiled)!r})"


def _render_column(col) -> str:
    parts = [repr(col.name), _render_type(col)]
    if col.primary_key:
        parts.append("primary_key=True")
    if not col.nullable and not col.primary_key:
        parts.append("nullable=False")
    elif col.nullable and not col.primary_key:
        parts.append("nullable=True")
    sd = _render_server_default(col)
    if sd:
        parts.append(f"server_default={sd}")
    return f"sa.Column({', '.join(parts)})"


def _fk_name(table_name: str, cols: list[str], ref_table: str) -> str:
    return f"fk_{table_name}_{'_'.join(cols)}_{ref_table}"


def _fk_sort_key(table_name: str, fk: ForeignKeyConstraint) -> str:
    local_cols = [c.name for c in fk.columns]
    ref_table = fk.referred_table.name
    return fk.name or _fk_name(table_name, local_cols, ref_table)


def _render_fk(table_name: str, fk: ForeignKeyConstraint) -> str:
    local_cols = [c.name for c in fk.columns]
    remote_cols = [
        f"{fk.elements[i].column.table.name}.{fk.elements[i].column.name}"
        for i in range(len(fk.elements))
    ]
    ref_table = fk.elements[0].column.table.name
    name = fk.name or _fk_name(table_name, local_cols, ref_table)
    parts = [repr(local_cols), repr(remote_cols), f"name={name!r}"]
    if fk.ondelete:
        parts.append(f"ondelete={fk.ondelete!r}")
    return f"sa.ForeignKeyConstraint({', '.join(parts)})"


def _render_unique(uq: UniqueConstraint) -> str:
    cols = [repr(c.name) for c in uq.columns]
    name = uq.name
    return f"sa.UniqueConstraint({', '.join(cols)}, name={name!r})"


def _render_index(ix, table_name: str) -> str:
    cols = [repr(c.name) for c in ix.columns]
    parts = [repr(ix.name), repr(table_name), f"[{', '.join(cols)}]"]
    if ix.unique:
        parts.append("unique=True")
    where = ix.dialect_options["postgresql"].get("where")
    if where is not None:
        parts.append(f"postgresql_where=sa.text({str(where)!r})")
    return f"    op.create_index({', '.join(parts)})"


def generate() -> str:
    lines: list[str] = []
    lines.append('"""bootstrap schema')
    lines.append("")
    lines.append("Revision ID: 0001_bootstrap")
    lines.append("Revises:")
    lines.append("Create Date: 2026-10-06")
    lines.append("")
    lines.append("Explicit, deterministic initial schema for the self-hosted platform.")
    lines.append("")
    lines.append("This revision is a frozen snapshot: every table/index/constraint below is")
    lines.append("spelled out literally (op.create_table / op.create_index / explicit")
    lines.append("sa.ForeignKeyConstraint / sa.UniqueConstraint). It does NOT import or read")
    lines.append("`app.models` / `Base.metadata` at execution time, so running")
    lines.append("`alembic upgrade head` always reproduces exactly this historical schema,")
    lines.append("even if the ORM models change later.")
    lines.append("")
    lines.append("From here on, iterate with granular autogenerated revisions:")
    lines.append('    alembic revision --autogenerate -m "add <x>"')
    lines.append("so each schema change is reviewable in isolation and the down migrations stay")
    lines.append("correct. Do not hand-edit this bootstrap file after real deployments exist.")
    lines.append('"""')
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("import sqlalchemy as sa")
    lines.append("from alembic import op")
    lines.append("from sqlalchemy.dialects import postgresql")
    lines.append("")
    lines.append('revision = "0001_bootstrap"')
    lines.append("down_revision = None")
    lines.append("branch_labels = None")
    lines.append("depends_on = None")
    lines.append("")
    lines.append("")
    lines.append("def upgrade() -> None:")

    tables = Base.metadata.sorted_tables
    for t in tables:
        lines.append("    op.create_table(")
        lines.append(f"        {t.name!r},")
        for col in t.columns:
            lines.append(f"        {_render_column(col)},")
        for fk in sorted(t.foreign_key_constraints, key=lambda f: _fk_sort_key(t.name, f)):
            lines.append(f"        {_render_fk(t.name, fk)},")
        for const in t.constraints:
            if isinstance(const, UniqueConstraint):
                lines.append(f"        {_render_unique(const)},")
        lines.append("    )")
    lines.append("")
    for t in tables:
        for ix in sorted(t.indexes, key=lambda i: i.name):
            lines.append(_render_index(ix, t.name))
    lines.append("")
    lines.append("")
    lines.append("def downgrade() -> None:")
    for t in reversed(tables):
        for ix in sorted(t.indexes, key=lambda i: i.name, reverse=True):
            lines.append(f"    op.drop_index({ix.name!r}, table_name={t.name!r})")
    for t in reversed(tables):
        lines.append(f"    op.drop_table({t.name!r})")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    print(generate())
