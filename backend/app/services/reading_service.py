"""Reading passages (Phase 5): the text side of a dependent question.

`passage_service` owns the mechanics both passage types share - the sets filed under a
text, the membership rules, the lifecycle words, the list gates. What is reading-only
lives here:

* **`word_count` is derived, never accepted.** The text on the page and the number beside
  it are produced in the same transaction from the same characters, so a teacher sizing a
  reading for a 45-minute lesson is looking at the truth.
* **The layout is stored, not inferred.** `above`, `split` and `tabbed` decide where the
  text sits relative to the questions on the learner's screen, and on a phone that is a
  different exercise.
* **A learner's payload is the text plus answerable questions.** The questions come from
  `question_service.student_view` with the passage body left out of each item: repeating
  a 600-word text above every one of its twelve questions would be the same paragraph
  twelve times.

Trash is soft and questions stay behind: trashing a text does not trash the twenty
exercises bound to it.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums
from app.models.content import Question, Reading
from app.schemas import passage as p_schemas
from app.schemas import reading as r_schemas
from app.services import audit_service, passage_service, question_service
from app.services.passage_service import READING, PassageError

KIND = READING

SORTABLE = {
    "created_at": Reading.created_at,
    "updated_at": Reading.updated_at,
    "title": Reading.title,
    "level": Reading.level,
    "status": Reading.status,
    "word_count": Reading.word_count,
}

#: How much of the text a list row shows: enough to recognise a passage at a glance,
#: without shipping every document on the page in full.
EXCERPT_CHARACTERS = 240

_LEARNER_STATUS = enums.ContentStatus.READY


def _label(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def count_words(body: str) -> int:
    """The teacher's own number: whitespace-separated runs of characters in the text."""
    return len(body.split())


def excerpt(body: str) -> str:
    """The opening of the text, with the line breaks flattened for a table row."""
    flat = " ".join(body.split())
    if len(flat) <= EXCERPT_CHARACTERS:
        return flat
    return flat[:EXCERPT_CHARACTERS].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Rows and payloads
# --------------------------------------------------------------------------- #


async def get(db: AsyncSession, reading_id: uuid.UUID, *, allow_trash: bool = True) -> Reading | None:
    row = await db.get(Reading, reading_id)
    if row is None or (row.deleted_at is not None and not allow_trash):
        return None
    return row


def _summary(row: Reading, set_count: int, question_count: int) -> dict:
    return r_schemas.ReadingSummary(
        id=row.id,
        title=row.title,
        excerpt=excerpt(row.body),
        language=row.language,
        level=row.level,
        layout=row.layout,
        status=_label(row.status),
        word_count=row.word_count,
        set_count=set_count,
        question_count=question_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    ).model_dump(mode="json")


def _learner_summary(row: Reading, question_count: int) -> dict:
    return r_schemas.ReadingLearnerSummary(
        id=row.id,
        title=row.title,
        excerpt=excerpt(row.body),
        language=row.language,
        level=row.level,
        layout=row.layout,
        word_count=row.word_count,
        question_count=question_count,
    ).model_dump(mode="json")


async def to_read(db: AsyncSession, row: Reading) -> dict:
    """The editor payload: the text, its sets, and the questions not filed under one."""
    sets = await passage_service.list_sets(db, KIND, row.id)
    unfiled = await passage_service.unfiled_questions(db, KIND, row.id)
    set_counts, question_counts = await passage_service.passage_counts(db, KIND, [row.id])
    return r_schemas.ReadingRead(
        id=row.id,
        title=row.title,
        body=row.body,
        language=row.language,
        level=row.level,
        layout=row.layout,
        status=_label(row.status),
        word_count=row.word_count,
        source_file_id=row.source_file_id,
        set_count=set_counts.get(row.id, 0),
        question_count=question_counts.get(row.id, 0),
        sets=sets,
        unfiled=unfiled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    ).model_dump(mode="json")


