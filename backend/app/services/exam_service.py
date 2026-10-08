"""Exams: the teacher's side of Phase 7 - authoring, publishing, assigning.

An exam is not a catalog with a timer on it. The two share only the shape of their content:
both hold references into the central banks and never a copy. Everything else differs because
the two things are asked to do.

**An exam pins a version, a catalog does not.** Every item on a paper is one answerable
question at a specific `QuestionVersion`, and the mark it carries is the mark that version
had. A learner graded last term has to remain gradable this term, after the question has been
rewritten twice.

**An item is exactly one answer.** `attempt_answer` is keyed by one exam item, so an item that
carried three questions would have three answers under one key. A reading or a recording
reaches a paper through the questions filed under it: naming a text adds *its* questions, each
one bound to that text, and the learner's runner serves the text to them as context. The
relationship the content bank already keeps (`question.reading_id`) is what makes the context
travel with the question - the paper does not store a second copy of it.

**Composition locks the moment somebody sits.** Adding, removing, reordering or re-pointing an
item, or changing the mark one carries, would rewrite what a finished attempt was made of. The
freeze is on the composition, not on the rules: timing, shuffling, feedback and visibility are
copied into the attempt's own blueprint when it starts, so editing them reaches the sittings
that come after, and the learner who is mid-paper keeps the paper they were given.

**A paper is assigned, not opened.** A catalog is visible to every learner who can reach it;
an exam reaches the students and groups the teacher named, and nothing else.
"""
from __future__ import annotations

import random
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums, security
from app.core.exceptions import RuleBroken
from app.models.assessment import (
    Exam,
    ExamAssignment,
    ExamAttempt,
    ExamItem,
    ExamSection,
    ManualReview,
)
from app.models.content import (
    Listening,
    ListeningQuestionSet,
    ListeningSetQuestion,
    Question,
    QuestionVersion,
    Reading,
    ReadingQuestionSet,
    ReadingSetQuestion,
)
from app.models.identity import Group, GroupMembership, Student
from app.schemas import exam as e_schemas
from app.services import audit_service, passage_service

SETTABLE_STATUSES = list(e_schemas.SETTABLE_STATUSES)
VIEWS = e_schemas.VIEWS
MAX_ITEMS = e_schemas.MAX_ITEMS_PER_EXAM

#: The lifecycle moves a teacher may ask for. Anything not listed here is refused rather than
#: interpreted, because "back to draft" on a paper learners have already handed in is not a
#: move to guess at.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"scheduled", "active"},
    "scheduled": {"active", "draft"},
    "active": {"finished", "archived"},
    "finished": {"active", "archived"},
    "archived": {"draft"},
}

#: Rules that decide what the sitting feels like. They are copied into the attempt's blueprint
#: when it starts, so they may be edited while learners are working: the sitting in progress
#: keeps the copy it was given, and the next one gets the new rules.
RULE_FIELDS = (
    "available_from",
    "available_to",
    "duration_minutes",
    "must_finish_before_close",
    "max_attempts",
    "passing_score",
    "shuffle_questions",
    "shuffle_options",
    "resume_after_disconnect",
    "restrict_copy_paste",
    "monitor_tab_switch",
    "tab_switch_limit",
    "tab_switch_action",
    "allow_previous",
    "feedback_timing",
    "show_correct_answers",
    "show_explanations",
    "result_visibility",
    "partial_scoring_enabled",
    "negative_marking_enabled",
    "grading_mode",
    "auto_submit_on_expiry",
)

_SORTABLE = {
    "title": Exam.title,
    "created_at": Exam.created_at,
    "updated_at": Exam.updated_at,
    "available_from": Exam.available_from,
}

_PASSAGE_MODEL: dict[str, tuple[Any, Any, Any]] = {
    # kind -> (passage model, its block table, the membership table under the block)
    "reading": (Reading, ReadingQuestionSet, ReadingSetQuestion),
    "listening": (Listening, ListeningQuestionSet, ListeningSetQuestion),
}

#: The blueprint's own version stamp. An attempt reads its composition through it, so a later
#: change to what a blueprint holds cannot be mistaken for the shape an old sitting used.
BLUEPRINT_SCHEMA = 1


class ExamError(RuleBroken, ValueError):
    """A teacher-facing refusal about an exam, its items or its rules; the endpoints 422."""

    code = "exam_rule"


class ExamNotFound(RuleBroken, Exception):
    """No such exam, section, item or assignment. 404."""

    code = "exam_not_found"


class NameTaken(RuleBroken, Exception):
    """Another live paper already carries this title (409)."""

    code = "exam_name_taken"


class DuplicateReference(RuleBroken, Exception):
    """This paper already holds that question (409)."""

    code = "exam_reference_exists"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        params: dict | None = None,
        existing_item_id: uuid.UUID | None = None,
    ) -> None:
        super().__init__(message, code=code, params=params)
        self.existing_item_id = existing_item_id


class LockedComposition(RuleBroken, Exception):
    """The paper has been sat, so what it consists of cannot change (409)."""

    code = "exam_composition_locked"


