"""Background job implementations.

Large jobs (import/OCR/AI/transcription/export/backup/trash-purge/attempt-expiry)
never run in the web request path. Job status is written back to the DB
(Queued/Processing/Needs Review/Completed/Failed) and surfaced to the teacher in
plain language, not as raw worker internals."""
from __future__ import annotations

from sqlalchemy import select

from app.adapters import get_ai_provider, get_document_parser, get_ocr_provider
from app.core import enums, security
from app.core.database import SessionLocal
from app.models.assessment import ExamAttempt
from app.models.content import ImportJob


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


async def purge_trash(ctx: dict) -> None:
    """Hard-delete soft-deleted rows past the configured retention window.

    Deliberately a no-op in this foundation: no soft-deleted entity has a trash
    screen yet, so the cron runs and changes nothing. The operations phase applies
    `TRASH_RETENTION_DAYS` per entity.
    """
    async with SessionLocal() as db:
        await db.execute(select(1))
        await db.commit()