async def meta(db: AsyncSession) -> dict:
    """Everything the reading screen builds its pickers from, taken from the code that
    enforces it - never a list the browser keeps its own copy of."""
    options = passage_service.lifecycle_options(SORTABLE)
    return {
        "learning_languages": await passage_service.enabled_languages(db),
        "levels": constants.CEFR_LEVELS,
        "layouts": options["layouts"],
        "statuses": options["statuses"],
        "views": options["views"],
        "sortable": options["sortable"],
        "max_body_characters": p_schemas.MAX_BODY_CHARACTERS,
        "max_sets": p_schemas.MAX_SETS_PER_PASSAGE,
        "excerpt_characters": EXCERPT_CHARACTERS,
    }


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


async def create(db: AsyncSession, payload: r_schemas.ReadingCreate, *, admin_id: uuid.UUID) -> dict:
    await passage_service.check_language(db, payload.language)
    row = Reading(
        title=payload.title,
        body=payload.body,
        language=payload.language,
        level=payload.level,
        layout=passage_service.layout_of(payload.layout),
        status=passage_service.status_enum(payload.status),
        word_count=count_words(payload.body),
    )
    db.add(row)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="reading.created",
        actor_id=admin_id,
        target_type="reading",
        target_id=row.id,
        after={"title": row.title, "word_count": row.word_count, "status": _label(row.status)},
    )
    return await to_read(db, row)


async def update(
    db: AsyncSession, row: Reading, payload: r_schemas.ReadingUpdate, *, admin_id: uuid.UUID
) -> dict:
    """Apply a patch. The body and its word count are one change, never two.

    The audit row records how long the text became rather than the text itself: an
    editor's history of every draft of a passage would be a copy of the library, and the
    immutable per-question history is where content change is actually tracked.
    """
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise PassageError("nothing to change")

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if "title" in changes:
        title = (changes["title"] or "").strip()
        if not title:
            raise PassageError("a reading needs a title")
        before["title"], row.title = row.title, title
        after["title"] = title

    if "body" in changes:
        body = (changes["body"] or "").strip()
        if not body:
            raise PassageError("a reading needs a text")
        before["word_count"], row.body = row.word_count, body
        after["word_count"] = count_words(body)
        row.word_count = after["word_count"]

    if "language" in changes:
        await passage_service.check_language(db, changes["language"])
        before["language"], row.language = row.language, changes["language"]
        after["language"] = changes["language"]

    if "level" in changes:
        before["level"], row.level = row.level, changes["level"]
        after["level"] = changes["level"]

    if "layout" in changes:
        before["layout"], row.layout = row.layout, passage_service.layout_of(changes["layout"])
        after["layout"] = row.layout

    await db.flush()
    await audit_service.record_audit(
        db,
        action="reading.updated",
        actor_id=admin_id,
        target_type="reading",
        target_id=row.id,
        before=before,
        after=after,
    )
    return await to_read(db, row)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def set_status(db: AsyncSession, row: Reading, raw_status: str, *, admin_id: uuid.UUID) -> dict:
    await passage_service.change_status(db, KIND, row, raw_status, admin_id=admin_id)
    return await to_read(db, row)


async def trash(db: AsyncSession, row: Reading, *, admin_id: uuid.UUID) -> dict:
    await passage_service.trash_passage(db, KIND, row, admin_id=admin_id)
    return await to_read(db, row)


async def restore(db: AsyncSession, row: Reading, *, admin_id: uuid.UUID) -> dict:
    await passage_service.restore_passage(db, KIND, row, admin_id=admin_id)
    return await to_read(db, row)


