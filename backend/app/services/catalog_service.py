"""Catalogs: the teacher's reusable collections of references (Phase 6).

A catalog is *not* an exam and *not* a copy of content. It is an ordered list of
references - `catalog_item(kind, ref_id)` - into the four banks the platform already owns,
plus a few practice settings of its own. Everything else here follows from that:

* **Reading a catalog reads through to the banks.** `to_read` resolves each reference
  against the current row, so a word corrected in the vocabulary bank is the corrected word
  here too, in every catalog that names it. Nothing is denormalised, so nothing can drift.
* **Grouping writes no content.** Adding, moving or removing a reference touches
  `catalog_item` and never `question`, so it appends no `QuestionVersion`. Phase 5
  established the same rule for a passage's sets, for the same reason: reorganising a
  lesson must not snapshot exercises that did not change.
* **One catalog names one piece of content once.** The service refuses the duplicate with
  its title in the sentence, and migration `0005`'s unique index is what keeps that true
  when two writes land in the same moment.
* **A reference to content that is gone is refused, not forgiven.** Pointing at a row that
  does not exist, or one in the trash, is rejected at the door: a catalog that opens with a
  hole in it looks like a saved lesson and behaves like a broken one.

Folders come from `parent_id`, which is why two rules live here that the other banks do not
need: siblings cannot share a name, and a catalog cannot be moved inside itself. The nesting
cap is measured on the way down as well as up, because moving a subtree is when depth
actually changes.

Availability is a chain, not a flag: a learner reaches a catalog only when it is ready and
un-trashed *and* every folder above it is too. A teacher who bins a unit means the whole
unit to leave the class's practice list, and a list that kept showing the lessons inside it
would be a list of things nobody can open.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums, security
from app.models.assessment import Catalog, CatalogItem
from app.models.content import (
    Listening,
    ListeningQuestionSet,
    Question,
    Reading,
    ReadingQuestionSet,
    VocabularyEntry,
)
from app.schemas import catalog as c_schemas
from app.services import audit_service, passage_service

SETTABLE_STATUSES = list(c_schemas.SETTABLE_STATUSES)
VIEWS = c_schemas.VIEWS
MAX_ITEMS = c_schemas.MAX_ITEMS_PER_CATALOG
MAX_DEPTH = c_schemas.MAX_DEPTH

SORTABLE = {
    "name": Catalog.name,
    "created_at": Catalog.created_at,
    "updated_at": Catalog.updated_at,
}


@dataclass(frozen=True)
class KindTarget:
    """One bank a catalog can point at.

    `title_attr` is the column the bank prints its name from and `sets`/`passage_fk`
    describe the block table above its passages, when it has one. Listing the differences
    as data is what lets `resolve` describe any reference with one code path.
    """

    kind: str
    label: str
    model: Any
    title_attr: str
    sets: Any = None
    passage_fk: str | None = None


_TARGETS: dict[str, KindTarget] = {
    target.kind: target
    for target in (
        KindTarget(kind="question", label="question", model=Question, title_attr="prompt"),
        KindTarget(kind="vocabulary", label="word", model=VocabularyEntry, title_attr="word"),
        KindTarget(
            kind="reading",
            label="reading text",
            model=Reading,
            title_attr="title",
            sets=ReadingQuestionSet,
            passage_fk="reading_id",
        ),
        KindTarget(
            kind="listening",
            label="recording",
            model=Listening,
            title_attr="title",
            sets=ListeningQuestionSet,
            passage_fk="listening_id",
        ),
    )
}

KIND_LABELS = {kind: target.label for kind, target in _TARGETS.items()}


class CatalogError(ValueError):
    """A teacher-facing problem with a catalog or one of its references; the endpoints 422."""


class CatalogNotFound(Exception):
    """No such catalog, or no such reference in the catalog named. 404."""


class NameTaken(Exception):
    """Another catalog of this name already sits in this folder (409)."""

    def __init__(self, message: str, *, existing_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class DuplicateReference(Exception):
    """This catalog already holds that piece of content (409)."""

    def __init__(self, message: str, *, existing_item_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.existing_item_id = existing_item_id


def _label_of(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def status_enum(raw: str) -> enums.ContentStatus:
    try:
        value = enums.ContentStatus(raw)
    except ValueError:
        raise CatalogError(f"status must be one of: {', '.join(SETTABLE_STATUSES)}") from None
    if value == enums.ContentStatus.TRASH:
        raise CatalogError("trash is a deletion, not a status - use the trash action")
    return value


def timing_of(raw: str | None) -> str:
    """The catalog's feedback timing, defaulting to the one a teacher never has to set."""
    if raw is None:
        return "instant"
    if raw not in c_schemas.FEEDBACK_TIMINGS:
        raise CatalogError("feedback timing must be one of: " + ", ".join(c_schemas.FEEDBACK_TIMINGS))
    return raw


