"""Document import and review (Phase 8): a paper comes in, a teacher decides what of it is content.

The pipeline is deliberately split in three, because the three parts fail in different
places and only one of them is allowed to guess.

* **Reading the file** is `app.core.doc_types` and `document_parsing`: bytes decide the
  format, and a format with no text layer says so. Nothing here re-identifies a file the
  browser described.
* **Deciding what is a candidate** is `import_candidates`: it classifies shapes and never
  completes meaning, so an option list with no answer key arrives as a question that is
  missing `answer` rather than as a question with a guessed one.
* **Deciding what becomes content** is a teacher, one row at a time or one page at a time.
  This module is the record of that decision and the only place that writes to the bank.

Three rules hold the design up.

* **One source per file of bytes.** The sha256 is taken while the upload is spooling, and
  migration `0007` makes a live checksum unique, so a paper two colleagues both sent has
  one review queue. The second teacher is shown the queue that exists instead of a
  duplicate of it - and a race between the two is settled by the index, the same way the
  media library settles it.
* **The document's words stay the document's words.** `import_item.extracted` is never
  edited; a teacher's corrections go to `corrected`. `filing` holds what a person decided
  about the bank (level, lifecycle, language, taxonomy) and nothing else, so after the
  fact it can still be told which sentence came off the paper.
* **Nothing is filed that is still incomplete.** `pending_fields` is re-evaluated at
  approval from the text and filing the row holds at that moment, so "approve the page"
  files the rows that are genuinely finished and reports the others with the field each
  one still needs.
* **One candidate is filed once.** The row is re-read under a lock, into the object the
  decision is made from, before anything is written: two teachers who approve the same
  sentence in the same moment leave one question in the bank and a 409 for the second one.

Heavy work stays in the worker. `run_job` claims a queued row with one conditional UPDATE
- two workers, or a retry racing itself, cannot both read the same paper - and every
read of the object store and every parse runs in a thread, never on the event loop.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core import doc_types, enums, security, tasks
from app.core.config import get_settings
from app.core.exceptions import APIError, RuleBroken
from app.core.storage import (
    ObjectMissing,
    StorageUnavailable,
    get_storage,
    safe_display_name,
)
from app.models.content import (
    ImportItem,
    ImportJob,
    Question,
    Reading,
    SourceFile,
    VocabularyEntry,
)
from app.schemas import imports as i_schemas
from app.schemas import question as q_schemas
from app.schemas import reading as r_schemas
from app.schemas import vocabulary as v_schemas
from app.services import (
    audit_service,
    document_parsing,
    import_candidates,
    question_service,
    reading_service,
    vocabulary_service,
)
from app.services.passage_service import PassageError
from app.services.question_service import QuestionInputError
from app.services.vocabulary_service import DuplicateWord, VocabularyError

logger = logging.getLogger(__name__)

#: The shelf the originals live on. Private, like every other object: a paper a teacher
#: uploaded is read back through this application, never through a link.
SHELF = "documents"

#: How much of an upload is held in memory to identify it; the rest goes to disk.
_SPOOL_CHUNK = 1 << 20

#: The three shapes the parsers can produce a question in. `import_candidates` only ever
#: emits these, and a document whose own `type` column says something else is a candidate
#: a teacher has to re-file - the alternative is inventing a config the paper never held.
QUESTION_TYPES = ("multiple_choice", "multi_select", "short_answer")
CHOICE_TYPES = ("multiple_choice", "multi_select")

#: Kinds a teacher can approve into content. `note` is "not classified yet", so it has a
#: kind to choose first.
APPROVABLE_KINDS = ("question", "vocabulary", "reading")

#: What the filing object may hold, in the order the review screen shows the fields.
FILING_KEYS = ("status", "level", "language", "topic_ids", "tag_ids")

#: The lifecycle states a filed row may be created in. Read from the enum rather than
#: restated, because the bank's own services are what enforce it.
FILING_STATUSES = [status.value for status in enums.ContentStatus if status != enums.ContentStatus.TRASH]

JOB_STATUSES = [status.value for status in enums.JobStatus]
DECISIONS = [decision.value for decision in enums.ImportItemDecision]

#: A decided row is final: the content it filed lives in the bank now, and re-deciding the
#: candidate would say something about the paper that is no longer true of it.
FINAL_DECISIONS = (enums.ImportItemDecision.APPROVED, enums.ImportItemDecision.EDITED)

JOB_SORTABLE = {
    "created_at": ImportJob.created_at,
    "updated_at": ImportJob.updated_at,
    "status": ImportJob.status,
    "title": SourceFile.title,
}

ITEM_SORTABLE = {
    "position": ImportItem.position,
    "confidence": ImportItem.confidence,
    "kind": ImportItem.detected_kind,
    "page": ImportItem.source_page,
    "decision": ImportItem.decision,
}

VIEWS = ("queue", "all")


class ImportProblem(RuleBroken, ValueError):
    """A teacher-facing refusal about a document, a candidate or a job; the endpoints 422."""

    code = "import_rule"


class JobNotFound(RuleBroken, Exception):
    """No such import job or candidate. 404."""

    code = "import_not_found"


class IncompleteCandidate(RuleBroken, ValueError):
    """The candidate still lacks something a person has to supply. 422."""

    code = "candidate_incomplete"


class AlreadyFiled(RuleBroken, Exception):
    """This candidate has already produced content, or was refused. 409."""

    code = "candidate_filed"


class AlreadyInBank(RuleBroken, Exception):
    """The bank already holds that exact word. 409."""

    code = "content_exists"


class QueueDown(RuleBroken, Exception):
    """The worker could not be reached, so the job waits instead of pretending. 503."""

    code = "queue_unavailable"


#: The reading that stopped for a reason no `_fail` call saw - the worker itself gave up. The
#: pair is the code the screen translates (`errors.worker_stopped`) and the sentence a script or
#: the log reads, so the two never disagree about what happened.
STOPPED_REFUSAL = ("worker_stopped", "The import stopped unexpectedly. Read this document again.")

#: The remark auto mode leaves when the bank answers a complete card with a refusal. The card
#: carries the refusal's code, and the code's own sentence needs the reason the bank gave - a
#: sentence written by the bank's services in English, so it belongs in the log and not on a card
#: read in four languages. This remark says what happened to the card and what to do next; the
#: reason arrives, translated, when a person presses Approve and the bank answers them directly.
REFUSAL_NOTES: dict[str, str] = {
    "bank_refused": "The bank would not hold this card as it was read. Correct it, then approve it to read why.",
}


def note_codes() -> dict[str, str]:
    """Every remark a card can carry, from the reader or from auto mode, in its own words."""
    return {**import_candidates.NOTE_CODES, **REFUSAL_NOTES}


@dataclass(frozen=True)
class _Spooled:
    """One upload, on local disk, with its digest and first bytes in hand."""

    path: str
    handle: Any
    checksum: str
    size: int
    head: bytes


# --------------------------------------------------------------------------- #
# What the importer accepts, and what it says about itself
# --------------------------------------------------------------------------- #

def upload_limit_bytes() -> int:
    """The ceiling for one document, in bytes, exactly as configured."""
    return get_settings().max_document_upload_mb * 1024 * 1024


def import_options() -> dict:
    """Everything the upload and review screens are built from, taken from the code that
    enforces it.

    The formats are the ones `doc_types` can name, the ceilings are the settings, and the
    question types are the shapes `import_candidates` can honestly produce. A screen that
    carried its own list of any of these would offer something an upload then refuses.
    """
    limit = upload_limit_bytes()
    return {
        "formats": [
            {"name": fmt.name, "mime_type": fmt.mime, "extension": fmt.extension, "label": fmt.label}
            for fmt in doc_types.accepted_formats()
        ],
        "kinds": list(i_schemas.KINDS),
        "approvable_kinds": list(APPROVABLE_KINDS),
        "question_types": list(QUESTION_TYPES),
        "editable_fields": {kind: list(fields) for kind, fields in i_schemas.EDITABLE_FIELDS.items()},
        "filing_statuses": FILING_STATUSES,
        "job_statuses": JOB_STATUSES,
        "decisions": DECISIONS,
        "views": list(VIEWS),
        "max_document_bytes": limit,
        "max_document_mb": limit // (1024 * 1024),
        "auto_mode_default": get_settings().import_auto_mode_default,
        "low_confidence_threshold": get_settings().import_low_confidence_threshold,
        "sortable_jobs": sorted(JOB_SORTABLE),
        "sortable_items": sorted(ITEM_SORTABLE),
        "max_bulk_items": i_schemas.MAX_BULK_ITEMS,
        "missing_fields": _MISSING_EXPLANATIONS,
        "note_codes": note_codes(),
    }


#: Every code `pending_fields` can answer with, in the server's own words. The screen
#: translates the code (`imports.missing.<code>`); this map is for a colleague's script
#: and for the log line, where a bare code would say nothing.
_MISSING_EXPLANATIONS = {
    "prompt": "the question's text",
    "options": "at least two options",
    "accepted": "an accepted answer",
    "answer": "which option is correct",
    "type": "a question type this importer can build",
    "word": "the word itself",
    "definition": "the word's meaning",
    "language": "the language the word belongs to",
    "title": "the passage's title",
    "body": "the passage's text",
    "kind": "what kind of content this text should become",
}


def effective_text(item: ImportItem) -> dict:
    """The candidate's text as it stands now: the teacher's correction, or the file."""
    return item.corrected or item.extracted or {}


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _correct_count(options: Any) -> int:
    if not isinstance(options, list):
        return 0
    return sum(1 for option in options if isinstance(option, dict) and option.get("correct"))


def _accepted_answers(text: dict) -> list[str]:
    """The answer of a question with no options, whichever key the document used."""
    accepted = text.get("accepted")
    if isinstance(accepted, list):
        return [str(value).strip() for value in accepted if str(value).strip()]
    if _has_text(text.get("answer_text")):
        return [str(text["answer_text"]).strip()]
    return []


def pending_fields(item: ImportItem) -> list[str]:
    """What still has to be supplied before this candidate can become content.

    Re-evaluated from the row's own text every time, never trusted from the parser: the
    teacher may have filled the gap in the review screen, and an approval that read the
    stale list would refuse work that is actually finished. The codes are stable because
    the screen names them, and a candidate cannot be approved while this is not empty -
    that is the rule that keeps a guessed answer key out of the bank.
    """
    kind = item.detected_kind or "note"
    text = effective_text(item)
    filing = item.filing or {}
    gaps: list[str] = []

    if kind == "question":
        question_type = item.detected_type or "multiple_choice"
        if question_type not in QUESTION_TYPES:
            gaps.append("type")
        if not _has_text(text.get("prompt")):
            gaps.append("prompt")
        if question_type in CHOICE_TYPES:
            options = text.get("options")
            count = len(options) if isinstance(options, list) else 0
            if count < 2:
                gaps.append("options")
            else:
                # The bank's own rule for a choice question, answered here so a complete
                # candidate cannot be offered to Approve and then refused by the engine:
                # `multiple_choice` takes exactly one right option, `multi_select` at least
                # one right and one still wrong to select against.
                correct = _correct_count(options)
                if question_type == "multiple_choice":
                    if correct != 1:
                        gaps.append("answer")
                elif correct < 1 or correct == count:
                    gaps.append("answer")
        elif question_type == "short_answer" and not _accepted_answers(text):
            gaps.append("answer")
    elif kind == "vocabulary":
        if not _has_text(text.get("word")):
            gaps.append("word")
        if not _has_text(text.get("definition")):
            gaps.append("definition")
        if not _has_text(filing.get("language")):
            gaps.append("language")
    elif kind == "reading":
        if not _has_text(text.get("title")):
            gaps.append("title")
        if not _has_text(text.get("body")):
            gaps.append("body")
    elif kind == "note":
        gaps.append("kind")
    return gaps


def is_approvable(item: ImportItem) -> bool:
    """Whether Approve would create content from this row right now."""
    return (
        item.decision not in FINAL_DECISIONS
        and (item.detected_kind or "") in APPROVABLE_KINDS
        and not pending_fields(item)
    )


def needs_attention(item: ImportItem) -> bool:
    """A row a person should look at before deciding: incomplete, or read with doubt."""
    incomplete = bool(pending_fields(item))
    confidence = item.confidence if item.confidence is not None else 0.0
    return incomplete or confidence < get_settings().import_low_confidence_threshold


# --------------------------------------------------------------------------- #
# Upload: store the file, open the queue
# --------------------------------------------------------------------------- #


def _discard(path: str, handle: Any) -> None:
    """Close a spooled upload and remove its temporary file, whatever state it is in."""
    try:
        if not handle.closed:
            handle.close()
    except OSError:
        logger.warning("temporary import file %s could not be closed", path)
    try:
        os.unlink(path)
    except OSError:
        logger.warning("temporary import file %s was left behind", path)


async def _spool(upload: Any) -> _Spooled:
    """Stream an upload to a temporary file, hashing it as it arrives.

    The whole document is never held in memory: a 40 MB paper becomes a file on local
    disk plus a running sha256, and the first few kilobytes stay in RAM because that is
    what `doc_types` reads to name the format. The temporary file is what `zipfile` opens
    to list an archive's members - the ZIP's own directory sits at the end of the file, so
    the head alone cannot say whether it holds a Word document.
    """
    handle = tempfile.NamedTemporaryFile(prefix="llp-import-", delete=False)
    digest = hashlib.sha256()
    head = bytearray()
    total = 0
    ceiling = upload_limit_bytes()
    try:
        while True:
            chunk = await upload.read(_SPOOL_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > ceiling:
                raise ImportProblem(
                    f"A document may be at most {ceiling // (1024 * 1024)} MB; this file is "
                    f"{total // (1024 * 1024)} MB.",
                    code="document_too_large",
                    params={"mb": ceiling // (1024 * 1024), "sent": total // (1024 * 1024)},
                )
            digest.update(chunk)
            remaining = doc_types.HEAD_BYTES - len(head)
            if remaining > 0:
                head.extend(chunk[:remaining])
            await run_in_threadpool(handle.write, chunk)
        if total == 0:
            raise ImportProblem("That file is empty, so there is nothing to import.", code="document_empty")
        await run_in_threadpool(handle.flush)
        await run_in_threadpool(handle.seek, 0)
        spooled = _Spooled(
            path=handle.name, handle=handle, checksum=digest.hexdigest(), size=total, head=bytes(head)
        )
        return spooled
    except Exception:
        # Whatever stopped the upload, a temporary copy of a teacher's paper must not
        # stay on this machine's disk.
        _discard(handle.name, handle)
        raise


async def _zip_names(path: str) -> list[str]:
    try:
        return await run_in_threadpool(_read_zip_names, path)
    except (zipfile.BadZipFile, OSError):
        return []


def _read_zip_names(path: str) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _extension_of(filename: str | None) -> str | None:
    """A document's own suffix, which is only ever a tie-breaker between text formats.

    The bytes still decide everything: `doc_types.identify` consults the hint after it has
    read the file as text and found the three-line column rule unproven, so a Word document
    renamed `.txt` is still named from its members, and a two-line `.tsv` saved by a
    spreadsheet's "export the selection" is not called prose.
    """
    if not filename:
        return None
    return os.path.splitext(filename)[1] or None


async def _identify(spooled: _Spooled, hint: str | None) -> doc_types.DocFormat:
    """The format these bytes belong to, or the sentence that refuses them."""
    names = await _zip_names(spooled.path) if spooled.head[:2] == b"PK" else None
    fmt = doc_types.identify(spooled.head, zip_names=names, hint=hint)
    if fmt is None:
        raise ImportProblem(
            doc_types.refusal_reason(spooled.head, zip_names=names), code="document_unreadable"
        )
    return fmt


async def _live_by_checksum(db: AsyncSession, checksum: str) -> SourceFile | None:
    """The live source holding exactly these bytes, if a colleague already sent it."""
    return (
        await db.execute(
            select(SourceFile).where(
                SourceFile.checksum == checksum, SourceFile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def _duplicate_payload(db: AsyncSession, source: SourceFile) -> dict:
    """The queue an identical upload already opened, for the teacher who sent it twice.

    A live document always has a job to show: the two rows are created in one transaction,
    and the source is only trashed once no queue cites it. The newest is the one standing.
    """
    job = (
        await db.execute(
            select(ImportJob)
            .where(ImportJob.source_file_id == source.id)
            .order_by(ImportJob.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if job is None:
        raise JobNotFound(
            "This document is in the library but has no import queue. Read it again to open "
            "one.",
            code="queue_missing",
        )
    payload = await job_read(db, job)
    payload["source"] = await source_read(db, source)
    return payload


async def create_from_upload(
    db: AsyncSession,
    upload: Any,
    *,
    title: str | None = None,
    auto_mode: bool | None = None,
    admin_id: uuid.UUID,
) -> tuple[dict, bool]:
    """Store one document and open its review queue, or point at the queue that exists.

    Returns the job payload and whether this paper was already here. A duplicate is not
    an error: the teacher is shown the queue that exists rather than being handed a second
    copy of every candidate, and the object store keeps holding the file once.
    """
    spooled = await _spool(upload)
    try:
        filename = safe_display_name(getattr(upload, "filename", None))
        fmt = await _identify(spooled, _extension_of(filename))
        existing = await _live_by_checksum(db, spooled.checksum)
        if existing is not None:
            return await _duplicate_payload(db, existing), True

        key = get_storage().build_key(SHELF, f"upload.{fmt.extension}")
        try:
            await run_in_threadpool(get_storage().put_file, key, spooled.handle, fmt.mime, spooled.size)
        except (ObjectMissing, StorageUnavailable) as exc:
            raise _storage_failure(exc) from exc

        label = (title or "").strip() or filename or fmt.label
        source = SourceFile(
            title=label[:400],
            original_filename=filename,
            mime_type=fmt.mime,
            storage_key=key,
            keep_original=True,
            checksum=spooled.checksum,
            meta={"format": fmt.name, "label": fmt.label, "bytes": spooled.size},
        )
        try:
            db.add(source)
            await db.flush()

            job = ImportJob(
                source_file_id=source.id,
                status=enums.JobStatus.QUEUED,
                profile=f"native:{fmt.name}",
                auto_mode=get_settings().import_auto_mode_default if auto_mode is None else auto_mode,
                progress={
                    "stage": "queued",
                    "format": fmt.name,
                    "bytes": spooled.size,
                    "requested_by": str(admin_id),
                },
            )
            db.add(job)
            await db.flush()
        except IntegrityError as error:
            # The partial unique index settled a race: the same paper arrived a moment
            # ago. It conflicts on the source row itself, which is the first thing written,
            # so both flushes are inside the one guard - a check that said "not here" and an
            # index that says "it is now" are the same two moments.
            await db.rollback()
            try:
                await run_in_threadpool(get_storage().delete, key)
            except (ObjectMissing, StorageUnavailable) as exc:
                logger.warning("orphan document %s could not be removed: %s", key, exc)
            winner = await _live_by_checksum(db, spooled.checksum)
            if winner is None:
                # The queue that won this race was taken away again in the same moment - the
                # index only counts live rows, so its source went into the trash while this
                # request was being answered. Nothing here can point at a queue that is gone.
                raise ImportProblem(
                    "This document was added and removed in the same moment. Send it again.",
                    code="upload_race",
                ) from error
            return await _duplicate_payload(db, winner), True

        await audit_service.record_audit(
            db,
            action="import.uploaded",
            actor_id=admin_id,
            target_type="import_job",
            target_id=job.id,
            after={
                "source_file_id": str(source.id),
                "format": fmt.name,
                "bytes": spooled.size,
                "filename": filename,
                "auto_mode": job.auto_mode,
            },
        )
        payload = await job_read(db, job)
        payload["source"] = await source_read(db, source)
        return payload, False
    finally:
        _discard(spooled.path, spooled.handle)


async def dispatch(db: AsyncSession, job: ImportJob) -> None:
    """Commit the job, then tell the worker it exists.

    This order is not a detail. A worker handed an id whose rows are not yet committed
    finds no job, and the teacher is left with a failed import of a file that was
    perfectly good. If the queue itself cannot be reached, the rows stay exactly as they
    are and the refusal says what to do about it: the screen's retry calls this again.
    """
    job_id = job.id
    await db.commit()
    try:
        await tasks.enqueue(tasks.IMPORT_JOB_TASK, str(job_id))
    except tasks.QueueUnavailable as exc:
        raise QueueDown(
            "The document is stored and its queue is waiting, but the background worker "
            "could not be reached. Start the import again in a moment.",
            code="queue_unavailable",
        ) from exc


async def start_upload(
    db: AsyncSession,
    upload: Any,
    *,
    title: str | None = None,
    auto_mode: bool | None = None,
    admin_id: uuid.UUID,
) -> tuple[dict, bool]:
    """Take a document from a request: store it, open its queue, hand it to the worker.

    The upload route's whole job is this one call, so the order that matters - the rows
    committed before the queue is told - cannot be assembled wrongly at the route.

    A duplicate is not sent to the worker. Its paper is already here and its queue was
    dispatched when it arrived; a second message would only race with the first. What a
    teacher does with a queue that stopped is `retry_job`, which the payload's own status
    says whether it applies to.
    """
    payload, duplicate = await create_from_upload(
        db, upload, title=title, auto_mode=auto_mode, admin_id=admin_id
    )
    if duplicate:
        return payload, True
    job = await get_job(db, uuid.UUID(payload["id"]))
    await dispatch(db, job)
    return payload, False


def _storage_failure(exc: Exception) -> ImportProblem:
    """A storage fault, phrased for a teacher rather than as a bare 500."""
    logger.warning("import storage failure: %s", exc)
    if isinstance(exc, ObjectMissing):
        return ImportProblem(
            "The stored file for this document is missing from storage.", code="document_missing"
        )
    raise APIError(
        "storage_unavailable",
        "The file storage is not reachable right now. Try again in a moment.",
        503,
    )


# --------------------------------------------------------------------------- #
# The job itself: read the paper, fill the queue
# --------------------------------------------------------------------------- #


async def run_job(db: AsyncSession, job_id: uuid.UUID) -> str:
    """Read one stored document into review candidates. Runs in the worker.

    Unlike a request-path function this one owns its transaction: the claim is committed
    on its own, so a second worker that arrives while the first is still parsing finds
    nothing to claim and leaves the queue alone.
    """
    claimed = await db.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.status == enums.JobStatus.QUEUED)
        .values(status=enums.JobStatus.PROCESSING, started_at=security.utcnow(), error=None)
    )
    await db.commit()
    if claimed.rowcount != 1:
        return "not_claimed"

    job = await db.get(ImportJob, job_id)
    if job is None:
        return "not_found"

    source = await db.get(SourceFile, job.source_file_id) if job.source_file_id else None
    if source is None or source.deleted_at is not None:
        await _fail(db, job, error="The uploaded document is no longer in the library.", code="document_missing")
        return "failed"
    if not source.storage_key:
        await _fail(db, job, error="No stored file was kept for this document.", code="document_missing")
        return "failed"

    try:
        data = await run_in_threadpool(get_storage().get_bytes, source.storage_key)
    except (ObjectMissing, StorageUnavailable) as exc:
        failure = _storage_failure(exc)
        await _fail(db, job, error=str(failure), code=failure.code)
        return "failed"

    names = await _zip_names_of(data) if data[:2] == b"PK" else None
    fmt = doc_types.identify(
        data[: doc_types.HEAD_BYTES], zip_names=names, hint=_extension_of(source.original_filename)
    )
    if fmt is None:
        reason = doc_types.refusal_reason(data[: doc_types.HEAD_BYTES], zip_names=names)
        await _fail(db, job, error=reason, code="document_unreadable")
        return "failed"

    try:
        parsed = await run_in_threadpool(document_parsing.parse, data, fmt)
    except document_parsing.DocumentUnreadable as exc:
        await _fail(db, job, error=exc.reason, code="document_unreadable")
        return "failed"
    except (ValueError, RuntimeError) as exc:
        # A parser that gave up mid-file is a fault about this document, not a fault of
        # the platform: the teacher has to be told which paper, and why.
        logger.warning("document %s could not be parsed: %s", source.id, exc)
        await _fail(db, job, error=f"This {fmt.label} could not be read.", code="document_unreadable")
        return "failed"

    job.profile = f"native:{fmt.name}"
    source.page_count = parsed.page_count
    candidates = import_candidates.candidates(parsed, fmt)

    admin_id = _requested_by(job)
    for position, candidate in enumerate(candidates):
        item = ImportItem(
            job_id=job.id,
            position=position,
            detected_kind=candidate.detected_kind,
            detected_type=candidate.detected_type,
            source_page=candidate.source_page,
            source_sheet=candidate.source_sheet,
            confidence=candidate.confidence,
            decision=enums.ImportItemDecision.PENDING,
            extracted=candidate.extracted,
            missing=[],
            filing=_empty_filing(),
            note=candidate.note,
        )
        # The column holds this module's answer, not the parser's: a word row is complete
        # as text and still missing the language a person has to choose, and a queue that
        # listed nothing as missing would offer Approve on a row the approval must refuse.
        item.missing = pending_fields(item)
        db.add(item)
    await db.flush()
    # The candidates are the finding about this paper, and auto mode below may have to undo
    # a transaction of its own. Saving the queue first means a conflict never costs the
    # teacher the reading that was already done.
    await db.commit()
    job.progress = {
        "stage": "extracted",
        "format": fmt.name,
        "bytes": len(data),
        "pages": parsed.page_count,
        "sheets": parsed.sheet_names,
        "encoding": parsed.encoding,
        "candidates": len(candidates),
        "by_kind": _count_kinds(candidates),
        "requested_by": (job.progress or {}).get("requested_by"),
    }

    if not candidates:
        # An empty queue is a finding about the document, and the two reasons a teacher can
        # act on are different: a paper with no text at all, and a scanned paper that needs
        # OCR before anyone can read it.
        job.status = enums.JobStatus.COMPLETED
        job.finished_at = security.utcnow()
        # A JSONB column tracks an assignment, not a change made inside the dictionary it
        # holds, so the finding is written as a new dict rather than patched into the old one.
        job.progress = {**(job.progress or {}), "outcome": "no_text_layer" if fmt.name == "pdf" else "no_text"}
        await audit_service.record_audit(
            db,
            action="import.job.completed",
            actor_id=admin_id,
            target_type="import_job",
            target_id=job.id,
            after={"candidates": 0, "outcome": job.progress["outcome"], "format": fmt.name},
        )
        await db.commit()
        return "empty"

    if job.auto_mode and admin_id is not None:
        await _file_confident_rows(db, job.id, admin_id)

    pending = await _count_by(db, job.id, ImportItem.decision, enums.ImportItemDecision.PENDING)
    job.status = enums.JobStatus.NEEDS_REVIEW if pending else enums.JobStatus.COMPLETED
    job.finished_at = security.utcnow()
    job.progress = {**(job.progress or {}), "outcome": "extracted"}
    await audit_service.record_audit(
        db,
        action="import.job.reviewed",
        actor_id=admin_id,
        target_type="import_job",
        target_id=job.id,
        after={
            "candidates": len(candidates),
            "pending": pending,
            "auto_approved": len(candidates) - pending if job.auto_mode else 0,
            "format": fmt.name,
        },
    )
    await db.commit()
    return "ok"


async def _zip_names_of(data: bytes) -> list[str]:
    """The member list of an archive already held in memory (the worker's copy)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return []


def _requested_by(job: ImportJob) -> uuid.UUID | None:
    """The teacher whose upload opened this queue, as recorded when it was created.

    A worker has no session and no browser, so an automatic approval acts for the person
    who asked for the import - and the audit row says so. A job created without that
    record (a row from before this column was filled) is treated as system work.
    """
    raw = (job.progress or {}).get("requested_by")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except (ValueError, TypeError):
        return None


def _empty_filing() -> dict:
    return {"status": "draft", "level": None, "language": None, "topic_ids": [], "tag_ids": []}


def _count_kinds(candidates: list[import_candidates.Candidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.detected_kind] = counts.get(candidate.detected_kind, 0) + 1
    return counts


async def _count_by(db: AsyncSession, job_id: uuid.UUID, column: Any, value: Any) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(ImportItem).where(
                ImportItem.job_id == job_id, column == value
            )
        )
        or 0
    )


