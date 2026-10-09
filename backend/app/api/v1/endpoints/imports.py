"""Document import API (Phase 8): a teacher's paper, its review queue, and the content it becomes.

One audience only. A document is the teacher's material, and nothing here is reachable by a
learner: a paper that has not been reviewed has no place in the bank, and the bank is the only
thing learners are ever shown.

The routes follow the three stages the importer works in:

* **bytes** - `POST /imports` stores a file and opens its queue. The format is decided from
  the bytes themselves, so an upload names no content type and carries no parser choice.
* **shapes** - the worker reads the paper into candidates, and `GET /imports/{job_id}/items`
  shows them in the order the paper was written. A candidate is the document's own text.
* **a person** - `PATCH /imports/items/{item_id}` and the two decision routes are where a
  teacher's words join the file's, and where content is actually created.

`/meta` and `/bulk` are declared before `/{job_id}` so their literal paths are never parsed as
a UUID. Item routes carry the item's own id: a candidate already knows its queue, and giving
the browser two ways to name the same row is two ways to disagree about it.

A duplicate upload answers 200 rather than 409, with `duplicate` true and the queue that
exists. Sending the same paper twice is not a mistake to punish - it is a teacher making sure
the file arrived - and a second queue of identical candidates would be the worse answer.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import ORJSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import APIError, as_conflict, as_invalid, as_missing
from app.models.identity import AdminUser
from app.schemas import imports as i_schemas
from app.services import import_service
from app.services.import_service import (
    AlreadyFiled,
    AlreadyInBank,
    ImportProblem,
    IncompleteCandidate,
    JobNotFound,
    QueueDown,
)

router = APIRouter(prefix="/imports", tags=["imports"])

#: The refusals that mean "this request could not be answered as written" - the service's own
#: rules about a document, a candidate or a page of the queue. 422, with the rule's name.
_INVALID = (ImportProblem, IncompleteCandidate)
#: The refusals that mean "something else has already settled this". 409: the candidate is
#: filed, the word is in the bank, or the row lock said the other teacher got there first.
_CLASH = (AlreadyFiled, AlreadyInBank)


def _down(exc: QueueDown) -> APIError:
    """The worker could not be reached. The document is stored and the queue waits: 503."""
    return APIError(exc.code, str(exc), 503, exc.params)


@router.get("/meta", response_model=None)
async def imports_meta(
    _admin: AdminUser = Depends(deps.get_current_admin),
) -> dict:
    """Formats, ceilings, kinds, question types and the missing-field words.

    The upload and review screens are built from this rather than carrying their own lists,
    so a screen can never offer a format the importer would refuse or a field a candidate has
    no room for.
    """
    return import_service.import_options()


@router.post("/bulk", response_model=None)
async def bulk_items(
    payload: i_schemas.ImportBulkRequest,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve, refuse or file a page of the queue, answered row by row.

    The answer is honest about a mixed batch: `done` names the rows that became content,
    `refused` gives each row that did not and the code for why, and `not_found` the ids that
    are not in any queue. A row the bank refuses does not stop the rows beside it, and a body
    that cannot be carried at all - `file` with no filing to set - is refused as a bad
    request rather than answered row by row.
    """
    try:
        return await import_service.bulk(db, payload, admin_id=admin.id)
    except _INVALID as exc:
        raise as_invalid(exc) from None


@router.get("", response_model=None)
async def list_imports(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
    q: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """Import history, newest first, each row carrying its queue counts."""
    try:
        return await import_service.list_jobs(
            db, status=status, q=q, sort=sort, order=order, page=page, page_size=page_size
        )
    except _INVALID as exc:
        raise as_invalid(exc) from None


@router.post("", response_model=None)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=400),
    auto_mode: bool | None = Form(default=None),
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> ORJSONResponse:
    """Add a document to the library and open its review queue.

    201 when these bytes are new; 200 with `duplicate` true when the same paper is already
    here, in which case the payload is the queue that exists and nothing was stored twice.
    `auto_mode` is the teacher asking that obviously-extracted candidates not wait for a
    second decision; it is optional per upload, and unset means the platform's default.
    """
    try:
        payload, duplicate = await import_service.start_upload(
            db, file, title=title, auto_mode=auto_mode, admin_id=admin.id
        )
    except _INVALID as exc:
        raise as_invalid(exc) from None
    except QueueDown as exc:
        raise _down(exc) from None
    return ORJSONResponse(
        status_code=200 if duplicate else 201,
        content={**payload, "duplicate": duplicate},
    )