def kind_of(raw: str) -> KindTarget:
    target = _TARGETS.get(raw)
    if target is None:
        raise CatalogError("kind must be one of: " + ", ".join(c_schemas.ITEM_KINDS))
    return target


# --------------------------------------------------------------------------- #
# One row, and the chain it hangs in
# --------------------------------------------------------------------------- #


async def get(db: AsyncSession, catalog_id: uuid.UUID, *, allow_trash: bool = True) -> Catalog:
    row = await db.get(Catalog, catalog_id)
    if row is None or (row.deleted_at is not None and not allow_trash):
        raise CatalogNotFound("no catalog with that id")
    return row


async def get_item(db: AsyncSession, item_id: uuid.UUID) -> tuple[Catalog, CatalogItem]:
    """A reference and the catalog that holds it.

    Every item write arrives by item id, so the catalog is read through the item - which is
    also what makes "that item is not in this catalog" impossible to say by accident.
    """
    item = await db.get(CatalogItem, item_id)
    if item is None:
        raise CatalogNotFound("no catalog item with that id")
    catalog = await db.get(Catalog, item.catalog_id)
    if catalog is None:
        raise CatalogNotFound("no catalog with that id")
    return catalog, item


async def ancestors(db: AsyncSession, catalog: Catalog) -> list[Catalog]:
    """The folders above this catalog, nearest first, cycle-safe and depth-bounded.

    A `parent_id` chain can only loop through a row written before the check existed or
    edited by hand. Walking with a visited set means such a row yields an empty chain
    instead of a request that never finishes.
    """
    chain: list[Catalog] = []
    seen = {catalog.id}
    parent_id = catalog.parent_id
    while parent_id is not None and len(chain) <= MAX_DEPTH:
        parent = await db.get(Catalog, parent_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        chain.append(parent)
        parent_id = parent.parent_id
    return chain


async def path_of(db: AsyncSession, catalog: Catalog) -> list[dict]:
    """Root-down breadcrumb, excluding the catalog itself."""
    chain = await ancestors(db, catalog)
    return [{"id": str(row.id), "name": row.name} for row in reversed(chain)]


async def subtree_depth(db: AsyncSession, catalog: Catalog) -> int:
    """How many folder levels hang below this catalog (0 when it is a leaf)."""
    depth = 0
    frontier = [catalog.id]
    seen = {catalog.id}
    while frontier and depth <= MAX_DEPTH:
        rows = (await db.execute(select(Catalog.id).where(Catalog.parent_id.in_(frontier)))).scalars().all()
        frontier = [row for row in rows if row not in seen]
        seen.update(frontier)
        if frontier:
            depth += 1
    return depth


async def visible_to_learner(db: AsyncSession, catalog: Catalog) -> bool:
    """Ready, un-trashed, and every folder above it the same."""
    if catalog.deleted_at is not None or catalog.status != enums.ContentStatus.READY:
        return False
    for parent in await ancestors(db, catalog):
        if parent.deleted_at is not None or parent.status != enums.ContentStatus.READY:
            return False
    return True


# --------------------------------------------------------------------------- #
# One reference, described
# --------------------------------------------------------------------------- #


def _title_of(target: KindTarget, row: Any) -> str | None:
    if row is None:
        return None
    value = getattr(row, target.title_attr, None)
    return str(value) if value is not None else None


def _state_of(row: Any) -> str:
    if row is None:
        return "missing"
    if getattr(row, "deleted_at", None) is not None:
        return "trashed"
    return _label_of(row.status)


def _set_id_of(item: CatalogItem) -> uuid.UUID | None:
    """The block a reference names, as a UUID - `config` stores it as text."""
    raw = (item.config or {}).get("set_id")
    return uuid.UUID(str(raw)) if raw else None


async def _block_of(db: AsyncSession, target: KindTarget, set_id: uuid.UUID) -> Any:
    """The block row a reference names, or None when it does not exist."""
    if target.sets is None:
        return None
    return await db.get(target.sets, set_id)


def _item_read(target: KindTarget, item: CatalogItem, row: Any, block: Any) -> dict:
    """One reference printed, from the content row it points at.

    `detail` is the short line that tells two references apart in a list - a question's type
    and mark, a word's part of speech, a passage's level, a catalog block's title. It is read
    from the row every time and never stored here, so it cannot go stale.
    """
    state = _state_of(row)
    detail: str | None = None
    if row is not None:
        if target.kind == "question":
            detail = _label_of(row.type) + (f" · {row.score:g}" if row.score is not None else "")
        elif target.kind == "vocabulary":
            detail = row.part_of_speech or row.level
        else:
            detail = row.level

    config = dict(item.config or {})
    if config.get("set_id") and row is not None:
        if block is None or getattr(block, target.passage_fk) != item.ref_id:
            # Either the group was deleted or it belongs to a different passage: in both
            # cases this catalog claims a lesson that cannot be served. The state says it,
            # and the screen words it in the teacher's own language.
            state = "broken_block"
        elif block.title:
            # Which block is bound is the screen's to say - it numbers an untitled block
            # from its position in the text - so only the teacher's own title is added here.
            detail = " · ".join(part for part in (detail, block.title) if part)

    return c_schemas.CatalogItemRead(
        id=item.id,
        kind=target.kind,
        ref_id=item.ref_id,
        position=item.position,
        config=config,
        title=_title_of(target, row),
        detail=detail,
        state=state,
        available_to_learner=state == "ready",
    ).model_dump(mode="json")


async def resolve(db: AsyncSession, item: CatalogItem) -> dict:
    """One `catalog_item` row plus what its content is called and whether it can be used."""
    target = kind_of(_label_of(item.kind))
    row = await db.get(target.model, item.ref_id)
    set_id = _set_id_of(item)
    block = await _block_of(db, target, set_id) if set_id is not None and row is not None else None
    return _item_read(target, item, row, block)


async def resolve_many(db: AsyncSession, items: list[CatalogItem]) -> list[dict]:
    """The same descriptions, fetched one query per bank instead of one per reference.

    A catalog holds up to three hundred references and the editor draws every one of them,
    so resolving them individually would be three hundred round trips to open one lesson.
    """
    grouped: dict[str, list[CatalogItem]] = {}
    for item in items:
        grouped.setdefault(_label_of(item.kind), []).append(item)

    rows: dict[tuple[str, uuid.UUID], Any] = {}
    blocks: dict[tuple[str, uuid.UUID], Any] = {}
    for kind, group in grouped.items():
        target = kind_of(kind)
        found = (
            await db.execute(select(target.model).where(target.model.id.in_({item.ref_id for item in group})))
        ).scalars().all()
        for row in found:
            rows[(kind, row.id)] = row
        set_ids = {_set_id_of(item) for item in group}
        set_ids.discard(None)
        if set_ids and target.sets is not None:
            found_blocks = (
                await db.execute(select(target.sets).where(target.sets.id.in_(set_ids)))
            ).scalars().all()
            for block in found_blocks:
                blocks[(kind, block.id)] = block

    out: list[dict] = []
    for item in items:
        kind = _label_of(item.kind)
        target = kind_of(kind)
        set_id = _set_id_of(item)
        block = blocks.get((kind, set_id)) if set_id is not None else None
        out.append(_item_read(target, item, rows.get((kind, item.ref_id)), block))
    return out


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #


def feedback_timing_of(catalog: Catalog) -> str:
    value = (catalog.meta or {}).get("feedback_timing")
    return str(value) if value else "instant"


def _summary(
    catalog: Catalog,
    *,
    item_count: int,
    counts: dict[str, int],
    child_count: int,
    parent_name: str | None,
    unavailable_count: int | None = None,
) -> dict:
    return c_schemas.CatalogSummary(
        id=catalog.id,
        name=catalog.name,
        description=catalog.description,
        parent_id=catalog.parent_id,
        parent_name=parent_name,
        learning_language=catalog.learning_language,
        level=catalog.level,
        shuffle_default=bool(catalog.shuffle_default),
        known_states_enabled=bool(catalog.known_states_enabled),
        feedback_timing=feedback_timing_of(catalog),
        status=_label_of(catalog.status),
        item_count=item_count,
        counts=counts,
        unavailable_count=unavailable_count,
        child_count=child_count,
        created_at=catalog.created_at.isoformat(),
        updated_at=catalog.updated_at.isoformat(),
        deleted_at=catalog.deleted_at.isoformat() if catalog.deleted_at else None,
    ).model_dump(mode="json")


async def stats_for(db: AsyncSession, catalog_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Item counts, per-kind counts and child counts for a page, in three grouped queries.

    All three are read for every row of the list screen, so asking per catalog would turn a
    page of fifty into a hundred and fifty queries.
    """
    if not catalog_ids:
        return {}
    counts: dict[uuid.UUID, dict[str, int]] = {catalog_id: {} for catalog_id in catalog_ids}
    totals: dict[uuid.UUID, int] = {catalog_id: 0 for catalog_id in catalog_ids}
    rows = (
        await db.execute(
            select(CatalogItem.catalog_id, CatalogItem.kind, func.count())
            .where(CatalogItem.catalog_id.in_(catalog_ids))
            .group_by(CatalogItem.catalog_id, CatalogItem.kind)
        )
    ).all()
    for catalog_id, kind, count in rows:
        counts[catalog_id][_label_of(kind)] = int(count)
        totals[catalog_id] += int(count)

    children: dict[uuid.UUID, int] = {catalog_id: 0 for catalog_id in catalog_ids}
    child_rows = (
        await db.execute(
            select(Catalog.parent_id, func.count()).where(Catalog.parent_id.in_(catalog_ids)).group_by(Catalog.parent_id)
        )
    ).all()
    for parent_id, count in child_rows:
        children[parent_id] = int(count)

    return {
        catalog_id: {"item_count": totals[catalog_id], "counts": counts[catalog_id], "child_count": children[catalog_id]}
        for catalog_id in catalog_ids
    }


async def _parent_names(db: AsyncSession, catalogs: list[Catalog]) -> dict[uuid.UUID, str | None]:
    parent_ids = {row.parent_id for row in catalogs if row.parent_id}
    if not parent_ids:
        return {row.id: None for row in catalogs}
    rows = (await db.execute(select(Catalog.id, Catalog.name).where(Catalog.id.in_(parent_ids)))).all()
    names = {row[0]: row[1] for row in rows}
    return {row.id: names.get(row.parent_id) for row in catalogs}


async def to_read(db: AsyncSession, catalog: Catalog) -> dict:
    """The catalog, its breadcrumb, and every reference in it with its content described."""
    stats = await stats_for(db, [catalog.id])
    names = await _parent_names(db, [catalog])
    one = stats[catalog.id]
    read = await list_items(db, catalog)
    unavailable = sum(1 for row in read if not row["available_to_learner"])
    payload = _summary(
        catalog,
        item_count=one["item_count"],
        counts=one["counts"],
        child_count=one["child_count"],
        parent_name=names[catalog.id],
        unavailable_count=unavailable,
    )
    payload["path"] = await path_of(db, catalog)
    payload["items"] = read
    payload["available_to_learner"] = await visible_to_learner(db, catalog)
    return c_schemas.CatalogRead(**payload).model_dump(mode="json")


async def list_items(db: AsyncSession, catalog: Catalog) -> list[dict]:
    rows = (
        await db.execute(
            select(CatalogItem)
            .where(CatalogItem.catalog_id == catalog.id)
            .order_by(CatalogItem.position, CatalogItem.id)
        )
    ).scalars().all()
    return await resolve_many(db, list(rows))


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


async def meta(db: AsyncSession) -> dict:
    """Everything the catalog screen builds its pickers from, taken from what enforces it."""
    return {
        "learning_languages": await passage_service.enabled_languages(db),
        "levels": constants.CEFR_LEVELS,
        "statuses": SETTABLE_STATUSES,
        "views": list(VIEWS),
        "sortable": sorted(SORTABLE),
        "item_kinds": [{"kind": kind, "label": KIND_LABELS[kind]} for kind in c_schemas.ITEM_KINDS],
        "bulk_actions": sorted(c_schemas.BULK_ACTIONS),
        "feedback_timings": list(c_schemas.FEEDBACK_TIMINGS),
        "max_items": MAX_ITEMS,
        "max_depth": MAX_DEPTH,
        "item_states": list(c_schemas.ITEM_STATES),
    }


async def list_catalogs(
    db: AsyncSession,
    *,
    q: str | None = None,
    kind: str | None = None,
    parent_id: uuid.UUID | None = None,
    root_only: bool = False,
    language: str | None = None,
    level: str | None = None,
    status: str | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    if view not in VIEWS:
        raise CatalogError("view must be one of: " + ", ".join(VIEWS))
    if sort not in SORTABLE:
        raise CatalogError("sort must be one of: " + ", ".join(sorted(SORTABLE)))
    if order not in ("asc", "desc"):
        raise CatalogError("order must be 'asc' or 'desc'")
    if kind is not None:
        kind = kind_of(kind).kind
    if status is not None:
        status = _label_of(status_enum(status))
    if level is not None:
        level = _check_level(level)
    page = max(1, page)
    page_size = min(max(1, page_size), 200)

    stmt = select(Catalog)
    if view == "bank":
        stmt = stmt.where(Catalog.deleted_at.is_(None))
    elif view == "trash":
        stmt = stmt.where(Catalog.deleted_at.is_not(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Catalog.name.ilike(like), Catalog.description.ilike(like)))
    if parent_id is not None:
        stmt = stmt.where(Catalog.parent_id == parent_id)
    if root_only:
        stmt = stmt.where(Catalog.parent_id.is_(None))
    if language:
        stmt = stmt.where(Catalog.learning_language == language)
    if level:
        stmt = stmt.where(Catalog.level == level)
    if status:
        stmt = stmt.where(Catalog.status == status)
    if kind:
        # "which catalogs hold a recording" is a question about the references, and the
        # EXISTS keeps it a subquery instead of a join that would duplicate rows.
        stmt = stmt.where(
            select(CatalogItem.id).where(CatalogItem.catalog_id == Catalog.id, CatalogItem.kind == kind).exists()
        )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    column = SORTABLE[sort]
    direction = column.desc() if order == "desc" else column.asc()
    rows = (
        await db.execute(stmt.order_by(direction, Catalog.id).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    catalogs = list(rows)
    stats = await stats_for(db, [row.id for row in catalogs])
    names = await _parent_names(db, catalogs)
    items = [
        _summary(
            row,
            item_count=stats[row.id]["item_count"],
            counts=stats[row.id]["counts"],
            child_count=stats[row.id]["child_count"],
            parent_name=names[row.id],
        )
        for row in catalogs
    ]
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Rules shared by create and update
# --------------------------------------------------------------------------- #


async def _check_language(db: AsyncSession, code: str | None) -> None:
    if code is None:
        return
    options = await passage_service.enabled_languages(db)
    if code not in options:
        raise CatalogError(
            f"'{code}' is not an enabled learning language (available: {', '.join(options) or 'none configured'})"
        )


def _check_level(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    band = value.strip().upper()
    if band not in constants.CEFR_LEVELS:
        raise CatalogError(f"'{value}' is not a CEFR level (use one of: {', '.join(constants.CEFR_LEVELS)})")
    return band


async def _check_name(
    db: AsyncSession,
    name: str,
    *,
    parent_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Two catalogs in one folder cannot share a name.

    Case-insensitive, because a teacher who types "Spring B2" twice means the same folder,
    and a list of two near-identical names is a list they cannot navigate. The null case is
    written as `IS NULL` on purpose: comparing a parent to `None` matches no row, so
    root-level duplicates would slip through an ordinary equality test.
    """
    stmt = select(Catalog.id).where(
        func.lower(Catalog.name) == name.lower(),
        Catalog.deleted_at.is_(None),
        Catalog.parent_id.is_(parent_id) if parent_id is None else Catalog.parent_id == parent_id,
    )
    if exclude_id is not None:
        stmt = stmt.where(Catalog.id != exclude_id)
    clash = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if clash is not None:
        raise NameTaken(f"a catalog called '{name}' is already in this folder", existing_id=clash)


async def _check_parent(
    db: AsyncSession,
    parent_id: uuid.UUID | None,
    *,
    self_id: uuid.UUID | None = None,
    subtree_depth: int = 0,
) -> None:
    """The folder a catalog is being put in must exist, be live, and not be inside it.

    `subtree_depth` is what hangs below the row being moved, so the cap applies to the whole
    subtree rather than the one row the request named. Without it a folder three levels deep
    could be moved under another three-level folder and produce a tree the screens cannot
    render.
    """
    if parent_id is None:
        return
    if self_id is not None and parent_id == self_id:
        raise CatalogError("a catalog cannot be inside itself")
    parent = await db.get(Catalog, parent_id)
    if parent is None or parent.deleted_at is not None:
        raise CatalogError("the folder to put this catalog in is not in the bank")
    if self_id is not None:
        chain = await ancestors(db, parent)
        if any(row.id == self_id for row in chain):
            raise CatalogError("a catalog cannot be moved inside one of its own folders")
    if len(await ancestors(db, parent)) + 1 + subtree_depth >= MAX_DEPTH:
        raise CatalogError(f"catalogs can be nested {MAX_DEPTH} levels deep at most")


# --------------------------------------------------------------------------- #
# Writing a catalog
# --------------------------------------------------------------------------- #


async def create(db: AsyncSession, payload: c_schemas.CatalogCreate, *, admin_id: uuid.UUID) -> dict:
    await _check_language(db, payload.learning_language)
    level = _check_level(payload.level)
    await _check_parent(db, payload.parent_id)
    await _check_name(db, payload.name, parent_id=payload.parent_id)
    catalog = Catalog(
        name=payload.name,
        description=payload.description,
        parent_id=payload.parent_id,
        learning_language=payload.learning_language,
        level=level,
        shuffle_default=bool(payload.shuffle_default),
        known_states_enabled=bool(payload.known_states_enabled),
        status=status_enum(payload.status),
        meta={"feedback_timing": timing_of(payload.feedback_timing)},
    )
    db.add(catalog)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.create",
        actor_id=admin_id,
        target_type="catalog",
        target_id=catalog.id,
        after={"name": catalog.name, "parent_id": str(catalog.parent_id) if catalog.parent_id else None},
    )
    return await to_read(db, catalog)


async def update(
    db: AsyncSession, catalog: Catalog, payload: c_schemas.CatalogUpdate, *, admin_id: uuid.UUID
) -> dict:
    given = payload.model_dump(exclude_unset=True)
    before = {
        "name": catalog.name,
        "parent_id": str(catalog.parent_id) if catalog.parent_id else None,
        "status": _label_of(catalog.status),
    }

    if "learning_language" in given:
        await _check_language(db, payload.learning_language)
        catalog.learning_language = payload.learning_language
    if "level" in given:
        catalog.level = _check_level(payload.level)
    if "feedback_timing" in given and payload.feedback_timing is not None:
        meta = dict(catalog.meta or {})
        meta["feedback_timing"] = timing_of(payload.feedback_timing)
        # A JSONB column is only rewritten as a whole object: mutating `catalog.meta` in
        # place would leave SQLAlchemy unable to see that anything changed.
        catalog.meta = meta
    if "shuffle_default" in given:
        catalog.shuffle_default = bool(payload.shuffle_default)
    if "known_states_enabled" in given:
        catalog.known_states_enabled = bool(payload.known_states_enabled)
    if "description" in given:
        catalog.description = payload.description

    if "parent_id" in given and given["parent_id"] != catalog.parent_id:
        depth = await subtree_depth(db, catalog)
        await _check_parent(db, payload.parent_id, self_id=catalog.id, subtree_depth=depth)
        catalog.parent_id = payload.parent_id
    if "name" in given and payload.name and payload.name != catalog.name:
        await _check_name(db, payload.name, parent_id=catalog.parent_id, exclude_id=catalog.id)
        catalog.name = payload.name

    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.update",
        actor_id=admin_id,
        target_type="catalog",
        target_id=catalog.id,
        before=before,
        after={
            "name": catalog.name,
            "parent_id": str(catalog.parent_id) if catalog.parent_id else None,
            "status": _label_of(catalog.status),
        },
    )
    return await to_read(db, catalog)


