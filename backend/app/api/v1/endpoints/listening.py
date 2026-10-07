"""Listening items API (Phase 5): the teacher's recordings and the learner's player.

Two routers, because a recording is an exercise on one screen and a player on another.

* `/listening` is the editor: the playback rules, the transcript, its sets. The set routes
  take `start_seconds` / `end_seconds`, which is a *question* boundary rather than a media
  edit - the stored file is untouched and the player is told which seconds to play.
  `/meta` and `/bulk` are declared before `/{listening_id}` so their literal paths are
  never parsed as a UUID.
* `/student/listening` serves ready items only, and the transcript on that surface follows
  `show_transcript`: a listening exercise whose words are on the screen is a reading
  exercise, and the teacher decided which one this is.

The bytes are not served here. `audio` carries a path into the media module, which
authorises each range request by the session that made it - so no route on this screen
accepts a URL, and no student path opens a file the library has thrown away.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import NotFound, ValidationFailed
from app.models.identity import AdminUser, Student
from app.schemas import listening as l_schemas
from app.schemas import passage as p_schemas
from app.services import listening_service, passage_service
from app.services.passage_service import PassageError, PassageNotFound

router = APIRouter(prefix="/listening", tags=["listening"])
student_router = APIRouter(prefix="/student/listening", tags=["student-listening"])

KIND = passage_service.LISTENING


def _fail(exc: PassageError) -> ValidationFailed:
    """One mapping, so a service-level rule cannot arrive at the browser as a 500.

    This is where "that file is in the trash" and "a recording needs audio or a
    transcript before it can be published" become teacher-facing sentences.
    """
    return ValidationFailed(str(exc))


async def _load(db: AsyncSession, listening_id: UUID, *, allow_trash: bool = True):
    try:
        return await passage_service.get_passage(db, KIND, listening_id, allow_trash=allow_trash)
    except PassageNotFound as exc:
        raise NotFound(str(exc)) from None


async def _load_set(db: AsyncSession, set_id: UUID):
    try:
        return await passage_service.get_set(db, KIND, set_id)
    except PassageNotFound as exc:
        raise NotFound(str(exc)) from None


@router.get("/meta", response_model=None)
async def listening_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Languages, bands, sort names, replay ceiling - from the code that enforces them."""
    return await listening_service.meta(db)


@router.post("/bulk", response_model=None)
async def bulk_action(
    payload: p_schemas.PassageBulkRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Status, trash, restore or level over a selection, answered per id.

    A recording with nothing to hear is refused row by row rather than aborting the whole
    selection, because a teacher publishing a unit of lessons should not have to re-run
    thirty-nine items because one has no file.
    """
    try:
        return await listening_service.bulk(db, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("", response_model=None)
async def list_listenings(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    status: str | None = None,
    has_audio: bool | None = None,
    show_transcript: bool | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    try:
        return await listening_service.list_listenings(
            db,
            q=q,
            language=language,
            level=level,
            status=status,
            has_audio=has_audio,
            show_transcript=show_transcript,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except PassageError as exc:
        raise _fail(exc) from None


@router.post("", response_model=None, status_code=201)
async def create_listening(
    payload: l_schemas.ListeningCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add a recording that points at a file already in the library.

    There is no upload here and no URL to paste: the file is uploaded to `/media` and
    named by its id, so the library keeps its checksum, its provenance and its answer to
    "is anything using this?".
    """
    try:
        return await listening_service.create(db, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.patch("/sets/{set_id}", response_model=None)
async def update_set(
    set_id: UUID,
    payload: p_schemas.ListeningSetUpdate,
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
    """Remove the block. The questions stay bound to the recording."""
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
    """Make this block hold exactly these questions, in this order."""
    _, passage = await _load_set(db, set_id)
    try:
        return await passage_service.assign_questions(
            db, KIND, passage, set_id, list(payload.question_ids), admin_id=admin.id
        )
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("/{listening_id}", response_model=None)
async def get_listening(
    listening_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    include_trash: bool = True,
) -> dict:
    row = await _load(db, listening_id, allow_trash=include_trash)
    return await listening_service.to_read(db, row)


@router.patch("/{listening_id}", response_model=None)
async def update_listening(
    listening_id: UUID,
    payload: l_schemas.ListeningUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Patch the item. Absent fields are untouched, and the transcript's provenance is
    written by the server from what arrives here."""
    row = await _load(db, listening_id, allow_trash=False)
    try:
        return await listening_service.update(db, row, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.delete("/{listening_id}", response_model=None)
async def trash_listening(
    listening_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft delete the item. The recording in the library is not touched."""
    row = await _load(db, listening_id)
    payload = await listening_service.trash(db, row, admin_id=admin.id)
    return {"ok": True, "trashed": payload["id"], "status": payload["status"]}


@router.post("/{listening_id}/restore", response_model=None)
async def restore_listening(
    listening_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _load(db, listening_id)
    return await listening_service.restore(db, row, admin_id=admin.id)


@router.post("/{listening_id}/status", response_model=None)
async def set_status(
    listening_id: UUID,
    payload: p_schemas.StatusUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Draft, ready or archived. Going to `ready` is refused when the class would hear
    nothing.

    A trashed recording still loads here, so that `change_status`'s own sentence about
    restoring it reaches the teacher instead of a bare 404.
    """
    row = await _load(db, listening_id)
    try:
        return await listening_service.set_status(db, row, payload.status, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("/{listening_id}/sets", response_model=None)
async def list_sets(
    listening_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _load(db, listening_id)
    try:
        return {"items": await passage_service.list_sets(db, KIND, row.id)}
    except PassageError as exc:
        raise _fail(exc) from None


@router.post("/{listening_id}/sets", response_model=None, status_code=201)
async def create_set(
    listening_id: UUID,
    payload: p_schemas.ListeningSetCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Append a block, optionally a slice of the recording."""
    row = await _load(db, listening_id, allow_trash=False)
    try:
        return await passage_service.create_set(db, KIND, row, payload, admin_id=admin.id)
    except PassageError as exc:
        raise _fail(exc) from None


@router.post("/{listening_id}/sets/reorder", response_model=None)
async def reorder_sets(
    listening_id: UUID,
    payload: p_schemas.SetReorderRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _load(db, listening_id, allow_trash=False)
    try:
        return {
            "items": await passage_service.reorder_sets(
                db, KIND, row, list(payload.set_ids), admin_id=admin.id
            )
        }
    except PassageError as exc:
        raise _fail(exc) from None


@router.get("/{listening_id}/preview", response_model=None)
async def preview_for_learner(
    listening_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The player a learner would get - transcript hidden unless the teacher allowed it."""
    row = await _load(db, listening_id)
    return await listening_service.learner_detail(db, row, served_to_admin=True)


@student_router.get("/meta", response_model=None)
async def student_listening_meta(
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    meta = await listening_service.meta(db)
    return {
        "learning_languages": meta["learning_languages"],
        "levels": meta["levels"],
        "sortable": meta["sortable"],
    }


@student_router.get("", response_model=None)
async def student_list_listenings(
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
    """Ready recordings, with only the answerable questions counted.

    A row whose file has been thrown out of the library says `has_audio: false` rather
    than promising a recording that will not play.
    """
    try:
        return await listening_service.list_for_learner(
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


@student_router.get("/{listening_id}", response_model=None)
async def student_listening(
    listening_id: UUID,
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await listening_service.get(db, listening_id)
    if row is None or not passage_service.is_learner_visible(row):
        raise NotFound("Listening not found")
    return await listening_service.learner_detail(db, row)
