"""Migration immutability guard (Phase 1-2 hardening: never regress to dynamic).

A committed Alembic revision must reproduce the schema it was written for, even
after the ORM models move on. If a revision reads the *current* application
metadata at execution time, the migration silently becomes "whatever the models
look like today" and history stops being deterministic.

These tests parse every file in `migrations/versions/` with `ast` and fail on the
prohibited shapes, so that architecture cannot return unnoticed. The last test
feeds the scanner the exact anti-pattern that was removed here, proving the
checks actually fire.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from migration_replay import VERSIONS_DIR

REVISION_FILES = sorted(VERSIONS_DIR.glob("*.py"))

# modules a frozen revision may legitimately import
_ALLOWED_IMPORT_ROOTS = {"__future__", "alembic", "sqlalchemy"}

_PROHIBITED_CHAINS: tuple[tuple[str, ...], ...] = (
    ("Base", "metadata"),
    ("metadata", "create_all"),
    ("metadata", "drop_all"),
    ("metadata", "sorted_tables"),
    ("metadata", "tables"),
    ("target_metadata",),
)

_SOURCE_PATTERN = re.compile(r"(Base\.metadata|metadata\.create_all|metadata\.drop_all|from\s+app[\s.])")


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _attr_chain(node: ast.Attribute) -> tuple[str, ...]:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def find_disallowed_imports(paths: list[Path]) -> dict[str, list[str]]:
    """revision file -> imported roots outside alembic/sqlalchemy."""
    out: dict[str, list[str]] = {}
    for path in paths:
        extra = sorted(_import_roots(_module(path)) - _ALLOWED_IMPORT_ROOTS)
        if extra:
            out[path.name] = extra
    return out


def find_dynamic_schema_builds(paths: list[Path]) -> list[str]:
    """Occurrences of live-metadata access or create_all/drop_all calls."""
    offenders: list[str] = []
    for path in paths:
        for node in ast.walk(_module(path)):
            if isinstance(node, ast.Attribute):
                chain = _attr_chain(node)
                for prohibited in _PROHIBITED_CHAINS:
                    if chain[-len(prohibited) :] == prohibited:
                        offenders.append(f"{path.name}:{node.lineno} -> {'.'.join(chain)}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"create_all", "drop_all"}:
                    offenders.append(f"{path.name}:{node.lineno} -> {node.func.attr}()")
    return offenders


def _docstring_end_line(tree: ast.Module) -> int:
    """Last line of the module docstring (0 when there is none)."""
    if tree.body and ast.get_docstring(tree) is not None:
        return getattr(tree.body[0], "end_lineno", 0) or 0
    return 0


def find_prohibited_source_lines(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        doc_end = _docstring_end_line(_module(path))  # docstring may *describe* the ban
        for lineno, line in enumerate(text.splitlines(), start=1):
            if lineno <= doc_end or line.lstrip().startswith("#"):
                continue
            if _SOURCE_PATTERN.search(line):
                hits.append(f"{path.name}:{lineno}: {line.strip()}")
    return hits


def test_repository_has_revisions() -> None:
    assert REVISION_FILES, f"no revisions found under {VERSIONS_DIR}"


def test_no_application_orm_imports_in_any_revision() -> None:
    offenders = find_disallowed_imports(REVISION_FILES)
    assert not offenders, (
        "revision modules may only import from alembic/sqlalchemy - importing "
        f"`app.*` would let a migration read current ORM state: {offenders}"
    )


def test_no_dynamic_metadata_construction_in_any_revision() -> None:
    offenders = find_dynamic_schema_builds(REVISION_FILES)
    assert not offenders, (
        "a committed revision must spell schema out with literal op.create_table / "
        f"op.create_index calls, never derive it from live metadata: {offenders}"
    )


def test_no_prohibited_source_patterns_in_any_revision() -> None:
    hits = find_prohibited_source_lines(REVISION_FILES)
    assert not hits, "prohibited pattern found in revision source:\n" + "\n".join(hits)


def test_bootstrap_revision_uses_only_explicit_schema_ops() -> None:
    """upgrade() must be built exclusively from create_table / create_index.

    `_RecordingOp` raises for any other op, so a clean replay is the proof.
    """
    from migration_replay import replay_upgrade

    result = replay_upgrade()
    assert result.created_tables, "bootstrap revision creates no tables"
    assert not result.unexpected


def _revision_ids() -> dict[str, str | None]:
    """Every committed revision id and what it revises, read from the files themselves."""
    revisions: dict[str, str | None] = {}
    for path in REVISION_FILES:
        ns: dict[str, object] = {}
        exec(compile(_module(path), str(path), "exec"), ns)  # noqa: S102 - local, parse-only
        rev = ns.get("revision")
        assert isinstance(rev, str), f"{path.name} has no string `revision`"
        assert rev not in revisions, f"duplicate revision id {rev} in {path.name}"
        revisions[rev] = ns.get("down_revision")
    return revisions


def test_revision_chain_is_single_and_linked() -> None:
    revisions = _revision_ids()

    bases = [r for r, down in revisions.items() if down is None]
    assert len(bases) == 1, f"expected exactly one base revision, found {bases}"
    linked = {d for d in revisions.values() if d is not None}
    heads = set(revisions) - linked
    assert len(heads) == 1, f"expected a single migration head, found {sorted(heads)}"


#: `alembic_version.version_num` is `VARCHAR(32)`, and Alembic creates that table for
#: itself on the first upgrade. A longer id is not a style problem: the UPDATE that
#: records the new version raises `StringDataRightTruncation`, so the migration runs its
#: DDL and then cannot be stamped - which offline SQL rendering never notices.
MAX_REVISION_ID_LENGTH = 32


def test_every_revision_id_fits_the_version_table() -> None:
    too_long = {rev: len(rev) for rev in _revision_ids() if len(rev) > MAX_REVISION_ID_LENGTH}
    assert not too_long, (
        f"revision ids longer than {MAX_REVISION_ID_LENGTH} characters cannot be written "
        f"to alembic_version.version_num: {too_long}"
    )


_DYNAMIC_REVISION = '''
"""a revision that must be rejected by every check above."""
from alembic import op
from app.core.database import Base
from app import models  # noqa: F401

revision = "9999_dynamic"
down_revision = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    for table in Base.metadata.sorted_tables:
        table.drop(op.get_bind())
'''


def test_guards_fire_on_a_dynamic_revision(tmp_path: Path) -> None:
    """Negative control: hand the scanner the anti-pattern this pass removed."""
    bad = tmp_path / "9999_dynamic.py"
    bad.write_text(_DYNAMIC_REVISION, encoding="utf-8")
    paths = [bad]

    assert find_disallowed_imports(paths) == {
        "9999_dynamic.py": ["app"]
    }, "import scanner missed `from app... import ...`"
    dynamic = find_dynamic_schema_builds(paths)
    assert any("Base.metadata.create_all" in o for o in dynamic), dynamic
    assert any("Base.metadata.sorted_tables" in o for o in dynamic), dynamic
    assert any("create_all()" in o for o in dynamic), dynamic
    assert find_prohibited_source_lines(paths), "source scanner missed Base.metadata"

    # and the real revisions pass the very same scanners
    assert find_disallowed_imports(REVISION_FILES) == {}
    assert find_dynamic_schema_builds(REVISION_FILES) == []


@pytest.mark.parametrize("path", REVISION_FILES, ids=lambda p: p.name)
def test_every_revision_is_valid_python(path: Path) -> None:
    _module(path)