async def set_status(db: AsyncSession, catalog: Catalog, raw_status: str, *, admin_id: uuid.UUID) -> dict:
    value = status_enum(raw_status)
    if catalog.deleted_at is not None:
        raise CatalogError("restore the catalog from the trash before changing its status")
    if value == enums.ContentStatus.READY:
        await _assert_publishable(db, catalog)
    before = _label_of(catalog.status)
    catalog.status = value
    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.status",
        actor_id=admin_id,
        target_type="catalog",
        target_id=catalog.id,
        before={"status": before},
        after={"status": _label_of(value)},
    )
    return await to_read(db, catalog)


async def _live_children(db: AsyncSession, catalog: Catalog) -> int:
    """Folders sitting inside this catalog that are not in the trash."""
    return int(
        (
            await db.execute(
                select(func.count()).where(Catalog.parent_id == catalog.id, Catalog.deleted_at.is_(None))
            )
        ).scalar_one()
    )


async def _assert_publishable(db: AsyncSession, catalog: Catalog) -> None:
    """A catalog a learner cannot use cannot be published.

    Publishing an empty catalog is refused because it is nearly always an accident: the
    class would open a practice with nothing in it. A catalog holding only *folders* is the
    exception, and has to be - availability is a chain, so "Unit 1" must be publishable for
    the lessons inside it to reach anybody at all. The third refusal is the one a teacher
    meets later: every reference in the catalog has been binned or deleted since it was last
    edited, which would otherwise surface as a class with nothing to open.
    """
    read = await list_items(db, catalog)
    if not read and not await _live_children(db, catalog):
        raise CatalogError("an empty catalog cannot be published - add something to practise first")
    if read and not any(row["available_to_learner"] for row in read):
        raise CatalogError(
            "none of the content in this catalog can be used right now - "
            "restore or replace the references before publishing it"
        )


