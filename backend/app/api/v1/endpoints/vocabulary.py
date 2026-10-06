"""Vocabulary bank API (Phase 4): the teacher's word list and the learner's cards.

Two routers, because two audiences read the same rows very differently.

* `/vocabulary` is the teacher surface: every entry, every field, lifecycle included.
  `/meta` and `/bulk` are declared before `/{entry_id}` so their literal paths are
  never parsed as a UUID.
* `/student/vocabulary` serves ready, non-trashed entries only, and its payload drops
  the teacher's private notes and provenance. A draft is a word the teacher has not
  finished, so it answers 404 rather than half a card.

Nothing here decides what a word means: validation and the duplicate rule live in
`vocabulary_service`, so the worker filling the bank in Phase 8 goes through exactly
the same rules a teacher's browser does.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models.identity import AdminUser, Student
from app.schemas import vocabulary as v_schemas
from app.services import vocabulary_service
from app.services.vocabulary_service import DuplicateWord, VocabularyError

router = APIRouter(prefix="/vocabulary", tags=["vocabulary"])
student_router = APIRouter(prefix="/student/vocabulary", tags=["student-vocabulary"])


def _fail(exc: VocabularyError) -> ValidationFailed:
    """One mapping, so a service-level rule cannot arrive at the browser as a 500."""
    return ValidationFailed(str(exc))


def _clash(exc: DuplicateWord) -> Conflict:
    return Conflict("word_exists", str(exc))


@router.get("/meta", response_model=None)
async def vocabulary_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Languages, levels and parts of speech the editor is built from - never a
    hardcoded client list."""
    return await vocabulary_service.language_options(db)


@router.post("/bulk", response_model=None)
async def bulk_action(
    payload: v_schemas.VocabularyBulkRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await vocabulary_service.bulk(db, payload, admin_id=admin.id)
    except VocabularyError as exc:
        raise _fail(exc) from None
    except DuplicateWord as exc:
        raise _clash(exc) from None


@router.get("", response_model=None)
async def list_vocabulary(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    q_kind: str = "contains",
    learning_language: str | None = None,
    level: str | None = None,
    status: str | None = None,
    part_of_speech: str | None = None,
    tag_id: UUID | None = None,
    translation_language: str | None = None,
    has_audio: bool | None = None,
    view: str = "bank",
    sort: str = "word",
    order: str = "asc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    try:
        return await vocabulary_service.list_entries(
            db,
            q=q,
            q_kind=q_kind,
            learning_language=learning_language,
            level=level,
            status=status,
            part_of_speech=part_of_speech,
            tag_id=tag_id,
            translation_language=translation_language,
            has_audio=has_audio,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except VocabularyError as exc:
        raise _fail(exc) from None


@router.post("", response_model=None, status_code=201)
async def create_vocabulary(
    payload: v_schemas.VocabularyCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await vocabulary_service.create_entry(db, payload, admin_id=admin.id)
    except VocabularyError as exc:
        raise _fail(exc) from None
    except DuplicateWord as exc:
        raise _clash(exc) from None


async def _load(db: AsyncSession, entry_id: UUID, *, allow_trash: bool = True):
    entry = await vocabulary_service.get(db, entry_id, allow_trash=allow_trash)
    if entry is None:
        raise NotFound("Vocabulary entry not found")
    return entry


@router.get("/{entry_id}", response_model=None)
async def get_vocabulary(
    entry_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    include_trash: bool = True,
) -> dict:
    entry = await _load(db, entry_id, allow_trash=include_trash)
    return await vocabulary_service.to_read(db, entry)


@router.patch("/{entry_id}", response_model=None)
async def update_vocabulary(
    entry_id: UUID,
    payload: v_schemas.VocabularyUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entry = await _load(db, entry_id, allow_trash=False)
    try:
        return await vocabulary_service.update_entry(db, entry, payload, admin_id=admin.id)
    except VocabularyError as exc:
        raise _fail(exc) from None
    except DuplicateWord as exc:
        raise _clash(exc) from None


@router.delete("/{entry_id}", response_model=None)
async def trash_vocabulary(
    entry_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft delete. Nothing is destroyed: the entry waits in the trash."""
    entry = await _load(db, entry_id)
    payload = await vocabulary_service.trash(db, entry, admin_id=admin.id)
    return {"ok": True, "trashed": payload["id"], "status": payload["status"]}


@router.post("/{entry_id}/restore", response_model=None)
async def restore_vocabulary(
    entry_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entry = await _load(db, entry_id)
    try:
        return await vocabulary_service.restore(db, entry, admin_id=admin.id)
    except DuplicateWord as exc:
        raise _clash(exc) from None


@router.post("/{entry_id}/status", response_model=None)
async def set_status(
    entry_id: UUID,
    payload: v_schemas.VocabularyStatusUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entry = await _load(db, entry_id, allow_trash=False)
    try:
        return await vocabulary_service.set_status(db, entry, payload.status, admin_id=admin.id)
    except VocabularyError as exc:
        raise _fail(exc) from None


@router.get("/{entry_id}/preview", response_model=None)
async def preview_for_learner(
    entry_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    language: str | None = Query(default=None, max_length=16),
) -> dict:
    """The study card this entry would produce, on the teacher's own screen.

    Projected by the same function a learner hits, so a card that looks wrong here is
    wrong for the class too. A draft previews as well: the teacher is deciding whether
    to publish it.
    """
    entry = await _load(db, entry_id)
    return await vocabulary_service.learner_view(db, entry, language=language)


@router.post("/{entry_id}/taxonomy", response_model=None)
async def assign_taxonomy(
    entry_id: UUID,
    payload: v_schemas.VocabularyTaxonomyAssignment,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entry = await _load(db, entry_id, allow_trash=False)
    try:
        return await vocabulary_service.assign_taxonomy(db, entry, payload, admin_id=admin.id)
    except VocabularyError as exc:
        raise _fail(exc) from None


@student_router.get("", response_model=None)
async def student_list_vocabulary(
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    level: str | None = None,
    learning_language: str | None = None,
    tag_id: UUID | None = None,
    translation_language: str | None = None,
    sort: str = "word",
    order: str = "asc",
    page: int = 1,
    page_size: int = 30,
) -> dict:
    """The word list a learner browses: ready entries, no teacher notes."""
    try:
        return await vocabulary_service.list_for_learner(
            db,
            q=q,
            level=level,
            learning_language=learning_language,
            tag_id=tag_id,
            translation_language=translation_language,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except VocabularyError as exc:
        raise _fail(exc) from None


@student_router.get("/meta", response_model=None)
async def student_vocabulary_meta(
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The options behind a learner's own filters, from the same configuration the
    teacher edits. Declared before `/{entry_id}` so `/meta` is never read as a word id.

    Deliberately narrower than the admin meta: no lifecycle states and no tag list,
    because a learner has neither to set.
    """
    options = await vocabulary_service.language_options(db)
    return {
        "learning_languages": options["learning_languages"],
        "translation_languages": options["translation_languages"],
        "levels": options["levels"],
    }


@student_router.get("/{entry_id}", response_model=None)
async def student_vocabulary_card(
    entry_id: UUID,
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
    language: str | None = Query(default=None, max_length=16),
) -> dict:
    entry = await vocabulary_service.get(db, entry_id)
    if entry is None or not vocabulary_service.is_learner_visible(entry):
        raise NotFound("Vocabulary entry not found")
    return await vocabulary_service.learner_view(db, entry, language=language)
