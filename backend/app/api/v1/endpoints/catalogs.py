"""Catalogs API (Phase 6): the teacher's reusable collections.

`/meta` and `/bulk` are declared before `/{catalog_id}` so their literal paths are never
parsed as a UUID. Item routes hang off `/items/{item_id}` for the same reason a passage's
sets do: a reference knows which catalog holds it, so the screen editing one item does not
have to name the catalog above it.

Three of these answers carry a code, not just a sentence: `catalog_name_taken` and
`reference_exists` are the two clashes a teacher can act on from the browser, and both
name the row that is already there so the UI can offer "open that one instead".
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models.identity import AdminUser
from app.schemas import catalog as c_schemas
from app.services import catalog_service, practice_service
from app.services.catalog_service import (
    CatalogError,
    CatalogNotFound,
    DuplicateReference,
    NameTaken,
)

router = APIRouter(prefix="/catalogs", tags=["catalogs"])


def _fail(exc: CatalogError) -> ValidationFailed:
    """One mapping, so a service-level rule cannot arrive at the browser as a 500."""
    return ValidationFailed(str(exc))


async def _load(db: AsyncSession, catalog_id: UUID, *, allow_trash: bool = True):
    try:
        return await catalog_service.get(db, catalog_id, allow_trash=allow_trash)
    except CatalogNotFound as exc:
        raise NotFound(str(exc)) from None


async def _load_item(db: AsyncSession, item_id: UUID):
    """The reference and the catalog that holds it."""
    try:
        return await catalog_service.get_item(db, item_id)
    except CatalogNotFound as exc:
        raise NotFound(str(exc)) from None


@router.get("/meta", response_model=None)
async def catalogs_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Languages, levels, item kinds, caps and sort names - from the code that enforces them."""
    return await catalog_service.meta(db)


@router.post("/bulk", response_model=None)
async def bulk_action(
    payload: c_schemas.CatalogBulkRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Status, trash, restore or level over a selection, answered per id."""
    try:
        return await catalog_service.bulk(db, payload, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None


@router.get("", response_model=None)
async def list_catalogs(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    kind: str | None = None,
    parent_id: UUID | None = None,
    root_only: bool = False,
    language: str | None = None,
    level: str | None = None,
    status: str | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    try:
        return await catalog_service.list_catalogs(
            db,
            q=q,
            kind=kind,
            parent_id=parent_id,
            root_only=root_only,
            language=language,
            level=level,
            status=status,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except CatalogError as exc:
        raise _fail(exc) from None


@router.post("", response_model=None, status_code=201)
async def create_catalog(
    payload: c_schemas.CatalogCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await catalog_service.create(db, payload, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None
    except NameTaken as exc:
        raise Conflict("catalog_name_taken", str(exc)) from None


@router.patch("/items/{item_id}", response_model=None)
async def update_item(
    item_id: UUID,
    payload: c_schemas.ItemUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Change which block of a passage this reference names."""
    catalog, item = await _load_item(db, item_id)
    try:
        return await catalog_service.update_item(db, catalog, item, payload, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None


@router.delete("/items/{item_id}", response_model=None)
async def remove_item(
    item_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Drop the reference. The content it named stays in its own bank."""
    _catalog, item = await _load_item(db, item_id)
    return await catalog_service.remove_item(db, item, admin_id=admin.id)


@router.get("/{catalog_id}", response_model=None)
async def get_catalog(
    catalog_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    include_trash: bool = True,
) -> dict:
    row = await _load(db, catalog_id, allow_trash=include_trash)
    return await catalog_service.to_read(db, row)


@router.patch("/{catalog_id}", response_model=None)
async def update_catalog(
    catalog_id: UUID,
    payload: c_schemas.CatalogUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _load(db, catalog_id, allow_trash=False)
    try:
        return await catalog_service.update(db, row, payload, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None
    except NameTaken as exc:
        raise Conflict("catalog_name_taken", str(exc)) from None


@router.delete("/{catalog_id}", response_model=None)
async def trash_catalog(
    catalog_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft delete, refused while live folders sit inside it."""
    row = await _load(db, catalog_id)
    try:
        payload = await catalog_service.trash(db, row, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None
    return {"ok": True, "trashed": payload["id"], "status": payload["status"]}


@router.post("/{catalog_id}/restore", response_model=None)
async def restore_catalog(
    catalog_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _load(db, catalog_id)
    try:
        return await catalog_service.restore(db, row, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None
    except NameTaken as exc:
        raise Conflict("catalog_name_taken", str(exc)) from None


@router.post("/{catalog_id}/status", response_model=None)
async def set_status(
    catalog_id: UUID,
    payload: c_schemas.StatusUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Draft, ready or archived. Publishing checks that something can actually be used."""
    row = await _load(db, catalog_id)
    try:
        return await catalog_service.set_status(db, row, payload.status, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None


@router.get("/{catalog_id}/preview", response_model=None)
async def preview_catalog(
    catalog_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    shuffle: bool | None = None,
    language: str | None = None,
) -> dict:
    """What a learner would be served, before this catalog is published.

    Reads only. Nothing is logged as a run, because a preview is the teacher deciding, and
    a history row nobody produced would be a lie in that learner's timeline.
    """
    row = await _load(db, catalog_id)
    return await practice_service.preview_run(db, row, shuffle=shuffle, language=language)


@router.get("/{catalog_id}/items", response_model=None)
async def list_items(
    catalog_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every reference in order, with the content it points at described from the row itself."""
    row = await _load(db, catalog_id)
    return {"items": await catalog_service.list_items(db, row)}


@router.post("/{catalog_id}/items", response_model=None, status_code=201)
async def add_items(
    catalog_id: UUID,
    payload: c_schemas.ItemAddRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Append references after the ones already held. All checked, or nothing written."""
    row = await _load(db, catalog_id, allow_trash=False)
    try:
        return await catalog_service.add_items(db, row, payload, admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None
    except DuplicateReference as exc:
        raise Conflict("reference_exists", str(exc)) from None


@router.post("/{catalog_id}/items/reorder", response_model=None)
async def reorder_items(
    catalog_id: UUID,
    payload: c_schemas.ItemReorderRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Renumber the references. The request must name every item of this catalog."""
    row = await _load(db, catalog_id, allow_trash=False)
    try:
        return await catalog_service.reorder(db, row, list(payload.item_ids), admin_id=admin.id)
    except CatalogError as exc:
        raise _fail(exc) from None