def _label_of(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def status_enum(raw: str) -> enums.ExamStatus:
    try:
        return enums.ExamStatus(raw)
    except ValueError as exc:
        raise ExamError(
            f"'{raw}' is not an exam state (use one of: {', '.join(SETTABLE_STATUSES)})",
            code="exam_state_unknown",
            params={"value": raw, "allowed": ", ".join(SETTABLE_STATUSES)},
        ) from exc


def kind_of(raw: str) -> str:
    if raw not in e_schemas.ITEM_KINDS:
        raise ExamError(
            "kind must be one of: " + ", ".join(e_schemas.ITEM_KINDS),
            code="exam_kind_unknown",
            params={"allowed": ", ".join(e_schemas.ITEM_KINDS)},
        )
    return raw


def _now() -> datetime:
    return security.utcnow()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Reading rows
# --------------------------------------------------------------------------- #


async def get(db: AsyncSession, exam_id: uuid.UUID, *, allow_trash: bool = True) -> Exam:
    exam = await db.get(Exam, exam_id)
    if exam is None or (exam.deleted_at is not None and not allow_trash):
        raise ExamNotFound("no such exam", code="exam_not_found")
    return exam


async def get_item(db: AsyncSession, item_id: uuid.UUID) -> tuple[Exam, ExamItem]:
    item = await db.get(ExamItem, item_id)
    if item is None:
        raise ExamNotFound("no such item on this exam", code="exam_item_not_found")
    return await get(db, item.exam_id), item


async def get_section(db: AsyncSession, section_id: uuid.UUID) -> tuple[Exam, ExamSection]:
    section = await db.get(ExamSection, section_id)
    if section is None:
        raise ExamNotFound("no such section on this exam", code="exam_section_not_found")
    return await get(db, section.exam_id), section


async def sections_of(db: AsyncSession, exam_id: uuid.UUID) -> list[ExamSection]:
    return list(
        (
            await db.execute(
                select(ExamSection)
                .where(ExamSection.exam_id == exam_id)
                .order_by(ExamSection.position, ExamSection.created_at)
            )
        ).scalars()
    )


async def items_of(db: AsyncSession, exam_id: uuid.UUID) -> list[ExamItem]:
    return list(
        (
            await db.execute(
                select(ExamItem).where(ExamItem.exam_id == exam_id).order_by(ExamItem.position, ExamItem.created_at)
            )
        ).scalars()
    )


async def attempt_count(db: AsyncSession, exam_id: uuid.UUID) -> int:
    return int(
        await db.scalar(select(func.count()).select_from(ExamAttempt).where(ExamAttempt.exam_id == exam_id)) or 0
    )


async def is_locked(db: AsyncSession, exam_id: uuid.UUID) -> bool:
    """True once any learner has sat this paper - the composition freeze.

    The count is of `exam_attempt` rows, not of submitted ones: an attempt that was started
    and abandoned is still a record of what that learner was given.
    """
    return await attempt_count(db, exam_id) > 0


# --------------------------------------------------------------------------- #
# Items: what they point at, and what they are worth
# --------------------------------------------------------------------------- #


async def _version_row(db: AsyncSession, question_id: uuid.UUID, version: int) -> QuestionVersion | None:
    return (
        await db.execute(
            select(QuestionVersion).where(
                QuestionVersion.question_id == question_id, QuestionVersion.version == version
            )
        )
    ).scalar_one_or_none()


async def snapshot_for_version(db: AsyncSession, version_id: uuid.UUID | None) -> dict | None:
    """The frozen question one pin points at, or None when the pin no longer resolves.

    Read through the version row rather than the live question, always: this is the only path
    by which a historical mark can be reproduced, and it is the reason a paper's totals never
    move when the bank is edited.
    """
    if version_id is None:
        return None
    row = await db.get(QuestionVersion, version_id)
    return row.snapshot if row else None


async def snapshots_for_versions(db: AsyncSession, version_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """The same read for a whole paper in one query."""
    if not version_ids:
        return {}
    return {
        row.id: row.snapshot
        for row in (
            await db.execute(select(QuestionVersion).where(QuestionVersion.id.in_(version_ids)))
        ).scalars()
    }


def effective_points(item: ExamItem, snapshot: dict | None) -> float:
    """The mark one item carries: the teacher's override, or the pinned version's own score."""
    if item.points is not None:
        return float(item.points)
    if snapshot:
        return float((snapshot.get("question") or {}).get("score") or 0.0)
    return 0.0


async def _passages(db: AsyncSession, questions: list[Question]) -> dict[str, dict]:
    """Every text and recording the given questions are bound to, in two queries.

    A paper of two hundred reading questions would otherwise ask for the same four texts two
    hundred times, and whether a question may be served depends on its passage being ready too.
    """
    reading_ids = {row.reading_id for row in questions if row.reading_id}
    listening_ids = {row.listening_id for row in questions if row.listening_id}
    readings = {
        row.id: row
        for row in (
            await db.execute(select(Reading).where(Reading.id.in_(reading_ids)))
        ).scalars()
    } if reading_ids else {}
    listenings = {
        row.id: row
        for row in (
            await db.execute(select(Listening).where(Listening.id.in_(listening_ids)))
        ).scalars()
    } if listening_ids else {}
    return {"reading": readings, "listening": listenings}


def _question_state(question: Question | None, passages: dict[str, dict]) -> str:
    """`ready` / `draft` / `archived` / `trashed` / `missing`.

    A question that belongs to a reading is only serveable when that reading is serveable too;
    otherwise a paper would carry an unpublished text into a learner's screen inside its own
    context.
    """
    if question is None:
        return "missing"
    if question.deleted_at is not None:
        return "trashed"
    state = _label_of(question.status)
    if state != "ready":
        return state
    for kind, column in (("reading", question.reading_id), ("listening", question.listening_id)):
        if column is None:
            continue
        passage = passages[kind].get(column)
        if passage is None:
            return "missing"
        if not passage_service.is_learner_visible(passage):
            return "draft"
    return "ready"


async def resolve_items(db: AsyncSession, items: list[ExamItem]) -> list[dict]:
    """One query per table for the whole paper, not one per item.

    A paper holds up to two hundred items and the editor draws every one of them; resolving
    them one at a time would be six hundred round trips to open one lesson.
    """
    if not items:
        return []
    question_ids = [item.ref_id for item in items if _label_of(item.kind) == "question"]
    version_ids = [item.question_version_id for item in items if item.question_version_id]

    questions = {
        row.id: row
        for row in (
            await db.execute(select(Question).where(Question.id.in_(question_ids)))
        ).scalars()
    } if question_ids else {}
    versions = {
        row.id: row
        for row in (
            await db.execute(
                select(QuestionVersion).where(QuestionVersion.id.in_(version_ids))
            )
        ).scalars()
    } if version_ids else {}
    sections = {row.id: row for row in await sections_of(db, items[0].exam_id)}
    passages = await _passages(db, list(questions.values()))

    out: list[dict] = []
    for item in items:
        question = questions.get(item.ref_id)
        version = versions.get(item.question_version_id) if item.question_version_id else None
        snapshot = version.snapshot if version else None
        frozen = (snapshot or {}).get("question") or {}
        state = _question_state(question, passages)
        context_id = None
        context_title = None
        if question is not None:
            if question.reading_id is not None:
                context_id = question.reading_id
                passage = passages["reading"].get(question.reading_id)
                context_title = passage.title if passage else None
            elif question.listening_id is not None:
                context_id = question.listening_id
                passage = passages["listening"].get(question.listening_id)
                context_title = passage.title if passage else None
        section = sections.get(item.section_id) if item.section_id else None
        detail = None
        if question is not None:
            detail = _label_of(question.type) + (
                f" · {frozen.get('score', question.score):g}"
            )
            if section is not None and section.title:
                detail = f"{detail} · {section.title}"
        out.append(
            e_schemas.ExamItemRead(
                id=item.id,
                kind=_label_of(item.kind),
                ref_id=item.ref_id,
                position=item.position,
                points=item.points,
                question_score=float(frozen["score"]) if "score" in frozen else (
                    float(question.score) if question is not None else None
                ),
                effective_points=effective_points(item, snapshot),
                # The number the pin resolved to is read from the version row: a snapshot holds
                # the question's text and marks, and the sequence belongs to the row.
                version=version.version if version is not None else (
                    question.current_version if question is not None else None
                ),
                current_version=question.current_version if question is not None else None,
                section_id=item.section_id,
                title=frozen.get("prompt") if frozen else (question.prompt if question else None),
                detail=detail,
                context_kind=_label_of(question.context_kind) if question else None,
                context_id=context_id,
                context_title=context_title,
                state=state,
                serveable=state == "ready" and snapshot is not None,
            ).model_dump(mode="json")
        )
    return out


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def _publish_blockers(
    exam: Exam, items: list[dict], *, assignments: int
) -> list[dict]:
    """Why this paper could not be handed out right now, as the codes a screen says in words.

    Computed from the same facts the lifecycle check uses and answered with the same codes it
    would refuse with, so the note above the form and the refusal below it are one sentence in
    the teacher's own language rather than two written in English.
    """
    blockers: list[dict] = []
    if not items:
        blockers.append({"code": "exam_has_no_questions"})
    unserved = [row for row in items if not row["serveable"]]
    if unserved:
        first = unserved[0]
        blockers.append(
            {
                "code": "exam_items_not_ready",
                "params": {
                    "n": len(unserved),
                    "example": str(first["title"] or "a question")[:80],
                },
            }
        )
    if _label_of(exam.status) == "scheduled" and exam.available_from is None:
        blockers.append({"code": "exam_schedule_needs_opening_time"})
    if not assignments and _label_of(exam.status) in ("active", "scheduled"):
        blockers.append({"code": "exam_no_audience"})
    return blockers


async def to_read(db: AsyncSession, exam: Exam) -> dict:
    items = await resolve_items(db, await items_of(db, exam.id))
    sections = await sections_of(db, exam.id)
    assignments = await list_assignments(db, exam)
    locked = await is_locked(db, exam.id)

    # Grouped by the id as the resolved rows carry it - a JSON string, because `resolve_items`
    # hands back dumped reads. Keyed by the `uuid.UUID` instead, every lookup would miss and each
    # part would report an empty paper while the items below it name that very part.
    by_section: dict[str | None, list[dict]] = {}
    for row in items:
        by_section.setdefault(str(row["section_id"]) if row["section_id"] else None, []).append(row)

    section_reads = [
        e_schemas.ExamSectionRead(
            id=section.id,
            title=section.title,
            position=section.position,
            shuffle_items=section.shuffle_items,
            instructions=(section.config or {}).get("instructions"),
            item_count=len(by_section.get(str(section.id), [])),
            points=round(sum(row["effective_points"] for row in by_section.get(str(section.id), [])), 4),
        ).model_dump(mode="json")
        for section in sections
    ]

    counts: dict[str, int] = {}
    for row in items:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1

    return e_schemas.ExamRead(
        id=exam.id,
        title=exam.title,
        description=exam.description,
        status=_label_of(exam.status),
        learning_language=exam.learning_language,
        level=exam.level,
        available_from=_iso(_aware(exam.available_from)),
        available_to=_iso(_aware(exam.available_to)),
        duration_minutes=exam.duration_minutes,
        must_finish_before_close=exam.must_finish_before_close,
        max_attempts=exam.max_attempts,
        passing_score=exam.passing_score,
        shuffle_questions=exam.shuffle_questions,
        shuffle_options=exam.shuffle_options,
        resume_after_disconnect=exam.resume_after_disconnect,
        restrict_copy_paste=exam.restrict_copy_paste,
        monitor_tab_switch=exam.monitor_tab_switch,
        tab_switch_limit=exam.tab_switch_limit,
        tab_switch_action=exam.tab_switch_action,
        allow_previous=exam.allow_previous,
        feedback_timing=_label_of(exam.feedback_timing),
        show_correct_answers=exam.show_correct_answers,
        show_explanations=exam.show_explanations,
        result_visibility=exam.result_visibility,
        partial_scoring_enabled=exam.partial_scoring_enabled,
        negative_marking_enabled=exam.negative_marking_enabled,
        grading_mode=_label_of(exam.grading_mode),
        auto_submit_on_expiry=exam.auto_submit_on_expiry,
        created_at=_iso(exam.created_at) or "",
        updated_at=_iso(exam.updated_at) or "",
        deleted_at=_iso(exam.deleted_at),
        item_count=len(items),
        counts=counts,
        points=round(sum(row["effective_points"] for row in items), 4),
        sections=section_reads,
        items=items,
        assignments=assignments,
        attempt_count=await attempt_count(db, exam.id),
        composition_locked=locked,
        publish_blockers=_publish_blockers(exam, items, assignments=len(assignments)),
    ).model_dump(mode="json")


async def stats_for(db: AsyncSession, exam_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Items, marks, assignments, sittings and pending reviews for a page of exams.

    Five grouped queries rather than five queries per row: the teacher's list page is the
    screen they live on, and it should not cost a hundred round trips to draw twenty rows.
    """
    if not exam_ids:
        return {}
    stats = {
        exam_id: {
            "item_count": 0,
            "points": 0.0,
            "assigned_count": 0,
            "attempt_count": 0,
            "submitted_count": 0,
            "review_count": 0,
        }
        for exam_id in exam_ids
    }

    item_rows = (
        await db.execute(
            select(ExamItem.exam_id, ExamItem.points, ExamItem.question_version_id).where(
                ExamItem.exam_id.in_(exam_ids)
            )
        )
    ).all()
    version_ids = [row[2] for row in item_rows if row[2]]
    scores: dict[uuid.UUID, float] = {}
    if version_ids:
        for vid, snapshot in (
            await db.execute(select(QuestionVersion.id, QuestionVersion.snapshot).where(QuestionVersion.id.in_(version_ids)))
        ).all():
            scores[vid] = float((snapshot.get("question") or {}).get("score") or 0.0)
    for exam_id, points, version_id in item_rows:
        stats[exam_id]["item_count"] += 1
        stats[exam_id]["points"] += float(points) if points is not None else scores.get(version_id, 0.0)

    for exam_id, count in (
        await db.execute(
            select(ExamAssignment.exam_id, func.count())
            .where(ExamAssignment.exam_id.in_(exam_ids))
            .group_by(ExamAssignment.exam_id)
        )
    ).all():
        stats[exam_id]["assigned_count"] = int(count)

    for exam_id, status, count in (
        await db.execute(
            select(ExamAttempt.exam_id, ExamAttempt.status, func.count())
            .where(ExamAttempt.exam_id.in_(exam_ids))
            .group_by(ExamAttempt.exam_id, ExamAttempt.status)
        )
    ).all():
        stats[exam_id]["attempt_count"] += int(count)
        if _label_of(status) in ("submitted", "auto_submitted"):
            stats[exam_id]["submitted_count"] += int(count)

    for exam_id, count in (
        await db.execute(
            select(ExamAttempt.exam_id, func.count())
            .join(ManualReview, ManualReview.attempt_id == ExamAttempt.id)
            .where(ExamAttempt.exam_id.in_(exam_ids), ManualReview.reviewed.is_(False))
            .group_by(ExamAttempt.exam_id)
        )
    ).all():
        stats[exam_id]["review_count"] = int(count)

    for value in stats.values():
        value["points"] = round(value["points"], 4)
    return stats


def _check_list_args(*, view: str, sort: str, order: str) -> None:
    """One gate for the list screen, and it speaks the exam's own vocabulary.

    A sort name is looked up in a dict rather than passed to SQL; an unknown view is refused
    before it can turn into "show me everything, including the trash".
    """
    if view not in VIEWS:
        raise ExamError(
            f"view must be one of: {', '.join(VIEWS)}",
            code="exam_view_unknown",
            params={"allowed": ", ".join(VIEWS)},
        )
    if sort not in _SORTABLE:
        allowed = ", ".join(sorted(_SORTABLE))
        raise ExamError(
            "sort must be one of: " + allowed,
            code="exam_sort_unknown",
            params={"allowed": allowed},
        )
    if order not in ("asc", "desc"):
        raise ExamError("order must be 'asc' or 'desc'", code="exam_order_unknown")


async def list_exams(
    db: AsyncSession,
    *,
    q: str | None = None,
    status: str | None = None,
    language: str | None = None,
    level: str | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    _check_list_args(view=view, sort=sort, order=order)

    stmt = select(Exam)
    if view == "bank":
        stmt = stmt.where(Exam.deleted_at.is_(None))
    elif view == "trash":
        stmt = stmt.where(Exam.deleted_at.is_not(None))

    if status:
        wanted = status_enum(status)
        if view == "trash":
            raise ExamError(
                "a trashed exam is chosen by its bin, not by its state",
                code="exam_bin_view_only",
            )
        stmt = stmt.where(Exam.status == wanted)
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Exam.title).like(pattern))
    if language:
        stmt = stmt.where(Exam.learning_language == language)
    if level:
        stmt = stmt.where(Exam.level == level.strip().upper())

    total = int(
        await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    )
    column = _SORTABLE[sort]
    ordered = stmt.order_by(column.desc() if order == "desc" else column.asc(), Exam.id)
    rows = list(
        (await db.execute(ordered.limit(page_size).offset((page - 1) * page_size))).scalars()
    )
    stats = await stats_for(db, [row.id for row in rows])

    items = []
    for exam in rows:
        got = stats.get(exam.id, {})
        items.append(
            e_schemas.ExamRowRead(
                id=exam.id,
                title=exam.title,
                status=_label_of(exam.status),
                learning_language=exam.learning_language,
                level=exam.level,
                available_from=_iso(_aware(exam.available_from)),
                available_to=_iso(_aware(exam.available_to)),
                duration_minutes=exam.duration_minutes,
                item_count=got.get("item_count", 0),
                points=got.get("points", 0.0),
                assigned_count=got.get("assigned_count", 0),
                attempt_count=got.get("attempt_count", 0),
                submitted_count=got.get("submitted_count", 0),
                review_count=got.get("review_count", 0),
                created_at=_iso(exam.created_at) or "",
                deleted_at=_iso(exam.deleted_at),
            ).model_dump(mode="json")
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def meta(db: AsyncSession) -> dict:
    """The dropdown lists, read from the code that enforces them."""
    languages = await passage_service.enabled_languages(db)
    return e_schemas.ExamMeta(
        statuses=list(SETTABLE_STATUSES),
        # Codes, not sentences. The editor's teacher may be working in any of the four interface
        # languages, so the screen translates these words; a label written here would arrive in
        # English no matter what the learner's or teacher's own language is set to.
        kinds=[{"kind": kind} for kind in e_schemas.ITEM_KINDS],
        feedback_timings=list(e_schemas.FEEDBACK_TIMINGS),
        result_visibilities=list(e_schemas.RESULT_VISIBILITIES),
        grading_modes=[
            {"mode": "automatic"},
            {"mode": "manual"},
            {"mode": "ai_assisted", "available": False},
        ],
        tab_switch_actions=list(e_schemas.TAB_SWITCH_ACTIONS),
        levels=list(constants.CEFR_LEVELS),
        languages=languages,
        limits={
            "max_items": MAX_ITEMS,
            "max_duration_minutes": e_schemas.MAX_DURATION_MINUTES,
            "max_attempts": e_schemas.MAX_ATTEMPTS,
            "max_tab_switch_limit": e_schemas.MAX_TAB_SWITCH_LIMIT,
        },
        views=list(VIEWS),
        bulk_actions=list(e_schemas.BULK_ACTIONS),
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Writing an exam
# --------------------------------------------------------------------------- #


async def _check_language(db: AsyncSession, code: str | None) -> None:
    if code is None:
        return
    options = await passage_service.enabled_languages(db)
    if code not in options:
        raise ExamError(
            f"'{code}' is not an enabled learning language (available: {', '.join(options) or 'none configured'})",
            code="exam_language_unavailable",
            params={"value": code, "allowed": ", ".join(options) or "none configured"},
        )


def _check_level(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    band = value.strip().upper()
    if band not in constants.CEFR_LEVELS:
        raise ExamError(
            f"'{value}' is not a level (use one of: {', '.join(constants.CEFR_LEVELS)})",
            code="exam_level_unknown",
            params={"value": value, "allowed": ", ".join(constants.CEFR_LEVELS)},
        )
    return band


async def _check_title(
    db: AsyncSession, title: str, *, exclude_id: uuid.UUID | None = None
) -> None:
    """Two live papers cannot carry the same title.

    Case-insensitive: a teacher who writes "Paper 1" twice means the same paper, and a list
    with two near-identical rows is a list they cannot pick from. Trashed papers are excluded
    because a binned title is free again.
    """
    stmt = select(Exam.id).where(
        func.lower(Exam.title) == title.lower(), Exam.deleted_at.is_(None)
    )
    if exclude_id is not None:
        stmt = stmt.where(Exam.id != exclude_id)
    clash = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if clash is not None:
        raise NameTaken(
            f"an exam called '{title}' is already in the bank", code="exam_name_taken"
        )


def _check_window(exam: Exam) -> None:
    if exam.available_from and exam.available_to and exam.available_to <= exam.available_from:
        raise ExamError("the closing time has to be after the opening time", code="exam_window_backwards")
    if exam.monitor_tab_switch and not exam.tab_switch_action:
        raise ExamError("switch monitoring needs an action to take", code="exam_monitor_needs_action")
    if exam.tab_switch_limit is not None and not exam.monitor_tab_switch:
        raise ExamError(
            "a switch limit only means something while monitoring is on",
            code="exam_switch_limit_needs_monitoring",
        )


def rules_payload(exam: Exam) -> dict:
    """The sitting's own copy of the paper's rules, stored in the blueprint.

    Every field that changes what a learner experiences while sitting is here, so an attempt
    can be served and graded from its own snapshot even after the teacher has edited the exam.
    """
    return {
        "duration_minutes": exam.duration_minutes,
        "available_from": _iso(_aware(exam.available_from)),
        "available_to": _iso(_aware(exam.available_to)),
        "must_finish_before_close": exam.must_finish_before_close,
        "max_attempts": exam.max_attempts,
        "passing_score": exam.passing_score,
        "allow_previous": exam.allow_previous,
        "restrict_copy_paste": exam.restrict_copy_paste,
        "monitor_tab_switch": exam.monitor_tab_switch,
        "tab_switch_limit": exam.tab_switch_limit,
        "tab_switch_action": exam.tab_switch_action,
        "resume_after_disconnect": exam.resume_after_disconnect,
        "feedback_timing": _label_of(exam.feedback_timing),
        "show_correct_answers": exam.show_correct_answers,
        "show_explanations": exam.show_explanations,
        "result_visibility": exam.result_visibility,
        "partial_scoring_enabled": exam.partial_scoring_enabled,
        "negative_marking_enabled": exam.negative_marking_enabled,
        "grading_mode": _label_of(exam.grading_mode),
        "auto_submit_on_expiry": exam.auto_submit_on_expiry,
    }


async def create(db: AsyncSession, payload: e_schemas.ExamCreate, *, admin_id: uuid.UUID) -> dict:
    await _check_language(db, payload.learning_language)
    await _check_title(db, payload.title)
    exam = Exam(
        title=payload.title,
        description=payload.description,
        status=status_enum(payload.status),
        learning_language=payload.learning_language,
        level=_check_level(payload.level),
        available_from=payload.available_from,
        available_to=payload.available_to,
        duration_minutes=payload.duration_minutes,
        must_finish_before_close=payload.must_finish_before_close,
        max_attempts=payload.max_attempts,
        passing_score=payload.passing_score,
        shuffle_questions=payload.shuffle_questions,
        shuffle_options=payload.shuffle_options,
        resume_after_disconnect=payload.resume_after_disconnect,
        restrict_copy_paste=payload.restrict_copy_paste,
        monitor_tab_switch=payload.monitor_tab_switch,
        tab_switch_limit=payload.tab_switch_limit,
        tab_switch_action=payload.tab_switch_action,
        allow_previous=payload.allow_previous,
        feedback_timing=enums.FeedbackTiming(payload.feedback_timing),
        show_correct_answers=payload.show_correct_answers,
        show_explanations=payload.show_explanations,
        result_visibility=payload.result_visibility,
        partial_scoring_enabled=payload.partial_scoring_enabled,
        negative_marking_enabled=payload.negative_marking_enabled,
        grading_mode=enums.GradingMode(payload.grading_mode),
        auto_submit_on_expiry=payload.auto_submit_on_expiry,
    )
    db.add(exam)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ExamError("that exam could not be saved", code="exam_could_not_save") from exc
    _check_window(exam)
    await audit_service.record_audit(
        db,
        action="exam.create",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        after={"title": exam.title, "status": _label_of(exam.status)},
    )
    return await to_read(db, exam)


async def update(
    db: AsyncSession, exam: Exam, payload: e_schemas.ExamUpdate, *, admin_id: uuid.UUID
) -> dict:
    given = payload.model_dump(exclude_unset=True)
    before = {key: getattr(exam, key) for key in given if key != "title"}
    if "title" in given:
        await _check_title(db, given["title"], exclude_id=exam.id)
        exam.title = given["title"]
    if "description" in given:
        exam.description = given["description"]
    if "learning_language" in given:
        await _check_language(db, given["learning_language"])
        exam.learning_language = given["learning_language"]
    if "level" in given:
        exam.level = _check_level(given["level"])
    for key in RULE_FIELDS:
        if key in given:
            setattr(exam, key, given[key])
    _check_window(exam)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.update",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        before={k: _iso(v) if isinstance(v, datetime) else v for k, v in before.items()},
        after={
            k: _iso(v) if isinstance(v, datetime) else v
            for k, v in given.items()
            if k in RULE_FIELDS or k in ("title", "description", "level", "learning_language")
        },
        detail="rules and description only; the composition of a paper changes through its items",
    )
    return await to_read(db, exam)


async def _assert_publishable(db: AsyncSession, exam: Exam) -> None:
    """Handing a paper out means every learner who opens it gets a whole paper.

    Checked against the items as they resolve now, in one pass, because the refusal has to
    name what to fix rather than say "not ready".
    """
    items = await resolve_items(db, await items_of(db, exam.id))
    if not items:
        raise ExamError("this exam has no questions on it yet", code="exam_has_no_questions")
    unserved = [row for row in items if not row["serveable"]]
    if unserved:
        first = unserved[0]
        raise ExamError(
            f"{len(unserved)} item(s) point at content that is not ready for learners "
            f"(for example: {str(first['title'] or 'a question')[:80]})",
            code="exam_items_not_ready",
            params={"n": len(unserved), "example": str(first["title"] or "a question")[:80]},
        )
    if _label_of(exam.status) == "scheduled" and exam.available_from is None:
        raise ExamError(
            "a scheduled exam needs an opening time before it can be published",
            code="exam_schedule_needs_opening_time",
        )


async def set_status(db: AsyncSession, exam: Exam, raw_status: str, *, admin_id: uuid.UUID) -> dict:
    target = status_enum(raw_status)
    current = _label_of(exam.status)
    if target.value == current:
        return await to_read(db, exam)
    if target.value not in TRANSITIONS.get(current, set()):
        raise ExamError(
            f"an exam that is {current} cannot be moved to {target.value}",
            code="exam_state_change_not_allowed",
            params={"from": str(current), "to": target.value},
        )
    if target.value in ("active", "scheduled"):
        await _assert_publishable(db, exam)
    before = current
    exam.status = target
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.status",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        before={"status": before},
        after={"status": target.value},
    )
    return await to_read(db, exam)


async def trash(db: AsyncSession, exam: Exam, *, admin_id: uuid.UUID) -> dict:
    """Bin a paper. Sittings it already holds are not touched, and never will be by this call.

    A paper that is live on a learner's screen cannot vanish from underneath them: it has to
    be finished or archived first, which is the teacher's decision that the bin follows.
    """
    current = _label_of(exam.status)
    if exam.deleted_at is None and current in ("active", "scheduled"):
        raise ExamError(
            f"this exam is {current} - finish or archive it before binning it, so no learner "
            "loses a paper they were told to sit",
            code="exam_bin_open_paper_first",
            params={"state": str(current)},
        )
    exam.deleted_at = _now()
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.trash",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        after={"deleted_at": _iso(exam.deleted_at), "attempt_count": await attempt_count(db, exam.id)},
    )
    return {"id": str(exam.id), "deleted_at": _iso(exam.deleted_at)}


async def restore(db: AsyncSession, exam: Exam, *, admin_id: uuid.UUID) -> dict:
    if exam.deleted_at is None:
        raise ExamError("this exam is not in the trash", code="exam_not_in_bin")
    clash = (
        await db.execute(
            select(Exam.id).where(
                func.lower(Exam.title) == exam.title.lower(),
                Exam.deleted_at.is_(None),
                Exam.id != exam.id,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise NameTaken(
            f"another exam called '{exam.title}' is in the bank - rename one of them first",
            code="exam_name_taken",
        )
    exam.deleted_at = None
    await db.flush()
    await audit_service.record_audit(
        db, action="exam.restore", actor_id=admin_id, target_type="exam", target_id=exam.id
    )
    return {"id": str(exam.id), "restored": True}


async def clone(db: AsyncSession, exam: Exam, *, admin_id: uuid.UUID) -> dict:
    """A copy of a paper as a new draft, with the same pinned versions.

    The reason this is the answer to "I want to change the questions on a sat exam": the
    original keeps the composition its sittings were graded against, and the copy is where the
    edits go. Items are copied with their `question_version_id`, so the new paper starts as
    exactly as reproducible as the old one.
    """
    title = f"{exam.title} (copy)"[:300]
    await _check_title(db, title)
    copy = Exam(
        title=title,
        description=exam.description,
        status=enums.ExamStatus.DRAFT,
        learning_language=exam.learning_language,
        level=exam.level,
        available_from=exam.available_from,
        available_to=exam.available_to,
        duration_minutes=exam.duration_minutes,
        must_finish_before_close=exam.must_finish_before_close,
        max_attempts=exam.max_attempts,
        passing_score=exam.passing_score,
        shuffle_questions=exam.shuffle_questions,
        shuffle_options=exam.shuffle_options,
        resume_after_disconnect=exam.resume_after_disconnect,
        restrict_copy_paste=exam.restrict_copy_paste,
        monitor_tab_switch=exam.monitor_tab_switch,
        tab_switch_limit=exam.tab_switch_limit,
        tab_switch_action=exam.tab_switch_action,
        allow_previous=exam.allow_previous,
        feedback_timing=exam.feedback_timing,
        show_correct_answers=exam.show_correct_answers,
        show_explanations=exam.show_explanations,
        result_visibility=exam.result_visibility,
        partial_scoring_enabled=exam.partial_scoring_enabled,
        negative_marking_enabled=exam.negative_marking_enabled,
        grading_mode=exam.grading_mode,
        auto_submit_on_expiry=exam.auto_submit_on_expiry,
    )
    db.add(copy)
    await db.flush()

    section_map: dict[uuid.UUID, uuid.UUID] = {}
    for section in await sections_of(db, exam.id):
        new_section = ExamSection(
            exam_id=copy.id,
            title=section.title,
            position=section.position,
            shuffle_items=section.shuffle_items,
            config=dict(section.config or {}),
        )
        db.add(new_section)
        await db.flush()
        section_map[section.id] = new_section.id

    for item in await items_of(db, exam.id):
        db.add(
            ExamItem(
                exam_id=copy.id,
                section_id=section_map.get(item.section_id) if item.section_id else None,
                kind=item.kind,
                ref_id=item.ref_id,
                question_version_id=item.question_version_id,
                position=item.position,
                points=item.points,
                config=dict(item.config or {}),
            )
        )
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.clone",
        actor_id=admin_id,
        target_type="exam",
        target_id=copy.id,
        after={"from": str(exam.id), "title": copy.title},
    )
    return await to_read(db, copy)


async def bulk(db: AsyncSession, payload: e_schemas.ExamBulkRequest, *, admin_id: uuid.UUID) -> dict:
    """One action over many papers, answered per id - never a silent partial win.

    Each row goes through the same status and trash rules as a single edit, so a selection that
    includes a paper somebody is sitting is refused with the reason rather than forced. The
    answer keeps `refused` next to `updated` because a teacher who selected twelve and got nine
    needs to know which three and why.
    """
    target_status: str | None = None
    if payload.action == "status":
        if not payload.status:
            raise ExamError(
                "choose the status to set: " + ", ".join(SETTABLE_STATUSES),
                code="exam_status_required",
                params={"allowed": ", ".join(SETTABLE_STATUSES)},
            )
        target_status = status_enum(payload.status).value

    rows = (await db.execute(select(Exam).where(Exam.id.in_(list(payload.exam_ids))))).scalars().all()
    found = {row.id: row for row in rows}
    not_found = [str(exam_id) for exam_id in payload.exam_ids if exam_id not in found]

    updated: list[str] = []
    refused: list[dict] = []
    for exam_id in payload.exam_ids:
        exam = found.get(exam_id)
        if exam is None:
            continue
        try:
            if payload.action == "status":
                await set_status(db, exam, str(target_status), admin_id=admin_id)
            elif payload.action == "trash":
                await trash(db, exam, admin_id=admin_id)
            else:
                await restore(db, exam, admin_id=admin_id)
        except (ExamError, NameTaken) as exc:
            refused.append({"id": str(exam_id), "reason": str(exc)})
            continue
        updated.append(str(exam_id))

    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.bulk",
        actor_id=admin_id,
        target_type="exam",
        detail=(
            f"{payload.action}: {len(updated)} updated, {len(refused)} refused, {len(not_found)} not found"
        ),
    )
    return {"action": payload.action, "updated": updated, "refused": refused, "not_found": not_found}


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


async def _next_section_position(db: AsyncSession, exam_id: uuid.UUID) -> int:
    rows = await sections_of(db, exam_id)
    return max((row.position for row in rows), default=-1) + 1


async def create_section(
    db: AsyncSession, exam: Exam, payload: e_schemas.SectionCreate, *, admin_id: uuid.UUID
) -> dict:
    if await is_locked(db, exam.id):
        raise LockedComposition(
            "learners have already sat this exam, so its sections are part of a result that "
            "has been given - make a copy to change it",
            code="exam_sections_locked",
        )
    position = payload.position if payload.position is not None else await _next_section_position(db, exam.id)
    section = ExamSection(
        exam_id=exam.id,
        title=payload.title,
        position=position,
        shuffle_items=payload.shuffle_items,
        config={"instructions": payload.instructions} if payload.instructions else {},
    )
    db.add(section)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.section.create",
        actor_id=admin_id,
        target_type="exam_section",
        target_id=section.id,
        after={"exam_id": str(exam.id), "title": section.title},
    )
    return {
        "id": str(section.id),
        "title": section.title,
        "position": section.position,
        "shuffle_items": section.shuffle_items,
        "instructions": payload.instructions,
        "item_count": 0,
        "points": 0.0,
    }


async def update_section(
    db: AsyncSession, section: ExamSection, payload: e_schemas.SectionUpdate, *, admin_id: uuid.UUID
) -> dict:
    given = payload.model_dump(exclude_unset=True)
    if "title" in given:
        section.title = given["title"]
    if "shuffle_items" in given:
        if await is_locked(db, section.exam_id):
            raise LockedComposition(
                "this exam has been sat, so the way this section orders its questions is part "
                "of a result that has been given",
                code="exam_section_shuffling_locked",
            )
        section.shuffle_items = bool(given["shuffle_items"])
    config = dict(section.config or {})
    if "instructions" in given:
        if given["instructions"]:
            config["instructions"] = given["instructions"]
        else:
            config.pop("instructions", None)
    section.config = config
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.section.update",
        actor_id=admin_id,
        target_type="exam_section",
        target_id=section.id,
        after={k: v for k, v in given.items()},
    )
    return {
        "id": str(section.id),
        "title": section.title,
        "position": section.position,
        "shuffle_items": section.shuffle_items,
        "instructions": config.get("instructions"),
    }


async def delete_section(db: AsyncSession, section: ExamSection, *, admin_id: uuid.UUID) -> dict:
    """Remove a section, keeping the questions that were inside it.

    Deleting a heading is not deleting a paper: the items move out to the top level rather than
    disappearing with it, because a teacher who removes "Part B" did not mean to drop twelve
    questions and every sitting that answered them.
    """
    exam = await get(db, section.exam_id)
    if await is_locked(db, exam.id):
        raise LockedComposition(
            "learners have already sat this exam, so its sections are part of a result that "
            "has been given - make a copy to change it",
            code="exam_sections_locked",
        )
    moved = (
        await db.execute(select(ExamItem).where(ExamItem.section_id == section.id))
    ).scalars().all()
    for item in moved:
        item.section_id = None
    await db.delete(section)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.section.delete",
        actor_id=admin_id,
        target_type="exam_section",
        target_id=section.id,
        after={"exam_id": str(exam.id), "items_moved_out": len(moved)},
    )
    return await to_read(db, exam)


async def reorder_sections(
    db: AsyncSession, exam: Exam, payload: e_schemas.SectionReorderRequest, *, admin_id: uuid.UUID
) -> dict:
    if await is_locked(db, exam.id):
        raise LockedComposition(
            "this exam has been sat, so its section order is history",
            code="exam_section_order_locked",
        )
    rows = {row.id: row for row in await sections_of(db, exam.id)}
    if set(payload.section_ids) != set(rows):
        raise ExamError(
            "the list has to name every section of this exam, once each",
            code="exam_section_order_incomplete",
        )
    for position, section_id in enumerate(payload.section_ids):
        rows[section_id].position = position
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.section.reorder",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        after={"section_ids": [str(value) for value in payload.section_ids]},
    )
    return await to_read(db, exam)


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #


async def _existing_references(db: AsyncSession, exam_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
    """Which questions are already on this paper, and which item put them there."""
    return {
        row[0]: row[1]
        for row in (
            await db.execute(select(ExamItem.ref_id, ExamItem.id).where(ExamItem.exam_id == exam_id))
        ).all()
    }


async def _question_ids_of_passage(
    db: AsyncSession, kind: str, ref_id: uuid.UUID, set_id: uuid.UUID | None
) -> list[uuid.UUID]:
    """Every ready question under a text or recording, or under one block of it.

    In the order the passage itself lists them, so a teacher who adds "block two" gets block
    two in its own reading order rather than in whatever the database returned. A question that
    is bound to the passage but not yet filed under a heading still belongs to it, so the
    whole-passage read is an outer join over the headings rather than an inner one.
    """
    passage_model, set_model, membership = _PASSAGE_MODEL[kind]
    passage = await db.get(passage_model, ref_id)
    if passage is None:
        raise ExamError("that reading or recording is not in the bank", code="exam_source_not_in_bank")
    if passage.deleted_at is not None:
        raise ExamError("that reading or recording is in the trash", code="exam_source_in_bin")
    if _label_of(passage.status) != "ready":
        raise ExamError(
            "that reading or recording is not ready for learners yet",
            code="exam_source_not_ready",
        )

    owner_column = "reading_id" if kind == "reading" else "listening_id"
    if set_id is not None:
        block = await db.get(set_model, set_id)
        if block is None or getattr(block, owner_column) != ref_id:
            raise ExamError(
                f"that block does not belong to this {kind} text",
                code="exam_block_not_in_source",
            )
        stmt = (
            select(membership.question_id)
            .join(Question, Question.id == membership.question_id)
            .where(
                membership.set_id == set_id,
                Question.deleted_at.is_(None),
                Question.status == enums.ContentStatus.READY,
            )
            .order_by(membership.position, membership.created_at)
        )
        return list((await db.execute(stmt)).scalars())

    stmt = (
        select(Question.id)
        .outerjoin(membership, membership.question_id == Question.id)
        .outerjoin(set_model, set_model.id == membership.set_id)
        .where(
            getattr(Question, owner_column) == ref_id,
            Question.deleted_at.is_(None),
            Question.status == enums.ContentStatus.READY,
        )
        .order_by(
            set_model.position.nulls_last(),
            membership.position.nulls_last(),
            Question.created_at,
        )
    )
    return list((await db.execute(stmt)).scalars())


async def add_items(
    db: AsyncSession, exam: Exam, payload: e_schemas.ItemsAdd, *, admin_id: uuid.UUID
) -> dict:
    """Put questions on the paper, each pinned to a version.

    A reading or a recording is expanded here rather than stored as one composite item,
    because an answer is keyed by an item and a paper needs each question marked separately.
    """
    if await is_locked(db, exam.id):
        raise LockedComposition(
            "learners have already sat this exam, so its questions are part of a result that "
            "has been given - make a copy to change it",
            code="exam_questions_locked",
        )
    existing = await _existing_references(db, exam.id)
    rows = await items_of(db, exam.id)
    position = max((row.position for row in rows), default=-1) + 1
    sections = {row.id: row for row in await sections_of(db, exam.id)}

    added: list[ExamItem] = []
    skipped: list[dict[str, Any]] = []
    for request in payload.items:
        kind = kind_of(request.kind)
        if request.section_id is not None and request.section_id not in sections:
            raise ExamError("that section belongs to a different exam", code="exam_section_other_paper")

        if kind == "question":
            candidates = [request.ref_id]
            wanted_version = request.version
        else:
            candidates = await _question_ids_of_passage(db, kind, request.ref_id, request.set_id)
            if not candidates:
                raise ExamError(
                    "that text has no ready questions to add",
                    code="exam_source_has_no_ready_questions",
                )
            wanted_version = None

        for question_id in candidates:
            if question_id in existing:
                raise DuplicateReference(
                    "this exam already asks that question",
                    code="exam_reference_exists",
                    existing_item_id=existing[question_id],
                )
            question = await db.get(Question, question_id)
            if question is None or question.deleted_at is not None:
                skipped.append({"ref_id": str(question_id), "reason": "not in the bank"})
                continue
            if _label_of(question.status) != "ready":
                skipped.append({"ref_id": str(question_id), "reason": f"the question is {_label_of(question.status)}"})
                continue
            if len(rows) + len(added) >= MAX_ITEMS:
                raise ExamError(
                    f"an exam holds {MAX_ITEMS} questions at most",
                    code="exam_item_limit",
                    params={"n": MAX_ITEMS},
                )

            version_number = wanted_version or question.current_version
            version = await _version_row(db, question.id, version_number)
            if version is None:
                # A question whose version rows were never written: the bank guarantees one at
                # creation, so this is a data gap and not something to grade around.
                skipped.append({"ref_id": str(question_id), "reason": f"version {version_number} is not recorded"})
                continue

            item = ExamItem(
                exam_id=exam.id,
                section_id=request.section_id if kind == "question" else _section_for(request, sections),
                kind=enums.ContentKind.QUESTION,
                ref_id=question.id,
                question_version_id=version.id,
                position=position,
                points=request.points if kind == "question" else None,
                config={
                    "added_from": kind,
                    **({"set_id": str(request.set_id)} if request.set_id else {}),
                    **({"passage_id": str(request.ref_id)} if kind != "question" else {}),
                },
            )
            db.add(item)
            await db.flush()
            added.append(item)
            existing[question.id] = item.id
            position += 1

    if not added:
        detail = "; ".join(f"{row['reason']}" for row in skipped[:3]) or "nothing could be added"
        raise ExamError(
            f"nothing was added to the exam: {detail}",
            code="exam_nothing_added",
            params={"detail": detail},
        )
    await audit_service.record_audit(
        db,
        action="exam.items.add",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        after={"added": len(added), "skipped": len(skipped)},
    )
    return {
        "exam_id": str(exam.id),
        "added": len(added),
        "item_ids": [str(item.id) for item in added],
        "skipped": skipped,
        "exam": await to_read(db, exam),
    }


def _section_for(request: e_schemas.ItemAdd, sections: dict[uuid.UUID, ExamSection]) -> uuid.UUID | None:
    """A passage added without a section still lands somewhere predictable: the last one."""
    if request.section_id is not None:
        return request.section_id
    if not sections:
        return None
    return max(sections.values(), key=lambda row: row.position).id


async def update_item(
    db: AsyncSession, item: ExamItem, payload: e_schemas.ItemUpdate, *, admin_id: uuid.UUID
) -> dict:
    """Re-mark an item or move it between sections.

    Refused once the paper has been sat: the mark each question carried is what the totals were
    made of, and a number that moves underneath a result is not a correction, it is a rewrite.
    """
    exam = await get(db, item.exam_id)
    if await is_locked(db, exam.id):
        raise LockedComposition(
            "learners have already sat this exam, so the marks on it are part of a result "
            "that has been given - make a copy to change it",
            code="exam_marks_locked",
        )
    given = payload.model_dump(exclude_unset=True)
    if "section_id" in given and given["section_id"] is not None:
        if given["section_id"] not in {row.id for row in await sections_of(db, exam.id)}:
            raise ExamError("that section belongs to a different exam", code="exam_section_other_paper")
    if "points" in given:
        # An explicit null is the teacher taking the override back off, which leaves the item
        # worth whatever the pinned version says it is worth.
        item.points = float(given["points"]) if given["points"] is not None else None
    if "section_id" in given:
        item.section_id = given["section_id"]
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.item.update",
        actor_id=admin_id,
        target_type="exam_item",
        target_id=item.id,
        after={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in given.items()},
    )
    rows = await resolve_items(db, [item])
    return rows[0]


async def remove_item(db: AsyncSession, item: ExamItem, *, admin_id: uuid.UUID) -> dict:
    exam = await get(db, item.exam_id)
    if await is_locked(db, exam.id):
        raise LockedComposition(
            "learners have already sat this exam, so its questions are part of a result that "
            "has been given - make a copy to change it",
            code="exam_questions_locked",
        )
    await db.delete(item)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.item.remove",
        actor_id=admin_id,
        target_type="exam_item",
        target_id=item.id,
        after={"exam_id": str(exam.id), "question_id": str(item.ref_id)},
    )
    return await to_read(db, exam)


async def reorder(
    db: AsyncSession, exam: Exam, payload: e_schemas.ReorderRequest, *, admin_id: uuid.UUID
) -> dict:
    if await is_locked(db, exam.id):
        raise LockedComposition(
            "this exam has been sat, so the order of it is history",
            code="exam_item_order_locked",
        )
    rows = {row.id: row for row in await items_of(db, exam.id)}
    scoped = (
        [row for row in rows.values() if row.section_id == payload.section_id]
        if payload.section_id is not None
        else list(rows.values())
    )
    if {str(row.id) for row in scoped} != {str(value) for value in payload.item_ids}:
        raise ExamError(
            "the list has to name every question on this exam, once each",
            code="exam_item_order_incomplete",
        )
    for position, item_id in enumerate(payload.item_ids):
        rows[item_id].position = position
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.items.reorder",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        after={"count": len(payload.item_ids), "section_id": str(payload.section_id) if payload.section_id else None},
    )
    return await to_read(db, exam)


# --------------------------------------------------------------------------- #
# The frozen composition
# --------------------------------------------------------------------------- #


def _option_order(seed: bytes, item_id: uuid.UUID, count: int) -> list[int]:
    """The order one item's options are displayed in, decided once and stored.

    Derived from the attempt's own seed and the item, so a resumed sitting shows the same
    layout; the learner's answer carries the authored index of the option they picked, which
    is why reordering the display cannot change how an answer is graded.
    """
    rng = random.Random(seed + item_id.bytes)
    order = list(range(count))
    rng.shuffle(order)
    return order


def _deal(entries: list[dict], sections: dict[uuid.UUID, ExamSection], *, shuffle_all: bool, seed: bytes) -> list[dict]:
    """Lay the paper out: parts in order, questions in the order this sitting sees them.

    A section is a part of a paper, so no shuffle ever interleaves two parts. `shuffle_questions`
    deals every part; `shuffle_items` deals one part on its own while the rest of the paper stays
    in the authored order. The two switches answer different questions a teacher can ask.
    """
    authored = sorted(entries, key=lambda row: row["authored_position"])
    groups: list[tuple[str, list[dict], bool]] = []

    loose = [row for row in authored if row["section_id"] is None]
    if loose:
        groups.append(("", loose, shuffle_all))
    for section in sorted(sections.values(), key=lambda row: row.position):
        inside = [row for row in authored if row["section_id"] == str(section.id)]
        if inside:
            groups.append((str(section.id), inside, shuffle_all or section.shuffle_items))

    dealt: list[dict] = []
    for key, group, shuffles in groups:
        if shuffles and len(group) > 1:
            group = list(group)
            random.Random(seed + key.encode()).shuffle(group)
        dealt.extend(group)
    return dealt


def _seed_from(hex_string: str | None) -> bytes | None:
    """Parse a caller-supplied seed, and say so plainly when it is not one.

    The preview accepts a seed so a teacher can look at the same layout twice; a value that is
    not sixteen bytes of hex is a typo, not a crash inside `bytes.fromhex`.
    """
    if not hex_string:
        return None
    try:
        seed = bytes.fromhex(hex_string)
    except ValueError as exc:
        raise ExamError("a seed is 32 hexadecimal characters", code="exam_seed_shape") from exc
    if len(seed) != 16:
        raise ExamError("a seed is 32 hexadecimal characters", code="exam_seed_shape")
    return seed


def _frozen_reading(reading: Reading) -> dict:
    """The text as one sitting of this paper will read it.

    A question's own wording is frozen in its version; the passage around it is not part of
    that row, so it is copied into the blueprint at the moment the sitting starts. Without
    this, a teacher editing a text halfway through an exam would change what the last learner
    is asked about and what the first one was asked about, on the same paper.
    """
    return {
        "kind": "reading",
        "id": str(reading.id),
        "title": reading.title,
        "body": reading.body,
        "layout": reading.layout,
        "language": reading.language,
        "level": reading.level,
    }


def _frozen_listening(listening: Listening, block: ListeningQuestionSet | None) -> dict:
    """The recording's own rules and text, copied for one sitting.

    `media_asset_id` is copied rather than a URL: the file a learner is served is decided
    per request by their session, and a frozen link would outlive its signature.
    """
    return {
        "kind": "listening",
        "id": str(listening.id),
        "title": listening.title,
        "media_asset_id": str(listening.media_asset_id) if listening.media_asset_id else None,
        "replay_limit": listening.replay_limit,
        "allow_pause": listening.allow_pause,
        "allow_seek": listening.allow_seek,
        "show_transcript": listening.show_transcript,
        "transcript": listening.transcript if listening.show_transcript else None,
        "language": listening.language,
        "level": listening.level,
        "start_seconds": block.start_seconds if block else None,
        "end_seconds": block.end_seconds if block else None,
        "block_title": block.title if block else None,
        "block_instructions": block.instructions if block else None,
    }


async def contexts_for(db: AsyncSession, items: list[ExamItem]) -> dict[uuid.UUID, dict | None]:
    """The passage each item travels with, keyed by exam item id.

    Read in a handful of queries for the whole paper, and the block a question was added under
    is resolved from the item's own config so a slice of a recording keeps its interval.
    """
    questions = {
        row.id: row
        for row in (
            await db.execute(
                select(Question).where(Question.id.in_([item.ref_id for item in items]))
            )
        ).scalars()
    }
    reading_ids = {row.reading_id for row in questions.values() if row.reading_id}
    listening_ids = {row.listening_id for row in questions.values() if row.listening_id}
    readings = {
        row.id: row
        for row in (
            await db.execute(select(Reading).where(Reading.id.in_(reading_ids)))
        ).scalars()
    } if reading_ids else {}
    listenings = {
        row.id: row
        for row in (
            await db.execute(select(Listening).where(Listening.id.in_(listening_ids)))
        ).scalars()
    } if listening_ids else {}
    set_ids = {
        uuid.UUID(value)
        for item in items
        if (value := (item.config or {}).get("set_id"))
    }
    blocks: dict[uuid.UUID, Any] = {}
    if set_ids:
        for model in (ReadingQuestionSet, ListeningQuestionSet):
            for row in (
                await db.execute(select(model).where(model.id.in_(set_ids)))
            ).scalars():
                blocks[row.id] = row

    out: dict[uuid.UUID, dict | None] = {}
    for item in items:
        question = questions.get(item.ref_id)
        if question is None:
            out[item.ref_id] = None
            continue
        set_id = (item.config or {}).get("set_id")
        block = blocks.get(uuid.UUID(set_id)) if set_id else None
        if question.reading_id and question.reading_id in readings:
            out[item.id] = _frozen_reading(readings[question.reading_id])
        elif question.listening_id and question.listening_id in listenings:
            out[item.id] = _frozen_listening(listenings[question.listening_id], block)
        else:
            out[item.id] = None
    return out


async def freeze_composition(
    db: AsyncSession, exam: Exam, *, seed: bytes | None = None
) -> dict:
    """The paper as one sitting of it will be served: order, versions, marks, option layouts.

    Shared by the teacher's preview and by the attempt engine, so what a teacher sees before
    publishing is drawn by the same code that hands a learner their paper - and a preview that
    flattered the paper would be a bug in the only place it matters. Nothing is written here:
    the blueprint is stored when a sitting starts, and the preview returns it without saving.
    """
    seed = seed or secrets.token_bytes(16)
    items = await items_of(db, exam.id)
    resolved = await resolve_items(db, items)
    by_id = {row["id"]: row for row in resolved}
    sections = {row.id: row for row in await sections_of(db, exam.id)}
    contexts = await contexts_for(db, items)
    snapshots = await snapshots_for_versions(
        db, [row.question_version_id for row in items if row.question_version_id]
    )

    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    frozen_contexts: dict[str, dict] = {}
    for item in items:
        row = by_id[str(item.id)]
        if not row["serveable"]:
            skipped.append(
                {"exam_item_id": str(item.id), "title": row["title"], "reason": row["state"]}
            )
            continue
        snapshot = snapshots.get(item.question_version_id) if item.question_version_id else None
        question_type = ((snapshot or {}).get("question") or {}).get("type")
        options = []
        if question_type in ("multiple_choice", "multi_select"):
            options = ((snapshot or {}).get("question") or {}).get("config", {}).get("options") or []
        context = contexts.get(item.id)
        if context:
            # The text or recording is copied into the sitting's own record once per passage, so
            # a teacher editing a document cannot change what an open attempt is asked about -
            # and twelve questions of one 600-word text do not store twelve copies of it.
            frozen_contexts[context["id"]] = context
        entry: dict[str, Any] = {
            "exam_item_id": str(item.id),
            "kind": _label_of(item.kind),
            "ref_id": str(item.ref_id),
            "question_version_id": str(item.question_version_id) if item.question_version_id else None,
            "version": row["version"],
            "section_id": str(item.section_id) if item.section_id else None,
            "section_title": sections[item.section_id].title if item.section_id in sections else None,
            "points": row["effective_points"],
            "authored_position": item.position,
            "type": question_type,
            "title": row["title"],
            "context_id": str(row["context_id"]) if row["context_id"] else None,
            "context_title": row["context_title"],
        }
        if exam.shuffle_options and len(options) > 1:
            entry["option_order"] = _option_order(seed, item.id, len(options))
        entries.append(entry)

    entries = _deal(entries, sections, shuffle_all=bool(exam.shuffle_questions), seed=seed)
    for position, entry in enumerate(entries):
        entry["position"] = position

    rules = rules_payload(exam)
    return {
        "blueprint_schema": BLUEPRINT_SCHEMA,
        "seed": seed.hex(),
        "exam_id": str(exam.id),
        "title": exam.title,
        "status": _label_of(exam.status),
        "rules": rules,
        "items": entries,
        "contexts": frozen_contexts,
        "count": len(entries),
        "points": round(sum(entry["points"] for entry in entries), 4),
        "skipped": skipped,
    }


async def preview(db: AsyncSession, exam: Exam, *, seed_hex: str | None = None) -> dict:
    """What a sitting would look like, without starting one."""
    blueprint = await freeze_composition(db, exam, seed=_seed_from(seed_hex))
    return e_schemas.ExamPreviewRead(
        exam_id=exam.id,
        title=blueprint["title"],
        seed=blueprint["seed"],
        item_count=blueprint["count"],
        points=blueprint["points"],
        duration_minutes=blueprint["rules"]["duration_minutes"],
        opens_at=blueprint["rules"]["available_from"],
        closes_at=blueprint["rules"]["available_to"],
        items=[
            e_schemas.PreviewItemRead(
                position=entry["position"],
                kind=entry["kind"],
                ref_id=uuid.UUID(entry["ref_id"]),
                exam_item_id=uuid.UUID(entry["exam_item_id"]),
                title=entry.get("title"),
                version=entry["version"],
                points=entry["points"],
                section_title=entry["section_title"],
                context_id=uuid.UUID(entry["context_id"]) if entry["context_id"] else None,
                context_title=entry["context_title"],
                shuffled_options=entry.get("option_order"),
            ).model_dump(mode="json")
            for entry in blueprint["items"]
        ],
        skipped=blueprint["skipped"],
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Assignments
# --------------------------------------------------------------------------- #


def student_name(student: Student) -> str:
    return f"{student.name} {student.surname}".strip()


async def _sitting_counts(db: AsyncSession, exam_id: uuid.UUID) -> dict[uuid.UUID, tuple[int, int]]:
    """How many sittings each learner of their own has of this paper, and how many were handed in."""
    out: dict[uuid.UUID, tuple[int, int]] = {}
    for student_id, status, count in (
        await db.execute(
            select(ExamAttempt.student_id, ExamAttempt.status, func.count())
            .where(ExamAttempt.exam_id == exam_id)
            .group_by(ExamAttempt.student_id, ExamAttempt.status)
        )
    ).all():
        total, submitted = out.get(student_id, (0, 0))
        handed_in = _label_of(status) in ("submitted", "auto_submitted")
        out[student_id] = (
            total + int(count),
            submitted + int(count) if handed_in else submitted,
        )
    return out


async def list_assignments(db: AsyncSession, exam: Exam) -> list[dict]:
    """Who this paper is for, with the name of each one read from its own row.

    A group row reports the sittings of everyone currently in that group. It is counted from the
    membership table rather than from the attempts' own `group_id`, because a learner who moved
    class last month sat the paper as themselves - the group is who is being asked now.
    """
    rows = list(
        (
            await db.execute(
                select(ExamAssignment).where(ExamAssignment.exam_id == exam.id).order_by(ExamAssignment.created_at)
            )
        ).scalars()
    )
    if not rows:
        return []
    student_ids = [row.student_id for row in rows if row.student_id]
    group_ids = [row.group_id for row in rows if row.group_id]
    students = {
        row.id: row
        for row in (
            await db.execute(select(Student).where(Student.id.in_(student_ids)))
        ).scalars()
    } if student_ids else {}
    groups = {
        row.id: row
        for row in (
            await db.execute(select(Group).where(Group.id.in_(group_ids)))
        ).scalars()
    } if group_ids else {}
    members: dict[uuid.UUID, list[uuid.UUID]] = {}
    if group_ids:
        for group_id, student_id in (
            await db.execute(
                select(GroupMembership.group_id, GroupMembership.student_id).where(
                    GroupMembership.group_id.in_(group_ids)
                )
            )
        ).all():
            members.setdefault(group_id, []).append(student_id)
    counts = await _sitting_counts(db, exam.id)

    out: list[dict] = []
    for row in rows:
        if row.student_id is not None:
            student = students.get(row.student_id)
            name = student_name(student) if student else None
            reachable = student is not None and student.deleted_at is None
            kind = "student"
            mine = [row.student_id]
        else:
            group = groups.get(row.group_id)
            name = group.name if group else None
            reachable = group is not None and group.deleted_at is None
            kind = "group"
            mine = members.get(row.group_id, [])
        attempts = sum(counts.get(student_id, (0, 0))[0] for student_id in mine)
        submitted = sum(counts.get(student_id, (0, 0))[1] for student_id in mine)
        out.append(
            e_schemas.AssignmentRead(
                id=row.id,
                exam_id=row.exam_id,
                student_id=row.student_id,
                group_id=row.group_id,
                name=name,
                kind=kind,
                reachable=reachable,
                member_ids=mine,
                attempts=attempts,
                submitted=submitted,
                created_at=_iso(row.created_at) or "",
            ).model_dump(mode="json")
        )
    return out


async def assign(
    db: AsyncSession, exam: Exam, payload: e_schemas.AssignmentCreate, *, admin_id: uuid.UUID
) -> dict:
    """Hand the paper out.

    Every name is checked before any row is written, so a typo in one id cannot leave a half
    assignment the teacher then has to work out which part landed.
    """
    if not payload.student_ids and not payload.group_ids:
        # The screen keeps the button off until somebody is chosen; this is the same rule at the
        # door, so an empty hand-out is a refusal rather than a success that wrote nothing.
        raise ExamError(
            "nobody was chosen to receive this paper",
            code="exam_no_audience",
        )
    if _label_of(exam.status) == "draft":
        raise ExamError(
            "a draft exam has nothing to hand out - publish it, or schedule it first",
            code="exam_draft_not_assignable",
        )
    if exam.deleted_at is not None:
        raise ExamError("this exam is in the trash", code="exam_binned_not_assignable")

    students = list((await db.execute(select(Student).where(Student.id.in_(payload.student_ids)))).scalars()) if payload.student_ids else []
    missing_students = [str(value) for value in payload.student_ids if value not in {row.id for row in students}]
    if missing_students:
        raise ExamError(
            f"{len(missing_students)} of those students are not in the school",
            code="exam_students_unknown",
            params={"n": len(missing_students)},
        )
    binned = [student_name(row) for row in students if row.deleted_at is not None]
    if binned:
        raise ExamError(
            f"these students are in the trash: {', '.join(binned)}",
            code="exam_students_in_bin",
            params={"names": ", ".join(binned)},
        )

    groups = list((await db.execute(select(Group).where(Group.id.in_(payload.group_ids)))).scalars()) if payload.group_ids else []
    missing_groups = [str(value) for value in payload.group_ids if value not in {row.id for row in groups}]
    if missing_groups:
        raise ExamError(
            f"{len(missing_groups)} of those groups do not exist",
            code="exam_groups_unknown",
            params={"n": len(missing_groups)},
        )
    binned_groups = [row.name for row in groups if row.deleted_at is not None]
    if binned_groups:
        raise ExamError(
            f"these groups are in the trash: {', '.join(binned_groups)}",
            code="exam_groups_in_bin",
            params={"names": ", ".join(binned_groups)},
        )

    already = {
        (row.student_id, row.group_id)
        for row in (
            await db.execute(select(ExamAssignment).where(ExamAssignment.exam_id == exam.id))
        ).scalars()
    }
    created: list[ExamAssignment] = []
    repeated: list[str] = []
    for student in students:
        if (student.id, None) in already:
            repeated.append(student_name(student))
            continue
        row = ExamAssignment(exam_id=exam.id, student_id=student.id)
        db.add(row)
        created.append(row)
        already.add((student.id, None))
    for group in groups:
        if (None, group.id) in already:
            repeated.append(group.name)
            continue
        row = ExamAssignment(exam_id=exam.id, group_id=group.id)
        db.add(row)
        created.append(row)
        already.add((None, group.id))
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ExamError(
            "some of those were assigned in the same moment; the list is up to date",
            code="exam_assignment_race",
        ) from exc
    await audit_service.record_audit(
        db,
        action="exam.assign",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        after={"students": len(students), "groups": len(groups), "created": len(created)},
    )
    return {
        "exam_id": str(exam.id),
        "created": len(created),
        "already_assigned": repeated,
        "assignments": await list_assignments(db, exam),
    }


async def unassign(db: AsyncSession, assignment: ExamAssignment, *, admin_id: uuid.UUID) -> dict:
    """Take the paper away from one name.

    Sittings already under way are not cancelled by it: removing an assignment says nobody
    else should start this paper, not that what happened was undone. A learner mid-sitting on a
    removed assignment can finish the attempt they already opened.
    """
    exam = await get(db, assignment.exam_id)
    await db.delete(assignment)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.unassign",
        actor_id=admin_id,
        target_type="exam",
        target_id=exam.id,
        after={
            "assignment_id": str(assignment.id),
            "student_id": str(assignment.student_id) if assignment.student_id else None,
            "group_id": str(assignment.group_id) if assignment.group_id else None,
        },
    )
    return {"removed": True, "exam_id": str(exam.id)}


async def get_assignment(db: AsyncSession, assignment_id: uuid.UUID) -> ExamAssignment:
    row = await db.get(ExamAssignment, assignment_id)
    if row is None:
        raise ExamNotFound("no such assignment", code="exam_assignment_not_found")
    return row


async def assigned_exams(db: AsyncSession, student: Student) -> list[Exam]:
    """The papers reaching one learner: named directly, or through a group they belong to.

    One query over both routes, deduplicated by exam id. A learner in a group that is named
    alongside them personally still sees the paper once.
    """
    group_ids = list(
        (
            await db.execute(
                select(GroupMembership.group_id).where(GroupMembership.student_id == student.id)
            )
        ).scalars()
    )
    clauses = [ExamAssignment.student_id == student.id]
    if group_ids:
        clauses.append(ExamAssignment.group_id.in_(group_ids))
    rows = list(
        (
            await db.execute(
                select(Exam)
                .join(ExamAssignment, ExamAssignment.exam_id == Exam.id)
                .where(or_(*clauses), Exam.deleted_at.is_(None))
                .distinct()
                .order_by(Exam.available_from.asc().nulls_last(), Exam.title.asc())
            )
        ).scalars()
    )
    return rows
