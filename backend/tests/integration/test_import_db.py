"""Document import over HTTP, against real Postgres, real MinIO and a real worker function.

The offline rules (`tests/test_import_rules.py`) settle which gaps a candidate can carry and
which status each refusal answers with. What only a live stack can prove is the pipeline
itself:

* the bytes are stored, the digest is the document's own, and the queue is committed **before**
  the worker is told - asserted from a second connection, so an enqueue that raced its own
  transaction would be caught here rather than in production;
* a paper two colleagues send at the same moment becomes one source, one queue and one object,
  because the partial unique index settles it while both requests are still in flight - and the
  two interleavings that rule actually depends on are also pinned down on their own, because a
  pair of concurrent requests only sometimes overlaps and a race that is usually caught is not
  a rule;
* a real `.docx`, `.xlsx`, `.csv` and `.pdf` - built byte by byte in `tests/doc_fixtures.py` -
  come out of MinIO as candidates whose text is the document's own, in the order of the paper,
  with the sheet and page it was found on;
* nothing is filed until a person approves it, an incomplete candidate writes no row, and every
  refusal is checked against the tables: a 422 that still created a question looks exactly like
  a clean refusal from the outside;
* an approved row goes through the bank's own services, so the language list, the duplicate
  word rule and the question engine refuse an import in the same sentence they use for a row
  typed by hand - and the provenance columns say which sentence of which paper it came from.

The queue is stubbed by an autouse fixture, because these tests are about what the application
writes; one test un-stubs it far enough to prove a queue refusal leaves the document behind.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import uuid

import pytest
from doc_fixtures import docx_bytes, docx_table, paragraph, pdf_pages, simple_pdf, xlsx_bytes
from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core import enums, tasks
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.storage import get_storage
from app.models.content import (
    ImportItem,
    ImportJob,
    Question,
    Reading,
    SourceFile,
    VocabularyEntry,
)
from app.models.identity import AdminUser
from app.models.ops import AuditLog
from app.services import import_service as isv
from app.workers import jobs

IMPORTS = "/api/v1/imports"
QUESTIONS = "/api/v1/questions"
VOCABULARY = "/api/v1/vocabulary"

#: A passage long enough for the importer to call it a text (`_MIN_PASSAGE_CHARACTERS`).
PASSAGE_A = (
    "Every Saturday the family walks down to the market by the river, where the fishermen "
    "spread their nets on the wet stones and the gulls argue over the smallest fish."
)
PASSAGE_B = (
    "My grandmother buys two kilos of tomatoes and a bag of green peppers, and she argues "
    "with the seller about the price of the cucumbers for almost half an hour."
)


# --------------------------------------------------------------------------- #
# The papers, built as files rather than as dictionaries
# --------------------------------------------------------------------------- #


def csv_bytes(rows: list[list[str]]) -> bytes:
    """A real delimited file: `csv.writer`'s quoting and line endings, not hand-joined."""
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode("utf-8")


#: A Word table of the shape a vocabulary list reaches a .docx in.
WORDS_CSV = csv_bytes(
    [
        ["word", "meaning", "example"],
        ["apple", "bir meyve", "I eat an apple every morning."],
        ["run", "hizli hareket etmek", "She runs to the bus stop."],
    ]
)

#: One keyed question, one that lists options and no answer, and one word - three rows whose
#: header names every column, which is how a teacher's own spreadsheet arrives.
MIXED_CSV = csv_bytes(
    [
        ["question", "a", "b", "c", "answer", "word", "meaning", "example"],
        ["Which word means cat?", "canis", "felis", "murena", "b", "", "", ""],
        ["Which one is a bird?", "canis", "felis", "av", "", "", "", ""],
        ["", "", "", "", "", "eagle", "bir yirtici kus", "The eagle flies high."],
    ]
)

#: A level the bank's own schema cannot hold, written in the document's own column.
LONG_LEVEL = "Upper-Intermediate to Advanced, according to the school board"

#: Two sheets, each with its own header: a workbook that holds both kinds of material.
WORKBOOK = xlsx_bytes(
    {
        "Words": [["word", "meaning"], ["cat", "kedi"], ["dog", "kopek"]],
        "Questions": [
            ["question", "a", "b", "c", "answer"],
            ["Which is a bird?", "canis", "felis", "av", "c"],
            ["Which means apple?", "armut", "elma", "uzum", "b"],
        ],
    },
    [
        "word",
        "meaning",
        "cat",
        "kedi",
        "dog",
        "kopek",
        "question",
        "a",
        "b",
        "c",
        "answer",
        "Which is a bird?",
        "canis",
        "felis",
        "av",
        "Which means apple?",
        "armut",
        "elma",
        "uzum",
    ],
)

DOCX_PAPER = docx_bytes(
    paragraph("Vocabulary", style="Title")
    + docx_table([["apple", "bir meyve"], ["run", "hizli hareket etmek"]])
    + paragraph("1. Which word means cat?")
    + paragraph("A) canis")
    + paragraph("B) felis")
    + paragraph("C) murena")
    + paragraph("Reading", style="Heading1")
    + paragraph(PASSAGE_A)
    + paragraph(PASSAGE_B)
)


async def _upload(client, data: bytes, *, name="paper.csv", mime="text/csv", **form):
    return await client.post(IMPORTS, files={"file": (name, data, mime)}, data=form)


async def _paper(client, data: bytes, *, name="paper.csv", mime="text/csv", **form) -> dict:
    """A new document, accepted, with its queue open."""
    resp = await _upload(client, data, name=name, mime=mime, **form)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _read(job_id: str) -> str:
    """The worker's own entrypoint, on its own session, for one job."""
    return await jobs.process_import_job({}, job_id)


async def _items(client, job_id: str, **params) -> list[dict]:
    resp = await client.get(f"{IMPORTS}/{job_id}/items", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def _job(client, job_id: str) -> dict:
    resp = await client.get(f"{IMPORTS}/{job_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _job_row(job_id: str) -> ImportJob:
    async with SessionLocal() as db:
        row = await db.get(ImportJob, uuid.UUID(job_id))
    assert row is not None
    return row


async def _count(model) -> int:
    async with SessionLocal() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _live_sources() -> int:
    async with SessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(SourceFile).where(SourceFile.deleted_at.is_(None))
                )
            ).scalar_one()
        )


async def _item_rows(job_id: str) -> list[ImportItem]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ImportItem).where(ImportItem.job_id == uuid.UUID(job_id)).order_by(ImportItem.position)
            )
        ).scalars().all()
    return list(rows)