async def _load_job(db: AsyncSession, job_id: UUID):
    try:
        return await import_service.get_job(db, job_id)
    except JobNotFound as exc:
        raise as_missing(exc) from None


async def _load_item(db: AsyncSession, item_id: UUID):
    """A candidate by its own id: the queue above it is not the caller's to name."""
    try:
        return await import_service.get_item(db, item_id)
    except JobNotFound as exc:
        raise as_missing(exc) from None


@router.get("/{job_id}", response_model=None)
async def get_import(
    job_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One import: its status, its progress, and how far its queue has been decided."""
    job = await _load_job(db, job_id)
    return await import_service.job_read(db, job)


@router.get("/{job_id}/document", response_model=None)
async def get_document(
    job_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The paper this queue was made from, so a teacher can compare the two.

    The application reads the object and answers with its bytes. A storage key, a bucket or a
    link that authorises itself never reaches the browser.
    """
    job = await _load_job(db, job_id)
    try:
        data, mime, filename = await import_service.document_bytes(db, job)
    except JobNotFound as exc:
        raise as_missing(exc) from None
    name = filename or f"import-{job_id}.bin"
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/{job_id}/items", response_model=None)
async def list_items(
    job_id: UUID,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
    decision: str | None = None,
    kind: str | None = None,
    only_incomplete: bool = False,
    view: str = "queue",
    sort: str = "position",
    order: str = "asc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """One page of the review queue, in the order the paper was written.

    `view=queue` is the work still to be decided; `view=all` is everything the document
    produced, including what a teacher has already filed or refused.
    """
    try:
        return await import_service.list_items(
            db,
            job_id,
            decision=decision,
            kind=kind,
            only_incomplete=only_incomplete,
            view=view,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except _INVALID as exc:
        raise as_invalid(exc) from None


@router.patch("/items/{item_id}", response_model=None)
async def edit_item(
    item_id: UUID,
    payload: i_schemas.ImportItemEdit,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Write a candidate's text or its filing, without filing any content.

    The document's own words stay where they are: what a teacher types is stored beside them,
    and the queue says afterwards which parts came from the paper and which were corrected.
    A refused row reopens here, because a changed mind is a new decision.
    """
    item = await _load_item(db, item_id)
    try:
        return await import_service.edit_item(db, item, payload, admin_id=admin.id)
    except _INVALID as exc:
        raise as_invalid(exc) from None
    except _CLASH as exc:
        raise as_conflict(exc) from None


@router.post("/items/{item_id}/decision", response_model=None)
async def decide_item(
    item_id: UUID,
    payload: i_schemas.ImportItemDecision,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve a candidate into the bank, or refuse it.

    Approval goes through the bank's own services, so a candidate is held to the same rules as
    a question or word typed in by hand - and refused, with the same sentence, if it breaks one.
    A candidate that is still incomplete cannot be filed at all: it answers 422 naming the
    fields, because the importer will not invent an answer key to finish it.
    """
    item = await _load_item(db, item_id)
    try:
        return await import_service.decide_item(db, item, payload, admin_id=admin.id)
    except _INVALID as exc:
        raise as_invalid(exc) from None
    except _CLASH as exc:
        raise as_conflict(exc) from None


@router.delete("/{job_id}", response_model=None)
async def delete_import(
    job_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a queue and its candidates, leaving any filed content alone.

    Refused once the queue has produced content, because those rows are the only record of
    which sentence in which paper became which question. The stored document is trashed only
    when no other queue cites it.
    """
    job = await _load_job(db, job_id)
    try:
        return await import_service.delete_job(db, job, admin_id=admin.id)
    except _INVALID as exc:
        raise as_invalid(exc) from None
    except _CLASH as exc:
        raise as_conflict(exc) from None


@router.post("/{job_id}/retry", response_model=None)
async def retry_import(
    job_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Put a queued or failed job back in front of the worker.

    A failure is usually the platform's - storage unreachable, the queue down while nobody was
    looking - so this is the teacher's way of asking again. A job that already produced
    candidates is refused: re-reading the paper would double the queue, and the row count says
    so.
    """
    job = await _load_job(db, job_id)
    try:
        return await import_service.retry_job(db, job, admin_id=admin.id)
    except _INVALID as exc:
        raise as_invalid(exc) from None
    except _CLASH as exc:
        raise as_conflict(exc) from None
    except QueueDown as exc:
        raise _down(exc) from None