async def _fail(db: AsyncSession, job: ImportJob, *, error: str, code: str) -> None:
    """Mark a job failed with the sentence a teacher reads, and record it."""
    job.status = enums.JobStatus.FAILED
    job.error = error
    job.finished_at = security.utcnow()
    job.progress = {**(job.progress or {}), "stage": "failed", "outcome": code}
    await audit_service.record_audit(
        db,
        action="import.job.failed",
        actor_type="system",
        actor_id=_requested_by(job),
        target_type="import_job",
        target_id=job.id,
        after={"code": code, "error": error},
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# Reads: the job list, the queue, one candidate
# --------------------------------------------------------------------------- #


async def source_read(db: AsyncSession, source: SourceFile) -> dict:
    meta = source.meta or {}
    return {
        "id": str(source.id),
        "title": source.title,
        "original_filename": source.original_filename,
        "mime_type": source.mime_type,
        "format": meta.get("format"),
        "format_label": meta.get("label"),
        "bytes": meta.get("bytes"),
        "page_count": source.page_count,
        "language": source.language,
        "trashed": source.deleted_at is not None,
    }


async def job_read(db: AsyncSession, job: ImportJob) -> dict:
    counts = await _decision_counts(db, job.id)
    source = await db.get(SourceFile, job.source_file_id) if job.source_file_id else None
    return {
        "id": str(job.id),
        "status": job.status.value if isinstance(job.status, enums.JobStatus) else str(job.status),
        "auto_mode": job.auto_mode,
        "profile": job.profile,
        "error": job.error,
        "progress": job.progress or {},
        "created_at": _stamp(job.created_at),
        "updated_at": _stamp(job.updated_at),
        "started_at": _stamp(job.started_at),
        "finished_at": _stamp(job.finished_at),
        "source": await source_read(db, source) if source else None,
        "counts": counts,
    }


async def _decision_counts(db: AsyncSession, job_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await db.execute(
            select(ImportItem.decision, func.count())
            .where(ImportItem.job_id == job_id)
            .group_by(ImportItem.decision)
        )
    ).all()
    counts = {decision.value: 0 for decision in enums.ImportItemDecision}
    total = 0
    for decision, count in rows:
        key = decision.value if isinstance(decision, enums.ImportItemDecision) else str(decision)
        counts[key] = int(count)
        total += int(count)
    counts["total"] = total
    counts["incomplete"] = int(
        await db.scalar(
            select(func.count())
            .select_from(ImportItem)
            .where(
                ImportItem.job_id == job_id,
                ImportItem.decision == enums.ImportItemDecision.PENDING,
                func.jsonb_array_length(ImportItem.missing) > 0,
            )
        )
        or 0
    )
    return counts


def _stamp(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _page_clause(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise ImportProblem("page starts at 1", code="page_invalid")
    if not 1 <= page_size <= 200:
        raise ImportProblem("page_size is between 1 and 200", code="page_size_invalid")
    return (page - 1) * page_size, page_size


def _ordered(stmt: Any, sortable: dict, sort: str, order: str, fallback: Any) -> Any:
    column = sortable.get(sort, fallback)
    return stmt.order_by(column.desc() if order == "desc" else column.asc())


async def list_jobs(
    db: AsyncSession,
    *,
    status: str | None = None,
    q: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """The teacher's import history, newest first, with the queue counts each row needs."""
    offset, limit = _page_clause(page, page_size)
    clauses = []
    if status:
        try:
            clauses.append(ImportJob.status == enums.JobStatus(status))
        except ValueError:
            raise ImportProblem(
                f"status must be one of: {', '.join(JOB_STATUSES)}", code="status_invalid"
            ) from None
    if q and q.strip():
        clauses.append(SourceFile.title.ilike(f"%{q.strip()}%"))

    stmt = (
        select(ImportJob, SourceFile)
        .join(SourceFile, SourceFile.id == ImportJob.source_file_id)
        .where(*clauses)
    )
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(ImportJob)
            .join(SourceFile, SourceFile.id == ImportJob.source_file_id)
            .where(*clauses)
        )
        or 0
    )
    rows = (
        await db.execute(_ordered(stmt, JOB_SORTABLE, sort, order, ImportJob.created_at).offset(offset).limit(limit))
    ).all()
    payloads = []
    for job, source in rows:
        payload = await job_read(db, job)
        payload["source"] = await source_read(db, source)
        payloads.append(payload)
    return {
        "items": payloads,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + limit - 1) // limit if total else 0,
    }


async def get_job(db: AsyncSession, job_id: uuid.UUID, *, for_read: bool = True) -> ImportJob | None:
    job = await db.get(ImportJob, job_id)
    if job is None and for_read:
        raise JobNotFound("That import job is not here.", code="import_not_found")
    return job


async def list_items(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    decision: str | None = None,
    kind: str | None = None,
    only_incomplete: bool = False,
    view: str = "queue",
    sort: str = "position",
    order: str = "asc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """One page of the review queue, in the order the paper was written."""
    offset, limit = _page_clause(page, page_size)
    stmt = select(ImportItem).where(ImportItem.job_id == job_id)
    if view not in VIEWS:
        raise ImportProblem(f"view must be one of: {', '.join(VIEWS)}", code="view_invalid")
    if view == "queue":
        stmt = stmt.where(ImportItem.decision == enums.ImportItemDecision.PENDING)
    if decision:
        try:
            stmt = stmt.where(ImportItem.decision == enums.ImportItemDecision(decision))
        except ValueError:
            raise ImportProblem(
                f"decision must be one of: {', '.join(DECISIONS)}", code="decision_invalid"
            ) from None
    if kind:
        if kind not in i_schemas.KINDS:
            raise ImportProblem(
                f"kind must be one of: {', '.join(i_schemas.KINDS)}", code="kind_invalid"
            )
        stmt = stmt.where(ImportItem.detected_kind == kind)
    if only_incomplete:
        stmt = stmt.where(func.jsonb_array_length(ImportItem.missing) > 0)

    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await db.execute(
            _ordered(stmt, ITEM_SORTABLE, sort, order, ImportItem.position).offset(offset).limit(limit)
        )
    ).scalars()
    return {
        "items": [item_read(item) for item in rows],
        "total": total,
        "page": page,
        "page_size": limit,
        "pages": (total + limit - 1) // limit if total else 0,
    }


async def get_item(db: AsyncSession, item_id: uuid.UUID) -> ImportItem:
    item = await db.get(ImportItem, item_id)
    if item is None:
        raise JobNotFound("That candidate is not in any import queue.", code="import_not_found")
    return item


def item_read(item: ImportItem) -> dict:
    """One candidate, as the review screen needs it.

    `missing` is re-evaluated rather than echoed from the column, so a row whose gap the
    teacher has already filled stops reporting it on the same request that filled it.
    """
    return {
        "id": str(item.id),
        "job_id": str(item.job_id),
        "position": item.position,
        "kind": item.detected_kind,
        "type": item.detected_type,
        "page": item.source_page,
        "sheet": item.source_sheet,
        "confidence": item.confidence,
        "decision": item.decision.value if isinstance(item.decision, enums.ImportItemDecision) else str(item.decision),
        "extracted": item.extracted or {},
        "corrected": item.corrected,
        "has_correction": bool(item.corrected),
        "missing": pending_fields(item),
        "filing": item.filing or _empty_filing(),
        "note": item.note,
        "result": (
            {"kind": item.result_ref_type, "id": str(item.result_ref_id)} if item.result_ref_id else None
        ),
        "editable": item.decision not in FINAL_DECISIONS,
        "approvable": is_approvable(item),
        "attention": needs_attention(item),
    }


# --------------------------------------------------------------------------- #
# The teacher's edits
# --------------------------------------------------------------------------- #


def _clean_text(kind: str, raw: dict) -> dict:
    """A candidate's text, kept to the fields that kind has.

    An unknown key is refused rather than stored: `extracted` and `corrected` are the
    evidence of what the document said, and a browser that could add a field to them
    would break that claim.
    """
    allowed = i_schemas.EDITABLE_FIELDS.get(kind)
    if allowed is None:
        raise ImportProblem(f"'{kind}' is not a kind this importer knows", code="kind_invalid")
    unknown = sorted(str(key) for key in set(raw) - set(allowed))
    if unknown:
        raise ImportProblem(
            f"These fields are not part of a {kind}: {', '.join(unknown)}",
            code="unknown_field",
            params={"fields": unknown},
        )
    cleaned: dict = {}
    for key in allowed:
        if key not in raw:
            continue
        value = raw[key]
        if key == "options":
            cleaned[key] = _clean_options(value)
        elif key in ("accepted", "topic_ids", "tag_ids"):
            cleaned[key] = _clean_strings(value, key)
        elif key == "examples":
            cleaned[key] = _clean_examples(value)
        elif isinstance(value, str):
            cleaned[key] = value.strip()
        elif value is None:
            continue
        else:
            raise ImportProblem(
                f"{key} has to be written as text", code="field_type", params={"field": key}
            )
    return cleaned


def _clean_options(value: Any) -> list[dict]:
    """The choice list, in the shape the question bank stores it.

    A browser may send either the plain list the parsers produce or the ticked list the
    review screen edits, and both mean the same thing.
    """
    if not isinstance(value, list):
        raise ImportProblem(
            "The options have to be a list", code="field_list", params={"field": "options"}
        )
    out: list[dict] = []
    for option in value[:26]:
        if isinstance(option, str):
            text = option.strip()
            if text:
                out.append({"text": text, "correct": False})
            continue
        if isinstance(option, dict) and _has_text(option.get("text")):
            out.append({"text": str(option["text"]).strip(), "correct": bool(option.get("correct"))})
    return out


def _clean_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ImportProblem(
            f"{field} has to be written as a list", code="field_list", params={"field": field}
        )
    return [str(item).strip() for item in value if str(item).strip()]


def _clean_examples(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ImportProblem(
            "The examples have to be a list", code="field_list", params={"field": "examples"}
        )
    out: list[dict] = []
    for example in value[:40]:
        if isinstance(example, str):
            if example.strip():
                out.append({"sentence": example.strip()})
        elif isinstance(example, dict) and _has_text(example.get("sentence")):
            out.append({"sentence": str(example["sentence"]).strip()})
    return out


def _filing_of(payload: i_schemas.ImportFiling | None, current: dict | None) -> dict:
    """The filing a row will carry: the teacher's choices, with the rest kept."""
    base = {**_empty_filing(), **(current or {})}
    if payload is None:
        return base
    chosen = payload.model_dump()
    for key in FILING_KEYS:
        if key == "status":
            continue
        if chosen.get(key) is not None:
            base[key] = chosen[key]
    base["status"] = payload.status or base.get("status") or "draft"
    if base["status"] not in FILING_STATUSES:
        raise ImportProblem(
            f"status must be one of: {', '.join(FILING_STATUSES)}", code="status_invalid"
        )
    base["topic_ids"] = [str(value) for value in base.get("topic_ids") or []]
    base["tag_ids"] = [str(value) for value in base.get("tag_ids") or []]
    if base.get("language"):
        base["language"] = str(base["language"]).strip().lower()
    return base


async def _locked(db: AsyncSession, item: ImportItem) -> ImportItem:
    """The candidate again, read under a row lock.

    A decision has to be made against the row as it stands when the write happens, not as
    it looked when the request loaded it: two teachers who press Approve on one candidate in
    the same moment both read `pending`, and the one that gets there second has to see what
    the first filed. `uq_import_item_result` cannot settle that - each request mints its own
    content id, so the index has nothing to disagree about - and an extra content row nobody
    asked for is not an acceptable answer. The lock makes the second request wait for the
    first transaction, and the refusal then comes from the state it reads.

    The row is read into the object the decision is made from, so the lock alone is not
    enough: the request already loaded this candidate once, unlined, before the lock, and an
    ORM select hands back the instance in its session's identity map without refreshing the
    attributes it already holds. `populate_existing` is what makes the locking read mean
    something - without it the second teacher waits for the first, is handed the row back,
    and still reads the `pending` their own session saw before the lock was won.
    """
    row = (
        await db.execute(
            select(ImportItem)
            .where(ImportItem.id == item.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def edit_item(
    db: AsyncSession, item: ImportItem, payload: i_schemas.ImportItemEdit, *, admin_id: uuid.UUID
) -> dict:
    """Write the candidate's text or filing, without filing any content.

    A rejected row reopens: a teacher who changes their mind about a sentence has decided
    again, and the queue is where the new decision is made.
    """
    item = await _locked(db, item)
    if item.decision in FINAL_DECISIONS:
        raise AlreadyFiled(
            "This candidate has already been filed. Edit the content itself in the bank.",
            code="candidate_filed",
        )
    before = {"kind": item.detected_kind, "type": item.detected_type, "corrected": item.corrected}

    kind = payload.kind or item.detected_kind or "note"
    item.detected_kind = kind
    if payload.type is not None:
        item.detected_type = _normalise_type(payload.type)

    raw = payload.extracted
    if raw is not None:
        text = _clean_text(kind, raw)
        item.corrected = None if text == (item.extracted or {}) else text

    if payload.filing is not None:
        item.filing = _filing_of(payload.filing, item.filing)

    item.missing = pending_fields(item)
    if item.decision == enums.ImportItemDecision.REJECTED:
        item.decision = enums.ImportItemDecision.PENDING
    await db.flush()

    await audit_service.record_audit(
        db,
        action="import.item.edited",
        actor_id=admin_id,
        target_type="import_item",
        target_id=item.id,
        before=before,
        after={"kind": item.detected_kind, "type": item.detected_type, "missing": item.missing},
    )
    return item_read(item)


def _normalise_type(raw: str) -> str:
    """A question type as the registry spells it, whether the paper said `Multiple Choice`.

    A type this importer cannot build is kept as written and refused at approval, which
    names it to the teacher; a name quietly replaced by `multiple_choice` would file a
    question whose shape the document never had.
    """
    folded = raw.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "mc": "multiple_choice",
        "mcq": "multiple_choice",
        "choice": "multiple_choice",
        "single_choice": "multiple_choice",
        "multiplechoice": "multiple_choice",
        "multi": "multi_select",
        "multichoice": "multi_select",
        "multiselect": "multi_select",
        "short": "short_answer",
        "open": "short_answer",
        "text": "short_answer",
    }
    return aliases.get(folded, folded)


# --------------------------------------------------------------------------- #
# Approval: the only path from a document to the bank
# --------------------------------------------------------------------------- #

#: Which model and payload each kind is filed as. `ref_type` is what `import_item` records,
#: and it is the same word the rest of the product uses for a piece of content, so a
#: provenance read never has to translate two vocabularies.
_TARGETS: dict[str, tuple[str, type]] = {
    "question": ("question", Question),
    "vocabulary": ("vocabulary", VocabularyEntry),
    "reading": ("reading", Reading),
}


def _question_payload(item: ImportItem, text: dict, filing: dict) -> q_schemas.QuestionCreate:
    question_type = item.detected_type or "multiple_choice"
    config: dict = {}
    if question_type in CHOICE_TYPES:
        config["options"] = [
            {"text": option["text"], "correct": bool(option.get("correct"))}
            for option in text.get("options") or []
            if isinstance(option, dict) and option.get("text")
        ]
    else:
        config["accepted"] = _accepted_answers(text)
    return q_schemas.QuestionCreate(
        type=question_type,
        prompt=str(text.get("prompt") or "").strip(),
        config=config,
        status=filing.get("status") or "draft",
        level=filing.get("level") or text.get("level"),
        learning_language=filing.get("language"),
        explanation=(text.get("explanation") or None),
        topic_ids=[uuid.UUID(str(value)) for value in filing.get("topic_ids") or []],
        tag_ids=[uuid.UUID(str(value)) for value in filing.get("tag_ids") or []],
        change_note="Created by document import",
    )


def _vocabulary_payload(item: ImportItem, text: dict, filing: dict) -> v_schemas.VocabularyCreate:
    return v_schemas.VocabularyCreate(
        word=str(text.get("word") or "").strip(),
        learning_language=filing.get("language") or "",
        definition=(text.get("definition") or None),
        level=filing.get("level") or text.get("level"),
        status=filing.get("status") or "draft",
        examples=[
            v_schemas.ExampleInput(sentence=example["sentence"])
            for example in text.get("examples") or []
            if isinstance(example, dict) and example.get("sentence")
        ],
        tag_ids=[uuid.UUID(str(value)) for value in filing.get("tag_ids") or []],
    )


def _reading_payload(item: ImportItem, text: dict, filing: dict) -> r_schemas.ReadingCreate:
    return r_schemas.ReadingCreate(
        title=str(text.get("title") or "").strip(),
        body=str(text.get("body") or "").strip(),
        language=filing.get("language"),
        level=filing.get("level") or text.get("level"),
        status=filing.get("status") or "draft",
    )


_BUILDERS = {"question": _question_payload, "vocabulary": _vocabulary_payload, "reading": _reading_payload}


def _schema_refusal(exc: ValidationError) -> str:
    """The bank's own complaint about a payload, in the sentence a review screen shows.

    Pydantic names the field last in each location, which is the part a teacher can act on;
    the path in front of it is this module's own object, and the numbers of a candidate's
    row are not something the screen needs explained twice.
    """
    parts: list[str] = []
    for error in exc.errors(include_url=False):
        field = str(error["loc"][-1]) if error["loc"] else "body"
        parts.append(f"{field}: {error['msg']}")
    return "The bank could not hold this content - " + "; ".join(parts[:3])


async def _create_content(
    db: AsyncSession,
    item: ImportItem,
    filing: dict,
    *,
    admin_id: uuid.UUID | None,
    job: ImportJob,
    source: SourceFile | None,
) -> dict:
    """File one candidate through the bank's own service, and stamp where it came from.
    Going through `create_question` / `create_entry` / `reading_service.create` is the
    point: an imported row is subject to exactly the rules a teacher's browser row is, and
    Phase 8's worker later on cannot be a second, weaker path into the bank. The services
    take no provenance arguments, so the columns are written here, on the row they belong
    to, in the same transaction that created it.
    """
    kind = item.detected_kind or ""
    ref_type, model = _TARGETS[kind]
    text = effective_text(item)
    payload = _BUILDERS[kind](item, text, filing)

    if kind == "question":
        created = await question_service.create_question(db, payload, admin_id=admin_id)
    elif kind == "vocabulary":
        created = await vocabulary_service.create_entry(db, payload, admin_id=admin_id)
    else:
        created = await reading_service.create(db, payload, admin_id=admin_id)

    row = await db.get(model, uuid.UUID(str(created["id"])))
    if row is None:  # the service returned an id that is not in this session
        raise ImportProblem("The bank did not return the content it created.", code="import_rule")
    row.source_file_id = source.id if source else None
    if model is Question:
        # A question is the one kind whose provenance the schema carries: where on the
        # paper it was found, which sheet, and which queue filed it.
        row.source_page = item.source_page
        row.source_sheet = item.source_sheet
        row.extraction_method = job.profile
        row.import_job_id = item.job_id
    await db.flush()
    return {"ref_type": ref_type, "created": created, "row": row}


async def approve_item(
    db: AsyncSession,
    item: ImportItem,
    *,
    admin_id: uuid.UUID,
    filing: i_schemas.ImportFiling | None = None,
    job: ImportJob | None = None,
    source: SourceFile | None = None,
) -> dict:
    """File one candidate as content, or refuse with the field it still lacks.

    Every refusal here happens before a row is written: the completeness check is this
    module's own, and the bank's services validate the payload, the language and an
    already-existing word before they add anything. That is what lets "approve this page"
    walk a mixed queue without a half-filed candidate behind it - a candidate this function
    rejects is exactly where it was, and the rows around it carry on.

    The row lock is what makes that true for two teachers at once. The loser waits for the
    winner's transaction, reads the row the winner decided, and answers 409 with nothing of
    its own in the bank.
    """
    item = await _locked(db, item)
    if item.decision in FINAL_DECISIONS:
        raise AlreadyFiled("This candidate has already been filed.", code="candidate_filed")
    if (item.detected_kind or "") not in APPROVABLE_KINDS:
        raise ImportProblem(
            "Choose what this text should become before approving it.",
            code="nothing_to_file",
            params={"kind": item.detected_kind},
        )

    if filing is not None:
        item.filing = _filing_of(filing, item.filing)
    gaps = pending_fields(item)
    if gaps:
        names = ", ".join(_MISSING_EXPLANATIONS.get(gap, gap) for gap in gaps)
        raise IncompleteCandidate(
            f"This candidate still needs: {names}.",
            code="candidate_incomplete",
            params={"fields": gaps},
        )

    job = job or await db.get(ImportJob, item.job_id)
    if job is None:
        raise JobNotFound("That candidate's import job is gone.", code="import_not_found")
    if source is None and job.source_file_id:
        source = await db.get(SourceFile, job.source_file_id)

    try:
        filed = await _create_content(
            db,
            item,
            item.filing or _empty_filing(),
            admin_id=admin_id,
            job=job,
            source=source,
        )
    except DuplicateWord as exc:
        raise AlreadyInBank(str(exc), code="content_exists") from exc
    except (QuestionInputError, VocabularyError, PassageError) as exc:
        raise ImportProblem(str(exc), code="bank_refused", params={"reason": str(exc)}) from exc
    except ValidationError as exc:
        # A document may write more than a column holds - a `level` of eighty characters in
        # a spreadsheet cell, say. The bank's schema is the rule, and the teacher has to be
        # told which field broke it in the same 422 the screen shows for any other refusal,
        # not in a 500 that blames the platform.
        reason = _schema_refusal(exc)
        raise ImportProblem(reason, code="bank_refused", params={"reason": reason}) from exc

    item.result_ref_type = filed["ref_type"]
    item.result_ref_id = uuid.UUID(str(filed["created"]["id"]))
    item.decision = (
        enums.ImportItemDecision.EDITED if item.corrected else enums.ImportItemDecision.APPROVED
    )
    item.missing = []
    await db.flush()

    await audit_service.record_audit(
        db,
        action="import.item.approved",
        actor_id=admin_id,
        target_type="import_item",
        target_id=item.id,
        after={
            "kind": filed["ref_type"],
            "content_id": str(filed["created"]["id"]),
            "job_id": str(item.job_id),
            "edited": bool(item.corrected),
        },
    )
    await _settle_job(db, item.job_id)
    return item_read(item)


async def reject_item(
    db: AsyncSession, item: ImportItem, *, admin_id: uuid.UUID, reason: str | None = None
) -> dict:
    """Refuse one candidate. Nothing is destroyed and nothing is created."""
    item = await _locked(db, item)
    if item.decision in FINAL_DECISIONS:
        raise AlreadyFiled(
            "This candidate has already been filed. Remove the content itself in the bank.",
            code="candidate_filed",
        )
    item.decision = enums.ImportItemDecision.REJECTED
    if reason and reason.strip():
        item.note = reason.strip()[:2000]
    await db.flush()
    await audit_service.record_audit(
        db,
        action="import.item.rejected",
        actor_id=admin_id,
        target_type="import_item",
        target_id=item.id,
        after={"job_id": str(item.job_id), "reason": item.note},
    )
    await _settle_job(db, item.job_id)
    return item_read(item)


async def _settle_job(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Close a queue that has no rows left to decide, so it stops saying "needs review".

    The status is derived from the rows rather than remembered: a teacher who approves the
    last candidate from one screen and rejects the one before it from another would
    otherwise leave a finished paper open forever.
    """
    pending = await _count_by(
        db, job_id, ImportItem.decision, enums.ImportItemDecision.PENDING
    )
    job = await db.get(ImportJob, job_id)
    if job is None or job.status == enums.JobStatus.PROCESSING:
        return
    job.status = enums.JobStatus.NEEDS_REVIEW if pending else enums.JobStatus.COMPLETED


async def _file_confident_rows(db: AsyncSession, job_id: uuid.UUID, admin_id: uuid.UUID) -> None:
    """File the candidates that need no decision at all, when auto mode was asked for.

    Only rows with nothing missing and a confidence at or above the configured threshold
    are touched, and a row the bank refuses is left in the queue with that refusal's code as
    its note, so the screen can say it in the reader's language. Auto mode is the teacher's
    own request that the obvious rows not wait for them; it is not a licence to guess.

    One commit per row, because a refusal the bank reports can undo the transaction it was
    found in - a word already in the library is refused by the unique index, and the only
    way out of that is to roll back. Each row is therefore read again by its own id before
    it is touched: an undone transaction expires every instance the session was holding, and
    writing to one of those would be a database read inside the middle of a note about the
    refusal.
    """
    threshold = get_settings().import_low_confidence_threshold
    ids = list(
        (
            await db.execute(
                select(ImportItem.id)
                .where(
                    ImportItem.job_id == job_id,
                    ImportItem.decision == enums.ImportItemDecision.PENDING,
                    ImportItem.confidence >= threshold,
                )
                .order_by(ImportItem.position)
            )
        ).scalars()
    )
    for item_id in ids:
        item = await db.get(ImportItem, item_id)
        if item is None or not is_approvable(item):
            continue
        try:
            await approve_item(db, item, admin_id=admin_id)
            await db.commit()
        except (
            ImportProblem,
            IncompleteCandidate,
            JobNotFound,
            AlreadyFiled,
            AlreadyInBank,
        ) as exc:
            row = await db.get(ImportItem, item_id)
            if row is None:
                continue
            # The refusal's code, not its English sentence: this text reaches a teacher's
            # screen, and which fields were missing is already in the row's own `missing`.
            row.note = getattr(exc, "code", None) or str(exc)[:2000]
            await db.commit()


async def decide_item(
    db: AsyncSession, item: ImportItem, payload: i_schemas.ImportItemDecision, *, admin_id: uuid.UUID
) -> dict:
    if payload.approve:
        return await approve_item(db, item, admin_id=admin_id, filing=payload.filing)
    return await reject_item(db, item, admin_id=admin_id)


async def bulk(
    db: AsyncSession, payload: i_schemas.ImportBulkRequest, *, admin_id: uuid.UUID
) -> dict:
    """One action over a page of the queue, answered row by row - never a silent partial win.

    "Approve this page" over a mixed queue is the normal case, and the honest answer is
    the list of rows that became content plus the list of rows still waiting, each with
    the field it is missing. Nothing here rolls back because one candidate was unfinished.

    A row is committed as it is filed, so `done` names the content that exists, and each row
    is read again by its own id before it is touched: a word the bank already holds is
    refused by the unique index, and the only way out of that is to undo the transaction it
    was found in - which leaves every instance the batch was holding expired.

    A body this action cannot carry at all - `file` with nothing to file under - is refused
    before any row is read, because answering it row by row would tell the teacher the queue
    was the problem rather than the request.
    """
    requested = list(dict.fromkeys(payload.item_ids))
    if payload.action == "file" and payload.filing is None:
        raise ImportProblem("The file action needs the filing to set.", code="filing_missing")

    known = set((await db.execute(select(ImportItem.id).where(ImportItem.id.in_(requested)))).scalars())
    not_found = [str(value) for value in requested if value not in known]

    done: list[str] = []
    refused: list[dict] = []
    for requested_id in requested:
        if requested_id not in known:
            continue
        item = await db.get(ImportItem, requested_id)
        if item is None:
            not_found.append(str(requested_id))
            continue
        try:
            if payload.action == "approve":
                await approve_item(db, item, admin_id=admin_id, filing=payload.filing)
            elif payload.action == "reject":
                await reject_item(db, item, admin_id=admin_id)
            else:
                item = await _locked(db, item)
                if item.decision in FINAL_DECISIONS:
                    raise AlreadyFiled(
                        "This candidate has already been filed.", code="candidate_filed"
                    )
                item.filing = _filing_of(payload.filing, item.filing)
                item.missing = pending_fields(item)
                await db.flush()
        except (
            ImportProblem,
            IncompleteCandidate,
            AlreadyFiled,
            AlreadyInBank,
            JobNotFound,
        ) as exc:
            refused.append(
                {"id": str(requested_id), "code": exc.code, "params": exc.params or {}, "reason": str(exc)}
            )
            continue
        await db.commit()
        done.append(str(requested_id))

    await audit_service.record_audit(
        db,
        action=f"import.bulk.{payload.action}",
        actor_id=admin_id,
        target_type="import_job",
        after={
            "requested": len(requested),
            "done": len(done),
            "refused": len(refused),
            "not_found": len(not_found),
        },
    )
    return {
        "action": payload.action,
        "done": done,
        "refused": refused,
        "not_found": not_found,
    }


# --------------------------------------------------------------------------- #
# The original document, and removing a queue
# --------------------------------------------------------------------------- #


async def document_bytes(db: AsyncSession, job: ImportJob) -> tuple[bytes, str, str | None]:
    """The paper this queue was made from, for a teacher comparing the two.

    The application reads the object and hands it over; the browser never sees a storage
    key, a bucket or a link that authorises itself.
    """
    source = await db.get(SourceFile, job.source_file_id) if job.source_file_id else None
    if source is None or not source.storage_key:
        raise JobNotFound("This import has no stored document.", code="document_missing")
    try:
        data = await run_in_threadpool(get_storage().get_bytes, source.storage_key)
    except (ObjectMissing, StorageUnavailable) as exc:
        raise _storage_failure(exc) from exc
    meta = source.meta or {}
    mime = source.mime_type or meta.get("format") or "application/octet-stream"
    return data, mime, source.original_filename


async def _filed_and_live(db: AsyncSession, job_id: uuid.UUID) -> int:
    """How many pieces of content this queue filed are still in the bank.

    Counted through the tables that hold the content rather than through the candidate rows,
    because the promise the refusal makes is that the teacher can get here once the content
    is gone. A question thrown away in the bank is gone for every purpose the product has:
    no screen serves it and no version of it is being edited, so the record of where it came
    from no longer has anything to protect.
    """
    total = 0
    for ref_type, model in _TARGETS.values():
        total += int(
            await db.scalar(
                select(func.count())
                .select_from(ImportItem)
                .join(model, model.id == ImportItem.result_ref_id)
                .where(
                    ImportItem.job_id == job_id,
                    ImportItem.result_ref_type == ref_type,
                    model.deleted_at.is_(None),
                )
            )
            or 0
        )
    return total


async def delete_job(db: AsyncSession, job: ImportJob, *, admin_id: uuid.UUID) -> dict:
    """Remove a queue and the candidates in it, leaving any filed content alone.

    Refused while the worker is reading the paper, and refused while the content this queue
    filed is still in the bank: the candidate rows are the only record of which sentence in
    which paper became which question, and that record is worth keeping until the content
    itself is gone. The stored document is only trashed when no other queue cites it.
    """
    if job.status == enums.JobStatus.PROCESSING:
        raise ImportProblem(
            "This document is being read right now. Wait for it to finish.", code="job_processing"
        )
    filed = await _filed_and_live(db, job.id)
    if filed:
        raise AlreadyFiled(
            f"This import filed {filed} piece{'s' if filed != 1 else ''} of content. "
            "Remove the content in the bank first - the record of where it came from stays "
            "until then.",
            code="job_has_content",
            params={"filed": filed},
        )

    source_id = job.source_file_id
    await audit_service.record_audit(
        db,
        action="import.job.deleted",
        actor_id=admin_id,
        target_type="import_job",
        target_id=job.id,
        before={"status": job.status.value, "source_file_id": str(source_id) if source_id else None},
    )
    await db.delete(job)
    await db.flush()

    if source_id is not None:
        remaining = int(
            await db.scalar(
                select(func.count()).select_from(ImportJob).where(ImportJob.source_file_id == source_id)
            )
            or 0
        )
        if not remaining:
            source = await db.get(SourceFile, source_id)
            if source is not None:
                source.deleted_at = security.utcnow()
                await db.flush()
    return {"ok": True, "deleted": str(job.id)}


async def retry_job(db: AsyncSession, job: ImportJob, *, admin_id: uuid.UUID) -> dict:
    """Put a queued or failed job back in front of the worker.

    A failure is usually the platform's - storage unreachable, the queue down while nobody
    was looking - so the teacher's own retry has to be enough to try again. Re-running a
    job that already produced candidates would double the queue, so only a job that read no
    rows may be retried, and the candidate count says why not.
    """
    if job.status == enums.JobStatus.PROCESSING:
        raise ImportProblem("This document is being read right now.", code="job_processing")
    existing = int(
        await db.scalar(select(func.count()).select_from(ImportItem).where(ImportItem.job_id == job.id)) or 0
    )
    if existing:
        raise ImportProblem(
            "This import already produced candidates. Clear the queue before reading it again.",
            code="job_has_candidates",
            params={"candidates": existing},
        )
    job.status = enums.JobStatus.QUEUED
    job.error = None
    job.started_at = None
    job.finished_at = None
    job.progress = {**(job.progress or {}), "stage": "queued", "outcome": None}
    await db.flush()
    await audit_service.record_audit(
        db,
        action="import.job.retried",
        actor_id=admin_id,
        target_type="import_job",
        target_id=job.id,
    )
    await dispatch(db, job)
    return await job_read(db, job)