async def trash(db: AsyncSession, catalog: Catalog, *, admin_id: uuid.UUID) -> dict:
    """Soft delete, unless live folders would be left inside it."""
    children = await _live_children(db, catalog)
    if children:
        raise CatalogError(
            f"this catalog still holds {children} folder(s) inside it - trash or move them first"
        )
    catalog.deleted_at = security.utcnow()
    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.trash",
        actor_id=admin_id,
        target_type="catalog",
        target_id=catalog.id,
        after={"name": catalog.name},
    )
    return {"id": str(catalog.id), "name": catalog.name, "status": _label_of(catalog.status)}


async def restore(db: AsyncSession, catalog: Catalog, *, admin_id: uuid.UUID) -> dict:
    """Bring the catalog back. A name taken while it was gone is refused, not renamed.

    The folder it lived in may itself be trashed. That is not a reason to refuse the
    restore - the teacher then could not even move it - and the catalog stays invisible
    through the chain rule until they sort the folder out, which is what the breadcrumb on
    its screen shows them.
    """
    await _check_name(db, catalog.name, parent_id=catalog.parent_id, exclude_id=catalog.id)
    catalog.deleted_at = None
    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.restore",
        actor_id=admin_id,
        target_type="catalog",
        target_id=catalog.id,
        after={"name": catalog.name},
    )
    return await to_read(db, catalog)


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #


