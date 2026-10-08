"""Practice: what a learner does with a catalog (Phase 6).

A catalog is a list of references; this module turns it into something a learner can work
through, and writes down what they did. Three decisions shape all of it.

**A run is a token, not a row.** Opening a catalog issues a `session_id` and writes one
start event; every answer carries that token. There is no practice-session table, because
there is nothing to freeze: practice always serves the *current* content, so a run opened
today and picked up next week is answered against the exercises as they are then. That is
the deliberate difference from an exam, which pins a `QuestionVersion` per item because a
score that was awarded has to stay reproducible. The summary of a run is therefore rebuilt
from its events on every request rather than read from a stored total.

**Answers are authorised against the catalog that was opened, not against the body.** The
learner names a session and a question; the catalog comes from the start event this backend
wrote. A question is answerable only if the catalog reaches it - as a question of its own,
or as a question inside a reading or recording the catalog names, and inside the one block
of it when the catalog named a block. Without that, "practise this catalog" would be a door
to every ready question in the school.

**The verdict follows the teacher's feedback timing.** `instant` grades on submit and says
so; `after_session` records the answer and returns only that it was recorded, with the
verdicts arriving when the run is finished. The grader's `detail` is stored in the event
payload and never projected back to the learner, because it is where the engine keeps the
things that decide correctness - the index of the right option among them.

A learner's own marks (`known` / `learning`) and their saved favorites are written to the
same log and the same table the rest of the platform reads, so Phase 9's timeline and
Phase 10's analytics look at these rows rather than at a parallel copy.
"""
from __future__ import annotations

import random
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums
from app.models.activity import ActivityEvent, Favorite
from app.models.assessment import Catalog, CatalogItem
from app.models.content import (
    Listening,
    ListeningSetQuestion,
    Question,
    Reading,
    ReadingSetQuestion,
    VocabularyEntry,
)
from app.models.identity import Student
from app.schemas import practice as pr_schemas
from app.services import (
    activity_service,
    catalog_service,
    listening_service,
    passage_service,
    question_service,
    reading_service,
    vocabulary_service,
)

#: How many of a learner's own runs the catalog screen lists. A practice history is a
#: motivator, not an archive: the whole log stays in `activity_event` for Phase 10.
RECENT_RUNS = 10

#: `human_summary` stays empty on purpose. The sentence a teacher reads is rendered by
#: their screen, in their own language, from `event_type` and `payload`; a string frozen
#: into the row here could only ever be in one of the four.
_NO_SUMMARY = None

_PASSAGE_MODEL = {"reading": Reading, "listening": Listening}
_PASSAGE_MEMBERSHIP = {"reading": ReadingSetQuestion, "listening": ListeningSetQuestion}
_SORTABLE = {"name": Catalog.name, "created_at": Catalog.created_at, "updated_at": Catalog.updated_at}


class PracticeError(ValueError):
    """A learner-facing refusal with a sentence they can act on; the endpoints 422 it."""


class PracticeNotFound(Exception):
    """No such catalog, run, question or card for this learner.

    Deliberately vague: a learner is not told whether a draft exists, only that they cannot
    open it. "Not found" is the same answer a stranger's id would get.
    """


