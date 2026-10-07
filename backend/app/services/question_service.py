"""Question bank persistence: CRUD, immutable versioning, taxonomy links, bulk work.

Two rules shape everything below.

* History is append-only. A content edit appends a full `QuestionVersion` snapshot
  and bumps `Question.current_version`; earlier rows are never updated or removed,
  because attempts (Phase 7) reference them and must keep scoring as they did.
* Deletion is soft. Trash sets `deleted_at` only, so a question keeps the lifecycle
  status it had and restoring it is exact. Nothing here hard-deletes a question;
  permanent removal is a retention job, not an API.

Everything type-specific is validated by `question_engine` before it reaches a row.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums, security
from app.models.content import (
    Listening,
    MediaAsset,
    Question,
    QuestionTag,
    QuestionTopic,
    QuestionVersion,
    Reading,
    Tag,
    Topic,
)
from app.schemas import question as q_schemas
from app.services import audit_service, media_service, passage_service, question_engine

# Changing any of these is a content change and produces a new version. Lifecycle
# (`status`) and classification (topics/tags) are handled by their own endpoints and
# are deliberately not content edits.
CONTENT_FIELDS = {
    "type",
    "prompt",
    "config",
    "score",
    "partial_scoring",
    "negative_scoring",
    "context_kind",
    "reading_id",
    "listening_id",
    "level",
    "difficulty",
    "learning_language",
    "explanation",
    "teacher_notes",
    "media_asset_id",
}

SORTABLE = {
    "created_at": Question.created_at,
    "updated_at": Question.updated_at,
    "type": Question.type,
    "level": Question.level,
    "difficulty": Question.difficulty,
    "prompt": Question.prompt,
    "status": Question.status,
}


class QuestionInputError(ValueError):
    """A teacher-facing validation problem; the endpoints turn it into a 422."""


def _label_of(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def status_enum(raw: str) -> enums.ContentStatus:
    """The lifecycle vocabulary the teacher may set.

    `TRASH` is excluded on purpose: being in the trash is recorded by `deleted_at`,
    so treating it as a status would create two sources of truth for one state.
    """
    try:
        value = enums.ContentStatus(raw)
    except ValueError:
        allowed = ", ".join(sorted(s.value for s in enums.ContentStatus if s != enums.ContentStatus.TRASH))
        raise QuestionInputError(f"status must be one of: {allowed}") from None
    if value == enums.ContentStatus.TRASH:
        raise QuestionInputError("trash is a deletion, not a status - use the trash endpoint")
    return value


def context_enum(raw: str) -> enums.QuestionContext:
    try:
        return enums.QuestionContext(raw)
    except ValueError:
        allowed = ", ".join(sorted(c.value for c in enums.QuestionContext))
        raise QuestionInputError(f"context_kind must be one of: {allowed}") from None


def validate_payload(question_type: str, config: Any, score: float, partial: dict, negative: dict) -> dict:
    """Config + scoring knobs are checked together, so a type and its knobs cannot disagree."""
    try:
        validated = question_engine.validate_config(question_type, config)
        question_engine.validate_scoring(question_type, score, partial, negative)
    except ValueError as exc:
        raise QuestionInputError(str(exc)) from None
    return validated


# --------------------------------------------------------------------------- #
# Referential checks
# --------------------------------------------------------------------------- #


async def _require_exists(db: AsyncSession, model: type, value: uuid.UUID | None, label: str) -> None:
    if value is None:
        return
    if await db.get(model, value) is None:
        raise QuestionInputError(f"{label} '{value}' does not exist")


#: Which passage type a dependent question binds to, for each context word a teacher picks.
_CONTEXT_PASSAGES = {
    enums.QuestionContext.READING: passage_service.READING,
    enums.QuestionContext.LISTENING: passage_service.LISTENING,
}


async def _check_context(
    db: AsyncSession,
    *,
    context_kind: enums.QuestionContext,
    reading_id: uuid.UUID | None,
    listening_id: uuid.UUID | None,
    question_id: uuid.UUID | None = None,
) -> None:
    """A dependent question must name its context, and only its context.

    This is what keeps a reading question from silently degrading into a loose question
    the moment its passage is replaced.

    `question_id` turns on the second half of the same rule: a question already filed
    under a set of some passage cannot be re-pointed at a different text from here. The
    binding and the filing are two rows that have to agree, and re-binding from the
    question screen would leave that set holding a question about a passage it is no
    longer attached to. Moving a filed question between passages means taking it out of
    the set first, on the passage screen, where the teacher can see both sides of it.
    """
    try:
        if context_kind == enums.QuestionContext.INDEPENDENT:
            if reading_id or listening_id:
                raise QuestionInputError("an independent question cannot carry a reading or listening id")
            await _refuse_rebind(db, question_id, kind=None, passage_id=None)
            return
        kind = _CONTEXT_PASSAGES[context_kind]
        if context_kind == enums.QuestionContext.READING:
            if listening_id:
                raise QuestionInputError("a reading-bound question must not also carry a listening_id")
            if not reading_id:
                raise QuestionInputError("a reading-bound question needs a reading_id")
            passage_id = reading_id
        else:
            if reading_id:
                raise QuestionInputError("a listening-bound question must not also carry a reading_id")
            if not listening_id:
                raise QuestionInputError("a listening-bound question needs a listening_id")
            passage_id = listening_id
        await passage_service.require_bindable_passage(db, kind, passage_id)
        await _refuse_rebind(db, question_id, kind=kind, passage_id=passage_id)
    except passage_service.PassageError as exc:
        raise QuestionInputError(str(exc)) from None


async def _refuse_rebind(
    db: AsyncSession,
    question_id: uuid.UUID | None,
    *,
    kind: passage_service.PassageKind | None,
    passage_id: uuid.UUID | None,
) -> None:
    """Stop a filed question from being moved to another passage behind its set's back."""
    if question_id is None:
        return
    filed = await passage_service.filed_under(db, question_id)
    if filed is None:
        return
    filed_kind, _filed_passage, filed_title = filed
    if kind is not None and filed_kind is kind and _filed_passage == passage_id:
        return
    where = f'"{filed_title}" of its {filed_kind.label}' if filed_title else f"an {filed_kind.label}"
    raise QuestionInputError(
        f"this question is filed under {where} - remove it from that set before re-binding it"
    )


