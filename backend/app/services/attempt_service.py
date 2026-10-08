"""The attempt engine: one learner sitting one paper, measured by the server.

Everything here hangs from two rules that the rest of the platform has to agree with.

**The learner holds a token; the server holds the clock.** A sitting is opened here, and the
only thing the runner keeps is the 32-character token this module issued. Every read recomputes
`remaining_seconds` from `expires_at - now()`, so switching device, closing a tab or moving the
local clock forward changes nothing about the deadline. The fifteen-second sweep in the worker is
a backstop for papers nobody came back for; a request that arrives after the deadline closes the
attempt on the way in, because otherwise a learner who submits one second late would be graded
by a different rule than the one who submits one second early.

**The paper a sitting was given is the paper it is graded on.** `ExamAttempt.blueprint` holds the
order, the pinned `QuestionVersion` ids, the marks, the option layout and the text or recording
each question travels with. Nothing in this module reads the live question to serve or to mark an
attempt, and nothing writes back to the exam: a question rewritten during an exam week changes
next term's paper, not the one already handed in.

The verdict a learner sees is a policy, not a fact about this module. `feedback_timing` decides
what one answer returns, `result_visibility` decides what the result screen shows, and
`show_correct_answers` and `show_explanations` decide what the marks are made of. Each of them is
read from the sitting's own blueprint, so editing the rule halfway through the exam week reaches
the learners who start after the edit and leaves the ones already sitting alone.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import enums, security
from app.core.exceptions import RuleBroken
from app.models.assessment import (
    AttemptAnswer,
    Exam,
    ExamAssignment,
    ExamAttempt,
    ManualReview,
    TeacherFeedback,
)
from app.models.content import MediaAsset
from app.models.identity import GroupMembership, Student
from app.schemas import attempt as a_schemas
from app.services import (
    activity_service,
    audit_service,
    exam_service,
    media_service,
    question_engine,
)

#: The words a screen branches on. They are listed here and served by `meta()` so the frontend
#: translates a code rather than guessing at a sentence the backend happened to write.
#: The list is exactly what a notice field can hold. Two facts a learner might expect to find here
#: are carried elsewhere: coming back to a live sitting is the `resumed` flag on the start read, and
#: the rule about resuming is stated on the brief page, before a sitting exists to notice anything.
NOTICES = (
    "closed",
    "expired",
    "tab_limit_reached",
    "tab_switched",
    "already_submitted",
    "monitoring_off",
    "withheld",
)

_AVAILABILITY_REASONS = ("open", "not_yet", "closed", "no_attempts_left", "already_submitted")
_RESULT_STATES = ("closed", "awaiting_teacher", "hidden", "shown")

#: Paper states a learner is never offered. `scheduled` is a teacher's plan rather than an issue,
#: and a paper that has gone back to being written, or has been put away, is not a task either.
_HIDDEN_STATUSES = ("draft", "scheduled", "archived")

_CLOSED_STATUSES = (
    enums.AttemptStatus.SUBMITTED,
    enums.AttemptStatus.AUTO_SUBMITTED,
    enums.AttemptStatus.EXPIRED,
)


class AttemptError(RuleBroken, ValueError):
    """A refusal about a sitting: the endpoints 422 with it, in the learner's own words."""

    code = "attempt_rule"


class AttemptNotFound(RuleBroken, Exception):
    """No such attempt, or not one this learner may act on. 404.

    A token that belongs to somebody else is answered the same way as a token that does not
    exist: an attempt id is not a secret a learner is entitled to probe.
    """

    code = "attempt_not_found"


class AttemptClosed(RuleBroken, Exception):
    """The sitting is over, so it no longer accepts answers. 409."""

    code = "attempt_closed"


class NotAssigned(RuleBroken, Exception):
    """This paper was not handed to this learner. 404 - the exam stays unnamed."""

    code = "exam_not_assigned"


# --------------------------------------------------------------------------- #
# The sitting's own facts
# --------------------------------------------------------------------------- #


def _label_of(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _now() -> datetime:
    return security.utcnow()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _from_iso(value: Any) -> datetime | None:
    """An instant read back out of a stored rule, which may be text or already a datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_client_stamp(value: Any) -> datetime | None:
    """A client's own timestamp, kept only if it is a timestamp.

    It is stored for the Phase 9 timeline and never used to decide anything: a browser that
    claims it answered before the paper opened is a browser reporting its own clock.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value).strip()
    if not text:
        return None
    return _from_iso(text)


def blueprint_of(attempt: ExamAttempt) -> dict:
    return attempt.blueprint or {}


def rules_of(attempt: ExamAttempt) -> dict:
    return blueprint_of(attempt).get("rules") or {}


def entries_of(attempt: ExamAttempt) -> list[dict]:
    return blueprint_of(attempt).get("items") or []


def _entry(attempt: ExamAttempt, exam_item_id: uuid.UUID) -> dict:
    wanted = str(exam_item_id)
    for entry in entries_of(attempt):
        if entry.get("exam_item_id") == wanted:
            return entry
    raise AttemptError("that question is not on your paper", code="attempt_item_not_on_paper")


def _blueprint_entry(attempt: ExamAttempt, exam_item_id: uuid.UUID | None) -> dict:
    """The same lookup for a read path, where a line missing from the blueprint is a fact to
    display rather than a request to refuse."""
    if exam_item_id is None:
        return {}
    wanted = str(exam_item_id)
    return next(
        (entry for entry in entries_of(attempt) if entry.get("exam_item_id") == wanted), {}
    )


async def _entries_map(db: AsyncSession, attempt_ids: list[uuid.UUID]) -> dict[tuple[str, str], dict]:
    """Blueprint lines for several sittings at once, keyed by (attempt id, exam item id).

    The grading queue needs the mark each line was worth in the paper being marked, and the
    only place that is written down is each attempt's own blueprint.
    """
    ids = [attempt_id for attempt_id in dict.fromkeys(attempt_ids)]
    if not ids:
        return {}
    out: dict[tuple[str, str], dict] = {}
    for attempt_id, blueprint in (
        await db.execute(select(ExamAttempt.id, ExamAttempt.blueprint).where(ExamAttempt.id.in_(ids)))
    ).all():
        for entry in (blueprint or {}).get("items") or []:
            out[(str(attempt_id), str(entry.get("exam_item_id")))] = entry
    return out


def remaining_seconds(attempt: ExamAttempt, *, at: datetime | None = None) -> int | None:
    """Seconds left, from the server's clock. `None` means the paper has no time limit."""
    deadline = _aware(attempt.expires_at)
    if deadline is None:
        return None
    return max(0, int((deadline - (at or _now())).total_seconds()))


def is_expired(attempt: ExamAttempt) -> bool:
    """True when an open sitting's deadline has passed.

    Only an in-progress attempt can be expired: a paper that was handed in closed at its own
    moment, and rewriting that after the fact would move a result the learner already has.
    """
    if attempt.status != enums.AttemptStatus.IN_PROGRESS:
        return False
    deadline = _aware(attempt.expires_at)
    return deadline is not None and deadline <= _now()


async def get_attempt(db: AsyncSession, token: str, *, student: Student | None = None) -> ExamAttempt:
    """The sitting one token names, optionally only when it belongs to this learner.

    `student` is not a filter that returns nothing: a learner asking about somebody else's paper
    is told the same thing as one asking about a paper that never was, so no request can be used
    to learn whether a given token is live.
    """
    row = (
        await db.execute(select(ExamAttempt).where(ExamAttempt.session_id == token))
    ).scalar_one_or_none()
    if row is None:
        raise AttemptNotFound("that is not an exam attempt", code="attempt_not_found")
    if student is not None and row.student_id != student.id:
        raise AttemptNotFound("that is not an exam attempt", code="attempt_not_found")
    return row


async def get_attempt_by_id(db: AsyncSession, attempt_id: uuid.UUID) -> ExamAttempt:
    row = await db.get(ExamAttempt, attempt_id)
    if row is None:
        raise AttemptNotFound("that is not an exam attempt", code="attempt_not_found")
    return row


async def _exam_of(db: AsyncSession, attempt: ExamAttempt) -> Exam:
    exam = await db.get(Exam, attempt.exam_id)
    if exam is None:
        raise AttemptNotFound("the exam this attempt was sat on is gone", code="attempt_exam_gone")
    return exam


def is_closed(attempt: ExamAttempt) -> bool:
    return attempt.status in _CLOSED_STATUSES


# --------------------------------------------------------------------------- #
# Assignment and availability
# --------------------------------------------------------------------------- #


