"""Exams API (Phase 7): the teacher's papers, their composition, and who they are handed to.

Three shapes run through this file, and each of them is a reason a route is where it is:

* **A paper pins its content.** Adding a question takes the version current at that moment and
  never follows the bank again, so `PATCH /items/{item_id}` can change a mark or a section but
  has no way to name a different question or version. A teacher who wants that makes a copy.
* **A composition locks when somebody sits it.** Once one attempt exists, sections and items
  refuse to move (409 `composition_locked`) while the paper's *rules* stay editable, because
  the rules are copied into each sitting's own blueprint when it opens.
* **A paper is assigned, not opened.** Nothing here decides who may sit what; the assignment
  rows do, and a learner who is not named cannot get a token for the paper.

`/meta` and `/bulk` are declared before `/{exam_id}` so their literal paths are never parsed as
a UUID. Section, item and attempt routes carry the id of the row being edited rather than the
exam above it - a reference already knows which paper holds it, and a screen that re-names the
exam on every edit gives the browser two ways to disagree.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import (
    NotFound,
    as_conflict,
    as_invalid,
    as_missing,
)
from app.models.identity import AdminUser, Student
from app.schemas import attempt as a_schemas
from app.schemas import exam as e_schemas
from app.services import attempt_service, exam_service
from app.services.exam_service import (
    DuplicateReference,
    ExamError,
    ExamNotFound,
    LockedComposition,
    NameTaken,
)

router = APIRouter(prefix="/exams", tags=["exams"])
grading_router = APIRouter(prefix="/grading", tags=["grading"])


async def _load(db: AsyncSession, exam_id: UUID, *, allow_trash: bool = True):
    try:
        return await exam_service.get(db, exam_id, allow_trash=allow_trash)
    except ExamNotFound as exc:
        raise as_missing(exc) from None


async def _load_item(db: AsyncSession, item_id: UUID):
    try:
        return await exam_service.get_item(db, item_id)
    except ExamNotFound as exc:
        raise as_missing(exc) from None


async def _load_section(db: AsyncSession, section_id: UUID):
    try:
        return await exam_service.get_section(db, section_id)
    except ExamNotFound as exc:
        raise as_missing(exc) from None


async def _load_attempt(db: AsyncSession, attempt_id: UUID):
    try:
        return await attempt_service.get_attempt_by_id(db, attempt_id)
    except attempt_service.AttemptNotFound as exc:
        raise as_missing(exc) from None


@router.get("/meta", response_model=None)
async def exams_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Statuses, kinds, timings, visibilities and caps - from the code that enforces them."""
    return await exam_service.meta(db)