async def _references_of(db: AsyncSession, catalog_id: uuid.UUID) -> dict[tuple[str, uuid.UUID], uuid.UUID]:
    rows = (
        await db.execute(
            select(CatalogItem.id, CatalogItem.kind, CatalogItem.ref_id).where(CatalogItem.catalog_id == catalog_id)
        )
    ).all()
    return {(_label_of(row[1]), row[2]): row[0] for row in rows}


async def _check_block(db: AsyncSession, target: KindTarget, ref_id: uuid.UUID, set_id: uuid.UUID | None) -> None:
    """A named block must be a block *of the passage this reference names*."""
    if set_id is None:
        return
    if target.sets is None:
        raise CatalogError(f"a {target.label} has no blocks to name")
    fk = getattr(target.sets, target.passage_fk)
    found = (
        await db.execute(select(target.sets.id).where(target.sets.id == set_id, fk == ref_id))
    ).scalar_one_or_none()
    if found is None:
        raise CatalogError(f"that block does not belong to the {target.label} this catalog points at")


async def add_items(
    db: AsyncSession, catalog: Catalog, payload: c_schemas.ItemAddRequest, *, admin_id: uuid.UUID
) -> dict:
    """Append references after the ones the catalog already holds.

    Every reference is checked before anything is written, and the rows are inserted in one
    transaction, so a request that names ten items and fails on the seventh changes nothing
    rather than leaving the teacher with six.
    """
    if catalog.deleted_at is not None:
        raise CatalogError("restore the catalog before adding to it")
    existing = await _references_of(db, catalog.id)
    if len(existing) + len(payload.items) > MAX_ITEMS:
        raise CatalogError(f"a catalog holds at most {MAX_ITEMS} items")

    checked: list[tuple[KindTarget, uuid.UUID, c_schemas.ItemConfig]] = []
    for item in payload.items:
        target = kind_of(item.kind)
        row = await db.get(target.model, item.ref_id)
        if row is None:
            raise CatalogError(f"no {target.label} with that id - the content may have been deleted")
        if getattr(row, "deleted_at", None) is not None:
            raise CatalogError(
                f"that {target.label} is in the trash - restore it before adding it to a catalog"
            )
        await _check_block(db, target, item.ref_id, item.config.set_id)
        key = (target.kind, item.ref_id)
        if key in existing:
            title = _title_of(target, row)
            raise DuplicateReference(
                f"this catalog already holds that {target.label}"
                + (f": '{str(title)[:80]}'" if title else ""),
                existing_item_id=existing[key],
            )
        # The schema already refuses the same (kind, ref_id) twice in one body, so
        # `existing` only has to remember what the database held before this request.
        checked.append((target, item.ref_id, item.config))

    last = (
        await db.execute(
            select(func.coalesce(func.max(CatalogItem.position), -1)).where(CatalogItem.catalog_id == catalog.id)
        )
    ).scalar_one()
    position = int(last) + 1
    created_rows: list[CatalogItem] = []
    for target, ref_id, config in checked:
        created = CatalogItem(
            catalog_id=catalog.id,
            kind=enums.ContentKind(target.kind),
            ref_id=ref_id,
            position=position,
            config=config.stored(),
        )
        db.add(created)
        created_rows.append(created)
        position += 1
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_catalog_item_reference" in str(exc.orig):
            raise DuplicateReference("this catalog already holds that content") from None
        raise
    await audit_service.record_audit(
        db,
        action="catalog.items.add",
        actor_id=admin_id,
        target_type="catalog",
        target_id=catalog.id,
        after={"added": len(created_rows), "kinds": [target.kind for target, _ref_id, _config in checked]},
    )
    return {
        "added": await resolve_many(db, created_rows),
        "catalog": await to_read(db, catalog),
    }


