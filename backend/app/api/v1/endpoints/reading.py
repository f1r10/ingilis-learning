"""Reading texts API (Phase 5): the teacher's passages and the learner's reading block.

Two routers, because one text is two different screens.

* `/reading` is the editor: the text, its lifecycle, and the question sets filed under it.
  `/meta` and `/bulk` are declared before `/{reading_id}` so their literal paths are never
  parsed as a UUID, and the set routes hang off `/sets/{set_id}` - a set knows its own
  passage, so the browser editing one block does not have to name the text above it.
* `/student/reading` serves ready texts only, and answers 404 for anything else: a draft
  is a lesson the teacher has not finished, and "not found" is the only honest sentence
  for a learner about it.

Nothing here decides whether a question may be filed under a set. That rule is
`passage_service`'s, so the Phase 8 importer and a teacher's drag go through the same
door - and a text's own field rules are `reading_service`'s, which is why `word_count`
is never accepted from a client.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import NotFound, ValidationFailed
from app.models.identity import AdminUser, Student
from app.schemas import passage as p_schemas
from app.schemas import reading as r_schemas
from app.services import passage_service, reading_service
from app.services.passage_service import PassageError, PassageNotFound

router = APIRouter(prefix="/reading", tags=["reading"])
student_router = APIRouter(prefix="/student/reading", tags=["student-reading"])

KIND = passage_service.READING


def _fail(exc: PassageError) -> ValidationFailed:
    """One mapping, so a service-level rule cannot arrive at the browser as a 500."""
    return ValidationFailed(str(exc))


async def _load(db: AsyncSession, reading_id: UUID, *, allow_trash: bool = True):
    try:
        return await passage_service.get_passage(db, KIND, reading_id, allow_trash=allow_trash)
    except PassageNotFound as exc:
        raise NotFound(str(exc)) from None


async def _load_set(db: AsyncSession, set_id: UUID):
    """The set and its passage. Both are needed because the rules are checked per passage."""
    try:
        return await passage_service.get_set(db, KIND, set_id)
    except PassageNotFound as exc:
        raise NotFound(str(exc)) from None


@router.get("/meta", response_model=None)
async def reading_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Languages, bands, layouts, sort names and caps - from the code that enforces them."""
    return await reading_service.meta(db)