@router.post("/bulk", response_model=None)
async def bulk_action(
    payload: e_schemas.ExamBulkRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Status, trash or restore over a selection, answered per id with a reason for each refusal."""
    try:
        return await exam_service.bulk(db, payload, admin_id=admin.id)
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.get("", response_model=None)
async def list_exams(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    status: str | None = None,
    language: str | None = None,
    level: str | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """The paper list: `bank` for working drafts, `trash` for the bin, `all` for both."""
    try:
        return await exam_service.list_exams(
            db,
            q=q,
            status=status,
            language=language,
            level=level,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.post("", response_model=None, status_code=201)
async def create_exam(
    payload: e_schemas.ExamCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A new paper, always a draft, with its rules already set to what the teacher asked for."""
    try:
        return await exam_service.create(db, payload, admin_id=admin.id)
    except NameTaken as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.get("/{exam_id}", response_model=None)
async def get_exam(
    exam_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One paper whole: rules, sections, items with the version each is pinned to, assignments."""
    exam = await _load(db, exam_id)
    return await exam_service.to_read(db, exam)


@router.patch("/{exam_id}", response_model=None)
async def update_exam(
    exam_id: UUID,
    payload: e_schemas.ExamUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Title, classification or rules. The rules stay editable after a sitting exists."""
    exam = await _load(db, exam_id, allow_trash=False)
    try:
        return await exam_service.update(db, exam, payload, admin_id=admin.id)
    except NameTaken as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.delete("/{exam_id}", response_model=None)
async def trash_exam(
    exam_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bin a paper. Sittings it already holds are not touched, and never will be by this call."""
    exam = await _load(db, exam_id)
    try:
        return await exam_service.trash(db, exam, admin_id=admin.id)
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.post("/{exam_id}/restore", response_model=None)
async def restore_exam(
    exam_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lift it out of the bin, refusing to create two live papers under one title."""
    exam = await _load(db, exam_id)
    try:
        return await exam_service.restore(db, exam, admin_id=admin.id)
    except NameTaken as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.post("/{exam_id}/status", response_model=None)
async def set_status(
    exam_id: UUID,
    payload: e_schemas.StatusUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Draft, scheduled, active, finished, archived. Publishing checks the paper is servable."""
    exam = await _load(db, exam_id, allow_trash=False)
    try:
        return await exam_service.set_status(db, exam, payload.status, admin_id=admin.id)
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.post("/{exam_id}/clone", response_model=None, status_code=201)
async def clone_exam(
    exam_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A new draft holding the same pinned versions - the answer to changing a paper already sat."""
    exam = await _load(db, exam_id)
    try:
        return await exam_service.clone(db, exam, admin_id=admin.id)
    except NameTaken as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.get("/{exam_id}/preview", response_model=None)
async def preview_exam(
    exam_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    seed: str | None = Query(default=None, max_length=32),
) -> dict:
    """The paper as one sitting of it would be dealt, drawn by the code that serves learners.

    Reads only, and nothing is stored: a preview is the teacher deciding, and writing a blueprint
    here would put a paper into the history that nobody sat. Pass `seed` back to see the order a
    sitting already got.
    """
    exam = await _load(db, exam_id)
    try:
        return await exam_service.preview(db, exam, seed_hex=seed)
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.post("/{exam_id}/sections", response_model=None, status_code=201)
async def create_section(
    exam_id: UUID,
    payload: e_schemas.SectionCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A named part of the paper. Items keep their own order inside it."""
    exam = await _load(db, exam_id, allow_trash=False)
    try:
        return await exam_service.create_section(db, exam, payload, admin_id=admin.id)
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.post("/{exam_id}/sections/reorder", response_model=None)
async def reorder_sections(
    exam_id: UUID,
    payload: e_schemas.SectionReorderRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Renumber the parts. The request has to name every section of this paper."""
    exam = await _load(db, exam_id, allow_trash=False)
    try:
        return await exam_service.reorder_sections(db, exam, payload, admin_id=admin.id)
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.patch("/sections/{section_id}", response_model=None)
async def update_section(
    section_id: UUID,
    payload: e_schemas.SectionUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    exam, section = await _load_section(db, section_id)
    try:
        return await exam_service.update_section(db, section, payload, admin_id=admin.id)
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.delete("/sections/{section_id}", response_model=None)
async def delete_section(
    section_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a part. Its items go back to the paper's unsectioned run rather than being deleted."""
    _exam, section = await _load_section(db, section_id)
    try:
        return await exam_service.delete_section(db, section, admin_id=admin.id)
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.get("/{exam_id}/items", response_model=None)
async def list_items(
    exam_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every line of the paper in order, each resolved against the version it is pinned to."""
    exam = await _load(db, exam_id)
    items = await exam_service.resolve_items(db, await exam_service.items_of(db, exam.id))
    return {"items": items, "count": len(items)}


@router.post("/{exam_id}/items", response_model=None, status_code=201)
async def add_items(
    exam_id: UUID,
    payload: e_schemas.ItemsAdd,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Append references. A passage adds the questions under it, each pinned to its own version.

    All of the request is checked before anything is written, so a selection that holds one
    already-added question adds nothing rather than half a paper.
    """
    exam = await _load(db, exam_id, allow_trash=False)
    try:
        return await exam_service.add_items(db, exam, payload, admin_id=admin.id)
    except DuplicateReference as exc:
        raise as_conflict(exc) from None
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.patch("/items/{item_id}", response_model=None)
async def update_item(
    item_id: UUID,
    payload: e_schemas.ItemUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """This line's mark, or which part it sits in. Never the question or the version behind it."""
    _exam, item = await _load_item(db, item_id)
    try:
        return await exam_service.update_item(db, item, payload, admin_id=admin.id)
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.delete("/items/{item_id}", response_model=None)
async def remove_item(
    item_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Take one line off the paper. Answers already given to it are not removed with it."""
    _exam, item = await _load_item(db, item_id)
    try:
        return await exam_service.remove_item(db, item, admin_id=admin.id)
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.post("/{exam_id}/items/reorder", response_model=None)
async def reorder_items(
    exam_id: UUID,
    payload: e_schemas.ReorderRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Renumber the whole paper, or one part of it - the request has to name every line."""
    exam = await _load(db, exam_id, allow_trash=False)
    try:
        return await exam_service.reorder(db, exam, payload, admin_id=admin.id)
    except LockedComposition as exc:
        raise as_conflict(exc) from None
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.get("/{exam_id}/assignments", response_model=None)
async def list_assignments(
    exam_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Who the paper was handed to, with how many of them have sat it."""
    exam = await _load(db, exam_id)
    return {"exam_id": str(exam.id), "items": await exam_service.list_assignments(db, exam)}


@router.post("/{exam_id}/assignments", response_model=None, status_code=201)
async def assign_exam(
    exam_id: UUID,
    payload: e_schemas.AssignmentCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Hand the paper to students or groups. A group is read live, so a member who joins tomorrow
    is assigned with the rest of the class without the teacher doing it twice."""
    exam = await _load(db, exam_id, allow_trash=False)
    try:
        return await exam_service.assign(db, exam, payload, admin_id=admin.id)
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.delete("/assignments/{assignment_id}", response_model=None)
async def unassign_exam(
    assignment_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Take the assignment back. Sittings already started are untouched."""
    try:
        assignment = await exam_service.get_assignment(db, assignment_id)
    except ExamNotFound as exc:
        raise as_missing(exc) from None
    try:
        return await exam_service.unassign(db, assignment, admin_id=admin.id)
    except ExamError as exc:
        raise as_invalid(exc) from None


@router.get("/{exam_id}/attempts", response_model=None)
async def exam_attempts(
    exam_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
    student_id: UUID | None = None,
    page: int = 1,
    page_size: int = Query(default=25, le=100),
) -> dict:
    """Every sitting of one paper, newest first, with how far each one has got."""
    exam = await _load(db, exam_id)
    try:
        return await attempt_service.attempts_for_exam(
            db,
            exam,
            status=status,
            student_id=student_id,
            page=page,
            page_size=page_size,
        )
    except attempt_service.AttemptError as exc:
        raise as_invalid(exc) from None


# --------------------------------------------------------------------------- #
# The teacher's grading surface
# --------------------------------------------------------------------------- #


@router.get("/attempts/{attempt_id}", response_model=None)
async def attempt_detail(
    attempt_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One sitting whole: the clock it ran on, every line, every answer, and what the switches cost."""
    attempt = await _load_attempt(db, attempt_id)
    return await attempt_service.attempt_detail(db, attempt)


@grading_router.get("/meta", response_model=None)
async def grading_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The attempt words the grading screens branch on, from the code that produces them."""
    return attempt_service.meta()


@grading_router.get("/summary", response_model=None)
async def grading_summary(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What is waiting, on which paper, for whom."""
    return await attempt_service.grading_summary(db)


@grading_router.get("/queue", response_model=None)
async def grading_queue(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    exam_id: UUID | None = None,
    student_id: UUID | None = None,
    reviewed: bool = False,
    page: int = 1,
    page_size: int = Query(default=25, le=100),
) -> dict:
    """The answers the engine could not decide, one row per answer waiting."""
    return await attempt_service.grading_queue(
        db,
        exam_id=exam_id,
        student_id=student_id,
        reviewed=reviewed,
        page=page,
        page_size=page_size,
    )


@grading_router.post("/answers/{review_id}/grade", response_model=None)
async def grade_answer(
    review_id: UUID,
    payload: a_schemas.GradeAnswerRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark one answer, and let the paper's total follow it. Re-marking is allowed and audited."""
    try:
        review = await attempt_service.get_review(db, review_id)
    except attempt_service.AttemptNotFound as exc:
        raise as_missing(exc) from None
    try:
        return await attempt_service.grade_answer(db, review, payload, admin_id=admin.id)
    except attempt_service.AttemptNotFound as exc:
        raise as_missing(exc) from None
    except attempt_service.AttemptError as exc:
        raise as_invalid(exc) from None


@grading_router.get("/answers/{review_id}", response_model=None)
async def read_review(
    review_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One waiting answer on its own, for a screen that opened it from the queue."""
    try:
        review = await attempt_service.get_review(db, review_id)
    except attempt_service.AttemptNotFound as exc:
        raise as_missing(exc) from None
    return await attempt_service.review_read(db, review)


@grading_router.get("/students/{student_id}/feedback", response_model=None)
async def list_feedback(
    student_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the teacher has written to this learner, in the order it was written."""
    return {"items": await attempt_service.feedback_for(db, student_id)}


@grading_router.post("/attempts/{attempt_id}/feedback", response_model=None, status_code=201)
async def add_feedback(
    attempt_id: UUID,
    payload: a_schemas.FeedbackRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Write something the learner may read, about one sitting of one paper."""
    try:
        attempt = await attempt_service.get_attempt_by_id(db, attempt_id)
    except attempt_service.AttemptNotFound as exc:
        raise as_missing(exc) from None
    student = await db.get(Student, attempt.student_id)
    if student is None:
        raise NotFound("that learner is not in the register", code="grading_student_unknown")
    try:
        return await attempt_service.add_feedback(
            db, student=student, payload=payload, admin_id=admin.id, attempt=attempt
        )
    except attempt_service.AttemptError as exc:
        raise as_invalid(exc) from None


@grading_router.delete("/feedback/{feedback_id}", response_model=None)
async def remove_feedback(
    feedback_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Take a note back. It is audited with the learner it was written for."""
    try:
        return await attempt_service.remove_feedback(db, feedback_id, admin_id=admin.id)
    except attempt_service.AttemptNotFound as exc:
        raise as_missing(exc) from None
