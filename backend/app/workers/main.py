"""arq worker entrypoint (`arq app.workers.main.WorkerSettings`).

Runs OCR/import/AI/transcription/export/backup jobs off the web request path so
the UI never freezes. Scheduled crons handle attempt expiry (server-authoritative
timer), trash purge and optional backups."""
from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.workers import jobs

_settings = get_settings()


async def startup(ctx: dict) -> None:
    ctx["settings"] = _settings


async def shutdown(ctx: dict) -> None:
    pass


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    functions = [
        jobs.process_import_job,
        jobs.expire_attempts,
        jobs.purge_trash,
    ]
    on_startup = startup
    on_shutdown = shutdown
    cron_jobs = [
        # Auto-submit timed-out exam attempts every minute.
        cron(jobs.expire_attempts, second={0, 15, 30, 45}),
        # Purge trash once a day.
        cron(jobs.purge_trash, hour=3, minute=30),
    ]