@router.post("/bulk", response_model=None)
async def bulk_action(
    payload: p_schemas.PassageBulkRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Status, trash, restore or level over a selection, answered per id."""
    try:
        return await reading_service.bulk(db, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("", response_model=None)
async def list_readings(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
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
    try:
        return await reading_service.list_readings(
            db,
            q=q,
            language=language,
            level=level,
            status=status,
            layout=layout,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except PassageError as exc:
        raise _fail(exc) from None


@router.post("", response_model=None, status_code=201)
async def create_reading(
    payload: r_schemas.ReadingCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await reading_service.create(db, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.patch("/sets/{set_id}", response_model=None)
async def update_set(
    set_id: UUID,
    payload: p_schemas.QuestionSetUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        _, passage = await _load_set(db, set_id)
        return await passage_service.update_set(
            db, KIND, passage, set_id, payload, admin_id=admin.id
        )
    except PassageError as exc:
        raise _fail(exc) from None


@router.delete("/sets/{set_id}", response_model=None)
async def delete_set(
    set_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove the grouping. The questions stay bound to the text and return to the pool."""
    _, passage = await _load_set(db, set_id)
    try:
        return await passage_service.delete_set(db, KIND, passage, set_id, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.post("/sets/{set_id}/questions", response_model=None)
async def assign_questions(
    set_id: UUID,
    payload: p_schemas.SetQuestionAssignment,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Make this set hold exactly these questions, in this order.

    The response names what moved in from a sibling set and what left the set, because a
    block that quietly lost a question reads like a save that failed somewhere else.
    Grouping writes no content, so no question version is appended by this call.
    """
    _, passage = await _load_set(db, set_id)
    try:
        return await passage_service.assign_questions(
            db, KIND, passage, set_id, list(payload.question_ids), admin_id=admin.id
        )
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("/{reading_id}", response_model=None)
async def get_reading(
    reading_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    include_trash: bool = True,
) -> dict:
    row = await _load(db, reading_id, allow_trash=include_trash)
    return await reading_service.to_read(db, row)


@router.patch("/{reading_id}", response_model=None)
async def update_reading(
    reading_id: UUID,
    payload: r_schemas.ReadingUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _load(db, reading_id, allow_trash=False)
    try:
        return await reading_service.update(db, row, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.delete("/{reading_id}", response_model=None)
async def trash_reading(
    reading_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft delete. The questions bound to the text are not touched."""
    row = await _load(db, reading_id)
    payload = await reading_service.trash(db, row, admin_id=admin.id)
    return {"ok": True, "trashed": payload["id"], "status": payload["status"]}


@router.post("/{reading_id}/restore", response_model=None)
async def restore_reading(
    reading_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _load(db, reading_id)
    return await reading_service.restore(db, row, admin_id=admin.id)


@router.post("/{reading_id}/status", response_model=None)
async def set_status(
    reading_id: UUID,
    payload: p_schemas.StatusUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Draft, ready or archived.

    A trashed text still loads here: `change_status` answers it with the sentence the
    teacher needs ("restore the reading from the trash before changing its status"),
    while a bare 404 would leave them wondering whether the row exists at all.
    """
    row = await _load(db, reading_id)
    try:
        return await reading_service.set_status(db, row, payload.status, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("/{reading_id}/sets", response_model=None)
async def list_sets(
    reading_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The sets with the questions filed under each, in order."""
    row = await _load(db, reading_id)
    try:
        return {"items": await passage_service.list_sets(db, KIND, row.id)}
    except PassageError as exc:
        raise _fail(exc) from None


@router.post("/{reading_id}/sets", response_model=None, status_code=201)
async def create_set(
    reading_id: UUID,
    payload: p_schemas.QuestionSetCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row = await _load(db, reading_id, allow_trash=False)
        return await passage_service.create_set(db, KIND, row, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.post("/{reading_id}/sets/reorder", response_model=None)
async def reorder_sets(
    reading_id: UUID,
    payload: p_schemas.SetReorderRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Renumber the sets. The request must name every set of this text."""
    row = await _load(db, reading_id, allow_trash=False)
    try:
        return {
            "items": await passage_service.reorder_sets(
                db, KIND, row, list(payload.set_ids), admin_id=admin.id
            )
        }
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("/{reading_id}/preview", response_model=None)
async def preview_for_learner(
    reading_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The reading block a learner would get, on the teacher's own screen.

    Projected by the same function the student route calls - a draft previews, because the
    teacher is deciding whether to publish it, and what they see here is exactly what a
    published version would show. The media paths are the teacher's own, because this
    response is read by the admin session that asked for it.
    """
    row = await _load(db, reading_id)
    return await reading_service.learner_detail(db, row, served_to_admin=True)


@student_router.get("/meta", response_model=None)
async def student_reading_meta(
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The options behind a learner's own filters. Declared before `/{reading_id}` so
    `/meta` is never read as a text id, and narrower than the teacher's meta: a learner
    has no lifecycle state, view or layout to set, and the sort names are exactly the
    ones the student list accepts."""
    meta = await reading_service.meta(db)
    return {
        "learning_languages": meta["learning_languages"],
        "levels": meta["levels"],
        "sortable": meta["sortable"],
    }


@student_router.get("", response_model=None)
async def student_list_readings(
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Ready texts, with only the questions a learner may answer counted on each row."""
    try:
        return await reading_service.list_for_learner(
            db,
            q=q,
            language=language,
            level=level,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except PassageError as exc:
        raise _fail(exc) from None


@student_router.get("/{reading_id}", response_model=None)
async def student_reading(
    reading_id: UUID,
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await reading_service.get(db, reading_id)
    if row is None or not passage_service.is_learner_visible(row):
        raise NotFound("Reading not found")
    return await reading_service.learner_detail(db, row)
