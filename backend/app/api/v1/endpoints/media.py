"""Media library API (Phase 5): the teacher's files and the learner's players.

Two routers, because the two audiences may not do the same things with the same bytes.

* `/media` is the library: upload, browse, caption, measure, trash and restore. A
  teacher may open any file in it, including one in the trash - the trash screen has to
  show what is about to be lost.
* `/student/media` has one route, `/student/media/{id}/content`, and it answers only
  for a file some ready piece of learner-facing content points at. There is no learner
  browse of the library, and no student route that accepts a range of the teacher's
  keys.

Every read of a file is proxied by the application rather than delegated to the object
store, so authorisation is decided per request instead of being encoded in a URL. Both
routers use the same service function for that response; what differs is who is allowed
to ask, which is settled before the bytes are opened.

Nothing here decides what a file *is*: that is `app.core.media_types` reading the
upload's own bytes, and `media_service` refuses anything it cannot name.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models.identity import AdminUser, Student
from app.schemas import media as m_schemas
from app.services import media_service
from app.services.media_service import AssetInUse, DuplicateAsset, MediaError

router = APIRouter(prefix="/media", tags=["media"])
student_router = APIRouter(prefix="/student/media", tags=["student-media"])


def _fail(exc: MediaError) -> ValidationFailed:
    """One mapping, so a service-level rule cannot arrive at the browser as a 500."""
    return ValidationFailed(str(exc))


def _clash(exc: Exception) -> Conflict:
    """Both 409s the library can produce: bytes already here, file still in use."""
    if isinstance(exc, AssetInUse):
        return Conflict("asset_in_use", str(exc))
    return Conflict("asset_exists", str(exc))


@router.get("/meta", response_model=None)
async def media_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Formats accepted and size ceilings, straight from the code that enforces them.

    The upload screen prints this instead of carrying its own list, so a browser can
    refuse an oversized file before sending it rather than after.
    """
    return await media_service.library_options(db)


@router.post("/bulk", response_model=None)
async def bulk_action(
    payload: m_schemas.MediaBulkRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await media_service.bulk(db, payload, admin_id=admin.id)


@router.get("", response_model=None)
async def list_media(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    kind: str | None = None,
    source_origin: str | None = None,
    view: str = "bank",
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    try:
        return await media_service.list_assets(
            db,
            q=q,
            kind=kind,
            source_origin=source_origin,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except MediaError as exc:
        raise _fail(exc) from None


@router.post("", response_model=None)
async def upload_media(
    file: UploadFile = File(...),
    label: str | None = Form(default=None, max_length=200),
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> ORJSONResponse:
    """Add a file to the library by uploading it. There is no other way in.

    The response describes the asset either way, and its status says what happened:
    201 when these bytes were new to the library, 200 when the same file was already
    stored (`deduplicated` true). Answering 201 to a request that created no row would
    tell the teacher's browser it had just added a second copy of a recording.
    """
    try:
        payload, already_held = await media_service.create_from_upload(
            db, file, label=label, admin_id=admin.id
        )
    except MediaError as exc:
        raise _fail(exc) from None
    except DuplicateAsset as exc:
        raise _clash(exc) from None
    return ORJSONResponse(
        status_code=200 if already_held else 201,
        content={**payload, "deduplicated": already_held},
    )


async def _load(db: AsyncSession, asset_id: UUID, *, allow_trash: bool = True) -> object:
    asset = await media_service.get(db, asset_id, allow_trash=allow_trash)
    if asset is None:
        raise NotFound("Media asset not found")
    return asset


@router.get("/{asset_id}", response_model=None)
async def get_media(
    asset_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    include_trash: bool = True,
) -> dict:
    asset = await _load(db, asset_id, allow_trash=include_trash)
    return await media_service.to_read(db, asset)  # type: ignore[arg-type]


@router.patch("/{asset_id}", response_model=None)
async def update_media(
    asset_id: UUID,
    payload: m_schemas.MediaMetadataPatch,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Caption the file, or report what the player measured.

    This is the only route that writes a duration or a pixel size, and it writes what a
    browser observed - the backend has no codec library and will not guess at one.

    A trashed asset still loads here: `update_metadata` refuses it with the sentence the
    teacher can act on ("restore the asset from the trash before editing it"), and a
    bare 404 would hide that the file exists and where to find it.
    """
    asset = await _load(db, asset_id)
    try:
        return await media_service.update_metadata(db, asset, payload, admin_id=admin.id)  # type: ignore[arg-type]
    except MediaError as exc:
        raise _fail(exc) from None


@router.delete("/{asset_id}", response_model=None)
async def trash_media(
    asset_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft delete. The file stays in storage; live content still needing it is refused."""
    asset = await _load(db, asset_id)
    try:
        payload = await media_service.trash(db, asset, admin_id=admin.id)  # type: ignore[arg-type]
    except AssetInUse as exc:
        raise _clash(exc) from None
    return {"ok": True, "trashed": payload["id"], "state": payload["state"]}


@router.post("/{asset_id}/restore", response_model=None)
async def restore_media(
    asset_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    asset = await _load(db, asset_id)
    try:
        return await media_service.restore(db, asset, admin_id=admin.id)  # type: ignore[arg-type]
    except DuplicateAsset as exc:
        raise _clash(exc) from None


@router.get("/{asset_id}/content", response_model=None)
async def media_content(
    asset_id: UUID,
    request: Request,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> object:
    """The file's bytes, with one range honoured.

    A trashed asset is still served to a teacher: the trash screen shows what is about
    to be lost, and a preview of a recording the teacher may still restore is the
    library's own job. The answer is the application's, never the object store's.
    """
    asset = await _load(db, asset_id)
    return await media_service.build_content_response(request, asset)  # type: ignore[arg-type]


@student_router.get("/{asset_id}/content", response_model=None)
async def student_media_content(
    asset_id: UUID,
    request: Request,
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> object:
    """Play a file that belongs to an exercise the learner is being shown.

    Two different situations answer the same way, because telling a student "that asset
    exists but is not yours" is a leak of the teacher's library: no ready listening,
    question or word points at it, or it is in the trash.
    """
    asset = await media_service.get(db, asset_id, allow_trash=False)
    if asset is None or not await media_service.served_to_student(db, asset.id):
        raise NotFound("Media asset not found")
    return await media_service.build_content_response(request, asset)