async def _check_media(db: AsyncSession, asset_id: uuid.UUID | None) -> None:
    """A question's picture must be a live image, not merely a row with a foreign key.

    `served_to_student` will not open a trashed asset, so a question pointing at one is a
    blank box on a learner's screen - and a file the library keeps as something else is a
    blank box shaped like a photograph.
    """
    if asset_id is None:
        return
    try:
        await media_service.require_usable(db, asset_id, kind="image", subject="a question")
    except media_service.MediaError as exc:
        raise QuestionInputError(str(exc)) from None


# --------------------------------------------------------------------------- #
# Snapshots and versions
# --------------------------------------------------------------------------- #


async def _taxonomy_of(db: AsyncSession, question_id: uuid.UUID) -> tuple[list[dict], list[dict]]:
    topics = (
        await db.execute(
            select(Topic.id, Topic.name)
            .join(QuestionTopic, QuestionTopic.topic_id == Topic.id)
            .where(QuestionTopic.question_id == question_id)
            .order_by(Topic.name)
        )
    ).all()
    tags = (
        await db.execute(
            select(Tag.id, Tag.name)
            .join(QuestionTag, QuestionTag.tag_id == Tag.id)
            .where(QuestionTag.question_id == question_id)
            .order_by(Tag.name)
        )
    ).all()
    return (
        [{"id": str(row[0]), "name": row[1]} for row in topics],
        [{"id": str(row[0]), "name": row[1]} for row in tags],
    )


async def build_snapshot(db: AsyncSession, question: Question) -> dict:
    """The complete frozen question, including the classification it was used with."""
    topics, tags = await _taxonomy_of(db, question.id)
    return {
        "snapshot_schema": 1,
        "question": {
            "id": str(question.id),
            "type": question.type,
            "prompt": question.prompt,
            "status": _label_of(question.status),
            "context_kind": _label_of(question.context_kind),
            "reading_id": str(question.reading_id) if question.reading_id else None,
            "listening_id": str(question.listening_id) if question.listening_id else None,
            "score": question.score,
            "partial_scoring": question.partial_scoring,
            "negative_scoring": question.negative_scoring,
            "level": question.level,
            "difficulty": question.difficulty,
            "learning_language": question.learning_language,
            "explanation": question.explanation,
            "teacher_notes": question.teacher_notes,
            "media_asset_id": str(question.media_asset_id) if question.media_asset_id else None,
            "config": question.config,
        },
        "topics": topics,
        "tags": tags,
    }


