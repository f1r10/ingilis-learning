"""The reading and listening passages, and the question sets that group under them.

Both entities are the same shape twice: a body of content (a text, a recording) with
ordered sets, each holding some of the questions bound to it. The rules below are
therefore written once and driven by a `PassageKind` that names the three tables
involved. Adding a third passage type is one descriptor, not a third copy of the
membership logic that decides whether a question may be filed here.

Three rules do the real work.

* **Grouping is not a content edit.** Filing a question under a set writes
  `reading_set_question` / `listening_set_question` and never touches `Question`, so it
  cannot append a `QuestionVersion`. A teacher reorganising a passage must not create a
  dozen versioned snapshots of twelve questions that did not change.
* **A question answers one block of one passage.** The unique index from migration `0003`
  makes `question_id` unique across the membership table, so assigning a question to a
  second set *moves* it rather than duplicating it - and the response says so,
  because a set that silently lost a question reads as a save failure somewhere else.
* **Only a question bound to this passage may be filed in it.** The binding
  (`context_kind` plus `reading_id` / `listening_id`) is what keeps a reading question
  from degrading into a loose question, so membership checks it before writing, and
  `filed_under` stops the binding from being changed while the question is filed.

Listing rules are shared as well: `view=bank` hides the trash, `view=trash` shows only
it, `view=all` shows both, and the learner surface is *ready and not trashed* for the
passage **and** for every question inside it. A draft question under a published text is
unfinished work, and a learner must never be asked to answer it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import enums, security
from app.models.content import (
    Listening,
    ListeningQuestionSet,
    ListeningSetQuestion,
    Question,
    Reading,
    ReadingQuestionSet,
    ReadingSetQuestion,
)
from app.schemas import passage as p_schemas
from app.services import audit_service, settings_service

#: The lifecycle words a teacher may set. TRASH is recorded by `deleted_at`, so accepting
#: it here would create a second source of truth for one state.
SETTABLE_STATUSES = ["draft", "ready", "archived"]

VIEWS = ("bank", "trash", "all")

LAYOUTS = ("above", "split", "tabbed")


class PassageError(ValueError):
    """A teacher-facing problem with a passage or one of its sets; the endpoints 422 it."""


class PassageNotFound(Exception):
    """The passage or set is not there, or is not the one asked for; the endpoints 404."""


@dataclass(frozen=True)
class PassageKind:
    """One passage type and the three tables that hold it."""

    name: str  # "reading" | "listening"
    label: str  # the word the teacher is sent to look for
    passage: Any
    set_model: Any
    membership: Any
    context: enums.QuestionContext
    passage_fk: str  # column on the set table
    question_fk: str  # column on `question`
    #: Listening sets can play a slice of the recording; reading sets cannot.
    has_interval: bool
    #: A set printed for the editor, and the same set with the authoring fields removed
    #: for a learner. Both are schemas rather than dicts so the two surfaces cannot drift
    #: into two different shapes without one of them refusing to build.
    set_schema: Any
    learner_set_schema: Any


READING = PassageKind(
    name="reading",
    label="reading text",
    passage=Reading,
    set_model=ReadingQuestionSet,
    membership=ReadingSetQuestion,
    context=enums.QuestionContext.READING,
    passage_fk="reading_id",
    question_fk="reading_id",
    has_interval=False,
    set_schema=p_schemas.QuestionSetRead,
    learner_set_schema=p_schemas.LearnerSetRead,
)

LISTENING = PassageKind(
    name="listening",
    label="recording",
    passage=Listening,
    set_model=ListeningQuestionSet,
    membership=ListeningSetQuestion,
    context=enums.QuestionContext.LISTENING,
    passage_fk="listening_id",
    question_fk="listening_id",
    has_interval=True,
    set_schema=p_schemas.ListeningSetRead,
    learner_set_schema=p_schemas.ListeningLearnerSetRead,
)

KINDS: dict[str, PassageKind] = {kind.name: kind for kind in (READING, LISTENING)}


def kind_of(name: str) -> PassageKind:
    try:
        return KINDS[name]
    except KeyError:
        raise PassageError(f"passage type must be one of: {', '.join(sorted(KINDS))}") from None


def _label_of(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def status_enum(raw: str) -> enums.ContentStatus:
    try:
        value = enums.ContentStatus(raw)
    except ValueError:
        raise PassageError(f"status must be one of: {', '.join(SETTABLE_STATUSES)}") from None
    if value == enums.ContentStatus.TRASH:
        raise PassageError("trash is a deletion, not a status - use the trash action")
    return value


def layout_of(raw: str | None) -> str:
    """The one layout word the reading editor knows, or the default."""
    if raw is None:
        return "above"
    if raw not in LAYOUTS:
        raise PassageError(f"layout must be one of: {', '.join(LAYOUTS)}")
    return raw


# --------------------------------------------------------------------------- #
# Passage rows
# --------------------------------------------------------------------------- #


async def get_passage(
    db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID, *, allow_trash: bool = True
) -> Any:
    row = await db.get(kind.passage, passage_id)
    if row is None or (row.deleted_at is not None and not allow_trash):
        raise PassageNotFound(f"no {kind.label} with that id")
    return row


def is_learner_visible(row: Any) -> bool:
    """Ready and not trashed - the only state a learner is ever served from."""
    return row.deleted_at is None and row.status == enums.ContentStatus.READY


async def require_bindable_passage(
    db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID
) -> Any:
    """The passage a question is about to be bound to must exist and not be in the trash.

    A draft is a work in progress and a teacher may write its questions first; a trashed
    text is one the teacher threw away, and binding a question to it would make the
    question invisible the moment it is saved.
    """
    row = await db.get(kind.passage, passage_id)
    if row is None:
        raise PassageError(f"that {kind.label} does not exist")
    if row.deleted_at is not None:
        raise PassageError(
            f"that {kind.label} is in the trash - restore it before adding questions to it"
        )
    return row


# --------------------------------------------------------------------------- #
# Language rules
# --------------------------------------------------------------------------- #


async def enabled_languages(db: AsyncSession) -> list[str]:
    """The learning languages the platform is configured for - never a client-side list."""
    ui = await settings_service.get_ui_config(db)
    return [str(code) for code in ui["learning_languages"]]


async def check_language(db: AsyncSession, code: str | None) -> None:
    """A passage is filed under one of the configured languages, or under none yet.

    The check is against configuration rather than a hardcoded `en`, because the same
    platform teaches English in Azerbaijani, Russian and Turkish classes, and a language
    an admin has not enabled cannot be filtered for.
    """
    if code is None:
        return
    options = await enabled_languages(db)
    if code not in options:
        raise PassageError(
            f"'{code}' is not an enabled learning language "
            f"(available: {', '.join(options) or 'none configured'})"
        )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def change_status(
    db: AsyncSession,
    kind: PassageKind,
    row: Any,
    raw_status: str,
    *,
    admin_id: uuid.UUID,
    refuse_publish: Any = None,
) -> None:
    """Move a passage between draft, ready and archived. Writes nothing else.

    A status is not a content edit, so it must not touch the body, the sets or the
    questions filed under the passage - publishing a text is not a way of publishing the
    unfinished questions underneath it.

    `refuse_publish` is a passage type's own veto over being published (a recording with
    nothing to play). It is asked only on the way *into* ready, and it raises rather than
    returns, because this is one teacher's deliberate click, not a selection of forty.
    """
    if row.deleted_at is not None:
        raise PassageError(f"restore the {kind.label} from the trash before changing its status")
    target = status_enum(raw_status)
    if (
        target == enums.ContentStatus.READY
        and row.status != enums.ContentStatus.READY
        and refuse_publish is not None
    ):
        await refuse_publish(db, row)
    before = _label_of(row.status)
    row.status = target
    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.status.changed",
        actor_id=admin_id,
        target_type=kind.passage.__tablename__,
        target_id=row.id,
        before={"status": before},
        after={"status": target.value},
    )


async def trash_passage(db: AsyncSession, kind: PassageKind, row: Any, *, admin_id: uuid.UUID) -> None:
    """Soft delete: `deleted_at` only, so the status survives and a restore is exact.

    The questions bound to the passage are not touched. They belong to the bank and are
    often reused; a teacher trashing one text must not throw away twenty exercises.
    """
    if row.deleted_at is not None:
        return
    row.deleted_at = security.utcnow()
    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.trashed",
        actor_id=admin_id,
        target_type=kind.passage.__tablename__,
        target_id=row.id,
        after={"title": row.title, "deleted_at": row.deleted_at.isoformat()},
    )


async def restore_passage(db: AsyncSession, kind: PassageKind, row: Any, *, admin_id: uuid.UUID) -> None:
    if row.deleted_at is None:
        return
    was_deleted_at = row.deleted_at
    row.deleted_at = None
    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.restored",
        actor_id=admin_id,
        target_type=kind.passage.__tablename__,
        target_id=row.id,
        before={"deleted_at": was_deleted_at.isoformat()},
        after={"status": _label_of(row.status)},
    )


#: The four operations a teacher may apply to a whole selection of passages.
BULK_ACTIONS = p_schemas.BULK_ACTIONS


async def bulk_passages(
    db: AsyncSession,
    kind: PassageKind,
    payload: p_schemas.PassageBulkRequest,
    *,
    admin_id: uuid.UUID,
    refuse_publish: Any = None,
) -> dict:
    """One action over many passages, answered per id - never a silent partial win.

    `refuse_publish` is a passage type's veto over its own publication (a recording with
    neither audio nor transcript has nothing to publish). It is a parameter rather than a
    branch here so this stays the one implementation of the rule that a refused row does
    not stop the other thirty-nine.
    """
    rows = (
        await db.execute(select(kind.passage).where(kind.passage.id.in_(payload.passage_ids)))
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    missing = [str(value) for value in payload.passage_ids if value not in by_id]

    target_status = None
    if payload.action == "status":
        if not payload.status:
            raise PassageError("the status action needs a status")
        target_status = status_enum(payload.status)
    if payload.action == "set_level" and payload.level is None:
        raise PassageError("set_level needs a level")

    updated: list[str] = []
    refused: list[dict] = []
    for requested in payload.passage_ids:
        row = by_id.get(requested)
        if row is None:
            continue
        reason = await _apply_bulk(
            db, kind, row, payload, target_status, refuse_publish=refuse_publish
        )
        if reason:
            refused.append({"id": str(row.id), "reason": reason})
            continue
        updated.append(str(row.id))

    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.bulk.{payload.action}",
        actor_id=admin_id,
        target_type=kind.passage.__tablename__,
        after={
            "requested": len(payload.passage_ids),
            "updated": len(updated),
            "refused": len(refused),
            "not_found": len(missing),
        },
    )
    return {"action": payload.action, "updated": updated, "refused": refused, "not_found": missing}


async def _apply_bulk(
    db: AsyncSession,
    kind: PassageKind,
    row: Any,
    payload: p_schemas.PassageBulkRequest,
    target_status: enums.ContentStatus | None,
    *,
    refuse_publish: Any,
) -> str | None:
    """Apply one action to one passage, or return the reason this passage refuses it.

    A returned reason instead of a raised error is deliberate: a selection of forty texts
    where one cannot be published is a real Wednesday, and the other thirty-nine must
    still go through.
    """
    action = payload.action
    if action == "status":
        if row.deleted_at is not None:
            return "this passage is in the trash"
        if (
            target_status == enums.ContentStatus.READY
            and row.status != enums.ContentStatus.READY
            and refuse_publish is not None
        ):
            reason = await refuse_publish(db, row)
            if reason:
                return reason
        row.status = target_status
    elif action == "trash":
        if row.deleted_at is not None:
            return "already in the trash"
        row.deleted_at = security.utcnow()
    elif action == "restore":
        if row.deleted_at is None:
            return "this passage is not in the trash"
        row.deleted_at = None
    elif action == "set_level":
        row.level = payload.level
    return None


async def filed_under(
    db: AsyncSession, question_id: uuid.UUID
) -> tuple[PassageKind, uuid.UUID, str | None] | None:
    """Which set a question currently answers, if any - and in which passage.

    `question_service` asks this before it lets a question's context be re-bound. A
    question silently moved to another text would leave its old set holding a
    question that no longer belongs to that passage, and the tree would lie.
    """
    for kind in KINDS.values():
        fk = getattr(kind.set_model, kind.passage_fk)
        row = (
            await db.execute(
                select(fk, kind.set_model.title, kind.set_model.id)
                .join(kind.membership, kind.membership.set_id == kind.set_model.id)
                .where(kind.membership.question_id == question_id)
            )
        ).first()
        if row is not None:
            return kind, row[0], row[1]
    return None


# --------------------------------------------------------------------------- #
# Counts
# --------------------------------------------------------------------------- #


async def passage_counts(
    db: AsyncSession, kind: PassageKind, passage_ids: list[uuid.UUID], *, ready_only: bool = False
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """Set count and bound-question count per passage, in two grouped queries.

    Both are read on every row of the list screen; asking per passage would turn a page
    of fifty into a hundred queries.

    `ready_only` asks for the questions a learner may actually answer. Without it the
    number on a learner's list row would count a draft the teacher has not finished, and
    the count is the thing they choose an exercise by.
    """
    fk = getattr(kind.set_model, kind.passage_fk)
    question_fk = getattr(Question, kind.question_fk)
    if not passage_ids:
        return {}, {}

    set_rows = (
        await db.execute(select(fk, func.count()).where(fk.in_(passage_ids)).group_by(fk))
    ).all()
    criteria = [question_fk.in_(passage_ids), Question.deleted_at.is_(None)]
    if ready_only:
        criteria.append(Question.status == enums.ContentStatus.READY)
    question_rows = (
        await db.execute(select(question_fk, func.count()).where(*criteria).group_by(question_fk))
    ).all()
    return (
        {row[0]: int(row[1]) for row in set_rows},
        {row[0]: int(row[1]) for row in question_rows},
    )


# --------------------------------------------------------------------------- #
# Sets and their questions
# --------------------------------------------------------------------------- #


class SetQuestionRow:
    """A question shaped from a column list rather than loaded as a row.

    The set list needs five fields per question and nothing else; pulling the ORM
    entity in would load the answer key of every question on the page into memory, and
    the learner screen must not be one `model_dump` mistake away from sending it.
    """

    __slots__ = ("id", "position", "prompt", "score", "status", "type")

    def __init__(
        self,
        *,
        id: uuid.UUID,
        position: int,
        type: str,
        prompt: str | None,
        status: Any,
        score: float,
    ) -> None:
        self.id = id
        self.position = position
        self.type = type
        self.prompt = prompt
        self.status = status
        self.score = score


def _question_stub(row: SetQuestionRow) -> dict:
    return p_schemas.SetQuestionRead(
        id=row.id,
        position=row.position,
        type=row.type,
        prompt=row.prompt,
        status=_label_of(row.status),
        score=row.score,
    ).model_dump(mode="json")


def _loose_stub(row: Question) -> dict:
    """A question bound to the passage but filed under no set yet."""
    return p_schemas.QuestionStubRead(
        id=row.id,
        type=row.type,
        prompt=row.prompt,
        status=_label_of(row.status),
        score=row.score,
    ).model_dump(mode="json")


async def _questions_by_set(
    db: AsyncSession, kind: PassageKind, set_ids: list[uuid.UUID], *, ready_only: bool
) -> dict[uuid.UUID, list[SetQuestionRow]]:
    """The questions under each set, in set order.

    A trashed question is not shown: it has been deleted from the bank's point of view,
    and its membership row waits out the trash with it, in case it is restored.
    """
    if not set_ids:
        return {}
    stmt = (
        select(
            kind.membership.set_id,
            kind.membership.position,
            Question.id,
            Question.type,
            Question.prompt,
            Question.status,
            Question.score,
        )
        .join(Question, Question.id == kind.membership.question_id)
        .where(kind.membership.set_id.in_(set_ids), Question.deleted_at.is_(None))
        .order_by(kind.membership.set_id, kind.membership.position, kind.membership.id)
    )
    if ready_only:
        stmt = stmt.where(Question.status == enums.ContentStatus.READY)
    grouped: dict[uuid.UUID, list[SetQuestionRow]] = {set_id: [] for set_id in set_ids}
    for set_id, position, qid, qtype, prompt, qstatus, qscore in (await db.execute(stmt)).all():
        grouped[set_id].append(
            SetQuestionRow(
                id=qid, position=int(position), type=qtype, prompt=prompt, status=qstatus, score=qscore
            )
        )
    return grouped


def set_read(kind: PassageKind, row: Any, questions: list[SetQuestionRow]) -> dict:
    """One set, printed the same way on every screen that shows it to a teacher."""
    built: dict[str, Any] = {
        "id": row.id,
        "title": row.title,
        "instructions": row.instructions,
        "position": row.position,
        "config": row.config or {},
        "question_count": len(questions),
        "questions": [_question_stub(question) for question in questions],
    }
    if kind.has_interval:
        built["start_seconds"] = row.start_seconds
        built["end_seconds"] = row.end_seconds
    return kind.set_schema(**built).model_dump(mode="json")


async def list_sets(
    db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID, *, ready_only: bool = False
) -> list[dict]:
    """Every set of a passage, with its questions.

    One passage has a handful of sets, so the whole tree is fetched in two queries
    instead of an endpoint per set; the editor that draws it needs all of it at once.
    """
    fk = getattr(kind.set_model, kind.passage_fk)
    rows = (
        (
            await db.execute(
                select(kind.set_model)
                .where(fk == passage_id)
                .order_by(kind.set_model.position, kind.set_model.id)
            )
        )
        .scalars()
        .all()
    )
    by_set = await _questions_by_set(db, kind, [row.id for row in rows], ready_only=ready_only)
    built = [set_read(kind, row, by_set[row.id]) for row in rows]
    if ready_only:
        # A set whose questions are all drafts asks the learner to do nothing. It is
        # unfinished work, and the screen that shows it looks like a broken exercise.
        built = [item for item in built if item["questions"]]
    return built


async def unfiled_questions(
    db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID, *, ready_only: bool = False
) -> list[dict]:
    """Questions bound to this passage that belong to no set yet.

    The editor shows them as a pool to drag from. They are not orphaned - `question`
    still names the passage - so removing a set never deletes a question.
    """
    question_fk = getattr(Question, kind.question_fk)
    stmt = (
        select(Question)
        .where(question_fk == passage_id, Question.deleted_at.is_(None))
        .where(Question.id.not_in(select(kind.membership.question_id)))
        .order_by(Question.created_at, Question.id)
    )
    if ready_only:
        stmt = stmt.where(Question.status == enums.ContentStatus.READY)
    return [_loose_stub(row) for row in (await db.execute(stmt)).scalars().all()]


async def learner_tree(
    db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID, *, project: Any
) -> tuple[list[dict], list[dict]]:
    """The passage as a learner's exercise: ready sets, with answerable question payloads.

    `list_sets` carries the five fields a *teacher* needs to recognise a question; a
    learner needs the whole widget - options, layout, scoring - with the answer key taken
    out. That projection belongs to `question_service`, so the caller passes it in as
    `project` rather than this module importing the question bank back around.

    A question named by both queries and missing from the projection has been deleted in
    the moment between them, and is dropped: an exercise that offers a question nobody
    can answer is worse than one that quietly has one fewer item.
    """
    sets = await list_sets(db, kind, passage_id, ready_only=True)
    unfiled = await unfiled_questions(db, kind, passage_id, ready_only=True)
    ids = [uuid.UUID(question["id"]) for item in sets for question in item["questions"]]
    ids += [uuid.UUID(question["id"]) for question in unfiled]
    payloads = await project(db, ids)

    built = []
    for item in sets:
        questions = [payloads[question["id"]] for question in item["questions"] if question["id"] in payloads]
        # The set's authoring bucket is the teacher's, and the learner's shape has no key
        # for it: a field nobody on that screen reads is a field not worth sending.
        item.pop("config", None)
        item["questions"] = questions
        item["question_count"] = len(questions)
        built.append(kind.learner_set_schema(**item).model_dump(mode="json"))
    return built, [payloads[question["id"]] for question in unfiled if question["id"] in payloads]


async def _get_set(
    db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID, set_id: uuid.UUID
) -> Any:
    fk = getattr(kind.set_model, kind.passage_fk)
    row = (
        await db.execute(select(kind.set_model).where(kind.set_model.id == set_id, fk == passage_id))
    ).scalar_one_or_none()
    if row is None:
        raise PassageNotFound(f"no set with that id under this {kind.label}")
    return row


async def get_set(db: AsyncSession, kind: PassageKind, set_id: uuid.UUID) -> tuple[Any, Any]:
    """A set and the passage that owns it, from the set's own id.

    The set rows carry their passage key, so a browser editing one block never has to
    name the text above it - and cannot point at a set under another passage by sending
    a passage id that does not match.
    """
    row = (await db.execute(select(kind.set_model).where(kind.set_model.id == set_id))).scalar_one_or_none()
    if row is None:
        raise PassageNotFound(f"no {kind.label} set with that id")
    passage = await db.get(kind.passage, getattr(row, kind.passage_fk))
    if passage is None:
        raise PassageNotFound("that set belongs to a " + kind.label + " which is no longer here")
    return row, passage


async def _next_position(db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID) -> int:
    fk = getattr(kind.set_model, kind.passage_fk)
    highest = (
        await db.execute(select(func.max(kind.set_model.position)).where(fk == passage_id))
    ).scalar_one_or_none()
    return 0 if highest is None else int(highest) + 1


async def create_set(
    db: AsyncSession,
    kind: PassageKind,
    passage: Any,
    payload: p_schemas.QuestionSetCreate,
    *,
    admin_id: uuid.UUID,
) -> dict:
    """Append a set. Its position is the end of the list, never a client's number."""
    kwargs: dict[str, Any] = {
        kind.passage_fk: passage.id,
        "title": payload.title,
        "instructions": payload.instructions,
        "config": payload.config or {},
        "position": await _next_position(db, kind, passage.id),
    }
    if kind.has_interval:
        kwargs["start_seconds"] = payload.start_seconds
        kwargs["end_seconds"] = payload.end_seconds
    if len(await _set_ids(db, kind, passage.id)) >= p_schemas.MAX_SETS_PER_PASSAGE:
        raise PassageError(
            f"this {kind.label} already has the maximum of {p_schemas.MAX_SETS_PER_PASSAGE} sets"
        )
    row = kind.set_model(**kwargs)
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise PassageError("this set cannot be added right now - reload and try again") from None
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.set.created",
        actor_id=admin_id,
        target_type=kind.set_model.__tablename__,
        target_id=row.id,
        after={"title": row.title, "position": row.position},
    )
    return set_read(kind, row, [])


