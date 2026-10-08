"""The activity log: the only place a learner's practice is written down (Phase 6).

`activity_event` is append-only by design, and this module is the only writer. Two rules
fall out of that, and both are why the practice runner has no table of its own:

* **A row is never edited to change a verdict.** An answer that was wrong stays wrong;
  marking a word "I know this" after "still learning" does not delete the first row, it
  adds a second one. Whatever the screens show is read back from the log, so a teacher
  reading a timeline in Phase 9 and an analyst aggregating it in Phase 10 are looking at
  the same facts the learner produced.
* **A practice run is a `session_id`, not a row.** Student self-practice is deliberately
  not persisted as an exam definition: it has no pinned question versions, no timer and
  no submission deadline, so there is nothing to freeze. The run is the set of events that
  carry one server-issued token, and its summary is rebuilt from them on request.

`occurred_at` is written here rather than left to a database default, because the log is
sorted by it and an event appended inside a long transaction must still land where the
learner actually did it.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.activity import ActivityEvent
from app.models.identity import Student

#: The `category` split the analytics queries are built on: what a learner did, versus
#: what they were asked and how they answered.
CATEGORY_ACTIVITY = "activity"
CATEGORY_ASSESSMENT = "assessment"

EVENT_RUN_START = "practice_run_start"
EVENT_RUN_FINISH = "practice_run_finish"
EVENT_ANSWER = "practice_answer"
EVENT_MARK_KNOWN = "practice_mark_known"
EVENT_MARK_LEARNING = "practice_mark_learning"
EVENT_FAVORITE_ADD = "favorite_add"
EVENT_FAVORITE_REMOVE = "favorite_remove"

#: The two marks a learner may put on a card, as event types. Read back last-wins, so the
#: newest row for a (context_type, context_id) pair is the current state and no row has to
#: be rewritten. The words themselves belong to the request schema; only their event names
#: live here.
MARK_EVENTS = {EVENT_MARK_KNOWN: "known", EVENT_MARK_LEARNING: "learning"}

#: `ip` is a 64-character column and `device`/`browser` are 120: a User-Agent that is
#: longer than that is truncated rather than refused, because losing the event to a long
#: header would lose the answer with it.
_MAX_IP = 64
_MAX_TEXT = 120

#: Browser families named only when the client's own header says so. This is a coarse
#: label for a monitoring timeline, not a fingerprint, and "other" is honest about the
#: rest rather than guessing a name the header never contained.
_BROWSER_MARKERS = (
    ("edg", "edge"),
    ("opr", "opera"),
    ("firefox", "firefox"),
    ("chrome", "chrome"),
    ("safari", "safari"),
)


def _context() -> tuple[str | None, str | None]:
    """This request's IP and User-Agent, or two `None`s outside a request (a worker job)."""
    from app.core.middleware import request_context

    ctx = request_context.get()
    return ctx.ip, ctx.user_agent


