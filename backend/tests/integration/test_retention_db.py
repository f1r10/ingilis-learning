"""The trash retention job against real Postgres (Phase 5).

Every content screen now has a trash: a teacher can throw a word, a question, a text, a
recording or a file out of the bank, and the row stays. Something has to tell them how
long "stays" means, and the daily worker run is that something. Two things are asserted
here that no offline rule test can reach:

* the window is read from the live `TRASH_RETENTION_DAYS` setting and applied per table,
  so a row thrown out yesterday is not confused with one thrown out two months ago;
* the run **removes nothing**. That is the current contract, and a test that let it
  delete would be a test that let a cron destroy a term of work before the backup that
  makes a mistaken removal survivable exists. The count it records is real, and it is
  what the operations screens will show.

Rows are trashed through the same HTTP endpoints a teacher uses where one exists, so the
job is measured against the product surface and not against a fixture shape.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core import security
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.content import Listening, MediaAsset, Question, Reading, VocabularyEntry
from app.models.ops import AuditLog
from app.workers import jobs

READING_PATH = "/api/v1/reading"
LISTENING_PATH = "/api/v1/listening"
QUESTION_PATH = "/api/v1/questions"
VOCABULARY_PATH = "/api/v1/vocabulary"

TRANSCRIPT = "We met at the ferry and waited for the second boat."


async def _throw_out(model, row_id: str, *, days: int) -> None:
    """Age a trashed row to `days` in the past, as a trash that was never emptied would be."""
    async with SessionLocal() as db:
        row = await db.get(model, uuid.UUID(row_id))
        assert row is not None, f"{model.__name__} {row_id} is not in the database"
        row.deleted_at = security.utcnow() - timedelta(days=days)
        await db.commit()


async def _trashed_through_the_api(client, path: str, row_id: str) -> None:
    resp = await client.delete(f"{path}/{row_id}")
    assert resp.status_code == 200, resp.text


async def _reading(client) -> str:
    resp = await client.post(
        READING_PATH, json={"title": "Old market text", "body": "The quick brown fox jumps.", "language": "en"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _listening(client) -> str:
    resp = await client.post(LISTENING_PATH, json={"title": "Old ferry clip", "transcript": TRANSCRIPT})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _question(client) -> str:
    resp = await client.post(
        QUESTION_PATH,
        json={
            "type": "multiple_choice",
            "prompt": "Which word means 'kite'?",
            "config": {"options": [{"text": "apple"}, {"text": "kite", "correct": True}]},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _word(client) -> str:
    resp = await client.post(
        VOCABULARY_PATH, json={"word": "ferry", "learning_language": "en", "definition": "a boat that crosses"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _file_row(*, trashed_days: int | None = None) -> str:
    """A library row with no upload behind it.

    The job counts rows, so it needs a row and nothing else; no object is put in storage
    because no bytes are being read by anything under test here.
    """
    marker = uuid.uuid4().hex
    async with SessionLocal() as db:
        asset = MediaAsset(
            kind="audio",
            storage_key=f"retention-test/{marker}.mp3",
            original_filename=f"{marker[:8]}.mp3",
            mime_type="audio/mpeg",
            size_bytes=1024,
            checksum=marker,
            source_origin="upload",
            deleted_at=security.utcnow() - timedelta(days=trashed_days) if trashed_days else None,
        )
        db.add(asset)
        await db.commit()
        return str(asset.id)


async def _audit_rows() -> list[AuditLog]:
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "retention.trash.due")
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )


async def test_an_empty_trash_is_reported_as_empty_and_records_nothing(client):
    """A run with nothing due has no reason to write an audit line every night."""
    window = get_settings().trash_retention_days
    assert await jobs.past_retention(window) == {}

    result = await jobs.purge_trash({})
    assert result == {"retention_days": window, "due": {}, "removed": 0}
    assert await _audit_rows() == []


async def test_a_row_thrown_out_today_is_not_due_yet(client):
    """Time in the trash is the only clock the window reads."""
    reading_id = await _reading(client)
    await _trashed_through_the_api(client, READING_PATH, reading_id)

    assert await jobs.past_retention(get_settings().trash_retention_days) == {}

    rows = await client.get(f"{READING_PATH}?view=trash")
    assert [row["id"] for row in rows.json()["items"]] == [reading_id], "the trash screen still shows it"


async def test_the_window_is_measured_in_days_not_in_whether_a_row_looks_old(client):
    """An item that is merely aged but still in the bank is not on its way out."""
    kept = await _reading(client)
    aged = await _reading(client)
    async with SessionLocal() as db:
        for row_id in (kept, aged):
            row = await db.get(Reading, uuid.UUID(row_id))
            row.created_at = row.updated_at = security.utcnow() - timedelta(days=400)
        await db.commit()
    await _trashed_through_the_api(client, READING_PATH, aged)
    await _throw_out(Reading, aged, days=40)

    due = await jobs.past_retention(30)
    assert due == {"reading": 1}, "a live text is counted no matter how long it has sat there"
    assert await jobs.past_retention(90) == {}, "forty days in the trash is not ninety"


async def test_every_table_a_teacher_can_trash_is_counted(client):
    """One window across the whole bank, per table."""
    days = get_settings().trash_retention_days + 10
    reading_id = await _reading(client)
    listening_id = await _listening(client)
    question_id = await _question(client)
    word_id = await _word(client)
    asset_id = await _file_row()

    for path, row_id in (
        (READING_PATH, reading_id),
        (LISTENING_PATH, listening_id),
        (QUESTION_PATH, question_id),
        (VOCABULARY_PATH, word_id),
    ):
        await _trashed_through_the_api(client, path, row_id)
    await _throw_out(Reading, reading_id, days=days)
    await _throw_out(Listening, listening_id, days=days)
    await _throw_out(Question, question_id, days=days)
    await _throw_out(VocabularyEntry, word_id, days=days)
    await _throw_out(MediaAsset, asset_id, days=days)

    due = await jobs.past_retention(get_settings().trash_retention_days)
    assert due == {
        "question": 1,
        "vocabulary": 1,
        "reading": 1,
        "listening": 1,
        "media_asset": 1,
    }


async def test_the_run_counts_reports_and_leaves_the_rows_alone(client):
    """Nothing is destroyed by the nightly run while a mistaken removal has no backup."""
    days = get_settings().trash_retention_days + 3
    reading_id = await _reading(client)
    await _trashed_through_the_api(client, READING_PATH, reading_id)
    await _throw_out(Reading, reading_id, days=days)

    result = await jobs.purge_trash({})
    assert result["removed"] == 0
    assert result["due"] == {"reading": 1}
    assert result["retention_days"] == get_settings().trash_retention_days

    async with SessionLocal() as db:
        still_there = await db.get(Reading, uuid.UUID(reading_id))
    assert still_there is not None and still_there.deleted_at is not None, "the row was not deleted"

    rows = await _audit_rows()
    assert len(rows) == 1, "one run with something due, one line"
    entry = rows[0]
    assert entry.actor_type == "system" and entry.actor_id is None
    assert entry.after == {
        "retention_days": get_settings().trash_retention_days,
        "rows": {"reading": 1},
        "removed": 0,
    }
    assert "backup" in entry.detail


async def test_a_restore_takes_the_row_out_of_the_count(client):
    """The trash screen offers restore, and a restored row is not due for anything."""
    days = get_settings().trash_retention_days + 3
    reading_id = await _reading(client)
    await _trashed_through_the_api(client, READING_PATH, reading_id)
    await _throw_out(Reading, reading_id, days=days)
    assert (await jobs.purge_trash({}))["due"] == {"reading": 1}

    restored = await client.post(f"{READING_PATH}/{reading_id}/restore")
    assert restored.status_code == 200, restored.text

    assert await jobs.past_retention(get_settings().trash_retention_days) == {}


async def test_the_count_is_per_table_and_sums_the_whole_trash(client):
    """Three texts out for a term is three rows, not one line saying 'some'."""
    days = get_settings().trash_retention_days + 30
    ids = [await _reading(client) for _ in range(3)]
    for row_id in ids:
        await _trashed_through_the_api(client, READING_PATH, row_id)
        await _throw_out(Reading, row_id, days=days)
    listening_id = await _listening(client)
    await _trashed_through_the_api(client, LISTENING_PATH, listening_id)
    await _throw_out(Listening, listening_id, days=days)

    due = await jobs.past_retention(get_settings().trash_retention_days)
    assert due == {"reading": 3, "listening": 1}
    assert sum(due.values()) == 4


async def test_the_window_a_teacher_configures_is_the_window_used(client):
    """`TRASH_RETENTION_DAYS` is not decoration: a longer hold counts fewer rows out."""
    reading_id = await _reading(client)
    await _trashed_through_the_api(client, READING_PATH, reading_id)
    await _throw_out(Reading, reading_id, days=12)

    assert await jobs.past_retention(7) == {"reading": 1}
    assert await jobs.past_retention(30) == {}
    assert await jobs.past_retention(0) == {"reading": 1}, "a zero-day hold means 'as soon as it is out'"