async def _set_ids(db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID) -> list[uuid.UUID]:
    fk = getattr(kind.set_model, kind.passage_fk)
    return list((await db.execute(select(kind.set_model.id).where(fk == passage_id))).scalars().all())


async def update_set(
    db: AsyncSession,
    kind: PassageKind,
    passage: Any,
    set_id: uuid.UUID,
    payload: p_schemas.QuestionSetUpdate,
    *,
    admin_id: uuid.UUID,
) -> dict:
    row = await _get_set(db, kind, passage.id, set_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise PassageError("nothing to change")
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field, value in changes.items():
        before[field] = getattr(row, field)
        setattr(row, field, value)
        after[field] = value
    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.set.updated",
        actor_id=admin_id,
        target_type=kind.set_model.__tablename__,
        target_id=row.id,
        before=before,
        after=after,
    )
    questions = (await _questions_by_set(db, kind, [row.id], ready_only=False))[row.id]
    return set_read(kind, row, questions)


async def delete_set(
    db: AsyncSession, kind: PassageKind, passage: Any, set_id: uuid.UUID, *, admin_id: uuid.UUID
) -> dict:
    """Remove a set and its memberships. The questions stay bound to the passage.

    A set is deleted far more often than its questions are, and removing the grouping
    must never take the exercises with it - they return to the unfiled pool.
    """
    row = await _get_set(db, kind, passage.id, set_id)
    filed = int(
        (await db.execute(select(func.count()).where(kind.membership.set_id == row.id))).scalar_one()
    )
    await db.execute(delete(kind.membership).where(kind.membership.set_id == row.id))
    await db.delete(row)
    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.set.deleted",
        actor_id=admin_id,
        target_type=kind.set_model.__tablename__,
        target_id=set_id,
        after={"title": row.title, "returned_to_pool": filed},
    )
    return {"ok": True, "deleted": str(set_id), "returned_to_pool": filed}