async def update_item(
    db: AsyncSession, catalog: Catalog, item: CatalogItem, payload: c_schemas.ItemUpdate, *, admin_id: uuid.UUID
) -> dict:
    """Change the block a passage reference names. Content is never written here."""
    if catalog.deleted_at is not None:
        raise CatalogError("restore the catalog before editing its items")
    target = kind_of(_label_of(item.kind))
    await _check_block(db, target, item.ref_id, payload.config.set_id)
    before = dict(item.config or {})
    item.config = payload.config.stored()
    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.item.update",
        actor_id=admin_id,
        target_type="catalog_item",
        target_id=item.id,
        before={"config": before},
        after={"config": item.config},
    )
    return await resolve(db, item)


async def remove_item(db: AsyncSession, item: CatalogItem, *, admin_id: uuid.UUID) -> dict:
    """Drop the reference. The content it named stays exactly where it was.

    This is the difference between a catalog and a folder of copies: removing a word from a
    practice list does not remove it from the vocabulary bank, so a learner's saved favorite
    and any activity row that names it still point at a row that exists.
    """
    target = kind_of(_label_of(item.kind))
    row = await db.get(target.model, item.ref_id)
    title = _title_of(target, row)
    catalog_id = item.catalog_id
    await db.delete(item)
    await db.flush()
    left = int(
        (await db.execute(select(func.count()).where(CatalogItem.catalog_id == catalog_id))).scalar_one()
    )
    await audit_service.record_audit(
        db,
        action="catalog.item.remove",
        actor_id=admin_id,
        target_type="catalog_item",
        target_id=item.id,
        before={"kind": target.kind, "ref_id": str(item.ref_id), "title": title},
        after={"items_left": left},
    )
    return {"ok": True, "removed": str(item.id), "content_untouched": True, "items_left": left}