async def _append_version(db: AsyncSession, question: Question, *, change_note: str | None) -> QuestionVersion:
    """Append-only. There is deliberately no update or delete path for a version row."""
    row = QuestionVersion(
        question_id=question.id,
        version=question.current_version,
        snapshot=await build_snapshot(db, question),
        change_note=change_note,
    )
    db.add(row)
    await db.flush()
    return row


# --------------------------------------------------------------------------- #
# Read payloads
# --------------------------------------------------------------------------- #


async def to_read(db: AsyncSession, question: Question) -> dict:
    topics, tags = await _taxonomy_of(db, question.id)
    filed = await passage_service.filed_under(db, question.id)
    return q_schemas.QuestionRead(
        id=question.id,
        type=question.type,
        prompt=question.prompt,
        status=_label_of(question.status),
        context_kind=_label_of(question.context_kind),
        reading_id=question.reading_id,
        listening_id=question.listening_id,
        score=question.score,
        partial_scoring=question.partial_scoring,
        negative_scoring=question.negative_scoring,
        level=question.level,
        difficulty=question.difficulty,
        learning_language=question.learning_language,
        explanation=question.explanation,
        teacher_notes=question.teacher_notes,
        media_asset_id=question.media_asset_id,
        media_url=(
            constants.media_content_url(question.media_asset_id, for_student=False)
            if question.media_asset_id
            else None
        ),
        filed_in=(
            q_schemas.QuestionFiling(
                kind=filed[0].name, passage_id=filed[1], set_title=filed[2]
            )
            if filed
            else None
        ),
        config=question.config,
        current_version=question.current_version,
        topics=[q_schemas.TaxonomyRef.model_validate(t) for t in topics],
        tags=[q_schemas.TaxonomyRef.model_validate(t) for t in tags],
        source_file_id=question.source_file_id,
        source_page=question.source_page,
        extraction_method=question.extraction_method,
        created_at=question.created_at,
        updated_at=question.updated_at,
        deleted_at=question.deleted_at,
    ).model_dump(mode="json")


def _to_summary(question: Question, topic_names: list[str], tag_names: list[str]) -> dict:
    """Rows go through the schema, so a list item can never silently diverge from
    what the detail endpoint returns."""
    return q_schemas.QuestionSummary(
        id=question.id,
        type=question.type,
        prompt=question.prompt,
        status=_label_of(question.status),
        context_kind=_label_of(question.context_kind),
        level=question.level,
        difficulty=question.difficulty,
        learning_language=question.learning_language,
        score=question.score,
        current_version=question.current_version,
        topic_names=topic_names,
        tag_names=tag_names,
        has_media=question.media_asset_id is not None,
        created_at=question.created_at,
        updated_at=question.updated_at,
        deleted_at=question.deleted_at,
    ).model_dump(mode="json")


async def get(db: AsyncSession, question_id: uuid.UUID, *, allow_trash: bool = True) -> Question | None:
    question = await db.get(Question, question_id)
    if question is None:
        return None
    if question.deleted_at is not None and not allow_trash:
        return None
    return question


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


