"""Student exam API (Phase 7): the learner's papers, and the one sitting their token names.

The token is the whole relationship. A learner never names an attempt id, an exam id or an
attempt number when they answer - they present the token this backend issued when the sitting
opened, and `exam_id`, the order, the marks and the deadline are read back from the row it points
at. A body that let the browser name the attempt it was answering would let one tab report an
answer into somebody else's paper.

So a token that is not one, and a token that belongs to another learner, are answered the same
way (404): an attempt id is not a secret a learner is entitled to probe.

Every read carries `remaining_seconds` computed here on the server from the stored deadline, and
`/answer`, `/submit` and `/tab-switch` all close an expired sitting on the way in. The worker's
sweep is a backstop for papers nobody came back for, not the only path - otherwise a learner who
submits one second late would be graded by a different rule than one who submits early.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import (
    NotFound,
    as_conflict,
    as_invalid,
    as_missing,
)
from app.models.identity import Student
from app.schemas import attempt as a_schemas
from app.services import attempt_service, exam_service
from app.services.attempt_service import (
    AttemptClosed,
    AttemptError,
    AttemptNotFound,
    NotAssigned,
)

router = APIRouter(prefix="/student/exams", tags=["student-exams"])
attempts_router = APIRouter(prefix="/student/attempts", tags=["student-exams"])


async def _attempt(db: AsyncSession, token: str, student: Student):
    """The sitting this learner's token names - or the same 404 for any other answer."""
    try:
        return await attempt_service.get_attempt(db, token, student=student)
    except AttemptNotFound as exc:
        raise as_missing(exc) from None


async def _assigned_exam(db: AsyncSession, exam_id: UUID, student: Student):
    """The learner's own paper, or the one refusal that says no more than "this is not yours"."""
    not_mine = NotFound("no exam is assigned to you under that id", code="exam_not_assigned")
    try:
        exam = await exam_service.get(db, exam_id, allow_trash=False)
    except exam_service.ExamNotFound:
        # Answered exactly as a paper that exists but was never handed over: confirming that an
        # id is real would let a learner walk the register looking for one.
        raise not_mine from None
    if not await attempt_service.is_assigned(db, exam, student):
        raise not_mine
    return exam


@router.get("/meta", response_model=None)
async def student_exams_meta(
    _student: Student = Depends(deps.get_current_student),
) -> dict:
    """The words the exam and result screens branch on, from the code that produces them."""
    return attempt_service.meta()


@router.get("", response_model=None)
async def my_exams(
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every paper assigned to this learner, with whether it can be opened and what it cost."""
    return await attempt_service.learner_exams(db, student)


@router.get("/{exam_id}", response_model=None)
async def exam_brief(
    exam_id: UUID,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The page before the paper: the rules stated plainly, then the start button.

    No question, no option and no answer key is here - a learner deciding whether to start does
    not need the composition, and showing it would make the paper worth nothing.
    """
    exam = await _assigned_exam(db, exam_id, student)
    return await attempt_service.brief(db, exam, student)


@router.get("/{exam_id}/attempts", response_model=None)
async def my_attempts(
    exam_id: UUID,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """This learner's own history of one paper, in the order they sat it."""
    exam = await _assigned_exam(db, exam_id, student)
    return await attempt_service.learner_attempt_history(db, exam, student)


@router.post("/{exam_id}/start", response_model=None, status_code=201)
async def start_exam(
    exam_id: UUID,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Open the paper, or be handed back the sitting that is already open.

    A second tab, a reconnect and a device switch all arrive here and all get the same sitting
    with the same order and the same answers in it. The response carries the token every later
    request presents.
    """
    exam = await _assigned_exam(db, exam_id, student)
    try:
        return await attempt_service.start(db, exam, student)
    except NotAssigned as exc:
        # The assignment was withdrawn between the check and the open. The paper is not the
        # learner's to sit, and that is the whole answer.
        raise as_missing(exc) from None
    except attempt_service.AttemptError as exc:
        raise as_invalid(exc) from None


@attempts_router.get("/{token}", response_model=None)
async def attempt_state(
    token: str,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The sitting as the server sees it: the clock, how much is answered, which rule applies."""
    attempt = await _attempt(db, token, student)
    if attempt_service.is_expired(attempt):
        # Closing on the way in is what makes the deadline the same news on every screen: a
        # learner who never pressed anything still gets the sitting's own result, not a form.
        return await attempt_service.finalise(db, attempt, reason="expired")
    return await attempt_service.to_read(db, attempt)


@attempts_router.get("/{token}/steps", response_model=None)
async def attempt_steps(
    token: str,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The whole paper in the order this sitting was dealt, with the learner's own answers.

    A resume reads from here rather than from anything the browser kept, which is why a closed
    tab and a different device land back on the same page with the same marks.
    """
    attempt = await _attempt(db, token, student)
    return await attempt_service.steps(db, attempt)


@attempts_router.post("/answer", response_model=None)
async def save_answer(
    payload: a_schemas.AttemptAnswerRequest,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Store one answer. The verdict comes back only when the paper's own timing allows it.

    Under `after_session` the response says it was recorded and holds the verdict back with
    `withheld: true`, because a blank where a mark was expected reads to a learner as a bug.
    """
    attempt = await _attempt(db, payload.token, student)
    try:
        return await attempt_service.save_answer(db, attempt, payload)
    except AttemptClosed as exc:
        # A refusal at the deadline has already closed the paper: the score, the closing time and
        # the server's own seconds are written by that closure, and the session would roll them
        # back with the error this request is about to raise. The learner has been told their time
        # ran out, so the answer they were given has to be the state the table keeps.
        await db.commit()
        raise as_conflict(exc) from None
    except AttemptError as exc:
        raise as_invalid(exc) from None


@attempts_router.post("/submit", response_model=None)
async def submit_attempt(
    payload: a_schemas.AttemptSubmit,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Hand the paper in, with anything the tab buffered still to send.

    Handing in twice is not an error and is not a second result: the closing facts were written
    the first time, so the same result is served again with `already_submitted`.
    """
    attempt = await _attempt(db, payload.token, student)
    try:
        return await attempt_service.submit(db, attempt, payload)
    except AttemptError as exc:
        raise as_invalid(exc) from None


@attempts_router.post("/tab-switch", response_model=None)
async def tab_switch(
    payload: a_schemas.TabSwitchReport,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record that the learner's tab left the paper and came back.

    Only counted while the paper's own rule says to watch. It is the browser's report of a page
    becoming invisible, and nothing here calls it a finding about what was opened.
    """
    attempt = await _attempt(db, payload.token, student)
    try:
        return await attempt_service.report_tab_switch(db, attempt)
    except AttemptError as exc:
        raise as_invalid(exc) from None


@attempts_router.get("/{token}/result", response_model=None)
async def attempt_result(
    token: str,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The result screen, as far as the paper's visibility rules reach.

    `state` says which rule is holding the marks back: waiting for the paper to close, waiting
    for the teacher, or not shown at all.
    """
    attempt = await _attempt(db, token, student)
    if attempt_service.is_expired(attempt):
        await attempt_service.finalise(db, attempt, reason="expired")
    return await attempt_service.result(db, attempt)