async def reorder_sets(
    db: AsyncSession,
    kind: PassageKind,
    passage: Any,
    set_ids: list[uuid.UUID],
    *,
    admin_id: uuid.UUID,
) -> list[dict]:
    """Renumber the sets in the order given.

    The request must name every set: a partial list would leave the omitted ones at
    their old numbers, and two sets in position 3 is a passage that reads differently
    on every reload.
    """
    rows = (
        (
            await db.execute(
                select(kind.set_model)
                .where(getattr(kind.set_model, kind.passage_fk) == passage.id)
                .order_by(kind.set_model.position)
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    unknown = [str(value) for value in set_ids if value not in by_id]
    if unknown:
        raise PassageError(
            f"these sets do not belong to this {kind.label}: " + ", ".join(unknown)
        )
    if len(set_ids) != len(by_id):
        missing = [str(row.id) for row in rows if row.id not in set_ids]
        raise PassageError(
            "every set must be listed in the new order - missing: " + ", ".join(missing)
        )
    for index, set_id in enumerate(set_ids):
        by_id[set_id].position = index
    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"{kind.name}.set.reordered",
        actor_id=admin_id,
        target_type=kind.passage.__tablename__,
        target_id=passage.id,
        after={"order": [str(value) for value in set_ids]},
    )
    return await list_sets(db, kind, passage.id)


async def _require_fileable(
    db: AsyncSession, kind: PassageKind, passage_id: uuid.UUID, question_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Question]:
    """Every question named must exist, be alive, and be bound to *this* passage."""
    if not question_ids:
        return {}
    rows = (await db.execute(select(Question).where(Question.id.in_(question_ids)))).scalars().all()
    found = {row.id: row for row in rows}
    missing = [str(value) for value in question_ids if value not in found]
    if missing:
        raise PassageError("these questions are not in the bank: " + ", ".join(missing))
    trashed = [_describe(row) for row in found.values() if row.deleted_at is not None]
    if trashed:
        raise PassageError("these questions are in the trash and cannot be filed: " + ", ".join(trashed))
    strangers = [
        _describe(row)
        for row in found.values()
        if getattr(row, kind.question_fk) != passage_id or row.context_kind != kind.context
    ]
    if strangers:
        raise PassageError(
            f"a question can only be filed under the {kind.label} it belongs to. Not this "
            "one's: " + ", ".join(strangers)
        )
    return found


def _describe(row: Question) -> str:
    label = (row.prompt or "").strip().replace("\n", " ")
    return f'"{label[:60]}"' if label else f"{row.type} question {row.id}"


async def assign_questions(
    db: AsyncSession,
    kind: PassageKind,
    passage: Any,
    set_id: uuid.UUID,
    question_ids: list[uuid.UUID],
    *,
    admin_id: uuid.UUID,
) -> dict:
    """Make one set hold exactly these questions, in this order.

    A question that was filed under a sibling set moves here, because the unique
    index says a question answers one block; reporting the move is what keeps that from
    reading like a save that went wrong somewhere else. An omitted question is unfiled,
    never deleted.
    """
    row = await _get_set(db, kind, passage.id, set_id)
    ids = list(dict.fromkeys(question_ids))
    await _require_fileable(db, kind, passage.id, ids)

    wanted = set(ids)
    current = (
        await db.execute(select(kind.membership).where(kind.membership.set_id == row.id))
    ).scalars().all()
    leaving = [member for member in current if member.question_id not in wanted]
    staying = {member.question_id: member for member in current if member.question_id in wanted}

    # Coming in: not currently in this set. That includes questions filed under a
    # sibling, which are moved rather than refused.
    arriving = [question_id for question_id in ids if question_id not in staying]
    moved: list[dict] = []
    if arriving:
        elsewhere = (
            await db.execute(
                select(kind.membership, kind.set_model.title).join(
                    kind.set_model, kind.set_model.id == kind.membership.set_id
                ).where(kind.membership.question_id.in_(arriving))
            )
        ).all()
        for member, title in elsewhere:
            moved.append({"question_id": str(member.question_id), "from_title": title})
            await db.delete(member)
        # The unique index on `question_id` is checked per statement: the rows that let
        # go of these questions must be gone before the new ones are written.
        await db.flush()

    for member in leaving:
        await db.delete(member)
    if leaving:
        await db.flush()

    for index, question_id in enumerate(ids):
        member = staying.get(question_id)
        if member is None:
            db.add(kind.membership(set_id=row.id, question_id=question_id, position=index))
        else:
            member.position = index
    try:
        await db.flush()
    except IntegrityError:
        # Two teachers assigned the same question to two sets in the same moment and
        # the index decided. That is a conflict to be told about, not a 500.
        await db.rollback()
        raise PassageError(
            "one of those questions was filed under another set while this request was "
            "being saved - reload and try again"
        ) from None

    await audit_service.record_audit(
        db,
        action=f"{kind.name}.set.assigned",
        actor_id=admin_id,
        target_type=kind.set_model.__tablename__,
        target_id=row.id,
        after={
            "questions": [str(value) for value in ids],
            "unfiled": [str(member.question_id) for member in leaving],
            "moved_in": [item["question_id"] for item in moved],
        },
    )
    questions = (await _questions_by_set(db, kind, [row.id], ready_only=False))[row.id]
    return {
        **set_read(kind, row, questions),
        "unfiled": [str(member.question_id) for member in leaving],
        "moved": moved,
    }


# --------------------------------------------------------------------------- #
# Shared list query building
# --------------------------------------------------------------------------- #


def check_list_args(*, view: str, sort: str, order: str, sortable: dict) -> None:
    """One gate for both list screens.

    A sort name is looked up in a dict rather than passed to SQL, and an unknown view is
    refused before it can turn into "show me everything, including the trash".
    """
    if view not in VIEWS:
        raise PassageError("view must be one of: " + ", ".join(VIEWS))
    if sort not in sortable:
        raise PassageError("sort must be one of: " + ", ".join(sorted(sortable)))
    if order not in ("asc", "desc"):
        raise PassageError("order must be 'asc' or 'desc'")


def trash_view(kind: PassageKind, stmt, *, view: str):
    if view == "bank":
        return stmt.where(kind.passage.deleted_at.is_(None))
    if view == "trash":
        return stmt.where(kind.passage.deleted_at.is_not(None))
    return stmt


async def paginate(
    db: AsyncSession,
    stmt,
    *,
    model: Any,
    sort: str,
    sortable: dict,
    order: str,
    page: int,
    page_size: int,
) -> tuple[list[Any], int]:
    """Count, then fetch one page in the requested order with a stable tie-break.

    The id is the last sort key on purpose: without it, rows sharing a `updated_at`
    can move between page 1 and page 2 while the teacher is reading page 1, and the list
    quietly shows the same passage twice and never shows another.
    """
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    column = sortable[sort]
    direction = column.desc() if order == "desc" else column.asc()
    rows = (
        (
            await db.execute(
                stmt.order_by(direction.nulls_last(), model.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


def search_filter(kind: PassageKind, q: str, *extra_columns: Any):
    """Free text over the title plus whatever body field a passage type has."""
    like = f"%{q.strip()}%"
    return or_(kind.passage.title.ilike(like), *extra_columns)


def lifecycle_options(sortable: dict) -> dict:
    """What both passage screens offer, taken from the code that enforces it."""
    return {
        "statuses": list(SETTABLE_STATUSES),
        "views": list(VIEWS),
        "sortable": sorted(sortable),
        "layouts": list(LAYOUTS),
    }
