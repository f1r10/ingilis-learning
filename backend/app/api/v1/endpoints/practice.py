"""Practice API (Phase 6): the learner's own practice list, runs and saved items.

Everything here is the student's, and everything is scoped twice: by the session's student
id, and by the run token this backend issued. A learner cannot read another learner's run,
cannot answer a question the catalog they opened does not reach, and cannot see a catalog
the teacher has not published.

`/meta` is declared before `/catalogs/{catalog_id}` so it is never read as an id. The run
routes hang off `/runs/{session_id}` rather than under a catalog because a run belongs to
the learner who opened it - naming the catalog again would let one run's answers be filed
against a different catalog than the one that produced them.

Favorites get their own prefix: they outlive any one catalog, and a word saved from a
practice run is the same word saved from the vocabulary screen.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import NotFound, ValidationFailed
from app.models.assessment import Catalog
from app.models.identity import Student
from app.schemas import practice as pr_schemas
from app.services import practice_service
from app.services.practice_service import PracticeError, PracticeNotFound

router = APIRouter(prefix="/student/practice", tags=["student-practice"])
favorites_router = APIRouter(prefix="/student/favorites", tags=["student-practice"])


def _fail(exc: PracticeError) -> ValidationFailed:
    """One mapping, so a service-level rule cannot arrive at the browser as a 500."""
    return ValidationFailed(str(exc))


async def _catalog(db: AsyncSession, catalog_id: UUID) -> Catalog:
    try:
        return await practice_service.get_visible(db, catalog_id)
    except PracticeNotFound as exc:
        raise NotFound(str(exc)) from None


@router.get("/meta", response_model=None)
async def practice_meta(
    _student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The options behind the learner's own filters, from the code that enforces them."""
    return await practice_service.meta(db)


@router.get("/catalogs", response_model=None)
async def list_practice(
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    sort: str = "name",
    order: str = "asc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Published catalogs whose whole folder chain is published too."""
    try:
        return await practice_service.visible_catalogs(
            db,
            student,
            q=q,
            language=language,
            level=level,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except PracticeError as exc:
        raise _fail(exc) from None


@router.get("/catalogs/{catalog_id}", response_model=None)
async def practice_catalog(
    catalog_id: UUID,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One catalog, with this learner's own recent runs of it."""
    catalog = await _catalog(db, catalog_id)
    return await practice_service.catalog_detail(db, catalog, student)


@router.post("/catalogs/{catalog_id}/run", response_model=None)
async def open_run(
    catalog_id: UUID,
    payload: pr_schemas.RunRequest | None = None,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
    language: str | None = Query(default=None, max_length=16),
) -> dict:
    """Open the catalog as a run and get the session token every answer carries.

    `language` narrows a word card's translations to the learner's own, the same query the
    vocabulary card answers.
    """
    catalog = await _catalog(db, catalog_id)
    body = payload or pr_schemas.RunRequest()
    return await practice_service.start_run(db, catalog, student, shuffle=body.shuffle, language=language)


@router.get("/catalogs/{catalog_id}/known", response_model=None)
async def known_states(
    catalog_id: UUID,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """This learner's marks on the words of this catalog, newest mark winning."""
    catalog = await _catalog(db, catalog_id)
    return await practice_service.known_states(db, catalog, student)


@router.post("/catalogs/{catalog_id}/known", response_model=None)
async def mark_known(
    catalog_id: UUID,
    payload: pr_schemas.KnownStateRequest,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    catalog = await _catalog(db, catalog_id)
    try:
        return await practice_service.mark_known(db, catalog, student, payload)
    except PracticeError as exc:
        raise _fail(exc) from None
    except PracticeNotFound as exc:
        raise NotFound(str(exc)) from None


@router.post("/answer", response_model=None)
async def submit_answer(
    payload: pr_schemas.AnswerRequest,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Grade one answer and record it against the run it belongs to.

    Under `after_session` the response says only that it was recorded; the verdicts arrive
    when the run is finished.
    """
    try:
        return await practice_service.answer(db, student, payload)
    except PracticeError as exc:
        raise _fail(exc) from None
    except PracticeNotFound as exc:
        raise NotFound(str(exc)) from None


@router.get("/runs/{session_id}", response_model=None)
async def read_run(
    session_id: str,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A run the learner comes back to, with its verdicts shown or held as the timing says."""
    try:
        return await practice_service.run_of(db, student, session_id)
    except PracticeNotFound as exc:
        raise NotFound(str(exc)) from None


@router.get("/runs/{session_id}/steps", response_model=None)
async def resume_run(
    session_id: str,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
    language: str | None = Query(default=None, max_length=16),
) -> dict:
    """The exercises a run was opened with, served again so the learner can carry on.

    The verdicts live on `/runs/{session_id}`; this is the other half of coming back - a
    screen that shows the questions but not the order they were served in would make the
    stored shuffle seed pointless. Reading it writes nothing.
    """
    try:
        return await practice_service.resume_run(db, student, session_id, language=language)
    except PracticeNotFound as exc:
        raise NotFound(str(exc)) from None


@router.post("/runs/{session_id}/finish", response_model=None)
async def finish_run(
    session_id: str,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Close the run and get every verdict, whatever the feedback timing was."""
    try:
        return await practice_service.finish_run(db, student, session_id)
    except PracticeNotFound as exc:
        raise NotFound(str(exc)) from None


@favorites_router.get("", response_model=None)
async def list_favorites(
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The learner's own review list, each row named from the content it points at."""
    return await practice_service.list_favorites(db, student)


@favorites_router.post("", response_model=None, status_code=201)
async def add_favorite(
    payload: pr_schemas.FavoriteRequest,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Save an exercise or a word. Saving the same thing twice answers `added: false`."""
    try:
        return await practice_service.add_favorite(db, student, payload)
    except PracticeNotFound as exc:
        raise NotFound(str(exc)) from None


@favorites_router.post("/remove", response_model=None)
async def remove_favorite(
    payload: pr_schemas.FavoriteRequest,
    student: Student = Depends(deps.get_current_student),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Take it off the list. Removing something that is not there answers `removed: false`.

    A POST rather than a DELETE for the same reason the run routes are: the learner's
    surface carries no method that a permission check has to reason about, and a DELETE
    with a body is something proxies are free to drop.
    """
    return await practice_service.remove_favorite(db, student, payload)