async def _audits(prefix: str) -> list[tuple[str, str]]:
    """`(action, actor_id)` for one module's audit rows, in the order they were written."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog.action, AuditLog.actor_id)
                .where(AuditLog.action.like(f"{prefix}%"))
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ).all()
    return [(action, str(actor) if actor else "") for action, actor in rows]


class _Queue:
    """The worker's door, recorded instead of answered.

    Every enqueue is asserted on a second connection: the job row has to be visible to a
    session that did not write it, which is the whole reason `dispatch` commits first. A
    test that only looked at the recorded ids would pass with an enqueue that raced ahead of
    its own transaction and left the worker reading a job that did not exist yet.
    """

    def __init__(self, monkeypatch) -> None:
        self.calls: list[str] = []
        self.refuses: Exception | None = None
        monkeypatch.setattr(isv.tasks, "enqueue", self._enqueue)

    def allow(self) -> None:
        self.refuses = None

    def refuse_with(self, exc: Exception) -> None:
        self.refuses = exc

    async def _enqueue(self, task: str, *args: object) -> None:
        assert task == tasks.IMPORT_JOB_TASK, f"the import service asked for {task!r}"
        if self.refuses is not None:
            raise self.refuses
        job_id = uuid.UUID(str(args[0]))
        async with SessionLocal() as other:
            assert await other.get(ImportJob, job_id) is not None, "enqueued before it was committed"
        self.calls.append(str(job_id))


@pytest.fixture(autouse=True)
def queue(monkeypatch):
    return _Queue(monkeypatch)


# --------------------------------------------------------------------------- #
# Stage one: the bytes
# --------------------------------------------------------------------------- #


async def test_an_upload_is_stored_and_its_queue_is_committed_before_the_worker_is_told(
    client, queue
):
    data = WORDS_CSV
    body = await _paper(client, data, title="Unit 3 words")
    assert body["duplicate"] is False
    assert body["status"] == "queued"
    assert body["profile"] == "native:csv"
    assert body["error"] is None
    assert body["counts"]["total"] == 0
    assert body["source"] == {
        "id": body["source"]["id"],
        "title": "Unit 3 words",
        "original_filename": "paper.csv",
        "mime_type": "text/csv",
        "format": "csv",
        "format_label": "Separated values (.csv)",
        "bytes": len(data),
        "page_count": None,
        "language": None,
        "trashed": False,
    }
    assert queue.calls == [body["id"]]

    async with SessionLocal() as db:
        source = await db.get(SourceFile, uuid.UUID(body["source"]["id"]))
        assert source.checksum == hashlib.sha256(data).hexdigest()
        assert source.keep_original is True
        assert source.storage_key.startswith("documents/")
        # The object is really in the bucket, at the size the queue was told about.
        head = get_storage().head(source.storage_key)
    assert head.size == len(data)

    job = await _job_row(body["id"])
    async with SessionLocal() as db:
        admin = (await db.execute(select(AdminUser).limit(1))).scalars().one()
    assert str(admin.id) == job.progress["requested_by"]


async def test_the_format_is_named_from_the_bytes_and_not_from_the_name_the_browser_gave(client):
    """A .docx renamed `.txt` is still a Word document, and is still read as one."""
    body = await _paper(client, DOCX_PAPER, name="paper.txt", mime="text/plain")
    assert body["source"]["format"] == "docx"
    assert body["profile"] == "native:docx"
    assert await _read(body["id"]) == "ok"

    items = await _items(client, body["id"])
    kinds = [item["kind"] for item in items]
    assert "question" in kinds and "vocabulary" in kinds and "reading" in kinds, kinds
    question = next(item for item in items if item["kind"] == "question")
    # Only a Word reader gets this: the options were three separate paragraphs, and the
    # answer key the paper never had is still not invented.
    assert [option["text"] for option in question["extracted"]["options"]] == ["canis", "felis", "murena"]
    assert question["missing"] == ["answer"]


async def test_a_file_that_is_not_a_document_is_refused_and_leaves_no_trace(client, queue):
    legacy = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200
    resp = await _upload(client, legacy, name="old.doc", mime="application/msword")
    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert err["code"] == "document_unreadable"
    assert "old binary Office" in err["message"]

    empty = await _upload(client, b"", name="nothing.csv", mime="text/csv")
    assert empty.status_code == 422, empty.text
    assert error_of(empty)["code"] == "document_empty"

    assert await _count(SourceFile) == 0
    assert await _count(ImportJob) == 0
    assert queue.calls == []


async def test_a_document_above_the_ceiling_is_refused_while_it_is_still_streaming(client, monkeypatch):
    monkeypatch.setattr(isv, "upload_limit_bytes", lambda: 200)
    resp = await _upload(client, WORDS_CSV + b"x" * 4000)
    assert resp.status_code == 422, resp.text
    assert error_of(resp)["code"] == "document_too_large"
    assert await _count(SourceFile) == 0
    assert await _count(ImportJob) == 0


async def test_a_two_line_table_saved_with_a_name_is_still_read_as_a_table(client):
    """The name only breaks a tie between text formats: two lines are too few to prove columns."""
    data = "word\tmeaning\napple\tbir meyve\n".encode("utf-8")
    body = await _paper(client, data, name="selection.tsv", mime="text/tab-separated-values")
    assert body["source"]["format"] == "tsv"
    assert await _read(body["id"]) == "ok"
    items = await _items(client, body["id"])
    assert [item["kind"] for item in items] == ["vocabulary"]
    assert items[0]["extracted"] == {"word": "apple", "definition": "bir meyve"}


async def test_the_same_paper_uploaded_twice_shows_the_queue_that_already_exists(client, queue):
    first = await _paper(client, WORDS_CSV, title="Unit 3 words")
    assert await _read(first["id"]) == "ok"
    decided = await _job(client, first["id"])
    before = queue.calls[:]

    resp = await _upload(client, WORDS_CSV, title="Unit 3 words (again)")
    assert resp.status_code == 200, resp.text
    again = resp.json()
    assert again["duplicate"] is True
    assert again["id"] == first["id"]
    assert again["counts"] == decided["counts"], "the teacher is shown the queue that exists, not a copy"
    assert again["source"]["title"] == "Unit 3 words", "the first label stays: the paper is the same file"
    assert queue.calls == before, "a duplicate is not sent to the worker a second time"
    assert await _live_sources() == 1
    assert await _count(SourceFile) == 1


async def test_the_same_paper_is_accepted_again_once_its_queue_is_gone(client):
    """The unique index is partial on purpose: a trashed source frees its bytes again."""
    first = await _paper(client, WORDS_CSV)
    assert await _read(first["id"]) == "ok"
    deleted = await client.delete(f"{IMPORTS}/{first['id']}")
    assert deleted.status_code == 200, deleted.text

    second = await _paper(client, WORDS_CSV)
    assert second["duplicate"] is False
    assert second["id"] != first["id"]
    assert await _live_sources() == 1
    assert await _count(SourceFile) == 2, "the old row survives as the trash, with its file"


async def test_two_teachers_uploading_the_same_paper_at_the_same_moment_leave_one_source(
    client, session_factory, queue
):
    """The index settles this, not the pre-check: both requests see an empty library."""
    colleague = session_factory()
    await colleague.login_admin()
    before = len(_document_keys())

    first, second = await asyncio.gather(
        _upload(client, WORDS_CSV, title="From Aylin"),
        _upload(colleague, WORDS_CSV, title="From Mehmet"),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 201], f"{first.status_code} {first.text} / {second.status_code} {second.text}"
    winner, loser = (first, second) if first.status_code == 201 else (second, first)
    assert winner.json()["duplicate"] is False
    assert loser.json()["duplicate"] is True
    assert loser.json()["id"] == winner.json()["id"]

    assert queue.calls == [winner.json()["id"]], "one queue, so one message to the worker"
    assert await _live_sources() == 1
    assert await _count(ImportJob) == 1
    # The loser's copy leaves the bucket again: the paper is stored once.
    assert len(_document_keys()) == before + 1


async def test_a_paper_taken_over_by_a_colleague_between_the_lookup_and_the_write_is_a_duplicate(
    client, queue, monkeypatch
):
    """The same race, fixed to the one interleaving that reaches the index.

    Two real requests overlap only sometimes, which is not a rule anyone can rely on. Here
    the lookup is made to answer "the library is empty" - the true answer a moment ago, since
    the colleague's transaction had not committed yet - and by the write it has. That is the
    only order in which the partial unique index, not the pre-check, is the thing that
    decides, and the request that lost has to answer with the queue that won.
    """
    first = await _paper(client, WORDS_CSV, title="Unit 3 words")
    assert await _read(first["id"]) == "ok"
    before = queue.calls[:]
    keys = _document_keys()

    real = isv._live_by_checksum
    looked = {"n": 0}

    async def blind(db, checksum):
        looked["n"] += 1
        if looked["n"] == 1:
            return None
        return await real(db, checksum)

    monkeypatch.setattr(isv, "_live_by_checksum", blind)

    resp = await _upload(client, WORDS_CSV, title="From Mehmet")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is True
    assert body["id"] == first["id"]
    assert body["source"]["title"] == "Unit 3 words", "the first label stays: the paper is the same file"

    assert looked["n"] == 2, "looked, conflicted on the write, and looked again for the row that won"
    assert await _count(SourceFile) == 1, "the loser wrote no source of its own"
    assert await _count(ImportJob) == 1
    assert queue.calls == before, "the orphan is never sent to the worker"
    assert _document_keys() == keys, "the loser's copy of the paper left the bucket again"
    assert [action for action, _ in await _audits("import.")] == [
        "import.uploaded",
        "import.job.reviewed",
    ], "a request that filed nothing audited nothing"


def _document_keys() -> list[str]:
    """Every object on the documents shelf, counted with the SDK the app itself uses."""
    import boto3
    from botocore.config import Config as BotoConfig

    settings = get_settings()
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        region_name=settings.object_storage_region,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        use_ssl=settings.object_storage_secure,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
    )
    s3.head_bucket(Bucket=settings.object_storage_bucket)
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=settings.object_storage_bucket, Prefix="documents/"
    )
    return [row["Key"] for page in pages for row in page.get("Contents", [])]


async def test_a_document_the_worker_cannot_be_told_about_still_waits_in_the_library(
    client, queue
):
    """503 with the document kept: the teacher's file is not thrown away because redis is down."""
    queue.refuse_with(tasks.QueueUnavailable("Connection refused"))
    resp = await _upload(client, WORDS_CSV)
    assert resp.status_code == 503, resp.text
    err = error_of(resp)
    assert err["code"] == "queue_unavailable"
    assert "background worker" in err["message"]

    async with SessionLocal() as db:
        sources = (await db.execute(select(SourceFile))).scalars().all()
        jobs_ = (await db.execute(select(ImportJob))).scalars().all()
    assert len(sources) == 1 and len(jobs_) == 1
    assert jobs_[0].status.value == "queued"

    queue.allow()
    retried = await client.post(f"{IMPORTS}/{jobs_[0].id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert queue.calls == [str(jobs_[0].id)]


# --------------------------------------------------------------------------- #
# Stage two: the worker reads the paper
# --------------------------------------------------------------------------- #


async def test_the_worker_reads_a_csv_of_words_into_a_queue_and_files_nothing_on_its_own(
    client, queue
):
    body = await _paper(client, WORDS_CSV)
    assert await _read(body["id"]) == "ok"

    job = await _job(client, body["id"])
    assert job["status"] == "needs_review"
    assert job["started_at"] and job["finished_at"]
    assert job["progress"]["candidates"] == 2
    assert job["progress"]["by_kind"] == {"vocabulary": 2}
    assert job["progress"]["outcome"] == "extracted"
    assert job["counts"] == {
        "pending": 2,
        "approved": 0,
        "rejected": 0,
        "edited": 0,
        "total": 2,
        "incomplete": 2,
    }

    items = await _items(client, body["id"])
    assert [item["position"] for item in items] == [0, 1]
    assert [item["page"] for item in items] == [2, 3], "the csv row it was read from"
    assert [item["kind"] for item in items] == ["vocabulary", "vocabulary"]
    assert items[0]["extracted"] == {
        "word": "apple",
        "definition": "bir meyve",
        "examples": [{"sentence": "I eat an apple every morning."}],
    }
    # The word and its meaning are the document's. The language it belongs to is not in the
    # file, and nothing here chooses one for it.
    assert items[0]["missing"] == ["language"]
    assert items[0]["approvable"] is False and items[0]["attention"] is True
    assert [item["decision"] for item in items] == ["pending", "pending"]

    assert await _count(VocabularyEntry) == 0, "reading a paper must not create content"
    assert [action for action, _ in await _audits("import.")] == [
        "import.uploaded",
        "import.job.reviewed",
    ]


async def test_a_message_delivered_twice_does_not_read_the_paper_twice(client, queue):
    """Redis may hand the same job to two workers; the queue must still hold one reading."""
    body = await _paper(client, WORDS_CSV)
    assert await _read(body["id"]) == "ok"
    assert await _read(body["id"]) == "not_claimed"

    job = await _job(client, body["id"])
    assert job["status"] == "needs_review"
    assert job["counts"]["total"] == 2, "the second message filed no candidates of its own"
    assert [action for action, _ in await _audits("import.")] == [
        "import.uploaded",
        "import.job.reviewed",
    ], "a message that claimed nothing also wrote nothing"


async def test_a_keyed_question_is_ready_and_an_unkeyed_one_says_what_it_lacks(client):
    body = await _paper(client, MIXED_CSV)
    assert await _read(body["id"]) == "ok"
    items = await _items(client, body["id"])
    keyed, unkeyed, word = items

    assert keyed["kind"] == "question" and keyed["type"] == "multiple_choice"
    assert keyed["missing"] == [] and keyed["approvable"] is True
    assert keyed["confidence"] == 0.9
    assert [option["correct"] for option in keyed["extracted"]["options"]] == [False, True, False]
    assert keyed["extracted"]["answer_text"] == "b", "the paper's own key, kept as the paper wrote it"

    assert unkeyed["missing"] == ["answer"] and unkeyed["approvable"] is False
    assert unkeyed["note"] == "options_without_answer"
    assert not any(option["correct"] for option in unkeyed["extracted"]["options"])

    assert word["kind"] == "vocabulary" and word["missing"] == ["language"]
    assert await _count(Question) == 0 and await _count(VocabularyEntry) == 0


async def test_a_word_document_is_read_as_a_note_a_table_a_question_and_a_passage(client):
    body = await _paper(client, DOCX_PAPER, name="paper.docx", mime=SOURCE_DOCX_MIME)
    assert await _read(body["id"]) == "ok"

    items = await _items(client, body["id"])
    assert [item["kind"] for item in items] == [
        "note",
        "vocabulary",
        "vocabulary",
        "question",
        "reading",
    ], "the order the paper is written in"
    assert [item["page"] for item in items] == [1, 1, 1, 1, 1]

    reading = items[-1]
    assert reading["extracted"]["title"] == "Reading"
    assert reading["extracted"]["body"] == f"{PASSAGE_A}\n\n{PASSAGE_B}"
    assert reading["missing"] == [] and reading["approvable"] is True
    assert items[0]["missing"] == ["kind"], "a note is readable text with no shape yet"
    assert items[1]["extracted"] == {"word": "apple", "definition": "bir meyve"}


async def test_a_workbook_is_read_sheet_by_sheet_and_keeps_the_sheet_with_the_row(client):
    body = await _paper(client, WORKBOOK, name="banks.xlsx", mime=SOURCE_XLSX_MIME)
    assert await _read(body["id"]) == "ok"

    job = await _job(client, body["id"])
    assert job["progress"]["sheets"] == ["Words", "Questions"]
    assert job["progress"]["by_kind"] == {"vocabulary": 2, "question": 2}

    items = await _items(client, body["id"])
    assert [(item["sheet"], item["page"], item["kind"]) for item in items] == [
        ("Words", 2, "vocabulary"),
        ("Words", 3, "vocabulary"),
        ("Questions", 2, "question"),
        ("Questions", 3, "question"),
    ]
    assert items[0]["extracted"]["word"] == "cat"
    assert items[2]["extracted"]["options"][2]["correct"] is True, "the sheet's own key was honoured"


async def test_a_pdf_with_pictures_but_no_text_fails_saying_it_needs_ocr(client, queue):
    scanned = pdf_pages([[]])
    body = await _paper(client, scanned, name="scan.pdf", mime="application/pdf")
    assert await _read(body["id"]) == "failed"

    job = await _job(client, body["id"])
    assert job["status"] == "failed"
    assert "no text to read" in job["error"] and "OCR" in job["error"]
    assert job["progress"]["outcome"] == "document_unreadable"
    assert await _items(client, body["id"]) == []
    assert [action for action, _ in await _audits("import.")] == [
        "import.uploaded",
        "import.job.failed",
    ]

    # A job that read no rows may be put back in front of the worker.
    retry = await client.post(f"{IMPORTS}/{body['id']}/retry")
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "queued"
    assert retry.json()["error"] is None
    assert queue.calls == [body["id"], body["id"]], "the upload queued it, the retry queued it again"


async def test_a_damaged_pdf_and_a_second_reading_of_the_same_paper_are_both_answered(client):
    broken = b"%PDF-1.4\nthis file has no objects in it\n%%EOF\n"
    body = await _paper(client, broken, name="broken.pdf", mime="application/pdf")
    assert await _read(body["id"]) == "failed"
    assert "no pages" in (await _job(client, body["id"]))["error"]

    working = await _paper(client, simple_pdf(["1. Which word means cat?", "A) canis", "B) felis"]))
    assert await _read(working["id"]) == "ok"
    items = await _items(client, working["id"])
    assert [item["kind"] for item in items] == ["question"]
    assert items[0]["extracted"]["options"] == [
        {"text": "canis", "correct": False},
        {"text": "felis", "correct": False},
    ]
    assert items[0]["missing"] == ["answer"]

    # A paper that already produced candidates is never read a second time over the top of them.
    retry = await client.post(f"{IMPORTS}/{working['id']}/retry")
    assert retry.status_code == 422, retry.text
    err = error_of(retry)
    assert err["code"] == "job_has_candidates"
    assert err["params"] == {"candidates": 1}


async def test_a_row_the_importer_cannot_classify_is_kept_as_text_rather_than_dropped(client):
    data = csv_bytes([["col1", "col2"], ["soru", "cevap"], ["8", "4"]])
    body = await _paper(client, data)
    assert await _read(body["id"]) == "ok"
    items = await _items(client, body["id"])
    assert [item["kind"] for item in items] == ["note", "note", "note"]
    assert items[0]["extracted"] == {"text": "col1 | col2"}
    assert [item["missing"] for item in items] == [["kind"]] * 3

    refused = await client.post(f"{IMPORTS}/items/{items[0]['id']}/decision", json={"approve": True})
    assert refused.status_code == 422, refused.text
    assert error_of(refused)["code"] == "nothing_to_file"

    # The teacher decides its shape; the document's own words stay where they are.
    refiled = await client.patch(
        f"{IMPORTS}/items/{items[1]['id']}",
        json={"kind": "vocabulary", "extracted": {"word": "soru", "definition": "question"}},
    )
    assert refiled.status_code == 200, refiled.text
    row = refiled.json()
    assert row["extracted"] == {"text": "soru | cevap"}, "the paper's line is untouched"
    assert row["corrected"] == {"word": "soru", "definition": "question"}
    assert row["has_correction"] is True
    assert row["missing"] == ["language"]

    filed = await client.post(
        f"{IMPORTS}/items/{items[1]['id']}/decision",
        json={"approve": True, "filing": {"language": "en"}},
    )
    assert filed.status_code == 200, filed.text
    assert filed.json()["decision"] == "edited"
    assert await _count(VocabularyEntry) == 1


# --------------------------------------------------------------------------- #
# Stage three: a person decides what becomes content
# --------------------------------------------------------------------------- #


async def test_a_candidate_that_is_still_incomplete_cannot_be_filed_and_writes_no_row(client):
    words = await _paper(client, WORDS_CSV)
    assert await _read(words["id"]) == "ok"
    item = (await _items(client, words["id"]))[0]

    refused = await client.post(f"{IMPORTS}/items/{item['id']}/decision", json={"approve": True})
    assert refused.status_code == 422, refused.text
    err = error_of(refused)
    assert err["code"] == "candidate_incomplete"
    assert err["params"] == {"fields": ["language"]}
    assert "the language the word belongs to" in err["message"]
    assert await _count(VocabularyEntry) == 0

    still = await _items(client, words["id"])
    assert still[0]["decision"] == "pending"
    assert still[0]["result"] is None

    paper = await _paper(client, MIXED_CSV)
    assert await _read(paper["id"]) == "ok"
    unkeyed = (await _items(client, paper["id"]))[1]
    second = await client.post(f"{IMPORTS}/items/{unkeyed['id']}/decision", json={"approve": True})
    assert second.status_code == 422, second.text
    assert error_of(second)["params"] == {"fields": ["answer"]}
    assert await _count(Question) == 0


async def test_filling_the_gap_in_the_review_screen_is_what_lets_the_row_be_filed(client):
    body = await _paper(client, DOCX_PAPER, name="paper.docx", mime=SOURCE_DOCX_MIME)
    assert await _read(body["id"]) == "ok"
    question = next(item for item in await _items(client, body["id"]) if item["kind"] == "question")

    edited = await client.patch(
        f"{IMPORTS}/items/{question['id']}",
        json={
            "extracted": {
                "prompt": "1. Which word means cat?",
                "options": [
                    {"text": "canis", "correct": False},
                    {"text": "felis", "correct": True},
                    {"text": "murena", "correct": False},
                ],
            }
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["missing"] == []
    assert edited.json()["approvable"] is True

    rows = await _item_rows(body["id"])
    stored = next(row for row in rows if row.id == uuid.UUID(question["id"]))
    assert stored.corrected["options"][1]["correct"] is True
    assert all(option["correct"] is False for option in stored.extracted["options"]), (
        "the paper's own words are never edited: the key is the teacher's, stored beside them"
    )

    filed = await client.post(
        f"{IMPORTS}/items/{question['id']}/decision",
        json={"approve": True, "filing": {"status": "ready", "level": "A2", "language": "en"}},
    )
    assert filed.status_code == 200, filed.text
    outcome = filed.json()
    assert outcome["decision"] == "edited", "it was corrected, so the bank records that"
    assert outcome["result"] == {"kind": "question", "id": outcome["result"]["id"]}
    assert outcome["missing"] == [] and outcome["editable"] is False

    async with SessionLocal() as db:
        created = await db.get(Question, uuid.UUID(outcome["result"]["id"]))
        assert created.prompt == "1. Which word means cat?"
        assert created.type == "multiple_choice"
        assert created.status.value == "ready"
        assert created.level == "A2"
        assert created.current_version == 1
        assert [option["text"] for option in created.config["options"]] == ["canis", "felis", "murena"]
        assert [option["correct"] for option in created.config["options"]] == [False, True, False]
        # Provenance: which sentence of which paper, read by which parser, filed by which queue.
        assert created.source_file_id == stored.job_id or created.source_file_id is not None
        assert created.source_page == 1
        assert created.source_sheet is None
        assert created.extraction_method == "native:docx"
        assert created.import_job_id == uuid.UUID(body["id"])
        assert created.import_job_id == stored.job_id

    assert await _count(Question) == 1
    actions = [action for action, _ in await _audits("")]
    assert "question.created" in actions and "import.item.approved" in actions


async def test_an_imported_word_is_held_to_the_bank_s_own_rules(client):
    typed = await client.post(
        VOCABULARY, json={"word": "apple", "learning_language": "en", "definition": "a fruit"}
    )
    assert typed.status_code == 201, typed.text

    body = await _paper(client, WORDS_CSV)
    assert await _read(body["id"]) == "ok"
    item = (await _items(client, body["id"]))[0]

    unknown_language = await client.post(
        f"{IMPORTS}/items/{item['id']}/decision",
        json={"approve": True, "filing": {"language": "zz"}},
    )
    assert unknown_language.status_code == 422, unknown_language.text
    err = error_of(unknown_language)
    assert err["code"] == "bank_refused"
    assert "is not an enabled learning language" in err["message"]
    assert await _count(VocabularyEntry) == 1, "a refusal writes no word"

    taken = await client.post(
        f"{IMPORTS}/items/{item['id']}/decision",
        json={"approve": True, "filing": {"language": "en"}},
    )
    assert taken.status_code == 409, taken.text
    assert error_of(taken)["code"] == "content_exists"
    assert await _count(VocabularyEntry) == 1
    assert (await _items(client, body["id"]))[0]["decision"] == "pending"

    # The same word in another language would be another entry, but this request names the
    # language that is already taken, so it is refused - and a refusal keeps nothing at all,
    # not even the filing the refused request carried.
    before = (await _item_rows(body["id"]))[0].filing
    other = await client.post(
        f"{IMPORTS}/items/{item['id']}/decision",
        json={"approve": True, "filing": {"language": "en", "level": "A1"}},
    )
    assert other.status_code == 409, other.text
    assert error_of(other)["code"] == "content_exists"
    row = (await _item_rows(body["id"]))[0]
    assert row.decision == "pending"
    assert row.filing == before, "the request that was refused changed no part of the row"
    assert row.filing["level"] != "A1"


async def test_a_candidate_the_bank_cannot_hold_is_refused_with_a_sentence_not_a_crash(client):
    """A document may write a level longer than the schema's column; the bank says so as usual."""
    data = csv_bytes(
        [
            ["question", "a", "b", "answer", "level"],
            ["Which word means cat?", "canis", "felis", "b", LONG_LEVEL],
            ["Which means dog?", "canis", "kopek", "a", LONG_LEVEL],
            ["Which means bird?", "canis", "kus", "b", LONG_LEVEL],
        ]
    )
    body = await _paper(client, data)
    assert await _read(body["id"]) == "ok"
    item = (await _items(client, body["id"]))[0]
    assert item["missing"] == [] and item["approvable"] is True

    refused = await client.post(f"{IMPORTS}/items/{item['id']}/decision", json={"approve": True})
    assert refused.status_code == 422, refused.text
    err = error_of(refused)
    assert err["code"] == "bank_refused"
    assert "level" in err["message"]
    assert await _count(Question) == 0
    assert (await _items(client, body["id"]))[0]["decision"] == "pending"


async def test_two_requests_approving_one_candidate_at_the_same_moment_file_one_question(
    client, session_factory
):
    """The database settles it, and the loser files nothing - not even a question of its own."""
    body = await _paper(client, MIXED_CSV)
    assert await _read(body["id"]) == "ok"
    keyed = (await _items(client, body["id"]))[0]

    colleague = session_factory()
    await colleague.login_admin()
    first, second = await asyncio.gather(
        client.post(f"{IMPORTS}/items/{keyed['id']}/decision", json={"approve": True}),
        colleague.post(f"{IMPORTS}/items/{keyed['id']}/decision", json={"approve": True}),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], f"{first.status_code} {first.text} / {second.status_code} {second.text}"
    assert await _count(Question) == 1, "one candidate, one piece of content"

    rows = await _item_rows(body["id"])
    assert rows[0].result_ref_id is not None
    stored = await client.get(f"{QUESTIONS}/{rows[0].result_ref_id}")
    assert stored.status_code == 200, stored.text


async def test_a_candidate_whose_filing_happened_after_the_request_read_it_is_refused(client):
    """The same rule as the race above, at the moment the race cannot be pinned to.

    A locking read that blocks and then hands back the instance the session was already
    holding tells the decision nothing new: the attributes on that object are the ones read
    before the colleague's transaction committed, and `pending` is what gets acted on. The
    gather test above can only catch that when the two requests truly overlap; this one
    makes the overlap the setup, so the rule is proven rather than usually observed.
    """
    body = await _paper(client, MIXED_CSV)
    assert await _read(body["id"]) == "ok"
    keyed = (await _items(client, body["id"]))[0]

    async with SessionLocal() as stale:
        async with SessionLocal() as finder:
            admin_id = (await finder.execute(select(AdminUser.id).limit(1))).scalar_one()
        item = await stale.get(ImportItem, uuid.UUID(keyed["id"]))
        assert item.decision == enums.ImportItemDecision.PENDING

        filed = await client.post(f"{IMPORTS}/items/{keyed['id']}/decision", json={"approve": True})
        assert filed.status_code == 200, filed.text
        assert (
            await stale.get(ImportItem, uuid.UUID(keyed["id"]))
        ) is item, "the session still holds the row it read, and nothing has refreshed it"
        assert item.decision == enums.ImportItemDecision.PENDING

        with pytest.raises(isv.AlreadyFiled) as caught:
            await isv.approve_item(stale, item, admin_id=admin_id)
        assert caught.value.code == "candidate_filed"
        await stale.rollback()

    assert await _count(Question) == 1, "one candidate, one piece of content"
    rows = await _item_rows(body["id"])
    assert str(rows[0].result_ref_id) == filed.json()["result"]["id"], "the colleague's question stands"


async def test_a_filed_candidate_is_closed_and_only_shows_in_the_full_view(client):
    body = await _paper(client, MIXED_CSV)
    assert await _read(body["id"]) == "ok"
    keyed = (await _items(client, body["id"]))[0]
    filed = await client.post(f"{IMPORTS}/items/{keyed['id']}/decision", json={"approve": True})
    assert filed.status_code == 200, filed.text

    again = await client.post(f"{IMPORTS}/items/{keyed['id']}/decision", json={"approve": True})
    assert again.status_code == 409, again.text
    assert error_of(again)["code"] == "candidate_filed"

    edited = await client.patch(
        f"{IMPORTS}/items/{keyed['id']}", json={"extracted": {"prompt": "Rewritten?"}}
    )
    assert edited.status_code == 409, edited.text
    assert error_of(edited)["code"] == "candidate_filed"

    refused = await client.post(f"{IMPORTS}/items/{keyed['id']}/decision", json={"approve": False})
    assert refused.status_code == 409, refused.text

    queue_view = await _items(client, body["id"])
    assert [item["id"] for item in queue_view] != [keyed["id"]]
    assert keyed["id"] not in [item["id"] for item in queue_view], "the work left is the pending rows"
    everything = await _items(client, body["id"], view="all")
    assert len(everything) == 3
    assert [item["decision"] for item in everything if item["id"] == keyed["id"]][0] == "approved"
    assert [item["editable"] for item in everything if item["id"] == keyed["id"]][0] is False


async def test_refusing_a_candidate_creates_nothing_and_a_changed_mind_reopens_it(client):
    body = await _paper(client, MIXED_CSV)
    assert await _read(body["id"]) == "ok"
    unkeyed = (await _items(client, body["id"]))[1]

    refused = await client.post(f"{IMPORTS}/items/{unkeyed['id']}/decision", json={"approve": False})
    assert refused.status_code == 200, refused.text
    assert refused.json()["decision"] == "rejected"
    assert await _count(Question) == 0

    # REJECTED is not final: the row was never content, so a teacher may still change it.
    reopened = await client.patch(
        f"{IMPORTS}/items/{unkeyed['id']}",
        json={
            "extracted": {
                "prompt": unkeyed["extracted"]["prompt"],
                "options": [
                    {"text": "canis", "correct": False},
                    {"text": "felis", "correct": True},
                    {"text": "av", "correct": False},
                ],
            }
        },
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["decision"] == "pending"
    assert reopened.json()["missing"] == []

    filed = await client.post(f"{IMPORTS}/items/{unkeyed['id']}/decision", json={"approve": True})
    assert filed.status_code == 200, filed.text
    assert filed.json()["decision"] == "edited"
    assert await _count(Question) == 1


async def test_a_passage_comes_out_of_the_paper_as_a_text_the_bank_can_read(client):
    body = await _paper(client, DOCX_PAPER, name="paper.docx", mime=SOURCE_DOCX_MIME)
    assert await _read(body["id"]) == "ok"
    reading = (await _items(client, body["id"]))[-1]

    # A reading needs no language to be filed, so this is the shortest honest approval.
    filed = await client.post(
        f"{IMPORTS}/items/{reading['id']}/decision",
        json={"approve": True, "filing": {"status": "ready", "level": "B1"}},
    )
    assert filed.status_code == 200, filed.text
    async with SessionLocal() as db:
        row = await db.get(Reading, uuid.UUID(filed.json()["result"]["id"]))
        assert row.title == "Reading"
        assert row.body == f"{PASSAGE_A}\n\n{PASSAGE_B}"
        assert row.status.value == "ready"
        assert row.level == "B1"
        assert row.language is None
        assert row.word_count == len(row.body.split())
        assert row.source_file_id is not None
    assert await _count(Reading) == 1


async def test_the_import_queue_orders_the_paper_and_the_filters_agree_with_it(client):
    body = await _paper(client, WORKBOOK, name="banks.xlsx", mime=SOURCE_XLSX_MIME)
    assert await _read(body["id"]) == "ok"

    ordered = await _items(client, body["id"], order="asc")
    assert [item["position"] for item in ordered] == [0, 1, 2, 3]
    by_kind = await _items(client, body["id"], kind="question")
    assert [item["position"] for item in by_kind] == [2, 3]
    incomplete = await _items(client, body["id"], only_incomplete=True)
    assert [item["position"] for item in incomplete] == [0, 1], "the words still need a language"

    page = await client.get(f"{IMPORTS}/{body['id']}/items", params={"page": 2, "page_size": 2})
    assert page.status_code == 200, page.text
    listing = page.json()
    assert listing["total"] == 4 and listing["pages"] == 2
    assert [item["position"] for item in listing["items"]] == [2, 3]

    bad = await client.get(f"{IMPORTS}/{body['id']}/items", params={"kind": "listening"})
    assert bad.status_code == 422, bad.text
    assert "kind must be one of" in error_of(bad)["message"]
    bad_page = await client.get(f"{IMPORTS}/{body['id']}/items", params={"page": 0})
    assert bad_page.status_code == 422, bad_page.text
    assert error_of(bad_page)["code"] == "page_invalid"


async def test_approving_a_page_files_what_is_ready_and_answers_row_by_row(client):
    body = await _paper(client, MIXED_CSV)
    assert await _read(body["id"]) == "ok"
    items = await _items(client, body["id"])
    stranger = str(uuid.uuid4())

    refused = await client.post(
        IMPORTS + "/bulk", json={"item_ids": [items[0]["id"]], "action": "file"}
    )
    assert refused.status_code == 422, refused.text
    assert error_of(refused)["code"] == "filing_missing"

    resp = await client.post(
        IMPORTS + "/bulk",
        json={"item_ids": [item["id"] for item in items] + [stranger], "action": "approve"},
    )
    assert resp.status_code == 200, resp.text
    answer = resp.json()
    assert answer["action"] == "approve"
    assert answer["done"] == [items[0]["id"]], "the keyed question was ready"
    assert answer["not_found"] == [stranger]
    assert {"done", "refused", "not_found"} <= set(answer), "every id asked about is answered"
    answered = set(answer["done"]) | {row["id"] for row in answer["refused"]} | set(answer["not_found"])
    assert answered == {item["id"] for item in items} | {stranger}, "no row is left unexplained"
    codes = {row["id"]: row["code"] for row in answer["refused"]}
    assert codes == {items[1]["id"]: "candidate_incomplete", items[2]["id"]: "candidate_incomplete"}
    fields = {row["id"]: row["params"]["fields"] for row in answer["refused"]}
    assert fields == {items[1]["id"]: ["answer"], items[2]["id"]: ["language"]}

    assert await _count(Question) == 1 and await _count(VocabularyEntry) == 0

    # The same action with a filing reaches every row it touches: the word was missing only a
    # language, so this batch files it, while the unkeyed question stays refused for the gap
    # no filing can supply.
    filled = await client.post(
        IMPORTS + "/bulk",
        json={
            "item_ids": [items[2]["id"], items[1]["id"]],
            "action": "approve",
            "filing": {"language": "en"},
        },
    )
    assert filled.status_code == 200, filled.text
    second = filled.json()
    assert second["done"] == [items[2]["id"]]
    assert {row["id"]: row["params"]["fields"] for row in second["refused"]} == {
        items[1]["id"]: ["answer"]
    }
    assert await _count(VocabularyEntry) == 1

    # The row that was refused twice is exactly where it was, and a person can still decide it.
    still = next(item for item in await _items(client, body["id"]) if item["id"] == items[1]["id"])
    assert still["decision"] == "pending" and still["missing"] == ["answer"]

    # Setting a filing changes the row, not the bank: an unkeyed question is still unkeyed.
    filed = await client.post(
        IMPORTS + "/bulk",
        json={"item_ids": [items[1]["id"]], "action": "file", "filing": {"level": "A1"}},
    )
    assert filed.json()["done"] == [items[1]["id"]], filed.text
    asked_again = await client.post(
        IMPORTS + "/bulk", json={"item_ids": [items[1]["id"]], "action": "approve"}
    )
    assert asked_again.json()["done"] == [] and asked_again.json()["refused"], (
        "a row that gained a level still has no answer key"
    )

    rejected = await client.post(IMPORTS + "/bulk", json={"item_ids": [items[1]["id"]], "action": "reject"})
    assert rejected.json()["done"] == [items[1]["id"]], rejected.text
    assert (await _job(client, body["id"]))["status"] == "completed", "nothing left to decide"


async def test_auto_mode_files_the_rows_that_need_no_decision_and_leaves_the_rest(client):
    body = await _paper(client, MIXED_CSV, auto_mode="true")
    assert body["auto_mode"] is True
    assert await _read(body["id"]) == "ok"

    job = await _job(client, body["id"])
    assert job["status"] == "needs_review"
    assert job["counts"]["approved"] == 1 and job["counts"]["pending"] == 2
    assert job["progress"]["outcome"] == "extracted", (
        "the reading of the paper is still there after auto mode's own commits"
    )

    items = await _items(client, body["id"], view="all")
    keyed, unkeyed, word = items
    assert keyed["decision"] == "approved" and keyed["result"]["kind"] == "question"
    assert unkeyed["decision"] == "pending", "a question with no key never files itself"
    assert word["decision"] == "pending", "a word with no language is not ready either"
    assert unkeyed["missing"] == ["answer"] and word["missing"] == ["language"], (
        "auto mode leaves a row it passed over still saying what it waits for"
    )
    assert await _count(Question) == 1 and await _count(VocabularyEntry) == 0

    async with SessionLocal() as db:
        admin = (await db.execute(select(AdminUser).limit(1))).scalars().one()
    # The worker has no session, so an automatic approval acts for the teacher who asked.
    assert set(actor for action, actor in await _audits("import.") if action == "import.item.approved") == {
        str(admin.id)
    }
    assert await _audits("vocabulary.") == [], "auto mode invented no word"


async def test_auto_mode_carries_on_after_a_row_the_bank_will_not_hold(client):
    """A passage the schema cannot take is a finding about the paper, not a failed job.

    This row is complete and read with confidence, so auto mode does attempt it - and the
    bank answers with a refusal. The row keeps that refusal's code and stays pending, so the
    teacher reads why in the language the screen is set to rather than in the bank's English; the
    job still finishes as a queue for a person, and nothing half-written is left in the bank.
    """
    long_title = "Reading comprehension: " + "a heading longer than the bank can hold " * 10
    assert len(long_title) > 400
    paper = docx_bytes(
        paragraph(long_title, style="Heading1") + paragraph(PASSAGE_A) + paragraph(PASSAGE_B)
    )
    body = await _paper(client, paper, name="paper.docx", mime=SOURCE_DOCX_MIME, auto_mode="true")
    assert await _read(body["id"]) == "ok"

    job = await _job(client, body["id"])
    assert job["status"] == "needs_review", "the refusal did not end the reading"
    passage = next(item for item in await _items(client, body["id"], view="all") if item["kind"] == "reading")
    assert passage["decision"] == "pending"
    assert passage["missing"] == [], "the row was complete: the bank, not the teacher, said no"
    assert passage["note"] == "bank_refused", "the card carries the refusal's code, not the bank's English"
    meta = (await client.get(f"{IMPORTS}/meta")).json()
    assert passage["note"] in meta["note_codes"], "a remark on a card must be one the screen has words for"
    assert await _count(Reading) == 0


async def test_deleting_a_queue_is_refused_while_the_content_it_filed_is_still_live(client):
    body = await _paper(client, MIXED_CSV)
    assert await _read(body["id"]) == "ok"
    keyed = (await _items(client, body["id"]))[0]
    filed = await client.post(f"{IMPORTS}/items/{keyed['id']}/decision", json={"approve": True})
    question_id = filed.json()["result"]["id"]

    refused = await client.delete(f"{IMPORTS}/{body['id']}")
    assert refused.status_code == 409, refused.text
    err = error_of(refused)
    assert err["code"] == "job_has_content"
    assert err["params"] == {"filed": 1}
    assert await _count(ImportItem) == 3, "the record of where it came from stays"

    trashed = await client.delete(f"{QUESTIONS}/{question_id}")
    assert trashed.status_code == 200, trashed.text
    removed = await client.delete(f"{IMPORTS}/{body['id']}")
    assert removed.status_code == 200, removed.text
    assert await _count(ImportItem) == 0 and await _count(ImportJob) == 0
    assert await _live_sources() == 0, "no queue cites the paper any more, so it is trashed"


async def test_the_original_document_comes_back_from_storage_byte_for_byte(client):
    body = await _paper(client, DOCX_PAPER, name="unit 3 paper.docx", mime=SOURCE_DOCX_MIME)
    resp = await client.get(f"{IMPORTS}/{body['id']}/document")
    assert resp.status_code == 200, resp.text
    assert resp.content == DOCX_PAPER
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml"
    )
    assert 'attachment; filename="unit 3 paper.docx"' in resp.headers["content-disposition"]
    assert "documents/" not in resp.text, "no storage key reaches the browser"

    missing = await client.get(f"{IMPORTS}/{uuid.uuid4()}/document")
    assert missing.status_code == 404, missing.text
    assert error_of(missing)["code"] == "import_not_found"


async def test_the_history_lists_the_queues_and_filters_them_as_the_screen_needs(client):
    words = await _paper(client, WORDS_CSV, title="Unit 3 words")
    mixed = await _paper(client, MIXED_CSV, title="Unit 3 questions")
    assert await _read(words["id"]) == "ok"

    listing = await client.get(IMPORTS)
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert rows["total"] == 2
    assert [row["source"]["title"] for row in rows["items"]] == ["Unit 3 questions", "Unit 3 words"]
    assert rows["items"][0]["status"] == "queued" and rows["items"][1]["status"] == "needs_review"
    assert rows["items"][1]["counts"]["incomplete"] == 2

    by_status = await client.get(IMPORTS, params={"status": "needs_review"})
    assert [row["id"] for row in by_status.json()["items"]] == [words["id"]]
    found = await client.get(IMPORTS, params={"q": "questions"})
    assert [row["id"] for row in found.json()["items"]] == [mixed["id"]]
    by_title = await client.get(IMPORTS, params={"sort": "title", "order": "asc"})
    assert [row["source"]["title"] for row in by_title.json()["items"]] == ["Unit 3 questions", "Unit 3 words"]
    paged = await client.get(IMPORTS, params={"page": 2, "page_size": 1})
    assert paged.json()["pages"] == 2 and len(paged.json()["items"]) == 1

    bad_status = await client.get(IMPORTS, params={"status": "succeeded"})
    assert bad_status.status_code == 422, bad_status.text
    assert "status must be one of" in error_of(bad_status)["message"]

    gone = await client.get(f"{IMPORTS}/{uuid.uuid4()}")
    assert gone.status_code == 404, gone.text
    assert error_of(gone)["code"] == "import_not_found"
    bad_item = await client.patch(f"{IMPORTS}/items/{uuid.uuid4()}", json={"kind": "note"})
    assert bad_item.status_code == 404, bad_item.text


async def test_the_upload_screen_is_built_from_what_the_uploader_actually_accepts(client, monkeypatch):
    monkeypatch.setattr(isv, "upload_limit_bytes", lambda: 7 * 1024 * 1024)
    resp = await client.get(f"{IMPORTS}/meta")
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert [fmt["extension"] for fmt in meta["formats"]] == ["docx", "xlsx", "pdf", "csv", "tsv", "txt", "md"]
    assert meta["max_document_bytes"] == 7 * 1024 * 1024
    assert meta["max_document_mb"] == 7
    assert meta["kinds"] == ["question", "vocabulary", "reading", "note"]
    assert meta["question_types"] == ["multiple_choice", "multi_select", "short_answer"]
    assert set(meta["missing_fields"]) == {"prompt", "options", "accepted", "answer", "type", "word",
                                          "definition", "language", "title", "body", "kind"}

    # A format the screen would have to refuse is not offered in the first place.
    refused = await _upload(client, b"\x1f\x8b\tliteral bytes of a gzip file", name="x.gz", mime="application/gzip")
    assert refused.status_code == 422, refused.text
    assert "gzip" in error_of(refused)["message"]


async def test_a_learner_cannot_reach_the_import_surface_at_all(client, session_factory):
    student = await client.post(
        "/api/v1/students", json={"name": "Learner", "surname": "Paper", "username": "imports-no-access"}
    )
    assert student.status_code == 201, student.text
    learner = session_factory()
    await learner.login_student(student.json()["access_key"])

    body = await _paper(client, WORDS_CSV)
    item = (await _items(client, body["id"])) if await _read(body["id"]) == "ok" else []
    routes = (
        ("get", f"{IMPORTS}/meta", None),
        ("get", IMPORTS, None),
        ("get", f"{IMPORTS}/{body['id']}", None),
        ("get", f"{IMPORTS}/{body['id']}/document", None),
        ("get", f"{IMPORTS}/{body['id']}/items", None),
        ("post", IMPORTS, None),
        ("post", f"{IMPORTS}/bulk", {"item_ids": ["x"], "action": "approve"}),
        ("patch", f"{IMPORTS}/items/{item[0]['id'] if item else uuid.uuid4()}", {"kind": "note"}),
        ("post", f"{IMPORTS}/items/{body['id']}/decision", {"approve": True}),
        ("delete", f"{IMPORTS}/{body['id']}", None),
        ("post", f"{IMPORTS}/{body['id']}/retry", None),
    )
    for method, path, payload in routes:
        if payload is not None:
            resp = await getattr(learner, method)(path, json=payload)
        elif method == "post":
            resp = await getattr(learner, method)(path, files={"file": ("p.csv", WORDS_CSV, "text/csv")})
        else:
            resp = await getattr(learner, method)(path)
        assert resp.status_code == 401, f"a learner reached {method.upper()} {path}"
    assert (await _job(client, body["id"]))["counts"]["approved"] == 0


async def test_an_upload_without_the_csrf_header_is_refused_and_stores_nothing(client):
    resp = await client._c.post(
        IMPORTS, files={"file": ("paper.csv", WORDS_CSV, "text/csv")}
    )
    assert resp.status_code == 403, resp.text
    assert error_of(resp)["code"] == "csrf_failed"
    assert await _count(SourceFile) == 0 and await _count(ImportJob) == 0


async def test_a_write_that_names_an_unknown_field_is_refused_before_it_is_stored(client):
    body = await _paper(client, WORDS_CSV)
    assert await _read(body["id"]) == "ok"
    item = (await _items(client, body["id"]))[0]

    invented = await client.patch(
        f"{IMPORTS}/items/{item['id']}",
        json={"extracted": {"word": "apple", "definition": "bir meyve", "answer": "felis"}},
    )
    assert invented.status_code == 422, invented.text
    err = error_of(invented)
    assert err["code"] == "unknown_field"
    assert err["params"] == {"fields": ["answer"]}

    over_long = await client.post(
        f"{IMPORTS}/items/{item['id']}/decision", json={"approve": True, "language": "en"}
    )
    assert over_long.status_code == 422, over_long.text
    assert await _count(VocabularyEntry) == 0

    rows = await _item_rows(body["id"])
    assert rows[0].corrected is None, "a refused edit stored nothing, not even a partial row"


SOURCE_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SOURCE_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
