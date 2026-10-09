"""Handing work to the arq worker, and saying so when it cannot be reached.

A teacher's upload of a 40 MB paper must not be parsed inside the request that received
it, so the web process only records the job and asks the worker to run it. That makes
one more piece of infrastructure able to fail in front of a user, and the honest answer
to "the queue is down" is a sentence about the queue - not a job row that says `queued`
while nothing will ever read it, and not a failure that throws away a file the teacher
has already sent.

`enqueue` therefore either proves the worker accepted the job id or raises
`QueueUnavailable`. Callers commit their rows first, so a refusal here leaves real,
resumable state behind rather than a half-written one.
"""
from __future__ import annotations

import logging

from arq import create_pool
from arq.connections import RedisSettings
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: The worker's entrypoint name for one import job (`app.workers.jobs.process_import_job`).
IMPORT_JOB_TASK = "process_import_job"


class QueueUnavailable(RuntimeError):
    """The worker could not be told about a job; the job itself is fine and stored."""


async def enqueue(task: str, *args: object) -> None:
    """Ask the worker to run ``task``, or raise `QueueUnavailable`.

    A pool is opened for the call instead of being kept on the app state: enqueues are
    as rare as uploads, and a connection that outlives the settings behind it is the
    kind of state that makes a redeploy leave work in the old queue.
    """
    settings = get_settings()
    try:
        async with await create_pool(RedisSettings.from_dsn(settings.redis_url)) as pool:
            accepted = await pool.enqueue_job(task, *args)
    except (RedisError, OSError) as exc:
        logger.warning("could not reach the queue for %s: %s", task, exc)
        raise QueueUnavailable(str(exc)) from exc
    if accepted is None:
        # arq refuses a duplicate of a job that is still queued or running, which for an
        # import means the file is already being worked on. Nothing was lost.
        logger.info("%s was already queued", task)
