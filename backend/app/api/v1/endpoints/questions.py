"""Question bank API (Phase 3): the teacher's content library and the engine behind it.

`/types` and `/bulk` are declared before `/{question_id}` so their literal paths are
never parsed as a UUID. Every write requires an admin session; the student-facing
shape of a question is only ever produced by `question_service.student_view`, which
removes the answer key.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import NotFound, ValidationFailed
from app.models.identity import AdminUser
from app.schemas import question as q_schemas
from app.services import question_engine, question_service
from app.services.question_service import QuestionInputError

router = APIRouter(prefix="/questions", tags=["questions"])


def _fail(exc: QuestionInputError) -> ValidationFailed:
    """One mapping, so a service-level rule cannot arrive at the browser as a 500."""
    return ValidationFailed(str(exc))


@router.get("/types", response_model=None)
async def list_question_types(_admin: AdminUser = Depends(deps.get_current_admin)) -> dict:
    """The type vocabulary the editor is built from - never a hardcoded client list."""
    return {"items": question_engine.registry_info(), "total": len(question_engine.type_keys())}


@router.post("/bulk", response_model=None)
async def bulk_action(
    payload: q_schemas.BulkRequest,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await question_service.bulk(db, payload, admin_id=_admin.id)
    except QuestionInputError as exc:
        raise _fail(exc) from None


@router.get("", response_model=None)
async def list_questions(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    type: str | None = Query(default=None, alias="type"),
    status: str | None = None,
    level: str | None = None,
    learning_language: str | None = None,
    context_kind: str | None = None,
    topic_id: UUID | None = None,
    tag_id: UUID | None = None,
    reading_id: UUID | None = None,
    listening_id: UUID | None = None,
    source_file_id: UUID | None = None,
    has_media: bool | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    try:
        return await question_service.list_questions(
            db,
            q=q,
            question_type=type,
            status=status,
            level=level,
            learning_language=learning_language,
            context_kind=context_kind,
            topic_id=topic_id,
            tag_id=tag_id,
            reading_id=reading_id,
            listening_id=listening_id,
            source_file_id=source_file_id,
            has_media=has_media,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except QuestionInputError as exc:
        raise _fail(exc) from None


@router.post("", response_model=None, status_code=201)
async def create_question(
    payload: q_schemas.QuestionCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await question_service.create_question(db, payload, admin_id=admin.id)
    except QuestionInputError as exc:
        raise _fail(exc) from None


async def _load(db: AsyncSession, question_id: UUID, *, allow_trash: bool = True):
    question = await question_service.get(db, question_id, allow_trash=allow_trash)
    if question is None:
        raise NotFound("Question not found")
    return question


@router.get("/{question_id}", response_model=None)
async def get_question(
    question_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    include_trash: bool = True,
) -> dict:
    question = await _load(db, question_id, allow_trash=include_trash)
    return await question_service.to_read(db, question)


@router.patch("/{question_id}", response_model=None)
async def update_question(
    question_id: UUID,
    payload: q_schemas.QuestionUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    question = await _load(db, question_id, allow_trash=False)
    try:
        return await question_service.update_question(db, question, payload, admin_id=admin.id)
    except QuestionInputError as exc:
        raise _fail(exc) from None


@router.delete("/{question_id}", response_model=None)
async def trash_question(
    question_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft delete. Nothing is destroyed: the question waits in the trash."""
    question = await _load(db, question_id)
    payload = await question_service.trash(db, question, admin_id=admin.id)
    return {"ok": True, "trashed": payload["id"], "status": payload["status"]}


@router.post("/{question_id}/restore", response_model=None)
async def restore_question(
    question_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    question = await _load(db, question_id)
    return await question_service.restore(db, question, admin_id=admin.id)


@router.post("/{question_id}/status", response_model=None)
async def set_status(
    question_id: UUID,
    payload: q_schemas.QuestionStatusUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    question = await _load(db, question_id, allow_trash=False)
    try:
        return await question_service.set_status(db, question, payload.status, admin_id=admin.id)
    except QuestionInputError as exc:
        raise _fail(exc) from None


@router.post("/{question_id}/clone", response_model=None, status_code=201)
async def clone_question(
    question_id: UUID,
    status: str = Query(default="draft"),
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    question = await _load(db, question_id, allow_trash=False)
    try:
        return await question_service.clone_question(db, question, admin_id=admin.id, status=status)
    except QuestionInputError as exc:
        raise _fail(exc) from None


@router.post("/{question_id}/taxonomy", response_model=None)
async def assign_taxonomy(
    question_id: UUID,
    payload: q_schemas.TaxonomyAssignment,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    question = await _load(db, question_id, allow_trash=False)
    try:
        return await question_service.assign_taxonomy(db, question, payload, admin_id=admin.id)
    except QuestionInputError as exc:
        raise _fail(exc) from None


@router.get("/{question_id}/preview", response_model=None)
async def preview_question(
    question_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The question exactly as a learner would receive it - no answer key."""
    question = await _load(db, question_id, allow_trash=False)
    return await question_service.student_view(db, question)


@router.get("/{question_id}/versions", response_model=None)
async def list_versions(
    question_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _load(db, question_id)
    items = await question_service.list_versions(db, question_id)
    return {"items": items, "total": len(items)}


@router.get("/{question_id}/versions/{version}", response_model=None)
async def get_version(
    question_id: UUID,
    version: int,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _load(db, question_id)
    row = await question_service.get_version(db, question_id, version)
    if row is None:
        raise NotFound(f"Version {version} of this question does not exist")
    return row


@router.post("/{question_id}/grade", response_model=None)
async def grade_answer(
    question_id: UUID,
    payload: q_schemas.GradeRequest,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    at_version: int | None = Query(default=None),
) -> dict:
    """Check an answer now - the same code path an attempt will use later.

    `at_version` grades against a frozen snapshot, which is how a past attempt keeps
    scoring the way it did before the question was edited.
    """
    question = await _load(db, question_id)
    if at_version is not None and await question_service.get_version(db, question_id, at_version) is None:
        raise NotFound(f"Version {at_version} of this question does not exist")
    try:
        return await question_service.grade_response(db, question, payload.response, at_version=at_version)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from None
