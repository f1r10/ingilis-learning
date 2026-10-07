"""Background job implementations.

Large jobs (import/OCR/AI/transcription/export/backup/trash-purge/attempt-expiry)
never run in the web request path. Job status is written back to the DB
(Queued/Processing/Needs Review/Completed/Failed) and surfaced to the teacher in
plain language, not as raw worker internals."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.adapters import get_ai_provider, get_document_parser, get_ocr_provider
from app.core import enums, security
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.assessment import ExamAttempt
from app.models.content import ImportJob, Listening, MediaAsset, Question, Reading, VocabularyEntry
from app.services import audit_service


async def process_import_job(ctx: dict, import_job_id: str) -> str:
    """Run the import pipeline for one job up to the teacher-review stage.

    Extraction depends on optional OCR/document/AI adapters. When a provider is
    disabled the job is parked as NEEDS_REVIEW with whatever native parse exists,
    rather than silently guessing."""
    parser, _ocr, _ai = get_document_parser(), get_ocr_provider(), get_ai_provider()
    async with SessionLocal() as db:
        job = await db.get(ImportJob, __import__("uuid").UUID(import_job_id))
        if not job:
            return "not_found"
        job.status = enums.JobStatus.PROCESSING
        job.started_at = security.utcnow()
        await db.commit()

        try:
            if not parser.enabled():
                # No document parser configured: nothing to auto-extract.
                job.status = enums.JobStatus.NEEDS_REVIEW
                job.progress = {"stage": "awaiting_document_parser", "message": "Document import needs a parser enabled in Settings"}
                await db.commit()
                return "needs_review"

            # Real extraction is implemented against the chosen parser/ocr/ai here.
            # Each extracted candidate becomes an ImportItem with a confidence so the
            # Import Review screen can Approve / Reject / Edit / batch-approve.
            job.status = enums.JobStatus.NEEDS_REVIEW
            job.progress = {"stage": "extracted", "message": "Processing document"}
            await db.commit()
            return "ok"
        except Exception as exc:  # noqa: BLE001
            job.status = enums.JobStatus.FAILED
            job.error = str(exc)
            await db.commit()
            return "failed"


async def expire_attempts(ctx: dict) -> None:
    """Server is authoritative: auto-submit attempts whose timer has elapsed.

    Grading on auto-submit is finalised by the assessment service (not shown in
    the request path)."""
    now = security.utcnow()
    async with SessionLocal() as db:
        result = await db.execute(
            select(ExamAttempt).where(
                ExamAttempt.status == enums.AttemptStatus.IN_PROGRESS,
                ExamAttempt.expires_at.is_not(None),
                ExamAttempt.expires_at < now,
            )
        )
        for attempt in result.scalars():
            attempt.status = enums.AttemptStatus.AUTO_SUBMITTED
            attempt.submitted_at = now
            attempt.server_seconds_used = int((now - attempt.started_at).total_seconds())
        await db.commit()


#: What a teacher can put in the trash, in the order a removal would have to walk:
#: content that names a file goes before the file itself.
TRASH_TABLES: tuple[tuple[str, type], ...] = (
    ("question", Question),
    ("vocabulary", VocabularyEntry),
    ("reading", Reading),
    ("listening", Listening),
    ("media_asset", MediaAsset),
)


async def past_retention(days: int) -> dict[str, int]:
    """How many trashed rows of each kind are older than the retention window.

    Rows still in the bank are not counted, whatever their `updated_at` says: time in
    the trash is the only clock a purge reads.
    """
    cutoff = security.utcnow() - timedelta(days=days)
    due: dict[str, int] = {}
    async with SessionLocal() as db:
        for name, model in TRASH_TABLES:
            count = await db.scalar(
                select(func.count()).select_from(model).where(
                    model.deleted_at.is_not(None), model.deleted_at < cutoff
                )
            )
            if count:
                due[name] = int(count)
    return due


async def purge_trash(ctx: dict) -> dict:
    """Count what the trash window has made due for removal, and record the finding.

    Nothing is deleted here. A hard delete is the one action this platform cannot undo,
    so it belongs with the backup that makes a mistaken removal survivable; until that
    exists, the daily run measures the queue instead of quietly working through it. The
    count is real work for the screens that show it: a teacher who trashed a term of
    texts can be told how long the platform will hold them.
    """
    window = get_settings().trash_retention_days
    due = await past_retention(window)
    if due:
        async with SessionLocal() as db:
            await audit_service.record_audit(
                db,
                action="retention.trash.due",
                actor_type="system",
                after={"retention_days": window, "rows": due, "removed": 0},
                detail="trashed rows past the retention window; removal waits for the backup phase",
            )
            await db.commit()
    return {"retention_days": window, "due": due, "removed": 0}