async def list_questions(
    db: AsyncSession,
    *,
    q: str | None = None,
    question_type: str | None = None,
    status: str | None = None,
    level: str | None = None,
    learning_language: str | None = None,
    context_kind: str | None = None,
    topic_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    reading_id: uuid.UUID | None = None,
    listening_id: uuid.UUID | None = None,
    source_file_id: uuid.UUID | None = None,
    has_media: bool | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """`view=bank` hides the trash, `view=trash` shows only it, `view=all` shows both."""
    if view not in ("bank", "trash", "all"):
        raise QuestionInputError("view must be 'bank', 'trash' or 'all'")
    if sort not in SORTABLE:
        raise QuestionInputError("sort must be one of: " + ", ".join(sorted(SORTABLE)))
    if order not in ("asc", "desc"):
        raise QuestionInputError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = min(max(1, page_size), 200)

    stmt = select(Question)
    if view == "bank":
        stmt = stmt.where(Question.deleted_at.is_(None))
    elif view == "trash":
        stmt = stmt.where(Question.deleted_at.is_not(None))

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Question.prompt.ilike(like),
                Question.explanation.ilike(like),
                Question.teacher_notes.ilike(like),
            )
        )
    if question_type:
        # `type` is a free column by design; the registry still decides what is valid.
        if not question_engine.is_known(question_type):
            raise QuestionInputError(f"unknown question type '{question_type}'")
        stmt = stmt.where(Question.type == question_type)
    if status:
        stmt = stmt.where(Question.status == status_enum(status).name)
    if context_kind:
        stmt = stmt.where(Question.context_kind == context_enum(context_kind).name)
    if level:
        stmt = stmt.where(Question.level == level)
    if learning_language:
        stmt = stmt.where(Question.learning_language == learning_language)
    if reading_id:
        stmt = stmt.where(Question.reading_id == reading_id)
    if listening_id:
        stmt = stmt.where(Question.listening_id == listening_id)
    if source_file_id:
        stmt = stmt.where(Question.source_file_id == source_file_id)
    if has_media is not None:
        stmt = stmt.where(
            Question.media_asset_id.is_not(None) if has_media else Question.media_asset_id.is_(None)
        )
    if topic_id:
        stmt = stmt.where(
            Question.id.in_(select(QuestionTopic.question_id).where(QuestionTopic.topic_id == topic_id))
        )
    if tag_id:
        stmt = stmt.where(
            Question.id.in_(select(QuestionTag.question_id).where(QuestionTag.tag_id == tag_id))
        )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    column = SORTABLE[sort]
    direction = column.desc() if order == "desc" else column.asc()
    rows = (
        (
            await db.execute(
                # A stable tie-break, so page 2 never repeats or skips a row.
                stmt.order_by(direction.nulls_last(), Question.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    names = await _bulk_taxonomy_names(db, [row.id for row in rows])
    return {
        "items": [_to_summary(row, *names.get(row.id, ([], []))) for row in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


async def _bulk_taxonomy_names(
    db: AsyncSession, question_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[list[str], list[str]]]:
    if not question_ids:
        return {}
    topic_rows = (
        await db.execute(
            select(QuestionTopic.question_id, Topic.name)
            .join(Topic, Topic.id == QuestionTopic.topic_id)
            .where(QuestionTopic.question_id.in_(question_ids))
            .order_by(Topic.name)
        )
    ).all()
    tag_rows = (
        await db.execute(
            select(QuestionTag.question_id, Tag.name)
            .join(Tag, Tag.id == QuestionTag.tag_id)
            .where(QuestionTag.question_id.in_(question_ids))
            .order_by(Tag.name)
        )
    ).all()
    out: dict[uuid.UUID, tuple[list[str], list[str]]] = {qid: ([], []) for qid in question_ids}
    for qid, name in topic_rows:
        out[qid][0].append(name)
    for qid, name in tag_rows:
        out[qid][1].append(name)
    return out


# --------------------------------------------------------------------------- #
# Taxonomy links
# --------------------------------------------------------------------------- #


async def _link_ids(db: AsyncSession, question_id: uuid.UUID) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    topics = (
        await db.execute(select(QuestionTopic.topic_id).where(QuestionTopic.question_id == question_id))
    ).scalars().all()
    tags = (await db.execute(select(QuestionTag.tag_id).where(QuestionTag.question_id == question_id))).scalars().all()
    return list(topics), list(tags)


async def _replace_links(
    db: AsyncSession, question_id: uuid.UUID, *, topic_ids: list[uuid.UUID], tag_ids: list[uuid.UUID]
) -> None:
    await db.execute(QuestionTopic.__table__.delete().where(QuestionTopic.question_id == question_id))
    await db.execute(QuestionTag.__table__.delete().where(QuestionTag.question_id == question_id))
    for topic_id in dict.fromkeys(topic_ids):
        db.add(QuestionTopic(question_id=question_id, topic_id=topic_id))
    for tag_id in dict.fromkeys(tag_ids):
        db.add(QuestionTag(question_id=question_id, tag_id=tag_id))
    await db.flush()


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


async def create_question(
    db: AsyncSession, payload: q_schemas.QuestionCreate, *, admin_id: uuid.UUID
) -> dict:
    config = validate_payload(payload.type, payload.config, payload.score, payload.partial_scoring, payload.negative_scoring)
    context = context_enum(payload.context_kind)
    await _check_context(
        db, context_kind=context, reading_id=payload.reading_id, listening_id=payload.listening_id
    )
    await _check_media(db, payload.media_asset_id)
    for topic_id in payload.topic_ids:
        await _require_exists(db, Topic, topic_id, "topic")
    for tag_id in payload.tag_ids:
        await _require_exists(db, Tag, tag_id, "tag")

    question = Question(
        type=payload.type,
        prompt=payload.prompt,
        status=status_enum(payload.status),
        context_kind=context,
        reading_id=payload.reading_id,
        listening_id=payload.listening_id,
        score=payload.score,
        partial_scoring=payload.partial_scoring,
        negative_scoring=payload.negative_scoring,
        level=payload.level,
        difficulty=payload.difficulty,
        learning_language=payload.learning_language,
        explanation=payload.explanation,
        teacher_notes=payload.teacher_notes,
        media_asset_id=payload.media_asset_id,
        config=config,
        current_version=1,
    )
    db.add(question)
    await db.flush()

    await _replace_links(db, question.id, topic_ids=payload.topic_ids, tag_ids=payload.tag_ids)
    await _append_version(db, question, change_note=payload.change_note or "Created")
    await audit_service.record_audit(
        db,
        action="question.created",
        actor_id=admin_id,
        target_type="question",
        target_id=question.id,
        after={"type": question.type, "status": _label_of(question.status)},
    )
    return await to_read(db, question)


async def update_question(
    db: AsyncSession, question: Question, payload: q_schemas.QuestionUpdate, *, admin_id: uuid.UUID
) -> dict:
    """Apply a patch. A content change appends a version; classification does not."""
    changes = payload.model_dump(exclude_unset=True)
    content_patch = {k: v for k, v in changes.items() if k in CONTENT_FIELDS}

    if "type" in content_patch and "config" not in content_patch:
        raise QuestionInputError("changing the type must come with that type's config")

    merged_type = content_patch.get("type", question.type)
    merged_config = content_patch.get("config", question.config)
    merged_score = content_patch.get("score", question.score)
    merged_partial = content_patch.get("partial_scoring", question.partial_scoring)
    merged_negative = content_patch.get("negative_scoring", question.negative_scoring)
    config = validate_payload(merged_type, merged_config, merged_score, merged_partial, merged_negative)

    merged_context = context_enum(content_patch.get("context_kind", _label_of(question.context_kind)))
    merged_reading = content_patch.get("reading_id", question.reading_id)
    merged_listening = content_patch.get("listening_id", question.listening_id)
    await _check_context(
        db,
        context_kind=merged_context,
        reading_id=merged_reading,
        listening_id=merged_listening,
        question_id=question.id,
    )
    await _check_media(db, content_patch.get("media_asset_id", question.media_asset_id))
    if changes.get("topic_ids") is not None:
        for topic_id in changes["topic_ids"]:
            await _require_exists(db, Topic, topic_id, "topic")
    if changes.get("tag_ids") is not None:
        for tag_id in changes["tag_ids"]:
            await _require_exists(db, Tag, tag_id, "tag")

    # Every field the patch touches is captured on its own terms, so a patch that
    # re-sends the values a question already has is recognised as the no-op it is.
    before = {field: getattr(question, field) for field in content_patch}
    version_before = question.current_version

    for field, value in content_patch.items():
        if field == "config":
            question.config = config
        elif field == "context_kind":
            question.context_kind = merged_context
        else:
            setattr(question, field, value)

    await db.flush()

    # An edit that changes nothing must not create a version - otherwise the history
    # fills with identical snapshots and stops meaning anything. The comparison is
    # made against the values captured before the patch was applied.
    content_changed = any(
        _plain(before[field]) != _plain(getattr(question, field)) for field in content_patch
    )
    if content_changed:
        question.current_version += 1
        await _append_version(db, question, change_note=changes.get("change_note"))

    if changes.get("topic_ids") is not None or changes.get("tag_ids") is not None:
        current_topics, current_tags = await _link_ids(db, question.id)
        await _replace_links(
            db,
            question.id,
            topic_ids=current_topics if changes.get("topic_ids") is None else changes["topic_ids"],
            tag_ids=current_tags if changes.get("tag_ids") is None else changes["tag_ids"],
        )

    await audit_service.record_audit(
        db,
        action="question.updated",
        actor_id=admin_id,
        target_type="question",
        target_id=question.id,
        before={
            "current_version": version_before,
            **{field: _plain(value) for field, value in before.items()},
        },
        after={"fields": sorted(changes), "versioned": content_changed, "current_version": question.current_version},
    )
    return await to_read(db, question)


def _plain(value: Any) -> Any:
    """Compare values the way they are stored, so an enum column and its old value
    are judged on the same terms."""
    if isinstance(value, (enums.ContentStatus, enums.QuestionContext)):
        return value.value
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


async def set_status(db: AsyncSession, question: Question, raw_status: str, *, admin_id: uuid.UUID) -> dict:
    if question.deleted_at is not None:
        raise QuestionInputError("restore the question from the trash before changing its status")
    target = status_enum(raw_status)
    before = _label_of(question.status)
    question.status = target
    await db.flush()
    await audit_service.record_audit(
        db,
        action="question.status.changed",
        actor_id=admin_id,
        target_type="question",
        target_id=question.id,
        before={"status": before},
        after={"status": target.value},
    )
    return await to_read(db, question)


async def trash(db: AsyncSession, question: Question, *, admin_id: uuid.UUID) -> dict:
    """Soft delete: `deleted_at` only, so the status survives and restore is exact."""
    if question.deleted_at is not None:
        return await to_read(db, question)
    question.deleted_at = security.utcnow()
    await db.flush()
    await audit_service.record_audit(
        db,
        action="question.trashed",
        actor_id=admin_id,
        target_type="question",
        target_id=question.id,
        after={"status": _label_of(question.status), "deleted_at": question.deleted_at.isoformat()},
    )
    return await to_read(db, question)


async def restore(db: AsyncSession, question: Question, *, admin_id: uuid.UUID) -> dict:
    if question.deleted_at is None:
        return await to_read(db, question)
    was_deleted_at = question.deleted_at
    question.deleted_at = None
    await db.flush()
    await audit_service.record_audit(
        db,
        action="question.restored",
        actor_id=admin_id,
        target_type="question",
        target_id=question.id,
        before={"deleted_at": was_deleted_at.isoformat()},
        after={"status": _label_of(question.status)},
    )
    return await to_read(db, question)


async def clone_question(
    db: AsyncSession, question: Question, *, admin_id: uuid.UUID, status: str = "draft"
) -> dict:
    """A new, independent question with the same content, starting at version 1."""
    topics, tags = await _link_ids(db, question.id)
    clone = Question(
        type=question.type,
        prompt=question.prompt,
        status=status_enum(status),
        context_kind=question.context_kind,
        reading_id=question.reading_id,
        listening_id=question.listening_id,
        score=question.score,
        partial_scoring=question.partial_scoring,
        negative_scoring=question.negative_scoring,
        level=question.level,
        difficulty=question.difficulty,
        learning_language=question.learning_language,
        explanation=question.explanation,
        teacher_notes=question.teacher_notes,
        media_asset_id=question.media_asset_id,
        config=dict(question.config or {}),
        current_version=1,
    )
    db.add(clone)
    await db.flush()
    await _replace_links(db, clone.id, topic_ids=topics, tag_ids=tags)
    await _append_version(db, clone, change_note=f"Cloned from question {question.id}")
    await audit_service.record_audit(
        db,
        action="question.cloned",
        actor_id=admin_id,
        target_type="question",
        target_id=clone.id,
        detail=str(question.id),
    )
    payload = await to_read(db, clone)
    payload["cloned_from"] = str(question.id)
    return payload


async def assign_taxonomy(
    db: AsyncSession, question: Question, payload: q_schemas.TaxonomyAssignment, *, admin_id: uuid.UUID
) -> dict:
    current_topics, current_tags = await _link_ids(db, question.id)
    topic_ids = current_topics if payload.topic_ids is None else payload.topic_ids
    tag_ids = current_tags if payload.tag_ids is None else payload.tag_ids
    for topic_id in topic_ids:
        await _require_exists(db, Topic, topic_id, "topic")
    for tag_id in tag_ids:
        await _require_exists(db, Tag, tag_id, "tag")
    await _replace_links(db, question.id, topic_ids=topic_ids, tag_ids=tag_ids)
    await audit_service.record_audit(
        db,
        action="question.taxonomy.assigned",
        actor_id=admin_id,
        target_type="question",
        target_id=question.id,
        after={"topics": len(topic_ids), "tags": len(tag_ids)},
    )
    return await to_read(db, question)


# --------------------------------------------------------------------------- #
# Version history
# --------------------------------------------------------------------------- #


def _version_read(row: QuestionVersion) -> dict:
    return q_schemas.QuestionVersionRead(
        id=row.id,
        question_id=row.question_id,
        version=row.version,
        change_note=row.change_note,
        created_at=row.created_at,
        snapshot=row.snapshot,
    ).model_dump(mode="json")


async def list_versions(db: AsyncSession, question_id: uuid.UUID) -> list[dict]:
    rows = (
        await db.execute(
            select(QuestionVersion)
            .where(QuestionVersion.question_id == question_id)
            .order_by(QuestionVersion.version.desc())
        )
    ).scalars().all()
    return [_version_read(row) for row in rows]


async def get_version(db: AsyncSession, question_id: uuid.UUID, version: int) -> dict | None:
    row = (
        await db.execute(
            select(QuestionVersion).where(
                QuestionVersion.question_id == question_id, QuestionVersion.version == version
            )
        )
    ).scalar_one_or_none()
    return _version_read(row) if row else None


# --------------------------------------------------------------------------- #
# Student view and grading
# --------------------------------------------------------------------------- #


async def student_view(
    db: AsyncSession,
    question: Question,
    *,
    include_context: bool = True,
    served_to_admin: bool = False,
) -> dict:
    """What a learner sees: the prompt and a type payload with the answer key removed.

    `include_context` is off when the caller has already delivered the passage - a
    reading's own screen renders the text once above the questions, and repeating a
    600-word document inside every one of its twelve items is the same page twelve times.
    The questions endpoint, which serves one question on its own, leaves it on: a learner
    who is shown a reading question without its text is being asked about a passage they
    cannot see.
    """
    desc = question_engine.descriptor(question.type)
    payload: dict[str, Any] = {
        "id": str(question.id),
        "type": question.type,
        "prompt": question.prompt,
        "score": question.score,
        "answer_widget": desc.answer_widget,
        "requires_manual_grading": not desc.gradable_automatically,
        "config": question_engine.public_config(question.type, question.config),
        "explanation_available": bool(question.explanation),
    }
    if question.media_asset_id:
        # The learner is given the student path, not the id alone: which file this is, and
        # whether they may have it, is decided per request by their own session.
        asset = await db.get(MediaAsset, question.media_asset_id)
        if asset is not None and asset.deleted_at is None:
            payload["media"] = await media_service.learner_view(asset, served_to_admin=served_to_admin)
    if include_context:
        payload["context"] = await _learner_context(db, question, served_to_admin=served_to_admin)
    return payload


async def _learner_context(db: AsyncSession, question: Question, *, served_to_admin: bool = False) -> dict:
    context: dict[str, Any] = {"kind": _label_of(question.context_kind)}
    if question.reading_id:
        reading = await db.get(Reading, question.reading_id)
        if reading is not None:
            context["reading"] = {
                "id": str(reading.id),
                "title": reading.title,
                "body": reading.body,
                "layout": reading.layout,
                "language": reading.language,
                "level": reading.level,
            }
    if question.listening_id:
        listening = await db.get(Listening, question.listening_id)
        if listening is not None:
            audio = None
            if listening.media_asset_id:
                asset = await db.get(MediaAsset, listening.media_asset_id)
                if asset is not None and asset.deleted_at is None:
                    audio = await media_service.learner_view(asset, served_to_admin=served_to_admin)
            context["listening"] = {
                "id": str(listening.id),
                "title": listening.title,
                "audio": audio,
                "replay_limit": listening.replay_limit,
                "allow_pause": listening.allow_pause,
                "allow_seek": listening.allow_seek,
                # Whether the transcript may be opened is the teacher's decision, and
                # the transcript itself is only served when they allowed it.
                "transcript": listening.transcript if listening.show_transcript else None,
                "show_transcript": listening.show_transcript,
            }
    return context


async def grade_response(
    db: AsyncSession, question: Question, response: Any, *, at_version: int | None = None
) -> dict:
    """Grade one candidate answer against the live question or a frozen version."""
    if at_version is None:
        result = question_engine.grade(
            question.type,
            question.config,
            response,
            score=question.score,
            partial_scoring=question.partial_scoring,
            negative_scoring=question.negative_scoring,
        )
        return {**result.as_dict(), "version": question.current_version}

    frozen = await get_version(db, question.id, at_version)
    result = question_engine.grade_snapshot(frozen["snapshot"], response)
    return {**result.as_dict(), "version": at_version}


# --------------------------------------------------------------------------- #
# Bulk work
# --------------------------------------------------------------------------- #


async def bulk(db: AsyncSession, payload: q_schemas.BulkRequest, *, admin_id: uuid.UUID) -> dict:
    """One action over many questions, answered per id - never a silent partial win."""
    found = (await db.execute(select(Question).where(Question.id.in_(payload.question_ids)))).scalars().all()
    by_id = {row.id: row for row in found}
    missing = [str(value) for value in payload.question_ids if value not in by_id]

    target_status = None
    if payload.action == "status":
        if not payload.status:
            raise QuestionInputError("the status action needs a status")
        target_status = status_enum(payload.status)
    if payload.action in ("add_topic", "remove_topic"):
        if payload.topic_id is None:
            raise QuestionInputError("the topic actions need a topic_id")
        await _require_exists(db, Topic, payload.topic_id, "topic_id")
    if payload.action in ("add_tag", "remove_tag"):
        if payload.tag_id is None:
            raise QuestionInputError("the tag actions need a tag_id")
        await _require_exists(db, Tag, payload.tag_id, "tag_id")
    if payload.action == "set_level" and payload.level is None:
        raise QuestionInputError("set_level needs a level")
    if payload.action == "set_language" and payload.learning_language is None:
        raise QuestionInputError("set_language needs learning_language")

    updated: list[str] = []
    refused: list[dict] = []
    for requested in payload.question_ids:
        question = by_id.get(requested)
        if question is None:
            continue
        try:
            _apply_bulk(db, question, payload, target_status)
        except QuestionInputError as exc:
            refused.append({"id": str(question.id), "reason": str(exc)})
            continue
        if payload.action in ("add_topic", "remove_topic", "add_tag", "remove_tag"):
            await _mutate_link(db, question, payload)
        updated.append(str(question.id))

    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"question.bulk.{payload.action}",
        actor_id=admin_id,
        target_type="question",
        after={
            "requested": len(payload.question_ids),
            "updated": len(updated),
            "refused": len(refused),
            "not_found": len(missing),
        },
    )
    return {"action": payload.action, "updated": updated, "refused": refused, "not_found": missing}


def _apply_bulk(db: AsyncSession, question: Question, payload: q_schemas.BulkRequest, target_status) -> None:
    action = payload.action
    if action == "status":
        if question.deleted_at is not None:
            raise QuestionInputError("question is in the trash")
        question.status = target_status
    elif action == "trash":
        question.deleted_at = security.utcnow()
    elif action == "restore":
        question.deleted_at = None
    elif action == "set_level":
        question.level = payload.level
    elif action == "set_language":
        question.learning_language = payload.learning_language


async def _mutate_link(db: AsyncSession, question: Question, payload: q_schemas.BulkRequest) -> None:
    if payload.action in ("add_topic", "remove_topic"):
        existing = (
            await db.execute(
                select(QuestionTopic).where(
                    and_(QuestionTopic.question_id == question.id, QuestionTopic.topic_id == payload.topic_id)
                )
            )
        ).scalar_one_or_none()
        if payload.action == "add_topic":
            if existing is None:
                db.add(QuestionTopic(question_id=question.id, topic_id=payload.topic_id))
        elif existing is not None:
            await db.delete(existing)
        return

    existing = (
        await db.execute(
            select(QuestionTag).where(
                and_(QuestionTag.question_id == question.id, QuestionTag.tag_id == payload.tag_id)
            )
        )
    ).scalar_one_or_none()
    if payload.action == "add_tag":
        if existing is None:
            db.add(QuestionTag(question_id=question.id, tag_id=payload.tag_id))
    elif existing is not None:
        await db.delete(existing)