def _browser_of(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    lowered = user_agent.lower()
    for marker, name in _BROWSER_MARKERS:
        if marker in lowered:
            return name
    return "other"


def _device_of(user_agent: str | None) -> str | None:
    """`mobile`, `tablet` or `desktop` - or `None` when the header says nothing usable."""
    if not user_agent:
        return None
    lowered = user_agent.lower()
    if "tablet" in lowered or "ipad" in lowered:
        return "tablet"
    if "mobile" in lowered or "iphone" in lowered or "android" in lowered:
        return "mobile"
    return "desktop"


async def record(
    db: AsyncSession,
    *,
    student: Student,
    event_type: str,
    category: str = CATEGORY_ACTIVITY,
    session_id: str | None = None,
    human_summary: str | None = None,
    context_type: str | None = None,
    context_id: uuid.UUID | None = None,
    ref_question_id: uuid.UUID | None = None,
    correct: bool | None = None,
    time_spent_seconds: int | None = None,
    payload: dict[str, Any] | None = None,
) -> ActivityEvent:
    """Append one event. Nothing here updates a previous row.

    `time_spent_seconds` is what the learner's own browser measured and sent. It is stored
    as reported and never as a server-side guess: a duration this backend cannot vouch for
    would be a number nobody can defend when a teacher asks where it came from.
    """
    ip, user_agent = _context()
    event = ActivityEvent(
        student_id=student.id,
        session_id=session_id,
        category=category,
        event_type=event_type,
        human_summary=human_summary,
        context_type=context_type,
        context_id=context_id,
        ref_question_id=ref_question_id,
        correct=correct,
        time_spent_seconds=None if time_spent_seconds is None else max(0, int(time_spent_seconds)),
        ip=(ip or "")[:_MAX_IP] or None,
        device=(_device_of(user_agent) or "")[:_MAX_TEXT] or None,
        browser=(_browser_of(user_agent) or "")[:_MAX_TEXT] or None,
        occurred_at=security.utcnow(),
        payload=dict(payload or {}),
    )
    db.add(event)
    await db.flush()
    return event


async def events_of_session(
    db: AsyncSession, student_id: uuid.UUID, session_id: str
) -> list[ActivityEvent]:
    """One learner's own events under one run token, oldest first.

    Scoped by `student_id` as well as the token: a token is a 32-hex string, and the
    second condition is what makes "someone else's run" unreadable rather than merely
    unlikely to be guessed.
    """
    rows = (
        await db.execute(
            select(ActivityEvent)
            .where(
                ActivityEvent.student_id == student_id,
                ActivityEvent.session_id == session_id,
            )
            .order_by(ActivityEvent.occurred_at, ActivityEvent.id)
        )
    ).scalars().all()
    return list(rows)


async def session_ids_of(
    db: AsyncSession,
    student_id: uuid.UUID,
    *,
    context_type: str | None = None,
    context_id: uuid.UUID | None = None,
    event_type: str = EVENT_RUN_START,
) -> list[str]:
    """The run tokens this learner has opened, newest first.

    Read from the *start* events, so a run the learner never finished still appears in
    their history with whatever they did answer - and a token they invented appears
    nowhere, because nothing wrote a start row for it.
    """
    stmt = (
        select(ActivityEvent.session_id, ActivityEvent.occurred_at)
        .where(
            ActivityEvent.student_id == student_id,
            ActivityEvent.event_type == event_type,
            ActivityEvent.session_id.is_not(None),
        )
        .order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc())
    )
    if context_type is not None:
        stmt = stmt.where(ActivityEvent.context_type == context_type)
    if context_id is not None:
        stmt = stmt.where(ActivityEvent.context_id == context_id)
    rows = (await db.execute(stmt)).all()
    return [row[0] for row in rows if row[0]]


async def latest_states(
    db: AsyncSession, student_id: uuid.UUID, *, event_types: tuple[str, ...]
) -> dict[tuple[str, uuid.UUID], str]:
    """Last-event-wins state per `(context_type, context_id)` for this learner.

    One ordered scan of the learner's own rows, applied so the newest wins. The alternative
    - a "known words" table with an update per click - would throw away the history that
    says how many times a word was un-marked, which is the part a teacher asks about.
    """
    rows = (
        await db.execute(
            select(
                ActivityEvent.context_type,
                ActivityEvent.context_id,
                ActivityEvent.event_type,
            )
            .where(
                ActivityEvent.student_id == student_id,
                ActivityEvent.event_type.in_(list(event_types)),
                ActivityEvent.context_id.is_not(None),
            )
            .order_by(ActivityEvent.occurred_at, ActivityEvent.id)
        )
    ).all()
    states: dict[tuple[str, uuid.UUID], str] = {}
    for context_type, context_id, event_type in rows:
        state = MARK_EVENTS.get(event_type)
        if state is not None:
            states[(context_type, context_id)] = state
    return states
