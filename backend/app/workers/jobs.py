"""Background job implementations.

Large jobs (import/OCR/AI/transcription/export/backup/trash-purge/attempt-expiry)
never run in the web request path. Job status is written back to the DB
(Queued/Processing/Needs Review/Completed/Failed) and surfaced to the teacher in
plain language, not as raw worker internals."""
from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from app.core import enums, security
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.content import ImportJob, Listening, MediaAsset, Question, Reading, VocabularyEntry
from app.services import attempt_service, audit_service, import_service

logger = logging.getLogger(__name__)


async def process_import_job(ctx: dict, import_job_id: str) -> str:
    """Read one stored document into its review queue.

    What the paper contains is decided by `import_service`, not here: the worker runs the
    same function the teacher's screen later reads, so a queue cannot look different in the
    two places it exists. This function's own job is to run it off the request path.

    A second message for a paper already read answers `not_claimed`. That is redis delivering
    the same job twice, not a mistake to punish - the queue the first delivery produced is
    already the answer, and a worker that claimed anyway would double every candidate.
    """
    try:
        job_id = UUID(import_job_id)
    except ValueError:
        # Nothing to mark failed: an id that names no job is a fault in the message, and the
        # log has to say so rather than a queue sitting in Processing forever.
        logger.warning("import message carries an id that is not a job: %r", import_job_id)
        return "bad_id"

    async with SessionLocal() as db:
        try:
            return await import_service.run_job(db, job_id)
        except Exception:  # noqa: BLE001
            # A worker that gives up quietly leaves a queue waiting for a reading that will
            # never arrive. The teacher's own retry is what undoes this, so the failure is
            # written as a failure, in words, and the detail stays in the log.
            logger.exception("import job %s stopped unexpectedly", job_id)
            await db.rollback()
            job = await db.get(ImportJob, job_id)
            if job is not None:
                outcome, sentence = import_service.STOPPED_REFUSAL
                job.status = enums.JobStatus.FAILED
                job.error = sentence
                job.finished_at = security.utcnow()
                job.progress = {**(job.progress or {}), "stage": "failed", "outcome": outcome}
                await db.commit()
            return "failed"


async def expire_attempts(ctx: dict) -> dict:
    """Close every sitting whose server deadline has passed.

    A learner's own requests close their attempt the moment they are read. This sweep is for
    the papers nobody came back for: a deadline cannot be outlasted simply by never pressing
    a button. The grading runs through the same closing path a submit uses, so a paper is not
    marked one way by the browser and another way by the worker.
    """
    async with SessionLocal() as db:
        report = await attempt_service.expire_due(db)
        await db.commit()
    return report


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