async def is_assigned(db: AsyncSession, exam: Exam, student: Student) -> bool:
    """Whether this paper was handed to this learner, directly or through a group they are in.

    Membership is read live: a learner who joined the class yesterday is assigned with the rest
    of it, and one who left still has any paper they already sat.
    """
    clauses = [ExamAssignment.student_id == student.id]
    group_ids = list(
        (
            await db.execute(
                select(GroupMembership.group_id).where(GroupMembership.student_id == student.id)
            )
        ).scalars()
    )
    if group_ids:
        clauses.append(ExamAssignment.group_id.in_(group_ids))
    found = (
        await db.execute(
            select(ExamAssignment.id)
            .where(ExamAssignment.exam_id == exam.id, or_(*clauses))
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def attempts_of(db: AsyncSession, exam_id: uuid.UUID, student_id: uuid.UUID) -> list[ExamAttempt]:
    return list(
        (
            await db.execute(
                select(ExamAttempt)
                .where(ExamAttempt.exam_id == exam_id, ExamAttempt.student_id == student_id)
                .order_by(ExamAttempt.attempt_number)
            )
        ).scalars()
    )


async def locked_attempts_of(
    db: AsyncSession, exam_id: uuid.UUID, student_id: uuid.UUID
) -> list[ExamAttempt]:
    """This learner's sittings of this paper, locked for the length of one transaction.

    Opening a paper reads them under the lock because the same read answers two questions - resume
    what is already open, or number a new sitting - and if the two answers came from reads taken at
    different moments, a second tab could see no open sitting and then count the sitting its
    sibling had just committed, and write a numbered second paper instead of resuming the first.
    """
    return list(
        (
            await db.execute(
                select(ExamAttempt)
                .where(ExamAttempt.exam_id == exam_id, ExamAttempt.student_id == student_id)
                .order_by(ExamAttempt.attempt_number)
                .with_for_update()
            )
        ).scalars()
    )


def _attempt_limit(exam: Exam) -> int:
    """How many sittings one learner may open of this paper.

    `max_attempts` unset means one, not unlimited: a paper is handed in once unless the teacher
    says otherwise, and a learner who sets no number should not be able to run a bank until they
    like the result.
    """
    return _frozen_limit({"max_attempts": exam.max_attempts})


def _frozen_limit(rules: dict) -> int:
    """The same allowance, read from the rules a sitting was dealt under.

    The learner's screen is shown this number rather than the raw field: `null` in a paper's rules
    means one sitting, and a read that answered `null` would promise unlimited attempts right up to
    the second start that refuses them.
    """
    value = rules.get("max_attempts")
    return int(value) if value is not None else 1


async def availability(
    db: AsyncSession,
    exam: Exam,
    student: Student,
    *,
    now: datetime | None = None,
    rows: list[ExamAttempt] | None = None,
) -> tuple[bool, str, ExamAttempt | None]:
    """Can this learner start this paper now, and if not, which rule says so.

    The reason is part of the answer because a learner looking at a greyed-out button needs the
    sentence that explains it: "not open yet" and "you have used your attempts" are different
    news, and only one of them is something they can wait for.

    A paper sits inside its window only while it is `active`. `scheduled` is a teacher's plan for
    a paper that has not been issued yet, and a learner cannot sit a paper that has not been
    issued - whatever the clock says about the hour it was planned for.

    `rows` lets a caller that has already read this learner's sittings - opening a paper reads them
    under a lock, and needs the same read for the numbering - hand them in instead of being asked
    for a second, later answer.
    """
    moment = now or _now()
    if rows is None:
        rows = await attempts_of(db, exam.id, student.id)
    open_row = next((row for row in rows if row.status == enums.AttemptStatus.IN_PROGRESS), None)

    if exam.deleted_at is not None or _label_of(exam.status) != "active":
        return False, "closed", open_row
    opens = _aware(exam.available_from)
    if opens is not None and moment < opens:
        return False, "not_yet", open_row
    closes = _aware(exam.available_to)
    if closes is not None and moment > closes and open_row is None:
        # A paper whose window has shut cannot be started. A learner still holding an open
        # sitting of it is not sent away by this function - the sitting's own rules decide.
        return False, "closed", None

    limit = _attempt_limit(exam)
    if len(rows) >= limit:
        return False, "no_attempts_left" if limit > 1 else "already_submitted", open_row
    return True, "open", open_row


# --------------------------------------------------------------------------- #
# Serving one step from the blueprint
# --------------------------------------------------------------------------- #


def _public_config(public: dict, option_order: list[int] | None) -> dict:
    """Lay the options out for this sitting, without touching what they mean.

    Reordering what is displayed is safe precisely because each option keeps the `index` it was
    authored with: the learner's answer names that index, so the mark does not depend on where
    the line happened to appear.
    """
    options = public.get("options")
    if not option_order or not isinstance(options, list):
        return public
    # A layout has to name each option exactly once. Anything else - a length that does not
    # match, a repeated index, one out of range - belongs to a different copy of the question,
    # and applying it would show the learner the same line twice and hide one they had to answer.
    if sorted(option_order) != list(range(len(options))):
        return public
    public["options"] = [options[i] for i in option_order]
    return public


async def _step_view(db: AsyncSession, attempt: ExamAttempt, entry: dict) -> dict:
    """One blueprint line as the learner's widget: frozen question, frozen context, live file."""
    snapshot = None
    if entry.get("question_version_id"):
        snapshot = await exam_service.snapshot_for_version(
            db, uuid.UUID(entry["question_version_id"])
        )
    question = (snapshot or {}).get("question") or {}
    q_type = question.get("type") or ""
    try:
        desc = question_engine.descriptor(q_type)
        widget, auto = desc.answer_widget, desc.gradable_automatically
        public = question_engine.public_config(q_type, question.get("config") or {})
    except ValueError:
        # A version written before its type existed is a data gap, not a crash: the line is
        # still shown, and the teacher marks it because nothing here can.
        widget, auto, public = "text", False, {}

    payload: dict[str, Any] = {
        "id": entry.get("ref_id"),
        "exam_item_id": entry.get("exam_item_id"),
        "type": q_type,
        "prompt": question.get("prompt"),
        "score": entry.get("points", 0.0),
        "answer_widget": widget,
        "requires_manual_grading": not auto,
        "config": _public_config(public, entry.get("option_order")),
        "explanation_available": bool(question.get("explanation")),
    }

    if question.get("media_asset_id"):
        asset = await db.get(MediaAsset, uuid.UUID(question["media_asset_id"]))
        if asset is not None and asset.deleted_at is None:
            payload["media"] = await media_service.learner_view(asset)

    context = _context_of(attempt, entry)
    if context is not None:
        payload["context"] = context
    return payload


def _context_of(attempt: ExamAttempt, entry: dict) -> dict | None:
    """The text or recording this line travels with, from the sitting's own copy.

    The body is the one that was dealt when the sitting opened, not the one the bank holds now,
    which is what makes a resumed paper the same paper. The audio is the exception on purpose:
    a stored link would outlive its signature, so only the asset id is frozen and the file is
    served fresh on every request.
    """
    context_id = entry.get("context_id")
    if not context_id:
        return None
    frozen = (blueprint_of(attempt).get("contexts") or {}).get(str(context_id))
    if frozen is None:
        return None
    if frozen.get("kind") == "reading":
        return {"kind": "reading", "reading": dict(frozen)}
    listening = dict(frozen)
    listening.pop("media_asset_id", None)
    return {"kind": "listening", "listening": listening}


async def _audio_for(db: AsyncSession, attempt: ExamAttempt, entry: dict) -> dict | None:
    """The signed playback for a listening line, resolved per request."""
    context = _context_of(attempt, entry)
    if not context or context.get("kind") != "listening":
        return None
    frozen = (blueprint_of(attempt).get("contexts") or {}).get(str(entry["context_id"])) or {}
    asset_id = frozen.get("media_asset_id")
    if not asset_id:
        return None
    asset = await db.get(MediaAsset, uuid.UUID(asset_id))
    if asset is None or asset.deleted_at is not None:
        return None
    return await media_service.learner_view(asset)


def _rules_read(rules: dict) -> dict:
    return a_schemas.AttemptRulesRead(
        allow_previous=bool(rules.get("allow_previous", True)),
        restrict_copy_paste=bool(rules.get("restrict_copy_paste", False)),
        monitor_tab_switch=bool(rules.get("monitor_tab_switch", False)),
        tab_switch_limit=rules.get("tab_switch_limit"),
        tab_switch_action=rules.get("tab_switch_action"),
        resume_after_disconnect=bool(rules.get("resume_after_disconnect", True)),
        feedback_timing=rules.get("feedback_timing", "after_session"),
        auto_submit_on_expiry=bool(rules.get("auto_submit_on_expiry", True)),
    ).model_dump(mode="json")


async def _answered_ids(db: AsyncSession, attempt_id: uuid.UUID) -> set[str]:
    return {
        str(row[0])
        for row in (
            await db.execute(
                select(AttemptAnswer.exam_item_id).where(AttemptAnswer.attempt_id == attempt_id)
            )
        ).all()
    }


async def _answers_of(db: AsyncSession, attempt_id: uuid.UUID) -> dict[str, AttemptAnswer]:
    return {
        str(row.exam_item_id): row
        for row in (
            await db.execute(select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt_id))
        ).scalars()
    }


# --------------------------------------------------------------------------- #
# Reads the runner lives on
# --------------------------------------------------------------------------- #


async def to_read(db: AsyncSession, attempt: ExamAttempt, *, notice: str | None = None) -> dict:
    """The sitting as the server sees it, clock included."""
    exam = await db.get(Exam, attempt.exam_id)
    blueprint = blueprint_of(attempt)
    rules = rules_of(attempt)
    answered = await _answered_ids(db, attempt.id)
    entries = entries_of(attempt)
    return a_schemas.AttemptRead(
        token=attempt.session_id or "",
        attempt_id=attempt.id,
        exam_id=attempt.exam_id,
        title=blueprint.get("title") or (exam.title if exam else ""),
        status=_label_of(attempt.status),
        attempt_number=attempt.attempt_number,
        max_attempts=_frozen_limit(rules),
        started_at=_iso(attempt.started_at) or "",
        expires_at=_iso(_aware(attempt.expires_at)),
        remaining_seconds=remaining_seconds(attempt),
        server_seconds_used=attempt.server_seconds_used,
        duration_minutes=rules.get("duration_minutes"),
        total_items=len(entries),
        answered_items=len(answered),
        points=blueprint.get("points", 0.0),
        tab_switches=attempt.tab_switch_count,
        rules=_rules_read(rules),
        notice=notice,
    ).model_dump(mode="json")


async def steps(db: AsyncSession, attempt: ExamAttempt) -> dict:
    """The whole paper in the order this sitting was dealt, with each learner's own answers.

    A resume reads from here rather than from anything the browser kept, which is why a closed
    tab and a different device both land back on the same page with the same marks.
    """
    if is_expired(attempt):
        await finalise(db, attempt, reason="expired")
    answered_map = await _answers_of(db, attempt.id)
    rows: list[dict] = []
    for entry in entries_of(attempt):
        answer = answered_map.get(entry["exam_item_id"])
        view = await _step_view(db, attempt, entry)
        audio = await _audio_for(db, attempt, entry)
        if audio is not None and "listening" in view.get("context", {}):
            view["context"]["listening"]["audio"] = audio
        rows.append(
            a_schemas.AttemptStepRead(
                exam_item_id=uuid.UUID(entry["exam_item_id"]),
                kind=entry.get("kind", "question"),
                ref_id=uuid.UUID(entry["ref_id"]),
                position=entry.get("position", 0),
                section_id=uuid.UUID(entry["section_id"]) if entry.get("section_id") else None,
                section_title=entry.get("section_title"),
                points=entry.get("points", 0.0),
                context_id=uuid.UUID(entry["context_id"]) if entry.get("context_id") else None,
                view=view,
                saved=answer.response if answer else None,
                answered=answer is not None,
            ).model_dump(mode="json")
        )
    return a_schemas.AttemptStepsRead(
        token=attempt.session_id or "",
        status=_label_of(attempt.status),
        remaining_seconds=remaining_seconds(attempt),
        steps=rows,
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Opening a sitting
# --------------------------------------------------------------------------- #


def _new_token() -> str:
    return secrets.token_hex(16)


async def _open_attempt(
    db: AsyncSession, exam: Exam, student: Student, blueprint: dict, *, attempt_number: int
) -> ExamAttempt:
    """Write the sitting and its frozen paper, and hand back the token that names it."""
    now = _now()
    rules = blueprint.get("rules") or {}
    expires_at: datetime | None = None
    minutes = rules.get("duration_minutes")
    if minutes:
        expires_at = now + timedelta(minutes=int(minutes))
    if rules.get("must_finish_before_close"):
        # The paper's own closing time can be sooner than the learner's full allowance. Handing
        # back a deadline the exam window does not honour would let a late start run past it.
        closes = _from_iso(rules.get("available_to"))
        if closes is not None:
            expires_at = min(expires_at, closes) if expires_at else closes

    attempt = ExamAttempt(
        exam_id=exam.id,
        student_id=student.id,
        session_id=_new_token(),
        status=enums.AttemptStatus.IN_PROGRESS,
        attempt_number=attempt_number,
        started_at=now,
        expires_at=expires_at,
        server_seconds_used=0,
        tab_switch_count=0,
        blueprint=blueprint,
        max_score=blueprint.get("points"),
    )
    db.add(attempt)
    return attempt


async def start(db: AsyncSession, exam: Exam, student: Student) -> dict:
    """Open the paper, or hand back the sitting that is already open.

    Three cases, and they must not be confused: a second tab on a live sitting resumes it; a
    deadline that passed while the learner was away closes that attempt and opens a numbered
    second one; and a paper the teacher marked `no resume after disconnect` closes the abandoned
    sitting from its autosaved answers rather than leaving it open forever.
    """
    if not await is_assigned(db, exam, student):
        raise NotAssigned("this exam has not been assigned to you", code="exam_not_assigned")

    rows = await locked_attempts_of(db, exam.id, student.id)
    allowed, reason, open_row = await availability(db, exam, student, rows=rows)
    if open_row is not None:
        if is_expired(open_row):
            await finalise(db, open_row, reason="expired")
        elif rules_of(open_row).get("resume_after_disconnect", True):
            return await _start_read(db, open_row, resumed=True)
        else:
            # The teacher's rule says a lost connection costs the sitting. Its autosaved answers
            # are already stored, so it is closed and graded from them rather than left open.
            await finalise(db, open_row, reason="expired")
    if not allowed:
        code, sentence = _start_refusal(reason)
        raise AttemptError(sentence, code=code)

    blueprint = await exam_service.freeze_composition(db, exam)
    if not blueprint.get("items"):
        raise AttemptError(
            "this exam has no questions ready to serve yet",
            code="exam_nothing_ready_to_serve",
        )

    exam_id, student_id = exam.id, student.id
    attempt = await _open_attempt(db, exam, student, blueprint, attempt_number=len(rows) + 1)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Two tabs at the same moment: `uq_attempt_number` is what stops a second row being
        # numbered 1, and the loser of that race resumes the winner rather than failing. The two
        # ids were read before the insert, because a refused flush leaves the session's instances
        # expired - asking the paper for its own id after that would try to reload it through a
        # session that has not rolled back yet, and the learner would meet that instead of their
        # resumed sitting.
        await db.rollback()
        rows = await attempts_of(db, exam_id, student_id)
        live = next((row for row in rows if row.status == enums.AttemptStatus.IN_PROGRESS), None)
        if live is None:
            raise AttemptError(
                "the exam could not be opened just now - try again",
                code="exam_could_not_open",
            ) from exc
        return await _start_read(db, live, resumed=True)

    # The start event belongs to the sitting that was actually opened: a tab that resumed or lost
    # the numbering race returns above, so one press of a button never reads as two sittings.
    await activity_service.record(
        db,
        student=student,
        event_type="exam_attempt_start",
        category=activity_service.CATEGORY_ASSESSMENT,
        session_id=attempt.session_id,
        context_type="exam",
        context_id=exam.id,
        payload={"attempt_number": attempt.attempt_number, "items": blueprint.get("count", 0)},
    )
    return await _start_read(db, attempt, resumed=False, include_steps=True)


#: Why a paper will not open, as the pair the learner's screen needs: the code it translates
#: and the sentence for whoever reads the API directly. "Used the one sitting" and "used every
#: attempt" are different news because one is a rule the teacher wrote and the other is arithmetic.
_START_REFUSALS = {
    "not_yet": ("exam_not_open_yet", "this exam is not open yet"),
    "closed": ("exam_closed", "this exam is closed"),
    "no_attempts_left": ("exam_no_attempts_left", "you have used every attempt at this exam"),
    "already_submitted": ("exam_one_sitting_only", "you have used the one sitting this exam allows"),
}

#: A reason the availability walk invents later must still refuse, rather than open a paper
#: whose rule nobody wrote a sentence for.
_START_REFUSAL_DEFAULT = ("exam_not_startable", "this exam cannot be opened right now")


def _start_refusal(reason: str) -> tuple[str, str]:
    return _START_REFUSALS.get(reason, _START_REFUSAL_DEFAULT)


async def _start_read(
    db: AsyncSession, attempt: ExamAttempt, *, resumed: bool, include_steps: bool = False
) -> dict:
    blueprint = blueprint_of(attempt)
    rules = rules_of(attempt)
    body = a_schemas.StartRead(
        token=attempt.session_id or "",
        attempt_id=attempt.id,
        exam_id=attempt.exam_id,
        title=blueprint.get("title", ""),
        attempt_number=attempt.attempt_number,
        max_attempts=_frozen_limit(rules),
        started_at=_iso(attempt.started_at) or "",
        expires_at=_iso(_aware(attempt.expires_at)),
        remaining_seconds=remaining_seconds(attempt),
        total_items=blueprint.get("count", len(entries_of(attempt))),
        points=blueprint.get("points", 0.0),
        rules=_rules_read(rules),
        resumed=resumed,
        steps=[],
    ).model_dump(mode="json")
    if include_steps or resumed:
        body["steps"] = (await steps(db, attempt))["steps"]
    return body


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #


def _grade(attempt: ExamAttempt, entry: dict, snapshot: dict | None, response: Any):
    """Mark one answer against the pinned version, under the rules this sitting was dealt.

    The exam's own switches decide how the engine scores a partial: `partial_scoring_enabled`
    off means every part of a multi-part question has to be right, and `negative_marking_enabled`
    off means a wrong tick costs nothing - which for `multi_select` is a penalty of zero rather
    than an absent key, because the engine's own default for that type cancels a correct tick.
    """
    question = (snapshot or {}).get("question") or {}
    rules = rules_of(attempt)
    maximum = float(entry.get("points") or 0.0)

    def awaiting_teacher(reason: str):
        return question_engine.GradeResult(
            score=0.0,
            max_score=maximum,
            correct=None,
            requires_manual=True,
            detail={"reason": reason},
        )

    if rules.get("grading_mode") == _label_of(enums.GradingMode.MANUAL):
        return awaiting_teacher("teacher_marks_everything")
    q_type = question.get("type")
    if not q_type:
        return awaiting_teacher("no_pinned_version")

    partial = dict(question.get("partial_scoring") or {})
    if not rules.get("partial_scoring_enabled", True):
        partial["mode"] = "all_or_nothing"
    negative = dict(question.get("negative_scoring") or {})
    if not rules.get("negative_marking_enabled", False):
        negative["penalty"] = 0

    try:
        descriptor = question_engine.descriptor(q_type)
    except ValueError:
        return awaiting_teacher("unknown_question_type")
    if not descriptor.gradable_automatically:
        return awaiting_teacher("type_needs_a_teacher")

    return question_engine.grade(
        q_type,
        question.get("config") or {},
        response,
        score=maximum,
        partial_scoring=partial,
        negative_scoring=negative,
    )


async def save_answer(db: AsyncSession, attempt: ExamAttempt, payload: a_schemas.AnswerSave) -> dict:
    """Store one answer, and return the verdict only if the paper's timing allows it.

    The same call serves the autosave and a submit-time batch. `changed_count` is the number of
    times the learner's own answer moved, which is a fact about the sitting rather than an
    accusation, and it is why a teacher looking at a changed answer can see that they are looking
    at a changed answer.
    """
    if is_closed(attempt):
        raise AttemptClosed("this attempt is already closed", code="attempt_already_closed")
    if is_expired(attempt):
        await finalise(db, attempt, reason="expired")
        raise AttemptClosed("your time on this exam ran out", code="attempt_time_up")

    entry = _entry(attempt, payload.exam_item_id)
    if payload.response is None:
        # A row means the learner wrote something, and a blank is scored as a blank by the
        # totals without needing a row of its own.
        raise AttemptError("there is nothing in that answer yet", code="attempt_answer_empty")

    existing = (
        await db.execute(
            select(AttemptAnswer).where(
                AttemptAnswer.attempt_id == attempt.id,
                AttemptAnswer.exam_item_id == payload.exam_item_id,
            )
        )
    ).scalar_one_or_none()

    version_id = uuid.UUID(entry["question_version_id"]) if entry.get("question_version_id") else None
    snapshot = await exam_service.snapshot_for_version(db, version_id)
    result = _grade(attempt, entry, snapshot, payload.response)

    row = existing
    if row is None:
        row = AttemptAnswer(
            attempt_id=attempt.id,
            exam_item_id=payload.exam_item_id,
            question_version_id=version_id,
            response=payload.response,
            correct=result.correct,
            score=result.score,
            graded=result.correct is not None or not result.requires_manual,
            needs_manual_review=result.requires_manual,
            time_spent_seconds=payload.time_spent_seconds,
            changed_count=0,
            client_updated_at=_parse_client_stamp(payload.client_updated_at) or _now(),
        )
        db.add(row)
    else:
        moved = row.response != payload.response
        if moved:
            row.changed_count = (row.changed_count or 0) + 1
            row.response = payload.response
            row.correct = result.correct
            row.score = result.score
            row.graded = result.correct is not None or not result.requires_manual
            row.needs_manual_review = result.requires_manual
            row.question_version_id = version_id
        stamp = _parse_client_stamp(payload.client_updated_at)
        if stamp is not None:
            newer = _aware(row.client_updated_at) is None or stamp > _aware(row.client_updated_at)
            if newer:
                row.client_updated_at = stamp
        if payload.time_spent_seconds is not None:
            row.time_spent_seconds = (row.time_spent_seconds or 0) + payload.time_spent_seconds
    await db.flush()

    rules = rules_of(attempt)
    instant = rules.get("feedback_timing") == _label_of(enums.FeedbackTiming.INSTANT)
    withheld = not instant
    return a_schemas.AnswerRead(
        recorded=True,
        token=attempt.session_id or "",
        exam_item_id=payload.exam_item_id,
        withheld=withheld,
        correct=None if withheld else result.correct,
        score=None if withheld else result.score,
        max_score=None if withheld else result.max_score,
        requires_manual=result.requires_manual,
        remaining_seconds=remaining_seconds(attempt),
        notice="withheld" if withheld else None,
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Closing a sitting
# --------------------------------------------------------------------------- #


async def _totals(db: AsyncSession, attempt: ExamAttempt) -> tuple[float, float, int, int, int, int, int]:
    """Score, maximum, and the counts a result screen is made of.

    The maximum is the paper's own total rather than the sum of what was answered: a blank
    question is worth nothing and still belongs to the mark out of.

    A line that took part of its mark is counted on its own. `correct` is a two-valued flag, so
    a partially credited answer is not `True`; calling it wrong in the summary would contradict
    the same line two lines above, which shows "2 of 3".
    """
    blueprint = blueprint_of(attempt)
    answers = await _answers_of(db, attempt.id)
    points = float(blueprint.get("points") or 0.0)
    if not points:
        points = round(sum(float(entry.get("points") or 0.0) for entry in entries_of(attempt)), 4)
    score = 0.0
    correct_count = partial_count = wrong_count = pending = 0
    for entry in entries_of(attempt):
        row = answers.get(entry["exam_item_id"])
        if row is None:
            continue
        score += float(row.score or 0.0)
        if row.correct is True:
            correct_count += 1
        elif row.correct is False:
            if float(row.score or 0.0) > 0:
                partial_count += 1
            else:
                wrong_count += 1
        if row.needs_manual_review and not row.graded:
            pending += 1
    return round(score, 4), points, correct_count, partial_count, wrong_count, pending, len(answers)


async def finalise(db: AsyncSession, attempt: ExamAttempt, *, reason: str) -> dict:
    """Close a sitting and mark it. The one path every closing goes through.

    A learner pressing submit, the worker sweeping deadlines and a tab limit all end here, so a
    paper cannot be graded one way by one route and another way by another. Answers already
    stored are graded as they were saved; nothing is re-read from the live bank.
    """
    if is_closed(attempt):
        return await to_read(db, attempt, notice="already_submitted")
    if reason not in a_schemas.CLOSE_REASONS:
        raise AttemptError(
            f"unknown close reason: {reason}",
            code="attempt_close_reason_unknown",
            params={"value": reason},
        )

    rules = rules_of(attempt)
    now = _now()
    started = _aware(attempt.started_at) or now
    attempt.server_seconds_used = max(0, int((now - started).total_seconds()))
    attempt.submitted_at = now

    auto = rules.get("auto_submit_on_expiry", True)
    if reason == "submitted":
        attempt.status = enums.AttemptStatus.SUBMITTED
    elif reason == "tab_limit":
        attempt.status = enums.AttemptStatus.AUTO_SUBMITTED
    else:
        attempt.status = (
            enums.AttemptStatus.AUTO_SUBMITTED if auto else enums.AttemptStatus.EXPIRED
        )

    review_rows = (
        await db.execute(
            select(AttemptAnswer).where(
                AttemptAnswer.attempt_id == attempt.id, AttemptAnswer.needs_manual_review.is_(True)
            )
        )
    ).scalars().all()
    already = {
        str(row[0])
        for row in (
            await db.execute(
                select(ManualReview.answer_id).where(ManualReview.attempt_id == attempt.id)
            )
        ).all()
    }
    for row in review_rows:
        if str(row.id) in already:
            continue
        db.add(ManualReview(attempt_id=attempt.id, answer_id=row.id, reviewed=False))

    score, points, correct_count, partial_count, wrong_count, pending, answered = await _totals(db, attempt)
    attempt.score = score
    attempt.max_score = points
    attempt.passed = _passed(score, points, rules, pending)
    await db.flush()

    student = await db.get(Student, attempt.student_id)
    if student is not None:
        await activity_service.record(
            db,
            student=student,
            event_type=f"exam_attempt_{reason}",
            category=activity_service.CATEGORY_ASSESSMENT,
            session_id=attempt.session_id,
            context_type="exam",
            context_id=attempt.exam_id,
            payload={
                "reason": reason,
                "score": score,
                "max_score": points,
                "answered": answered,
                "correct": correct_count,
                "partial": partial_count,
                "incorrect": wrong_count,
                "awaiting_teacher": pending,
                "status": _label_of(attempt.status),
            },
        )
        await db.flush()
    return await to_read(db, attempt, notice=reason)


def _passed(score: float, points: float, rules: dict, pending: int) -> bool | None:
    """Pass or fail, or `None` when the paper cannot be answered yet.

    A percentage against the paper's own total is how the teacher set it. While an essay is
    still waiting there is no honest answer to give, so the field stays empty rather than
    guessing a verdict that the missing marks may reverse.
    """
    threshold = rules.get("passing_score")
    if threshold is None or pending:
        return None
    if points <= 0:
        return None
    return round(score / points * 100, 4) >= float(threshold)


async def submit(db: AsyncSession, attempt: ExamAttempt, payload: a_schemas.AttemptSubmit) -> dict:
    """Hand the paper in, with anything the tab buffered still to send.

    Handing in twice is not an error and must not be a second result: the closing facts were
    written the first time, so the same result is served again.
    """
    if is_closed(attempt):
        return await result(db, attempt, notice="already_submitted")
    for line in payload.answers or []:
        if is_expired(attempt):
            break
        try:
            await save_answer(db, attempt, line)
        except AttemptError:
            # A line that no longer belongs to the paper cannot stop the rest of it being
            # handed in; the learner's own work is what must survive.
            continue
    await finalise(db, attempt, reason="submitted")
    return await result(db, attempt, notice="closed")


async def report_tab_switch(db: AsyncSession, attempt: ExamAttempt) -> dict:
    """Record that the learner's tab left the paper and came back.

    This is the browser's own report, stored as such: the platform counts it, applies the rule
    the teacher set, and never claims it as evidence of anything.
    """
    rules = rules_of(attempt)
    limit = rules.get("tab_switch_limit")
    action = rules.get("tab_switch_action") or "warn"
    if is_closed(attempt):
        return a_schemas.TabSwitchRead(
            token=attempt.session_id or "",
            tab_switches=attempt.tab_switch_count or 0,
            tab_switch_limit=limit,
            tab_switch_action=action,
            status=_label_of(attempt.status),
            notice="closed",
            remaining_seconds=remaining_seconds(attempt),
        ).model_dump(mode="json")
    if not rules.get("monitor_tab_switch"):
        return a_schemas.TabSwitchRead(
            token=attempt.session_id or "",
            tab_switches=attempt.tab_switch_count or 0,
            status=_label_of(attempt.status),
            notice="monitoring_off",
            remaining_seconds=remaining_seconds(attempt),
        ).model_dump(mode="json")

    attempt.tab_switch_count = (attempt.tab_switch_count or 0) + 1
    count = attempt.tab_switch_count

    student = await db.get(Student, attempt.student_id)
    if student is not None:
        await activity_service.record(
            db,
            student=student,
            event_type="exam_tab_switch",
            category=activity_service.CATEGORY_ASSESSMENT,
            session_id=attempt.session_id,
            context_type="exam",
            context_id=attempt.exam_id,
            payload={"count": count, "limit": limit, "action": action},
        )

    # `count > limit`, not `>=`: a limit of three switches means three are tolerated, and the
    # fourth is the one the rule is about.
    reached = limit is not None and count > int(limit)
    auto_submitted = False
    notice = "tab_switched"
    if reached and action == "auto_submit":
        await finalise(db, attempt, reason="tab_limit")
        notice, auto_submitted = "tab_limit_reached", True
    await db.flush()
    return a_schemas.TabSwitchRead(
        token=attempt.session_id or "",
        tab_switches=count,
        tab_switch_limit=limit,
        tab_switch_action=action,
        status=_label_of(attempt.status),
        notice=notice,
        auto_submitted=auto_submitted,
        remaining_seconds=remaining_seconds(attempt),
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


def _result_state(attempt: ExamAttempt, exam: Exam, *, pending: int) -> tuple[str, bool]:
    """`state` and `visible`, straight from the paper's own visibility rule.

    `awaiting_teacher` and `closed` are different news and the learner's screen says them
    differently: one means the marks exist and are being checked, the other means the paper
    itself is still open and nothing has been decided.

    `after_approval` needs no extra flag on the attempt: the teacher approving a paper is the
    act of marking the last answer on it, so once nothing is waiting the result is shown.
    """
    rules = rules_of(attempt)
    if not is_closed(attempt):
        return "closed", False
    visibility = rules.get("result_visibility") or exam.result_visibility
    if visibility == "hidden":
        return "hidden", False
    if pending:
        return "awaiting_teacher", False
    if visibility == "immediate":
        return "shown", True
    if visibility == "after_close":
        closes = _aware(exam.available_to)
        still_open = closes is not None and _now() < closes
        if still_open and _label_of(exam.status) == enums.ExamStatus.ACTIVE.value:
            return "closed", False
    return "shown", True


async def _reviewer_notes(db: AsyncSession, attempt_id: uuid.UUID) -> dict[str, str]:
    """The teacher's sentence for each answer they marked by hand, keyed by that answer.

    `manual_review.reviewer_note` is written because a learner asked why a line cost marks. Keeping
    it on the review row and off the result screen would make the sentence a note to self.
    """
    rows = (
        await db.execute(
            select(ManualReview.answer_id, ManualReview.reviewer_note).where(
                ManualReview.attempt_id == attempt_id,
                ManualReview.reviewer_note.isnot(None),
            )
        )
    ).all()
    return {str(answer_id): note.strip() for answer_id, note in rows if note and note.strip()}


async def _result_lines(
    db: AsyncSession, attempt: ExamAttempt, *, show_answers: bool, show_explanations: bool
) -> list[dict]:
    answers = await _answers_of(db, attempt.id)
    notes = await _reviewer_notes(db, attempt.id)
    out: list[dict] = []
    version_ids = [
        uuid.UUID(entry["question_version_id"])
        for entry in entries_of(attempt)
        if entry.get("question_version_id")
    ]
    snapshots = await exam_service.snapshots_for_versions(db, version_ids)
    for entry in entries_of(attempt):
        row = answers.get(entry["exam_item_id"])
        snapshot = (
            snapshots.get(uuid.UUID(entry["question_version_id"]))
            if entry.get("question_version_id")
            else None
        )
        question = (snapshot or {}).get("question") or {}
        out.append(
            a_schemas.AttemptResultLine(
                exam_item_id=uuid.UUID(entry["exam_item_id"]),
                prompt=entry.get("title") or question.get("prompt"),
                answered=row is not None,
                correct=row.correct if row is not None and show_answers else None,
                score=float(row.score or 0.0) if row else 0.0,
                max_score=float(entry.get("points") or 0.0),
                requires_manual=bool(row.needs_manual_review) if row else False,
                explanation=question.get("explanation") if show_explanations else None,
                reviewer_note=notes.get(str(row.id)) if row is not None else None,
            ).model_dump(mode="json")
        )
    return out


async def result(db: AsyncSession, attempt: ExamAttempt, *, notice: str | None = None) -> dict:
    """The learner's own result screen, as far as the paper's rules reach."""
    exam = await _exam_of(db, attempt)
    rules = rules_of(attempt)
    score, points, correct_count, partial_count, wrong_count, pending, answered = await _totals(db, attempt)
    if is_closed(attempt):
        # Once the paper is in, the review rows are the record of what is still owed. Counting
        # them here is what lets a re-mark move the total rather than the total chasing it.
        pending = await _pending_reviews(db, attempt.id)
    state, visible = _result_state(attempt, exam, pending=pending)
    show_answers = bool(rules.get("show_correct_answers"))
    show_explanations = bool(rules.get("show_explanations"))

    feedback = await feedback_for(db, attempt.student_id, attempt_id=attempt.id)
    return a_schemas.AttemptResultRead(
        token=attempt.session_id or "",
        exam_id=exam.id,
        title=blueprint_of(attempt).get("title") or exam.title,
        status=_label_of(attempt.status),
        submitted_at=_iso(_aware(attempt.submitted_at)),
        server_seconds_used=attempt.server_seconds_used,
        result_visibility=rules.get("result_visibility") or exam.result_visibility,
        visible=visible,
        state=state,
        notice=notice,
        score=score if visible else None,
        max_score=points if visible else None,
        passed=attempt.passed if visible else None,
        passing_score=rules.get("passing_score"),
        answered_items=answered,
        total_items=len(entries_of(attempt)),
        correct_count=correct_count if visible else 0,
        partial_count=partial_count if visible else 0,
        incorrect_count=wrong_count if visible else 0,
        manual_count=pending,
        show_correct_answers=show_answers,
        lines=await _result_lines(
            db, attempt, show_answers=show_answers, show_explanations=show_explanations
        )
        if visible
        else [],
        feedback=feedback if visible else [],
    ).model_dump(mode="json")


async def _pending_reviews(db: AsyncSession, attempt_id: uuid.UUID) -> int:
    counts = await _pending_of(db, [attempt_id])
    return counts.get(str(attempt_id), 0)


async def _pending_of(db: AsyncSession, attempt_ids: list[uuid.UUID]) -> dict[str, int]:
    """How many answers are still waiting, for several sittings at once.

    One grouped count rather than one per row: the learner's list shows every paper they have,
    and a screen that is going to be opened on a phone in a classroom should not cost a query
    per sitting.
    """
    if not attempt_ids:
        return {}
    rows = (
        await db.execute(
            select(ManualReview.attempt_id, func.count())
            .where(
                ManualReview.reviewed.is_(False),
                ManualReview.attempt_id.in_(attempt_ids),
            )
            .group_by(ManualReview.attempt_id)
        )
    ).all()
    return {str(attempt_id): int(count) for attempt_id, count in rows}


# --------------------------------------------------------------------------- #
# The learner's screens
# --------------------------------------------------------------------------- #


def _best_of(rows: list[ExamAttempt]) -> tuple[float | None, float | None]:
    marked = [row for row in rows if row.score is not None and row.max_score]
    if not marked:
        return None, None
    top = max(marked, key=lambda row: float(row.score))
    return float(top.score), float(top.max_score)


def _rules_read_from_exam(exam: Exam) -> dict:
    return _rules_read(exam_service.rules_payload(exam))


async def learner_exams(db: AsyncSession, student: Student) -> dict:
    """The learner's exam list: what is assigned, whether it can be opened, what it cost.

    Nothing about the composition is here - no questions, no answer keys, no versions. The
    learner is deciding whether to start, and the paper they get is not this screen's business.

    A paper that is still being written, put away, or only *scheduled* is not on the list: a
    teacher's plan for Friday is not yet a task, and the word for it in the availability ladder
    is `closed`, which would read as a missed deadline. The one exception is a learner holding a
    live sitting of such a paper - pulling a paper back must not throw away a sitting that is
    already open, so their row stays with its resume token on it.
    """
    exams = await exam_service.assigned_exams(db, student)
    out: list[dict] = []
    for exam in exams:
        rows = await attempts_of(db, exam.id, student.id)
        allowed, reason, open_row = await availability(db, exam, student)
        resumable = open_row is not None and not is_expired(open_row)
        if _label_of(exam.status) in _HIDDEN_STATUSES and not resumable:
            continue
        used = len(rows)
        best_score, best_max = _best_of(rows)
        latest = rows[-1] if rows else None
        waiting = await _pending_of(db, [row.id for row in rows])
        pending = waiting.get(str(latest.id), 0) if latest is not None and is_closed(latest) else 0
        item_count, points = await _serve_shape(db, exam)
        limit = _attempt_limit(exam)
        out.append(
            a_schemas.LearnerExamRead(
                id=exam.id,
                title=exam.title,
                description=exam.description,
                level=exam.level,
                learning_language=exam.learning_language,
                status=_label_of(exam.status),
                available=allowed,
                availability_reason=reason,
                opens_at=_iso(_aware(exam.available_from)),
                closes_at=_iso(_aware(exam.available_to)),
                duration_minutes=exam.duration_minutes,
                item_count=item_count,
                points=points,
                max_attempts=limit,
                attempts_used=used,
                attempts_left=max(0, limit - used),
                best_score=best_score,
                best_max_score=best_max,
                latest_status=_label_of(latest.status) if latest else None,
                resume_token=open_row.session_id if resumable else None,
                result_visible=any(
                    is_closed(row)
                    and _result_state(row, exam, pending=waiting.get(str(row.id), 0))[1]
                    for row in rows
                ),
                feedback_waiting=pending > 0,
            ).model_dump(mode="json")
        )
    return {"items": out, "total": len(out)}


async def _serve_shape(db: AsyncSession, exam: Exam) -> tuple[int, float]:
    """How many questions and how many marks, from the items as they resolve now.

    Deliberately not a frozen blueprint: a learner deciding whether to start is told what the
    paper is today, and the copy they actually get is fixed when the sitting opens.
    """
    items = await exam_service.resolve_items(db, await exam_service.items_of(db, exam.id))
    return len(items), round(sum(row["effective_points"] for row in items), 4)


async def brief(db: AsyncSession, exam: Exam, student: Student) -> dict:
    """The page before the paper: the rules stated plainly, then the start button."""
    if not await is_assigned(db, exam, student):
        raise NotAssigned("this exam has not been assigned to you", code="exam_not_assigned")
    allowed, reason, open_row = await availability(db, exam, student)
    rows = await attempts_of(db, exam.id, student.id)
    count, points = await _serve_shape(db, exam)
    counts: dict[str, int] = {}
    for row in rows:
        counts[_label_of(row.status)] = counts.get(_label_of(row.status), 0) + 1
    return a_schemas.StudentExamBriefRead(
        id=exam.id,
        title=exam.title,
        description=exam.description,
        level=exam.level,
        item_count=count,
        counts=counts,
        points=points,
        duration_minutes=exam.duration_minutes,
        opens_at=_iso(_aware(exam.available_from)),
        closes_at=_iso(_aware(exam.available_to)),
        must_finish_before_close=exam.must_finish_before_close,
        max_attempts=_attempt_limit(exam),
        attempts_used=len(rows),
        attempts_left=max(0, _attempt_limit(exam) - len(rows)),
        passing_score=exam.passing_score,
        rules=_rules_read_from_exam(exam),
        available=allowed,
        availability_reason=reason,
        resume_token=(
            open_row.session_id if open_row is not None and not is_expired(open_row) else None
        ),
    ).model_dump(mode="json")


async def learner_attempt_history(db: AsyncSession, exam: Exam, student: Student) -> dict:
    rows = await attempts_of(db, exam.id, student.id)
    return a_schemas.LearnerAttemptsRead(
        exam_id=exam.id,
        title=exam.title,
        attempts=[await attempt_row(db, row) for row in rows],
    ).model_dump(mode="json")


async def attempt_row(db: AsyncSession, attempt: ExamAttempt) -> dict:
    answered = await _answered_ids(db, attempt.id)
    return a_schemas.AttemptRowRead(
        id=attempt.id,
        token=attempt.session_id,
        exam_id=attempt.exam_id,
        student_id=attempt.student_id,
        student_name=await display_name(db, attempt.student_id),
        status=_label_of(attempt.status),
        attempt_number=attempt.attempt_number,
        started_at=_iso(attempt.started_at) or "",
        expires_at=_iso(_aware(attempt.expires_at)),
        submitted_at=_iso(_aware(attempt.submitted_at)),
        remaining_seconds=remaining_seconds(attempt),
        server_seconds_used=attempt.server_seconds_used,
        answered_items=len(answered),
        total_items=len(entries_of(attempt)),
        score=attempt.score,
        max_score=attempt.max_score,
        passed=attempt.passed,
        tab_switches=attempt.tab_switch_count,
        needs_review=await _pending_reviews(db, attempt.id),
    ).model_dump(mode="json")


async def display_name(db: AsyncSession, student_id: uuid.UUID) -> str | None:
    student = await db.get(Student, student_id)
    return exam_service.student_name(student) if student else None


# --------------------------------------------------------------------------- #
# The teacher's side
# --------------------------------------------------------------------------- #


async def attempts_for_exam(
    db: AsyncSession,
    exam: Exam,
    *,
    status: str | None = None,
    student_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """Every sitting of one paper, newest first, with how far each one has got.

    A status the teacher typed that isn't one of ours is a question they can answer, not a crash:
    the words come back in the refusal so the list screen can show them what was usable.
    """
    stmt = select(ExamAttempt).where(ExamAttempt.exam_id == exam.id)
    if status:
        try:
            wanted = enums.AttemptStatus(status)
        except ValueError as exc:
            allowed = ", ".join(_label_of(value) for value in enums.AttemptStatus)
            raise AttemptError(
                f"'{status}' is not a sitting status (use one of: {allowed})",
                code="attempt_status_unknown",
                params={"value": status, "allowed": allowed},
            ) from exc
        stmt = stmt.where(ExamAttempt.status == wanted)
    if student_id is not None:
        stmt = stmt.where(ExamAttempt.student_id == student_id)
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        (
            await db.execute(
                stmt.order_by(ExamAttempt.started_at.desc(), ExamAttempt.attempt_number.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        ).scalars()
    )
    return {
        "items": [await attempt_row(db, row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def _answer_reads(db: AsyncSession, attempt: ExamAttempt) -> list[dict]:
    answers = await _answers_of(db, attempt.id)
    version_ids = [
        uuid.UUID(entry["question_version_id"])
        for entry in entries_of(attempt)
        if entry.get("question_version_id")
    ]
    snapshots = await exam_service.snapshots_for_versions(db, version_ids)
    out: list[dict] = []
    for entry in entries_of(attempt):
        row = answers.get(entry["exam_item_id"])
        snapshot = (
            snapshots.get(uuid.UUID(entry["question_version_id"]))
            if entry.get("question_version_id")
            else None
        )
        question = (snapshot or {}).get("question") or {}
        out.append(
            a_schemas.AttemptAnswerRead(
                exam_item_id=uuid.UUID(entry["exam_item_id"]),
                position=entry.get("position", 0),
                kind=entry.get("kind", "question"),
                prompt=entry.get("title") or question.get("prompt"),
                question_type=question.get("type"),
                version=entry.get("version"),
                response=row.response if row else None,
                answered=row is not None,
                correct=row.correct if row else None,
                score=float(row.score) if row and row.score is not None else None,
                max_score=float(entry.get("points") or 0.0),
                graded=bool(row.graded) if row else False,
                needs_manual_review=bool(row.needs_manual_review) if row else False,
                flagged=bool(row.flagged) if row else False,
                time_spent_seconds=row.time_spent_seconds if row else None,
                changed_count=row.changed_count if row else 0,
            ).model_dump(mode="json")
        )
    return out


async def attempt_detail(db: AsyncSession, attempt: ExamAttempt) -> dict:
    """Everything the teacher sees about one sitting, including what the learner may not."""
    exam = await _exam_of(db, attempt)
    rules = rules_of(attempt)
    return a_schemas.AttemptDetailRead(
        id=attempt.id,
        token=attempt.session_id,
        exam_id=exam.id,
        exam_title=blueprint_of(attempt).get("title") or exam.title,
        student_id=attempt.student_id,
        student_name=await display_name(db, attempt.student_id),
        status=_label_of(attempt.status),
        attempt_number=attempt.attempt_number,
        started_at=_iso(attempt.started_at) or "",
        expires_at=_iso(_aware(attempt.expires_at)),
        submitted_at=_iso(_aware(attempt.submitted_at)),
        server_seconds_used=attempt.server_seconds_used,
        score=attempt.score,
        max_score=attempt.max_score,
        passed=attempt.passed,
        passing_score=rules.get("passing_score"),
        tab_switches=attempt.tab_switch_count,
        monitor_tab_switch=bool(rules.get("monitor_tab_switch")),
        tab_switch_action=rules.get("tab_switch_action"),
        tab_switch_limit=rules.get("tab_switch_limit"),
        answers=await _answer_reads(db, attempt),
        feedback=await feedback_for(db, attempt.student_id, attempt_id=attempt.id),
    ).model_dump(mode="json")


async def grading_queue(
    db: AsyncSession,
    *,
    exam_id: uuid.UUID | None = None,
    student_id: uuid.UUID | None = None,
    reviewed: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """The answers waiting for a teacher, with enough on each row to mark it from.

    A queue row is the review row, not the answer: an essay marked twice would be a total that
    moved under the learner, and `uq_manual_review_answer` is what stops that from being possible.
    """
    stmt = (
        select(
            ManualReview.id,
            ManualReview.attempt_id,
            ManualReview.answer_id,
            ManualReview.reviewed,
            ManualReview.final_score,
            ManualReview.reviewer_note,
            ManualReview.reviewed_at,
            ManualReview.ai_suggestion,
            AttemptAnswer.response,
            AttemptAnswer.score,
            AttemptAnswer.exam_item_id,
            AttemptAnswer.question_version_id,
            ExamAttempt.exam_id,
            ExamAttempt.student_id,
            ExamAttempt.submitted_at,
            Exam.title,
            Student,
        )
        .join(AttemptAnswer, AttemptAnswer.id == ManualReview.answer_id)
        .join(ExamAttempt, ExamAttempt.id == ManualReview.attempt_id)
        .join(Exam, Exam.id == ExamAttempt.exam_id)
        .join(Student, Student.id == ExamAttempt.student_id)
    )
    conditions = []
    if exam_id is not None:
        conditions.append(ExamAttempt.exam_id == exam_id)
    if student_id is not None:
        conditions.append(ExamAttempt.student_id == student_id)
    conditions.append(ManualReview.reviewed.is_(True) if reviewed else ManualReview.reviewed.is_(False))
    stmt = stmt.where(*conditions)

    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await db.execute(
            stmt.order_by(ExamAttempt.submitted_at.desc().nulls_last(), ManualReview.created_at)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    waiting = await _entries_map(db, [row.attempt_id for row in rows])
    snapshots = await exam_service.snapshots_for_versions(
        db, [row.question_version_id for row in rows if row.question_version_id]
    )
    items = []
    for row in rows:
        snapshot = snapshots.get(row.question_version_id) if row.question_version_id else None
        question = (snapshot or {}).get("question") or {}
        entry = waiting.get((str(row.attempt_id), str(row.exam_item_id))) or {}
        items.append(
            a_schemas.ManualReviewRead(
                id=row.id,
                attempt_id=row.attempt_id,
                answer_id=row.answer_id,
                exam_id=row.exam_id,
                exam_title=row.title,
                student_id=row.student_id,
                student_name=exam_service.student_name(row.Student),
                question_id=question.get("id"),
                question_type=question.get("type"),
                prompt=entry.get("title") or question.get("prompt"),
                response=row.response,
                #: Out of what the paper gave this line, not what the bank says now: the teacher
                #: has to be marking on the scale the learner was asked to answer in.
                max_score=float(entry.get("points") or 0.0),
                #: The engine's provisional mark exists only while the answer is waiting. Once
                #: the teacher has marked it, `final_score` is the number and there is no second
                #: one to show alongside it.
                auto_score=None if row.reviewed else row.score,
                reviewed=row.reviewed,
                final_score=row.final_score,
                reviewer_note=row.reviewer_note,
                submitted_at=_iso(_aware(row.submitted_at)),
                ai_suggestion=row.ai_suggestion,
            ).model_dump(mode="json")
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def get_review(db: AsyncSession, review_id: uuid.UUID) -> ManualReview:
    row = await db.get(ManualReview, review_id)
    if row is None:
        raise AttemptNotFound("no answer is waiting under that id", code="grading_review_not_found")
    return row


async def grade_answer(
    db: AsyncSession, review: ManualReview, payload: a_schemas.GradeAnswerRequest, *, admin_id: uuid.UUID
) -> dict:
    """Mark one answer the engine could not decide, and let the paper's total follow it.

    The teacher's mark is the record: `final_score` is what counts, and the answer row is updated
    to match so a result screen and a grading screen cannot disagree. Re-marking is allowed and
    audited with the value it moved from, because a misread script is corrected, not rewritten.
    """
    answer = await db.get(AttemptAnswer, review.answer_id)
    attempt = await get_attempt_by_id(db, review.attempt_id)
    if answer is None:
        raise AttemptNotFound("the answer this review points at is gone", code="grading_answer_gone")
    if not is_closed(attempt):
        raise AttemptError("the learner has not handed this paper in yet", code="grading_not_submitted_yet")

    entry = _entry(attempt, answer.exam_item_id)
    maximum = float(entry.get("points") or 0.0)
    score = float(payload.score) if payload.score is not None else 0.0
    if score > maximum:
        raise AttemptError(
            f"that answer is worth {maximum:g}, so it cannot be given {score:g}",
            code="grading_mark_above_max",
            params={"max": maximum, "score": score},
        )

    before = {"final_score": review.final_score, "reviewed": review.reviewed, "score": answer.score}
    review.final_score = score
    review.reviewed = True
    review.reviewed_at = _now()
    review.reviewer_note = payload.note
    answer.score = score
    answer.graded = True
    answer.needs_manual_review = False
    answer.correct = payload.correct if payload.correct is not None else score >= maximum

    rules = rules_of(attempt)
    score_total, points, _correct, _partial, _wrong, pending, _answered = await _totals(db, attempt)
    attempt.score = score_total
    attempt.max_score = points
    attempt.passed = _passed(score_total, points, rules, pending)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.answer.grade",
        actor_id=admin_id,
        target_type="attempt_answer",
        target_id=answer.id,
        before=before,
        after={"final_score": score, "correct": answer.correct, "note": payload.note},
        detail=f"manual mark on attempt {attempt.id} of exam {attempt.exam_id}",
    )
    return {
        "review": (await review_read(db, review)),
        "attempt": await attempt_detail(db, attempt),
    }


async def review_read(db: AsyncSession, review: ManualReview) -> dict:
    """One review row as the grading screen sees it."""
    answer = await db.get(AttemptAnswer, review.answer_id)
    attempt = await get_attempt_by_id(db, review.attempt_id)
    exam = await _exam_of(db, attempt)
    entry = _blueprint_entry(attempt, answer.exam_item_id if answer else None)
    student = await db.get(Student, attempt.student_id)
    snapshot = (
        await exam_service.snapshot_for_version(db, answer.question_version_id)
        if answer and answer.question_version_id
        else None
    )
    question = (snapshot or {}).get("question") or {}
    return a_schemas.ManualReviewRead(
        id=review.id,
        attempt_id=review.attempt_id,
        answer_id=review.answer_id,
        exam_id=exam.id,
        exam_title=blueprint_of(attempt).get("title") or exam.title,
        student_id=attempt.student_id,
        student_name=exam_service.student_name(student) if student else None,
        question_id=question.get("id"),
        question_type=question.get("type"),
        prompt=entry.get("title") or question.get("prompt"),
        response=answer.response if answer else None,
        max_score=float(entry.get("points") or 0.0),
        # This answer reached the review box because nothing marked it, so there is no engine
        # verdict to show beside the teacher's - and after marking, `final_score` is the number.
        auto_score=None,
        reviewed=review.reviewed,
        final_score=review.final_score,
        reviewer_note=review.reviewer_note,
        submitted_at=_iso(_aware(attempt.submitted_at)),
        ai_suggestion=review.ai_suggestion,
    ).model_dump(mode="json")


async def grading_summary(db: AsyncSession) -> dict:
    """What is waiting, and who is waiting for it, on one screen."""
    pending = int(
        await db.scalar(
            select(func.count()).select_from(ManualReview).where(ManualReview.reviewed.is_(False))
        )
        or 0
    )
    since = _now() - timedelta(days=1)
    reviewed_today = int(
        await db.scalar(
            select(func.count())
            .select_from(ManualReview)
            .where(ManualReview.reviewed.is_(True), ManualReview.reviewed_at >= since)
        )
        or 0
    )
    per_exam = (
        await db.execute(
            select(ExamAttempt.exam_id, func.count())
            .join(ManualReview, ManualReview.attempt_id == ExamAttempt.id)
            .where(ManualReview.reviewed.is_(False))
            .group_by(ExamAttempt.exam_id)
        )
    ).all()
    exams = []
    for exam_id, count in per_exam:
        exam = await db.get(Exam, exam_id)
        exams.append(
            {
                "exam_id": str(exam_id),
                "title": exam.title if exam else None,
                "pending": int(count),
            }
        )
    per_student = (
        await db.execute(
            select(ExamAttempt.student_id, func.count())
            .join(ManualReview, ManualReview.attempt_id == ExamAttempt.id)
            .where(ManualReview.reviewed.is_(False))
            .group_by(ExamAttempt.student_id)
        )
    ).all()
    students = []
    for student_id, count in per_student:
        student = await db.get(Student, student_id)
        students.append(
            {
                "student_id": str(student_id),
                "name": exam_service.student_name(student) if student else None,
                "pending": int(count),
            }
        )
    return a_schemas.GradingSummaryRead(
        pending=pending,
        reviewed_today=reviewed_today,
        exams=exams,
        students=students,
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Teacher feedback the learner may read
# --------------------------------------------------------------------------- #


async def feedback_for(
    db: AsyncSession, student_id: uuid.UUID, *, attempt_id: uuid.UUID | None = None
) -> list[dict]:
    """The notes a teacher wrote *to* a learner, in the order they were written.

    Private teacher notes are a different table and never appear here; this read is the surface
    a learner is allowed to see.
    """
    stmt = select(TeacherFeedback).where(TeacherFeedback.student_id == student_id)
    if attempt_id is not None:
        stmt = stmt.where(TeacherFeedback.attempt_id == attempt_id)
    return [
        a_schemas.FeedbackRead(
            id=row.id,
            attempt_id=row.attempt_id,
            answer_id=row.answer_id,
            student_id=row.student_id,
            body=row.body,
            created_at=_iso(row.created_at) or "",
        ).model_dump(mode="json")
        for row in (await db.execute(stmt.order_by(TeacherFeedback.created_at))).scalars()
    ]


async def add_feedback(
    db: AsyncSession,
    *,
    student: Student,
    payload: a_schemas.FeedbackRequest,
    admin_id: uuid.UUID,
    attempt: ExamAttempt | None = None,
) -> dict:
    if attempt is not None and attempt.student_id != student.id:
        raise AttemptNotFound("that attempt belongs to a different learner", code="attempt_other_learner")
    if payload.answer_id is not None:
        answer = await db.get(AttemptAnswer, payload.answer_id)
        if answer is None:
            raise AttemptNotFound("no such answer", code="attempt_answer_not_found")
        if attempt is None:
            attempt = await get_attempt_by_id(db, answer.attempt_id)
        if answer.attempt_id != (attempt.id if attempt else None):
            raise AttemptError(
                "that answer is not on the attempt being given feedback",
                code="grading_answer_not_on_attempt",
            )
    row = TeacherFeedback(
        attempt_id=attempt.id if attempt else None,
        answer_id=payload.answer_id,
        student_id=student.id,
        body=payload.body,
    )
    db.add(row)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.feedback.add",
        actor_id=admin_id,
        target_type="teacher_feedback",
        target_id=row.id,
        after={"student_id": str(student.id), "attempt_id": str(row.attempt_id) if row.attempt_id else None},
    )
    return a_schemas.FeedbackRead(
        id=row.id,
        attempt_id=row.attempt_id,
        answer_id=row.answer_id,
        student_id=row.student_id,
        body=row.body,
        created_at=_iso(row.created_at) or "",
    ).model_dump(mode="json")


async def remove_feedback(db: AsyncSession, feedback_id: uuid.UUID, *, admin_id: uuid.UUID) -> dict:
    row = await db.get(TeacherFeedback, feedback_id)
    if row is None:
        raise AttemptNotFound("no such feedback", code="feedback_not_found")
    await db.delete(row)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="exam.feedback.remove",
        actor_id=admin_id,
        target_type="teacher_feedback",
        target_id=feedback_id,
        before={"student_id": str(row.student_id)},
    )
    return {"removed": True}


# --------------------------------------------------------------------------- #
# The sweep the worker calls
# --------------------------------------------------------------------------- #


async def due_attempts(db: AsyncSession) -> list[ExamAttempt]:
    """Open sittings whose server deadline has passed.

    `ix_attempt_open_expiry` is what makes this a read of the open papers rather than of the
    whole attempt history, which is the only reason a sweep every fifteen seconds is affordable.
    """
    return list(
        (
            await db.execute(
                select(ExamAttempt)
                .where(
                    ExamAttempt.status == enums.AttemptStatus.IN_PROGRESS,
                    ExamAttempt.expires_at.is_not(None),
                    ExamAttempt.expires_at <= _now(),
                )
                .order_by(ExamAttempt.expires_at)
            )
        ).scalars()
    )


async def expire_due(db: AsyncSession) -> dict:
    """Close every open sitting that is past its deadline. Returns what it did.

    The learner's own requests close their attempt too - this is for the papers nobody came back
    for, so a deadline cannot be outlasted simply by not pressing a button.
    """
    closed = 0
    submitted = 0
    for attempt in await due_attempts(db):
        rules = rules_of(attempt)
        await finalise(db, attempt, reason="expired")
        closed += 1
        if rules.get("auto_submit_on_expiry", True):
            submitted += 1
    return {"closed": closed, "submitted": submitted, "at": _iso(_now())}


def meta() -> dict:
    """The words the attempt screens branch on, from the code that produces them."""
    return a_schemas.AttemptMeta(
        statuses=[_label_of(value) for value in enums.AttemptStatus],
        exam_statuses=list(exam_service.SETTABLE_STATUSES),
        availability_reasons=list(_AVAILABILITY_REASONS),
        result_states=list(_RESULT_STATES),
        close_reasons=list(a_schemas.CLOSE_REASONS),
        notices=list(NOTICES),
    ).model_dump(mode="json")