async def bulk(db: AsyncSession, payload: p_schemas.PassageBulkRequest, *, admin_id: uuid.UUID) -> dict:
    """One action over a whole selection, answered per id.

    A reading needs no publish veto: a text with no questions under it is still a text a
    class can read, and Phase 6's practice mode can serve it on its own.
    """
    return await passage_service.bulk_passages(db, KIND, payload, admin_id=admin_id)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def _filters(
    stmt,
    *,
    q: str | None,
    language: str | None,
    level: str | None,
    status: str | None,
    layout: str | None,
):
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Reading.title.ilike(like), Reading.body.ilike(like)))
    if language:
        stmt = stmt.where(Reading.language == language.strip().lower())
    if level:
        stmt = stmt.where(Reading.level == level)
    if status:
        stmt = stmt.where(Reading.status == passage_service.status_enum(status))
    if layout:
        stmt = stmt.where(Reading.layout == passage_service.layout_of(layout))
    return stmt


async def list_readings(
    db: AsyncSession,
    *,
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    status: str | None = None,
    layout: str | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    passage_service.check_list_args(view=view, sort=sort, order=order, sortable=SORTABLE)
    stmt = passage_service.trash_view(KIND, select(Reading), view=view)
    stmt = _filters(stmt, q=q, language=language, level=level, status=status, layout=layout)
    rows, total = await passage_service.paginate(
        db, stmt, model=Reading, sort=sort, sortable=SORTABLE, order=order, page=page, page_size=page_size
    )
    set_counts, question_counts = await passage_service.passage_counts(db, KIND, [row.id for row in rows])
    return {
        "items": [
            _summary(row, set_counts.get(row.id, 0), question_counts.get(row.id, 0)) for row in rows
        ],
        "total": total,
        "page": max(1, page),
        "page_size": min(max(1, page_size), 200),
    }


async def list_for_learner(
    db: AsyncSession,
    *,
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Ready, non-trashed texts - and only the questions a learner may answer are counted."""
    if sort not in SORTABLE:
        raise PassageError("sort must be one of: " + ", ".join(sorted(SORTABLE)))
    if order not in ("asc", "desc"):
        raise PassageError("order must be 'asc' or 'desc'")
    stmt = _filters(
        select(Reading).where(Reading.deleted_at.is_(None), Reading.status == _LEARNER_STATUS),
        q=q,
        language=language,
        level=level,
        status=None,
        layout=None,
    )
    rows, total = await passage_service.paginate(
        db, stmt, model=Reading, sort=sort, sortable=SORTABLE, order=order, page=page, page_size=page_size
    )
    _, question_counts = await passage_service.passage_counts(
        db, KIND, [row.id for row in rows], ready_only=True
    )
    return {
        "items": [_learner_summary(row, question_counts.get(row.id, 0)) for row in rows],
        "total": total,
        "page": max(1, page),
        "page_size": min(max(1, page_size), 100),
    }


# --------------------------------------------------------------------------- #
# The learner's reading
# --------------------------------------------------------------------------- #


async def _question_payloads(
    db: AsyncSession, ids: list[uuid.UUID], *, served_to_admin: bool = False
) -> dict[str, dict]:
    """Each question as an exercise, without the passage repeated inside it.

    `student_view` is the only projection of a question a learner is ever served, so a
    reading screen cannot leak an answer key that the exercise screen already refuses to
    send.
    """
    if not ids:
        return {}
    rows = (await db.execute(select(Question).where(Question.id.in_(ids)))).scalars().all()
    return {
        str(row.id): await question_service.student_view(
            db, row, include_context=False, served_to_admin=served_to_admin
        )
        for row in rows
    }


async def learner_detail(db: AsyncSession, row: Reading, *, served_to_admin: bool = False) -> dict:
    async def project(session, ids):
        return await _question_payloads(session, ids, served_to_admin=served_to_admin)

    sets, unfiled = await passage_service.learner_tree(db, KIND, row.id, project=project)
    return r_schemas.ReadingLearnerRead(
        id=row.id,
        title=row.title,
        body=row.body,
        language=row.language,
        level=row.level,
        layout=row.layout,
        sets=sets,
        unfiled=unfiled,
    ).model_dump(mode="json")