def _label_of(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# --------------------------------------------------------------------------- #
# What a learner may see
# --------------------------------------------------------------------------- #


async def visible_catalogs(
    db: AsyncSession,
    student: Student,
    *,
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    sort: str = "name",
    order: str = "asc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """The catalogs this learner can open, with what is inside each.

    Availability is a chain, so this reads the ready, un-trashed catalogs in one query and
    keeps the ones whose every ancestor is in that same set: a folder the teacher left as a
    draft takes its whole subtree off the list, which is what "not published" has to mean.
    The table is a school's practice library - tens of rows, not millions - so walking the
    chain in Python is one query instead of a recursive one per row.
    """
    if sort not in _SORTABLE:
        raise PracticeError("sort must be one of: " + ", ".join(sorted(_SORTABLE)))
    if order not in ("asc", "desc"):
        raise PracticeError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    rows = (
        await db.execute(
            select(Catalog).where(
                Catalog.deleted_at.is_(None),
                Catalog.status == enums.ContentStatus.READY,
            )
        )
    ).scalars().all()
    live = {row.id: row for row in rows}
    catalogs = [row for row in rows if _reachable(row, live)]

    if q:
        needle = q.strip().lower()
        catalogs = [
            row for row in catalogs if needle in row.name.lower() or needle in (row.description or "").lower()
        ]
    if language:
        wanted_language = language.strip().lower()
        catalogs = [row for row in catalogs if row.learning_language == wanted_language]
    if level:
        wanted_level = level.strip().upper()
        catalogs = [row for row in catalogs if row.level == wanted_level]

    attribute = _SORTABLE[sort].key
    catalogs.sort(key=lambda row: _sort_key(row, attribute), reverse=(order == "desc"))

    window = catalogs[(page - 1) * page_size : page * page_size]
    stats = await catalog_service.stats_for(db, [row.id for row in window])
    runs = await _run_counts(db, student.id, [row.id for row in window])
    items = [
        pr_schemas.CatalogRowRead(
            id=row.id,
            name=row.name,
            description=row.description,
            parent_id=row.parent_id,
            learning_language=row.learning_language,
            level=row.level,
            item_count=stats[row.id]["item_count"],
            counts=stats[row.id]["counts"],
            child_count=stats[row.id]["child_count"],
            shuffle_default=bool(row.shuffle_default),
            known_states_enabled=bool(row.known_states_enabled),
            feedback_timing=catalog_service.feedback_timing_of(row),
            runs=runs.get(row.id, 0),
        ).model_dump(mode="json")
        for row in window
    ]
    return {"items": items, "total": len(catalogs), "page": page, "page_size": page_size}


def _reachable(row: Catalog, live: dict[uuid.UUID, Catalog]) -> bool:
    """Every folder above this catalog is itself ready and un-trashed."""
    seen = {row.id}
    parent_id = row.parent_id
    while parent_id is not None:
        if parent_id in seen:
            return False
        parent = live.get(parent_id)
        if parent is None:
            return False
        seen.add(parent_id)
        parent_id = parent.parent_id
    return True


def _sort_key(row: Catalog, attribute: str) -> tuple[bool, Any, str]:
    """Sort with the empties last rather than comparing a `None` against a timestamp."""
    value = getattr(row, attribute)
    return (value is None, value, row.name)


async def _run_counts(
    db: AsyncSession, student_id: uuid.UUID, catalog_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """How many runs this learner has opened per catalog, in one grouped query."""
    if not catalog_ids:
        return {}
    rows = (
        await db.execute(
            select(ActivityEvent.context_id, func.count())
            .where(
                ActivityEvent.student_id == student_id,
                ActivityEvent.event_type == activity_service.EVENT_RUN_START,
                ActivityEvent.context_type == "catalog",
                ActivityEvent.context_id.in_(catalog_ids),
            )
            .group_by(ActivityEvent.context_id)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


async def get_visible(db: AsyncSession, catalog_id: uuid.UUID) -> Catalog:
    """The catalog a learner may open, or a 404 that does not explain itself."""
    row = await db.get(Catalog, catalog_id)
    if row is None or not await catalog_service.visible_to_learner(db, row):
        raise PracticeNotFound("Practice not found")
    return row


async def catalog_detail(db: AsyncSession, catalog: Catalog, student: Student) -> dict:
    """One catalog as a learner sees it: what is inside, and what they have done here."""
    stats = await catalog_service.stats_for(db, [catalog.id])
    one = stats[catalog.id]
    return {
        "id": str(catalog.id),
        "name": catalog.name,
        "description": catalog.description,
        "learning_language": catalog.learning_language,
        "level": catalog.level,
        "shuffle_default": bool(catalog.shuffle_default),
        "known_states_enabled": bool(catalog.known_states_enabled),
        "feedback_timing": catalog_service.feedback_timing_of(catalog),
        "item_count": one["item_count"],
        "counts": one["counts"],
        "path": await catalog_service.path_of(db, catalog),
        "runs": await recent_runs(db, student, catalog, limit=RECENT_RUNS),
    }


async def meta(db: AsyncSession) -> dict:
    """The lists a learner's own screens are built from - the same ones the teacher's are."""
    return {
        "learning_languages": await passage_service.enabled_languages(db),
        "levels": list(constants.CEFR_LEVELS),
        "sortable": sorted(_SORTABLE),
        "known_states": list(pr_schemas.KNOWN_STATES),
        "favorite_kinds": list(pr_schemas.FAVORITE_KINDS),
        "feedback_timings": ["instant", "after_session"],
    }


# --------------------------------------------------------------------------- #
# Opening a run
# --------------------------------------------------------------------------- #


async def _build_steps(
    db: AsyncSession,
    catalog: Catalog,
    *,
    shuffle: bool,
    language: str | None,
    served_to_admin: bool = False,
) -> tuple[list[dict], int]:
    """The servable steps of a catalog, in the order they will be worked through.

    Returns the steps and how many references had to be left out. `served_to_admin` picks
    the media paths of the session asking: a teacher previewing a run gets the teacher's
    own `/media` URLs, because the learner's paths would answer their browser with a 403.
    """
    items = (
        await db.execute(
            select(CatalogItem)
            .where(CatalogItem.catalog_id == catalog.id)
            .order_by(CatalogItem.position, CatalogItem.id)
        )
    ).scalars().all()

    steps: list[dict] = []
    skipped = 0
    for item in items:
        built = await _step_for(db, item, language=language, served_to_admin=served_to_admin)
        if built is None:
            skipped += 1
        else:
            steps.append(built)
    if shuffle:
        steps = _shuffled(steps, secrets.token_bytes(16))
    for position, step in enumerate(steps):
        step["position"] = position
    return steps, skipped


async def start_run(
    db: AsyncSession,
    catalog: Catalog,
    student: Student,
    *,
    shuffle: bool | None = None,
    language: str | None = None,
) -> dict:
    """Serve the catalog as a run, and write the start event that owns the token.

    A reference whose content cannot be served - a draft, something in the trash, a block
    that has been deleted - is skipped and counted, never served and never worked around.
    The learner is told how many steps were left out, because a run that silently has three
    fewer exercises than its catalog says it has looks like a save that failed.
    """
    mixed = bool(catalog.shuffle_default if shuffle is None else shuffle)
    token = secrets.token_hex(16)
    seed = secrets.token_bytes(16)
    steps, skipped = await _build_steps(db, catalog, shuffle=False, language=language)
    if mixed:
        steps = _shuffled(steps, seed)
        for position, step in enumerate(steps):
            step["position"] = position

    started = await activity_service.record(
        db,
        student=student,
        event_type=activity_service.EVENT_RUN_START,
        session_id=token,
        human_summary=_NO_SUMMARY,
        context_type="catalog",
        context_id=catalog.id,
        payload={
            "shuffle": mixed,
            # Stored so a run picked up later can be served in the order it was opened in,
            # rather than reshuffled under a learner who had got to step seven.
            "shuffle_seed": seed.hex() if mixed else None,
            "steps": len(steps),
            "skipped": skipped,
            "feedback_timing": catalog_service.feedback_timing_of(catalog),
        },
    )

    return pr_schemas.RunRead(
        session_id=token,
        catalog_id=catalog.id,
        catalog_name=catalog.name,
        feedback_timing=catalog_service.feedback_timing_of(catalog),
        shuffle=mixed,
        known_states_enabled=bool(catalog.known_states_enabled),
        skipped_count=skipped,
        steps=[pr_schemas.StepRead(**step) for step in steps],
        started_at=_iso(started.occurred_at) or "",
    ).model_dump(mode="json")


async def preview_run(
    db: AsyncSession, catalog: Catalog, *, shuffle: bool | None = None, language: str | None = None
) -> dict:
    """What a learner would be served, on the teacher's own screen.

    Nothing is written: a preview is the teacher deciding whether to publish, and a run
    event for a catalog nobody opened would be a row in a learner's history that no learner
    produced. There is no `session_id` in the answer for the same reason - answers are
    authorised against a run this backend issued.
    """
    mixed = bool(catalog.shuffle_default if shuffle is None else shuffle)
    steps, skipped = await _build_steps(
        db, catalog, shuffle=mixed, language=language, served_to_admin=True
    )
    return {
        "catalog_id": str(catalog.id),
        "catalog_name": catalog.name,
        "feedback_timing": catalog_service.feedback_timing_of(catalog),
        "shuffle": mixed,
        "known_states_enabled": bool(catalog.known_states_enabled),
        "skipped_count": skipped,
        "steps": [pr_schemas.StepRead(**step).model_dump(mode="json") for step in steps],
    }


def _shuffled(steps: list[dict], seed: bytes) -> list[dict]:
    """A reproducible shuffle.

    `random.Random(seed)` rather than the module-level generator: one learner's order must
    not depend on how many others opened a run in the same process, and the seed is on the
    start event, so the order can be rebuilt from the log alone.
    """
    ordered = list(steps)
    random.Random(seed).shuffle(ordered)
    return ordered


async def _step_for(
    db: AsyncSession, item: CatalogItem, *, language: str | None, served_to_admin: bool = False
) -> dict | None:
    """One reference as a learner's step, or None when it cannot be served."""
    kind = _label_of(item.kind)
    base = {"item_id": item.id, "kind": kind, "ref_id": item.ref_id, "position": item.position}
    set_id = (item.config or {}).get("set_id")

    if kind == "question":
        question = await db.get(Question, item.ref_id)
        if not await _serveable(db, question):
            return None
        view = await question_service.student_view(
            db, question, include_context=True, served_to_admin=served_to_admin
        )
        return {**base, "view": view}

    if kind == "vocabulary":
        entry = await db.get(VocabularyEntry, item.ref_id)
        if entry is None or not vocabulary_service.is_learner_visible(entry):
            return None
        view = await vocabulary_service.learner_view(
            db, entry, language=language, served_to_admin=served_to_admin
        )
        return {**base, "view": view}

    if kind in _PASSAGE_MODEL:
        row = await db.get(_PASSAGE_MODEL[kind], item.ref_id)
        if row is None or not passage_service.is_learner_visible(row):
            return None
        only = [uuid.UUID(str(set_id))] if set_id else None
        if kind == "reading":
            view = await reading_service.learner_detail(
                db, row, served_to_admin=served_to_admin, only_set_ids=only
            )
        else:
            view = await listening_service.learner_detail(
                db, row, served_to_admin=served_to_admin, only_set_ids=only
            )
        # A block that holds no ready question serves nothing: the reference has gone stale,
        # and an empty step is a screen with a spinner on it.
        if not view["sets"] and not view.get("unfiled"):
            return None
        return {**base, "view": view}

    return None


async def _serveable(db: AsyncSession, question: Question | None) -> bool:
    """Ready, un-trashed, and - when it belongs to a passage - that passage is open too.

    The second half is what stops a published question from carrying an unpublished text
    into a learner's screen inside its own `context`.
    """
    if question is None or question.deleted_at is not None:
        return False
    if _label_of(question.status) != "ready":
        return False
    passage_id = question.reading_id or question.listening_id
    if passage_id is None:
        return True
    kind = "reading" if question.reading_id is not None else "listening"
    passage = await db.get(_PASSAGE_MODEL[kind], passage_id)
    return passage is not None and passage_service.is_learner_visible(passage)


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #


async def _run_start(db: AsyncSession, student: Student, session_id: str) -> ActivityEvent:
    row = (
        await db.execute(
            select(ActivityEvent)
            .where(
                ActivityEvent.student_id == student.id,
                ActivityEvent.session_id == session_id,
                ActivityEvent.event_type == activity_service.EVENT_RUN_START,
            )
            .order_by(ActivityEvent.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise PracticeNotFound("Practice session not found")
    return row


async def _serving_item(db: AsyncSession, catalog_id: uuid.UUID, question: Question) -> CatalogItem | None:
    """The reference through which this question may be answered, or None.

    A question bound to a passage needs that passage to be open to learners whichever way
    it was reached, and when the catalog named one block of it the question has to be in
    that block: "do block two again" must not quietly become "do the whole recording".
    """
    passage_kind = None
    passage_id = None
    if question.reading_id is not None:
        passage_kind, passage_id = "reading", question.reading_id
    elif question.listening_id is not None:
        passage_kind, passage_id = "listening", question.listening_id

    if passage_kind is not None:
        passage = await db.get(_PASSAGE_MODEL[passage_kind], passage_id)
        if passage is None or not passage_service.is_learner_visible(passage):
            return None
        item = (
            await db.execute(
                select(CatalogItem).where(
                    CatalogItem.catalog_id == catalog_id,
                    CatalogItem.kind == enums.ContentKind(passage_kind),
                    CatalogItem.ref_id == passage_id,
                )
            )
        ).scalars().first()
        if item is not None:
            set_id = (item.config or {}).get("set_id")
            if not set_id or await _filed_under(db, passage_kind, uuid.UUID(str(set_id)), question.id):
                return item

    return (
        await db.execute(
            select(CatalogItem).where(
                CatalogItem.catalog_id == catalog_id,
                CatalogItem.kind == enums.ContentKind.QUESTION,
                CatalogItem.ref_id == question.id,
            )
        )
    ).scalars().first()


async def _filed_under(db: AsyncSession, passage_kind: str, set_id: uuid.UUID, question_id: uuid.UUID) -> bool:
    membership = _PASSAGE_MEMBERSHIP[passage_kind]
    found = (
        await db.execute(
            select(membership.id).where(membership.set_id == set_id, membership.question_id == question_id)
        )
    ).scalar_one_or_none()
    return found is not None


async def answer(db: AsyncSession, student: Student, payload: pr_schemas.AnswerRequest) -> dict:
    """Grade one answer, write it to the log, and say only what the timing allows."""
    start = await _run_start(db, student, payload.session_id)
    catalog = await db.get(Catalog, start.context_id)
    if catalog is None or not await catalog_service.visible_to_learner(db, catalog):
        raise PracticeNotFound("Practice not found")

    question = await db.get(Question, payload.question_id)
    if question is None or not await _serveable(db, question):
        raise PracticeNotFound("Question not found")
    item = await _serving_item(db, catalog.id, question)
    if item is None:
        # The question exists; it is just not in the run they opened. Saying that is the
        # only sentence that explains why nothing was recorded.
        raise PracticeError("that question is not part of this practice")

    grade = await question_service.grade_response(db, question, payload.response)
    withheld = catalog_service.feedback_timing_of(catalog) != "instant"

    await activity_service.record(
        db,
        student=student,
        event_type=activity_service.EVENT_ANSWER,
        category=activity_service.CATEGORY_ASSESSMENT,
        session_id=payload.session_id,
        human_summary=_NO_SUMMARY,
        context_type="catalog",
        context_id=catalog.id,
        ref_question_id=question.id,
        correct=grade["correct"],
        time_spent_seconds=payload.time_spent_seconds,
        payload={
            # `detail` is the grader's own reasoning and can name the right option, so it is
            # kept for the teacher and never projected to the learner.
            "grade": grade,
            "question_version": grade.get("version"),
            "item_kind": _label_of(item.kind),
            "withheld": withheld,
        },
    )

    revealed: dict[str, Any] = {}
    if not withheld:
        revealed = {
            "correct": grade["correct"],
            "score": grade["score"],
            "max_score": grade["max_score"],
            "requires_manual": bool(grade["requires_manual"]),
            "explanation": question.explanation or None,
        }
    return pr_schemas.AnswerRead(
        session_id=payload.session_id,
        question_id=question.id,
        withheld=withheld,
        **revealed,
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# The run, read back from its own events
# --------------------------------------------------------------------------- #


@dataclass
class _RunData:
    """Everything one or more runs need, fetched in three queries rather than per run."""

    starts: dict[str, ActivityEvent] = field(default_factory=dict)
    finishes: dict[str, ActivityEvent] = field(default_factory=dict)
    #: Newest answer per (session, question): practising means trying again, and a retry
    #: that was right must not be averaged away by the attempt before it.
    answers: dict[str, dict[uuid.UUID, ActivityEvent]] = field(default_factory=dict)
    questions: dict[uuid.UUID, Question] = field(default_factory=dict)


async def _load_runs(db: AsyncSession, student: Student, session_ids: list[str]) -> _RunData:
    data = _RunData()
    if not session_ids:
        return data
    rows = (
        await db.execute(
            select(ActivityEvent)
            .where(
                ActivityEvent.student_id == student.id,
                ActivityEvent.session_id.in_(session_ids),
                ActivityEvent.event_type.in_(
                    [activity_service.EVENT_RUN_START, activity_service.EVENT_RUN_FINISH]
                ),
            )
            .order_by(ActivityEvent.id)
        )
    ).scalars().all()
    for row in rows:
        bucket = data.starts if row.event_type == activity_service.EVENT_RUN_START else data.finishes
        bucket.setdefault(row.session_id, row)

    answers = (
        await db.execute(
            select(ActivityEvent)
            .where(
                ActivityEvent.student_id == student.id,
                ActivityEvent.session_id.in_(session_ids),
                ActivityEvent.event_type == activity_service.EVENT_ANSWER,
            )
            .order_by(ActivityEvent.occurred_at, ActivityEvent.id)
        )
    ).scalars().all()
    question_ids: set[uuid.UUID] = set()
    for row in answers:
        if row.session_id is None or row.ref_question_id is None:
            continue
        data.answers.setdefault(row.session_id, {})[row.ref_question_id] = row
        question_ids.add(row.ref_question_id)
    if question_ids:
        questions = (await db.execute(select(Question).where(Question.id.in_(question_ids)))).scalars().all()
        data.questions = {row.id: row for row in questions}
    return data


def _totals(answers: dict[uuid.UUID, ActivityEvent], *, reveal: bool) -> dict[str, Any]:
    score = 0.0
    max_score = 0.0
    correct = 0
    partial = 0
    incorrect = 0
    manual = 0
    for event in answers.values():
        grade = dict((event.payload or {}).get("grade") or {})
        earned = float(grade.get("score") or 0.0)
        score += earned
        max_score += float(grade.get("max_score") or 0.0)
        if grade.get("requires_manual"):
            manual += 1
        elif grade.get("correct") is True:
            correct += 1
        elif grade.get("correct") is False:
            # `correct` says full marks or not, so a line that took part of its mark would
            # otherwise be tallied as wrong while the same line above shows "1 of 2".
            if earned > 0:
                partial += 1
            else:
                incorrect += 1
    return {
        "answered": len(answers),
        "correct_count": correct if reveal else 0,
        "partial_count": partial if reveal else 0,
        "incorrect_count": incorrect if reveal else 0,
        "manual_count": manual,
        "score": round(score, 6) if reveal else 0.0,
        "max_score": round(max_score, 6) if reveal else 0.0,
    }


async def build_summary(db: AsyncSession, student: Student, session_id: str, *, reveal: bool = True) -> dict:
    """One run's summary, rebuilt from its events - with a result line per question."""
    data = await _load_runs(db, student, [session_id])
    if session_id not in data.starts:
        raise PracticeNotFound("Practice session not found")
    start = data.starts[session_id]
    catalog = await db.get(Catalog, start.context_id)
    answers = data.answers.get(session_id, {})
    results: list[pr_schemas.ResultRead] = []
    for question_id, event in sorted(answers.items(), key=lambda pair: pair[1].occurred_at):
        grade = dict((event.payload or {}).get("grade") or {})
        question = data.questions.get(question_id)
        results.append(
            pr_schemas.ResultRead(
                question_id=question_id,
                prompt=(question.prompt if question is not None else None),
                correct=grade.get("correct") if reveal else None,
                score=float(grade.get("score") or 0.0) if reveal else 0.0,
                max_score=float(grade.get("max_score") or 0.0) if reveal else 0.0,
                requires_manual=bool(grade.get("requires_manual")),
                explanation=(question.explanation if reveal and question is not None else None) or None,
                answered_at=_iso(event.occurred_at),
            )
        )
    summary = pr_schemas.RunSummaryRead(
        session_id=session_id,
        catalog_id=start.context_id,
        catalog_name=(catalog.name if catalog is not None else ""),
        finished=session_id in data.finishes,
        started_at=_iso(start.occurred_at),
        finished_at=_iso(data.finishes[session_id].occurred_at) if session_id in data.finishes else None,
        results=results,
        **_totals(answers, reveal=reveal),
    ).model_dump(mode="json")
    return summary


async def finish_run(db: AsyncSession, student: Student, session_id: str) -> dict:
    """Close the run and hand over every verdict, whatever the feedback timing was.

    Finishing twice answers the same way: the finish event is written once and the summary
    is read from the log either way, so a learner who reloads the results screen is not told
    their run has gone.
    """
    start = await _run_start(db, student, session_id)
    if await _finish_event(db, student, session_id) is None:
        await activity_service.record(
            db,
            student=student,
            event_type=activity_service.EVENT_RUN_FINISH,
            session_id=session_id,
            human_summary=_NO_SUMMARY,
            context_type="catalog",
            context_id=start.context_id,
            payload={},
        )
    return await build_summary(db, student, session_id, reveal=True)


async def resume_run(
    db: AsyncSession, student: Student, session_id: str, *, language: str | None = None
) -> dict:
    """The steps a run was opened with, served again without writing a second start.

    A learner who closes the tab mid-run comes back to the same exercises in the same order:
    the shuffle seed sits on the start event for exactly this, so re-serving with it puts
    step seven where it was rather than mixing it somewhere else. Nothing is written, because
    reading a run is not opening it again - a second start event would count the same visit
    twice on the learner's own history.

    Content that stopped being servable since the run began is skipped and counted again, so
    the number the learner is told matches what the screen can actually show. A run whose
    catalog has since been archived or binned is still readable here, which is what
    `run_of` already does with its verdicts; answering stays gated in `answer`.
    """
    start = await _run_start(db, student, session_id)
    catalog = await db.get(Catalog, start.context_id)
    if catalog is None:
        raise PracticeNotFound("Practice not found")

    payload = dict(start.payload or {})
    steps, skipped = await _build_steps(db, catalog, shuffle=False, language=language)
    seed = payload.get("shuffle_seed")
    if payload.get("shuffle") and isinstance(seed, str) and pr_schemas.SESSION_TOKEN_PATTERN.match(seed):
        steps = _shuffled(steps, bytes.fromhex(seed))
        for position, step in enumerate(steps):
            step["position"] = position

    return pr_schemas.RunRead(
        session_id=session_id,
        catalog_id=catalog.id,
        catalog_name=catalog.name,
        feedback_timing=catalog_service.feedback_timing_of(catalog),
        shuffle=bool(payload.get("shuffle")),
        known_states_enabled=bool(catalog.known_states_enabled),
        skipped_count=skipped,
        steps=[pr_schemas.StepRead(**step) for step in steps],
        started_at=_iso(start.occurred_at) or "",
    ).model_dump(mode="json")


async def _finish_event(db: AsyncSession, student: Student, session_id: str) -> ActivityEvent | None:
    return (
        await db.execute(
            select(ActivityEvent)
            .where(
                ActivityEvent.student_id == student.id,
                ActivityEvent.session_id == session_id,
                ActivityEvent.event_type == activity_service.EVENT_RUN_FINISH,
            )
            .order_by(ActivityEvent.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_of(db: AsyncSession, student: Student, session_id: str) -> dict:
    """A run the learner returns to, under the timing rule that was in force for it.

    Held-back verdicts stay held back: an unfinished `after_session` run is served with its
    answers counted but not marked, which is the whole point of that setting.
    """
    start = await _run_start(db, student, session_id)
    catalog = await db.get(Catalog, start.context_id)
    finished = await _finish_event(db, student, session_id) is not None
    if catalog is None:
        # The catalog itself has been deleted from under the run. The answers still belong
        # to the learner, so they are shown rather than hidden behind a 404.
        reveal = True
    else:
        reveal = finished or catalog_service.feedback_timing_of(catalog) == "instant"
    return await build_summary(db, student, session_id, reveal=reveal)


async def recent_runs(db: AsyncSession, student: Student, catalog: Catalog, *, limit: int = RECENT_RUNS) -> list[dict]:
    """This learner's last runs of one catalog, newest first, without the per-question lines.

    Three queries for the whole list rather than three per run: a history panel is read on
    every visit to the catalog, and the events it summarises are already in one table.
    """
    starts = (
        await db.execute(
            select(ActivityEvent.session_id, ActivityEvent.occurred_at)
            .where(
                ActivityEvent.student_id == student.id,
                ActivityEvent.event_type == activity_service.EVENT_RUN_START,
                ActivityEvent.context_type == "catalog",
                ActivityEvent.context_id == catalog.id,
                ActivityEvent.session_id.is_not(None),
            )
            .order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc())
            .limit(limit)
        )
    ).all()
    session_ids = [row[0] for row in starts if row[0]]
    if not session_ids:
        return []
    data = await _load_runs(db, student, session_ids)
    timing = catalog_service.feedback_timing_of(catalog)
    out: list[dict] = []
    for session_id, occurred_at in starts:
        reveal = session_id in data.finishes or timing == "instant"
        out.append(
            {
                "session_id": session_id,
                "started_at": _iso(occurred_at),
                "finished": session_id in data.finishes,
                "finished_at": _iso(data.finishes[session_id].occurred_at) if session_id in data.finishes else None,
                **_totals(data.answers.get(session_id, {}), reveal=reveal),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Known states and favorites
# --------------------------------------------------------------------------- #


async def mark_known(db: AsyncSession, catalog: Catalog, student: Student, payload: pr_schemas.KnownStateRequest) -> dict:
    """A learner's own mark on a word of this catalog.

    Gated on the catalog's `known_states_enabled`: the buttons only exist where the teacher
    turned them on, and a mark against a catalog that never asked for one is a row no screen
    would read back.
    """
    if not catalog.known_states_enabled:
        raise PracticeError("this practice does not use known / learning marks")
    entry = await db.get(VocabularyEntry, payload.ref_id)
    if entry is None or not vocabulary_service.is_learner_visible(entry):
        raise PracticeNotFound("Word not found")
    held = (
        await db.execute(
            select(CatalogItem.id).where(
                CatalogItem.catalog_id == catalog.id,
                CatalogItem.kind == enums.ContentKind.VOCABULARY,
                CatalogItem.ref_id == payload.ref_id,
            )
        )
    ).scalar_one_or_none()
    if held is None:
        raise PracticeError("that word is not part of this practice")

    event_type = (
        activity_service.EVENT_MARK_KNOWN if payload.state == "known" else activity_service.EVENT_MARK_LEARNING
    )
    await activity_service.record(
        db,
        student=student,
        event_type=event_type,
        human_summary=_NO_SUMMARY,
        context_type="vocabulary",
        context_id=payload.ref_id,
        payload={"catalog_id": str(catalog.id)},
    )
    return {"ref_id": str(payload.ref_id), "state": payload.state}


async def known_states(db: AsyncSession, catalog: Catalog, student: Student) -> dict:
    """The current mark on each word of this catalog, and how many of each there are.

    Marks belong to the learner rather than to the catalog, so a word marked in one practice
    reads as marked everywhere - which is what makes "words I still need" a list worth
    keeping.
    """
    ref_ids = (
        await db.execute(
            select(CatalogItem.ref_id).where(
                CatalogItem.catalog_id == catalog.id,
                CatalogItem.kind == enums.ContentKind.VOCABULARY,
            )
        )
    ).scalars().all()
    states = await activity_service.latest_states(db, student.id, event_types=tuple(activity_service.MARK_EVENTS))
    counts = {"known": 0, "learning": 0, "unmarked": 0}
    items = []
    for ref_id in ref_ids:
        state = states.get(("vocabulary", ref_id))
        counts[state if state in counts else "unmarked"] += 1
        if state is not None:
            items.append(pr_schemas.KnownStateRead(ref_id=ref_id, state=state).model_dump(mode="json"))
    return {"enabled": bool(catalog.known_states_enabled), "word_count": len(ref_ids), "counts": counts, "items": items}


async def add_favorite(db: AsyncSession, student: Student, payload: pr_schemas.FavoriteRequest) -> dict:
    """Save an exercise or a word. Saving twice is not an error.

    The unique index settles a double click and a race the same way, so the second write is
    answered as "already saved" rather than as a clash: the learner's list is the same
    either way, and a 409 on a button they pressed twice reads as a failure that was not.
    """
    if not await _favorite_content_ok(db, payload):
        raise PracticeNotFound("Content not found")

    favorite = Favorite(student_id=student.id, kind=enums.ContentKind(payload.kind), ref_id=payload.ref_id)
    db.add(favorite)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return {"ok": True, "added": False, "kind": payload.kind, "ref_id": str(payload.ref_id)}
    await activity_service.record(
        db,
        student=student,
        event_type=activity_service.EVENT_FAVORITE_ADD,
        human_summary=_NO_SUMMARY,
        context_type=payload.kind,
        context_id=payload.ref_id,
        payload={},
    )
    return {"ok": True, "added": True, "id": str(favorite.id), "kind": payload.kind, "ref_id": str(payload.ref_id)}


async def _favorite_content_ok(db: AsyncSession, payload: pr_schemas.FavoriteRequest) -> bool:
    if payload.kind == "question":
        return await _serveable(db, await db.get(Question, payload.ref_id))
    entry = await db.get(VocabularyEntry, payload.ref_id)
    return entry is not None and vocabulary_service.is_learner_visible(entry)


async def remove_favorite(db: AsyncSession, student: Student, payload: pr_schemas.FavoriteRequest) -> dict:
    """Take it off the list. Removing something that is not there is answered, not failed."""
    row = (
        await db.execute(
            select(Favorite).where(
                Favorite.student_id == student.id,
                Favorite.kind == enums.ContentKind(payload.kind),
                Favorite.ref_id == payload.ref_id,
            )
        )
    ).scalars().first()
    if row is None:
        return {"ok": True, "removed": False, "kind": payload.kind, "ref_id": str(payload.ref_id)}
    await db.delete(row)
    await db.flush()
    await activity_service.record(
        db,
        student=student,
        event_type=activity_service.EVENT_FAVORITE_REMOVE,
        human_summary=_NO_SUMMARY,
        context_type=payload.kind,
        context_id=payload.ref_id,
        payload={},
    )
    return {"ok": True, "removed": True, "kind": payload.kind, "ref_id": str(payload.ref_id)}


async def list_favorites(db: AsyncSession, student: Student) -> dict:
    """The learner's own review list, with each thing named from its own row."""
    rows = (
        await db.execute(
            select(Favorite)
            .where(Favorite.student_id == student.id)
            .order_by(Favorite.created_at.desc(), Favorite.id.desc())
        )
    ).scalars().all()

    question_ids = [row.ref_id for row in rows if _label_of(row.kind) == "question"]
    word_ids = [row.ref_id for row in rows if _label_of(row.kind) == "vocabulary"]
    questions: dict[uuid.UUID, Question] = {}
    words: dict[uuid.UUID, VocabularyEntry] = {}
    if question_ids:
        questions = {
            row.id: row
            for row in (await db.execute(select(Question).where(Question.id.in_(question_ids)))).scalars().all()
        }
    if word_ids:
        words = {
            row.id: row
            for row in (
                await db.execute(select(VocabularyEntry).where(VocabularyEntry.id.in_(word_ids)))
            ).scalars().all()
        }

    items = []
    for row in rows:
        kind = _label_of(row.kind)
        if kind == "question":
            source = questions.get(row.ref_id)
            title = (source.prompt if source is not None else None) or None
            detail = _label_of(source.type) if source is not None else None
            available = await _serveable(db, source)
        else:
            source = words.get(row.ref_id)
            title = source.word if source is not None else None
            detail = (source.part_of_speech or source.level) if source is not None else None
            available = source is not None and vocabulary_service.is_learner_visible(source)
        items.append(
            pr_schemas.FavoriteRead(
                id=row.id,
                kind=kind,
                ref_id=row.ref_id,
                title=title,
                detail=detail,
                available=available,
                created_at=_iso(row.created_at) or "",
            ).model_dump(mode="json")
        )
    return {"items": items, "total": len(items)}