async def reorder(
    db: AsyncSession, catalog: Catalog, item_ids: list[uuid.UUID], *, admin_id: uuid.UUID
) -> dict:
    """Renumber the references. The request must name every item of this catalog."""
    held = set((await _references_of(db, catalog.id)).values())
    wanted = list(item_ids)
    if set(wanted) != held:
        missing = len(held - set(wanted))
        extra = len(set(wanted) - held)
        raise CatalogError(
            "the new order must name every item of this catalog exactly once"
            + (f" ({missing} missing)" if missing else "")
            + (f" ({extra} not in this catalog)" if extra else "")
        )
    rows = (await db.execute(select(CatalogItem).where(CatalogItem.id.in_(wanted)))).scalars().all()
    by_id = {row.id: row for row in rows}
    for position, item_id in enumerate(wanted):
        by_id[item_id].position = position
    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.reorder",
        actor_id=admin_id,
        target_type="catalog",
        target_id=catalog.id,
        after={"count": len(wanted)},
    )
    return {"items": await list_items(db, catalog)}


# --------------------------------------------------------------------------- #
# Bulk work
# --------------------------------------------------------------------------- #


async def bulk(db: AsyncSession, payload: c_schemas.CatalogBulkRequest, *, admin_id: uuid.UUID) -> dict:
    """One action over many catalogs, answered per id - never a silent partial win.

    The two actions that carry a value are checked once here rather than per row: a
    `set_level` with no level in it would otherwise blank the level off every catalog the
    teacher had selected, which is a change they never asked for and cannot see happen.
    """
    target_status: enums.ContentStatus | None = None
    level: str | None = None
    if payload.action == "status":
        if not payload.status:
            raise CatalogError("choose the status to set: " + ", ".join(SETTABLE_STATUSES))
        target_status = status_enum(payload.status)
    elif payload.action == "set_level":
        if not (payload.level or "").strip():
            raise CatalogError("choose the level to set (use one of: " + ", ".join(constants.CEFR_LEVELS) + ")")
        level = _check_level(payload.level)

    rows = (await db.execute(select(Catalog).where(Catalog.id.in_(list(payload.catalog_ids))))).scalars().all()
    found = {row.id: row for row in rows}
    updated: list[str] = []
    refused: list[dict] = []
    not_found = [str(catalog_id) for catalog_id in payload.catalog_ids if catalog_id not in found]

    pending = [catalog_id for catalog_id in payload.catalog_ids if catalog_id in found]
    while pending:
        blocked: list[dict] = []
        for catalog_id in pending:
            try:
                await _apply_bulk(db, found[catalog_id], payload.action, target_status, level)
            except CatalogError as exc:
                blocked.append({"id": str(catalog_id), "reason": str(exc)})
                continue
            updated.append(str(catalog_id))
        refused = blocked
        # Trashing is the one action whose rows depend on each other: a folder cannot be
        # binned while a live folder sits inside it, and one selection can hold both. Going
        # round again over whatever was refused is what lets a unit and the lessons inside it
        # leave the class's list in a single click. A pass that changes nothing stops it, so a
        # genuinely blocked folder is still reported with its reason instead of being retried.
        if payload.action != "trash" or len(blocked) == len(pending):
            break
        await db.flush()
        pending = [uuid.UUID(row["id"]) for row in blocked]

    await db.flush()
    await audit_service.record_audit(
        db,
        action="catalog.bulk",
        actor_id=admin_id,
        target_type="catalog",
        detail=(
            f"{payload.action}: {len(updated)} updated, {len(refused)} refused, {len(not_found)} not found"
        ),
    )
    return {"action": payload.action, "updated": updated, "refused": refused, "not_found": not_found}


async def _apply_bulk(
    db: AsyncSession,
    catalog: Catalog,
    action: str,
    target_status: enums.ContentStatus | None,
    level: str | None,
) -> None:
    if action == "status":
        if catalog.deleted_at is not None:
            raise CatalogError("this catalog is in the trash")
        if target_status == enums.ContentStatus.READY:
            await _assert_publishable(db, catalog)
        catalog.status = target_status
        return
    if action == "trash":
        if catalog.deleted_at is not None:
            raise CatalogError("this catalog is already in the trash")
        children = await _live_children(db, catalog)
        if children:
            raise CatalogError(f"this catalog still holds {children} folder(s) inside it")
        catalog.deleted_at = security.utcnow()
        return
    if action == "restore":
        if catalog.deleted_at is None:
            raise CatalogError("this catalog is not in the trash")
        await _check_name(db, catalog.name, parent_id=catalog.parent_id, exclude_id=catalog.id)
        catalog.deleted_at = None
        return
    if action == "set_level":
        if catalog.deleted_at is not None:
            raise CatalogError("this catalog is in the trash")
        catalog.level = level
        return
    raise CatalogError(f"unknown action '{action}'")
