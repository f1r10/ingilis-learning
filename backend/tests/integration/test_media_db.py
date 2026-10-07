"""The media library over HTTP, against real Postgres and real MinIO (Phase 5).

What the offline rule tests cannot prove, and this file exists to prove:

* that an upload is identified from its **bytes** on a live request - a browser that
  names a web page `lesson.png` is refused, and the refusal leaves no row behind;
* that the same file sent twice is one asset, so the library's answer to "is anything
  using this?" stays true;
* that every byte read goes back out through this application, with one range honoured
  and an unsatisfiable one refused;
* that a learner cannot open a file by guessing an id - only a file some *ready* piece
  of content points at is theirs;
* that a file a lesson still needs cannot be thrown out, and can be thrown out the
  moment it is not;
* that storage being down is reported as storage being down, rather than as an asset
  that was saved or as a bare 500.

`leave_no_objects_behind` deletes this test's objects after it, so the bucket cannot
accumulate the suite's debris.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest_asyncio
from helpers import error_of
from sqlalchemy import select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core import media_types
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.storage import ObjectMissing, StorageUnavailable
from app.models.content import MediaAsset
from app.models.ops import AuditLog
from app.services import media_service

# A PNG signature followed by distinguishable payload bytes, so a range test can name the
# exact slice it expects back. Identification only ever reads the head.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(50))
MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(40))
#: The same length with different payload: a second, distinct object.
OTHER_MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(10, 60))
SVG_BYTES = b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'><rect/></svg>"
HTML_BYTES = b"<html><body>a page wearing a picture's name</body></html>"
PDF_BYTES = b"%PDF-1.7\n%\xc7\xec\x8f\xa2\ntrailer\n%%EOF\n"

TEACHER_PATH = "/api/v1/media"
STUDENT_PATH = "/api/v1/student/media"


@pytest_asyncio.fixture(autouse=True)
async def leave_no_objects_behind(clean_db, storage_ready):
    """Delete every object this test put in the bucket, before its rows are forgotten.

    The library's rows are truncated by the next test's setup, so the keys have to be
    read while the rows are still here. Deletion is best-effort: a test that never got
    as far as storing an object must not fail on cleanup. `storage_ready` is a
    dependency of the whole file - every upload here is a round trip to the object
    store, so without it a dead MinIO would read as a wall of 500s instead of the
    infrastructure failure it is.
    """
    yield
    async with SessionLocal() as db:
        keys = (
            await db.execute(
                select(MediaAsset.storage_key).where(MediaAsset.storage_key.is_not(None))
            )
        ).scalars().all()
    storage = media_service.get_storage()
    for key in keys:
        with contextlib.suppress(Exception):
            storage.delete(key)


async def _upload(client, *, name: str, content: bytes, label: str | None = None):
    data = {"label": label} if label is not None else None
    return await client.post(
        TEACHER_PATH, files={"file": (name, content, "application/octet-stream")}, data=data
    )


async def _stored(client, *, name: str, content: bytes, **kw) -> dict:
    """Upload and insist, so a test reads as a scenario instead of a pile of asserts."""
    resp = await _upload(client, name=name, content=content, **kw)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _asset(client, asset_id: str) -> dict:
    resp = await client.get(f"{TEACHER_PATH}/{asset_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _live_rows() -> list[MediaAsset]:
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(MediaAsset).where(MediaAsset.deleted_at.is_(None)).order_by(MediaAsset.id)
                )
            )
            .scalars()
            .all()
        )


async def _live_ids() -> list[str]:
    return [str(row.id) for row in await _live_rows()]


async def _checksum_of(asset_id: str) -> str:
    async with SessionLocal() as db:
        row = await db.get(MediaAsset, uuid.UUID(asset_id))
    assert row is not None
    return row.checksum


async def _new_listening(client, asset_id: str, *, status: str = "ready", title: str = "Play me") -> dict:
    resp = await client.post(
        "/api/v1/listening",
        json={"title": title, "media_asset_id": asset_id, "status": status, "language": "en"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _new_word(client, asset_id: str, *, word: str) -> dict:
    resp = await client.post(
        "/api/v1/vocabulary",
        json={
            "word": word,
            "learning_language": "en",
            "definition": "a test word",
            "audio_asset_id": asset_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _new_question(client, asset_id: str, *, prompt: str) -> dict:
    resp = await client.post(
        "/api/v1/questions",
        json={
            "type": "multiple_choice",
            "prompt": prompt,
            "config": {"options": [{"text": "one"}, {"text": "two", "correct": True}]},
            "media_asset_id": asset_id,
            "learning_language": "en",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _learner_session(client, session_factory, username: str):
    created = await client.post(
        "/api/v1/students", json={"name": "Play", "surname": "List", "username": username}
    )
    assert created.status_code == 201, created.text
    learner = session_factory()
    await learner.login_student(created.json()["access_key"])
    return learner


async def _audits(action: str) -> list[dict]:
    async with SessionLocal() as db:
        # `created_at` first: an id from gen_random_uuid() is not a sequence, so reading an
        # audit trail in id order is reading it in a random order.
        rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == action)
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ).scalars().all()
    return [{"target_id": str(row.target_id or ""), "before": row.before, "after": row.after} for row in rows]


# --------------------------------------------------------------------------- #
# Upload: the bytes decide
# --------------------------------------------------------------------------- #


async def test_an_upload_is_named_from_its_bytes_not_its_name(client):
    resp = await _upload(client, name="lesson.png", content=PNG_BYTES, label="Board photo")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["kind"], body["mime_type"], body["format_label"]) == (
        "image",
        "image/png",
        "PNG image",
    )
    assert body["original_filename"] == "lesson.png", "the name survives as the teacher's label"
    assert body["label"] == "Board photo"
    assert body["size_bytes"] == len(PNG_BYTES)
    assert body["source_origin"] == "upload"
    assert body["state"] == "available"
    assert body["deleted_at"] is None
    assert body["deduplicated"] is False
    assert body["referenced_by"] == {"words": 0, "listenings": 0, "questions": 0}
    assert body["duration_seconds"] is None, "no codec here: an unmeasured length stays unknown"
    assert body["content_url"].endswith(f"{body['id']}/content")
    # The URL is relative to the API root, and the frontend prepends its own base. That is
    # the whole guarantee: nothing here can be an object-store address, a presigned link,
    # or a URL that authorises itself.
    assert body["content_url"] == f"/media/{body['id']}/content"
    assert "://" not in body["content_url"], "no object-store URL is handed out"

    rows = await _live_rows()
    assert [row.id for row in rows] == [uuid.UUID(body["id"])]
    row = rows[0]
    assert row.checksum and len(row.checksum) == 64
    assert "lesson" not in row.storage_key, "an uploaded name must never steer the key"
    assert not row.storage_key.startswith("/"), "a key is relative to the bucket, not a path"


async def test_the_content_type_the_browser_declared_is_not_consulted(client):
    """`text/plain` on the wire neither demotes a PNG nor is believed about one."""
    resp = await client.post(
        TEACHER_PATH, files={"file": ("declared-plain.png", PNG_BYTES, "text/plain")}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["mime_type"] == "image/png"


async def test_a_page_wearing_a_pictures_name_is_refused(client):
    resp = await _upload(client, name="lesson.png", content=HTML_BYTES)
    assert resp.status_code == 422, resp.text
    assert "text or markup" in error_of(resp)["message"]
    assert await _live_ids() == [], "a refused upload must leave no row"


async def test_an_svg_is_refused_because_it_can_carry_scripts(client):
    resp = await _upload(client, name="diagram.svg", content=SVG_BYTES)
    assert resp.status_code == 422, resp.text
    assert "scripts" in error_of(resp)["message"]


async def test_a_document_is_turned_towards_the_import_screen(client):
    """A PDF is not media: Phase 8 owns documents, and its review step is the honest way in."""
    resp = await _upload(client, name="worksheet.pdf", content=PDF_BYTES)
    assert resp.status_code == 422, resp.text
    assert "import" in error_of(resp)["message"]


async def test_an_empty_upload_is_refused(client):
    resp = await _upload(client, name="empty.png", content=b"")
    assert resp.status_code == 422, resp.text
    assert "empty" in error_of(resp)["message"].lower()


async def test_no_library_row_survives_a_refused_upload(client):
    for name, content in (
        ("page.png", HTML_BYTES),
        ("drawing.svg", SVG_BYTES),
        ("doc.pdf", PDF_BYTES),
        ("nothing.png", b""),
        ("mystery.bin", b"\x00\x01\x02\x03not a media signature"),
    ):
        assert (await _upload(client, name=name, content=content)).status_code == 422, name
    assert await _live_ids() == []


async def test_the_upload_route_wants_a_file_part(client):
    """A request without bytes in it is a 422 about the missing part, not a 500."""
    resp = await client.post(TEACHER_PATH, data={"label": "nothing attached"})
    assert resp.status_code == 422, resp.text
    assert error_of(resp)["code"] == "validation_failed"


# --------------------------------------------------------------------------- #
# One file, one asset
# --------------------------------------------------------------------------- #


async def test_the_same_bytes_twice_are_one_asset(client):
    first = await _stored(client, name="recording.mp3", content=MP3_BYTES)
    second = await _upload(client, name="same-thing.mp3", content=MP3_BYTES)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first["id"]
    assert second.json()["deduplicated"] is True
    assert len(await _live_ids()) == 1, "identical bytes must not store a second row"


async def test_different_bytes_are_a_second_asset(client):
    await _stored(client, name="one.mp3", content=MP3_BYTES)
    second = await _stored(client, name="two.mp3", content=OTHER_MP3_BYTES)
    assert second["deduplicated"] is False
    assert len(await _live_ids()) == 2


async def test_a_second_copy_of_a_trashed_file_is_a_new_asset(client):
    """The unique index covers live rows, so the trash does not block a re-upload."""
    first = await _stored(client, name="recording.mp3", content=MP3_BYTES)
    await client.delete(f"{TEACHER_PATH}/{first['id']}")
    again = await _stored(client, name="recording.mp3", content=MP3_BYTES)
    assert again["deduplicated"] is False
    assert again["id"] != first["id"]
    assert len(await _live_ids()) == 1


# --------------------------------------------------------------------------- #
# Serving bytes
# --------------------------------------------------------------------------- #


async def test_a_teacher_reads_the_whole_file_or_one_slice_of_it(client):
    stored = await _stored(client, name="picture.png", content=PNG_BYTES)
    url = f"{TEACHER_PATH}/{stored['id']}/content"

    whole = await client.get(url)
    assert whole.status_code == 200, whole.text
    assert whole.content == PNG_BYTES, "the bytes returned are the bytes stored"
    assert whole.headers["content-type"] == "image/png"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert whole.headers.get("content-range") is None
    assert whole.headers["content-length"] == str(len(PNG_BYTES))

    piece = await client.get(url, headers={"Range": "bytes=8-17"})
    assert piece.status_code == 206, piece.text
    assert piece.content == PNG_BYTES[8:18]
    assert piece.headers["content-range"] == f"bytes 8-17/{len(PNG_BYTES)}"

    # Players routinely ask past the end; the answer is the part that exists.
    clamped = await client.get(url, headers={"Range": f"bytes=0-{len(PNG_BYTES) + 500}"})
    assert clamped.status_code == 206 and clamped.content == PNG_BYTES

    # A start past the file cannot be honoured by inventing an empty success.
    beyond = await client.get(url, headers={"Range": f"bytes={len(PNG_BYTES) + 10}-"})
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{len(PNG_BYTES)}"

    # A range this module does not implement is answered honestly with the whole file.
    multi = await client.get(url, headers={"Range": "bytes=0-4,10-14"})
    assert multi.status_code == 200 and multi.content == PNG_BYTES


async def test_the_etag_is_the_checksum_of_the_bytes_held(client):
    stored = await _stored(client, name="picture.png", content=PNG_BYTES)
    checksum = await _checksum_of(stored["id"])
    resp = await client.get(f"{TEACHER_PATH}/{stored['id']}/content")
    assert resp.headers["etag"] == f'"{checksum}"'


async def test_a_trashed_file_still_opens_for_the_teacher(client):
    """The trash screen has to show what is about to be lost."""
    stored = await _stored(client, name="gone.mp3", content=MP3_BYTES)
    await client.delete(f"{TEACHER_PATH}/{stored['id']}")

    assert (await _asset(client, stored["id"]))["state"] == "trashed"
    content = await client.get(f"{TEACHER_PATH}/{stored['id']}/content")
    assert content.status_code == 200 and content.content == MP3_BYTES


async def test_a_missing_object_is_reported_as_a_broken_row_not_a_crash(client, monkeypatch):
    """`ObjectMissing` means this library row has no file behind it: say so, in a 404."""
    stored = await _stored(client, name="vanished.png", content=PNG_BYTES)

    class _Gone:
        def head(self, key):
            raise ObjectMissing(f"no object at {key}")

        def open_range(self, key, start, end):
            raise ObjectMissing(f"no object at {key}")

    monkeypatch.setattr(media_service, "get_storage", lambda: _Gone())
    resp = await client.get(f"{TEACHER_PATH}/{stored['id']}/content")
    assert resp.status_code == 404, resp.text
    assert "missing" in error_of(resp)["message"].lower()


async def test_a_storage_outage_is_a_503_and_no_asset_is_claimed(client, monkeypatch):
    """The library never reports a file it could not store.

    A teacher told "uploaded" about a recording that is not in the bucket loses that
    lesson quietly, so an unreachable store has to reach them as an outage.
    """

    class _Down:
        def build_key(self, namespace, name):
            return f"{namespace}/{name}"

        def put_file(self, *args, **kwargs):
            raise StorageUnavailable("minio is not answering")

        def delete(self, key):
            raise StorageUnavailable("minio is not answering")

    monkeypatch.setattr(media_service, "get_storage", lambda: _Down())
    resp = await _upload(client, name="during-outage.png", content=PNG_BYTES)
    assert resp.status_code == 503, resp.text
    assert error_of(resp)["code"] == "storage_unavailable"
    assert await _live_ids() == []


# --------------------------------------------------------------------------- #
# The learner's one route
# --------------------------------------------------------------------------- #


async def test_a_learner_cannot_browse_the_library(client, session_factory):
    learner = await _learner_session(client, session_factory, "learner-browse")
    stored = await _stored(client, name="private.png", content=PNG_BYTES)

    listing = await learner.get(STUDENT_PATH)
    assert listing.status_code == 404, "there is no learner-facing library listing"
    assert error_of(listing)["code"] == "not_found"

    guess = await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")
    assert guess.status_code == 404, "an id alone is not permission"
    assert error_of(guess)["code"] == "not_found"

    for path in (TEACHER_PATH, f"{TEACHER_PATH}/{stored['id']}", f"{TEACHER_PATH}/{stored['id']}/content"):
        refused = await learner.get(path)
        assert refused.status_code == 401, f"a student reached {path}"


async def test_a_learner_opens_the_recording_a_ready_exercise_points_at(client, session_factory):
    stored = await _stored(client, name="dialogue.mp3", content=MP3_BYTES)
    await _new_listening(client, stored["id"], status="ready")
    learner = await _learner_session(client, session_factory, "learner-play")

    whole = await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")
    assert whole.status_code == 200, whole.text
    assert whole.content == MP3_BYTES
    assert whole.headers["content-type"] == "audio/mpeg"

    piece = await learner.get(
        f"{STUDENT_PATH}/{stored['id']}/content", headers={"Range": "bytes=0-9"}
    )
    assert piece.status_code == 206 and piece.content == MP3_BYTES[:10]


async def test_a_draft_exercise_does_not_hand_out_its_file(client, session_factory):
    """READY is the permission; publishing the same item is what opens the file."""
    stored = await _stored(client, name="draft.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="draft", title="Not finished")
    learner = await _learner_session(client, session_factory, "learner-draft")

    refused = await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")
    assert refused.status_code == 404, refused.text

    published = await client.post(
        f"/api/v1/listening/{created['id']}/status", json={"status": "ready"}
    )
    assert published.status_code == 200, published.text
    opened = await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")
    assert opened.status_code == 200, "the file follows its exercise's status, live"


async def test_permission_is_decided_per_request_not_per_asset(client, session_factory):
    """Archiving the exercise closes the file on the next byte request.

    A presigned URL would have kept it readable for the length of the link; this is why
    the application proxies the range instead of handing out a link.
    """
    stored = await _stored(client, name="switch.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="ready", title="Switch")
    learner = await _learner_session(client, session_factory, "learner-switch")
    assert (await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")).status_code == 200

    archived = await client.post(
        f"/api/v1/listening/{created['id']}/status", json={"status": "archived"}
    )
    assert archived.status_code == 200, archived.text
    closed = await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")
    assert closed.status_code == 404


async def test_a_ready_word_or_question_licenses_its_file(client, session_factory):
    """The three tables that can hold a file are the three that can serve it.

    One list (`_REFERENCE_SOURCES`) answers both questions, so a fourth kind of
    reference cannot be counted as blocking a trash and forgotten as permission to read.
    """
    learner = await _learner_session(client, session_factory, "learner-refs")

    picture = await _stored(client, name="prompt.png", content=PNG_BYTES)
    await _new_question(client, picture["id"], prompt="What is in the picture?")
    assert (await learner.get(f"{STUDENT_PATH}/{picture['id']}/content")).status_code == 200

    audio = await _stored(client, name="word.mp3", content=MP3_BYTES)
    await _new_word(client, audio["id"], word="improve-media-test")
    assert (await learner.get(f"{STUDENT_PATH}/{audio['id']}/content")).status_code == 200


async def test_a_trashed_exercise_does_not_keep_its_file_readable(client, session_factory):
    stored = await _stored(client, name="trashed-listening.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="ready", title="To the trash")
    learner = await _learner_session(client, session_factory, "learner-trashed-item")
    assert (await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")).status_code == 200

    assert (await client.delete(f"/api/v1/listening/{created['id']}")).status_code == 200
    closed = await learner.get(f"{STUDENT_PATH}/{stored['id']}/content")
    assert closed.status_code == 404, "content in the trash is not permission"


# --------------------------------------------------------------------------- #
# Trash, references, restore
# --------------------------------------------------------------------------- #


async def test_a_file_a_lesson_still_uses_cannot_be_thrown_out(client):
    stored = await _stored(client, name="needed.mp3", content=MP3_BYTES)
    await _new_listening(client, stored["id"], status="ready", title="Holds on")

    refused = await client.delete(f"{TEACHER_PATH}/{stored['id']}")
    assert refused.status_code == 409, refused.text
    err = error_of(refused)
    assert err["code"] == "asset_in_use"
    assert "1 listening exercise" in err["message"]

    detail = await _asset(client, stored["id"])
    assert detail["state"] == "available" and detail["deleted_at"] is None


async def test_the_refusal_names_every_kind_of_content_holding_on(client):
    """The refusal is a sentence about the teacher's own content, per shelf.

    One audio file held by a word and a recording, one image held by a question: those
    are the combinations the library can actually be handed, because each consumer
    insists on its own kind and a photograph cannot be a word's pronunciation.
    """
    sound = await _stored(client, name="shared.mp3", content=MP3_BYTES)
    await _new_word(client, sound["id"], word="shared-media-test")
    await _new_listening(client, sound["id"], status="ready", title="Shared listening")

    refused = await client.delete(f"{TEACHER_PATH}/{sound['id']}")
    assert refused.status_code == 409
    message = error_of(refused)["message"]
    assert "1 vocabulary word" in message and "1 listening exercise" in message, message
    assert (await _asset(client, sound["id"]))["referenced_by"] == {
        "words": 1,
        "listenings": 1,
        "questions": 0,
    }

    picture = await _stored(client, name="shared.png", content=PNG_BYTES)
    await _new_question(client, picture["id"], prompt="Look at this")

    also_refused = await client.delete(f"{TEACHER_PATH}/{picture['id']}")
    assert also_refused.status_code == 409
    assert "1 question" in error_of(also_refused)["message"]
    assert (await _asset(client, picture["id"]))["referenced_by"] == {
        "words": 0,
        "listenings": 0,
        "questions": 1,
    }


async def test_a_file_is_trashable_once_the_content_lets_go(client):
    stored = await _stored(client, name="detach.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="ready", title="Detaching")
    # The transcript is what keeps this legal: a published recording cannot be edited
    # into silence, and detaching its only audio is exactly that (see
    # `listening_service.refuse_publish`).
    with_transcript = await client.patch(
        f"/api/v1/listening/{created['id']}", json={"transcript": "hello class"}
    )
    assert with_transcript.status_code == 200, with_transcript.text

    detached = await client.patch(f"/api/v1/listening/{created['id']}", json={"media_asset_id": None})
    assert detached.status_code == 200, detached.text
    assert (await _asset(client, stored["id"]))["referenced_by"]["listenings"] == 0

    trashed = await client.delete(f"{TEACHER_PATH}/{stored['id']}")
    assert trashed.status_code == 200, trashed.text
    assert trashed.json()["state"] == "trashed"


async def test_a_published_recording_cannot_be_edited_into_silence(client):
    """The veto is a 422 with a sentence, not a silent write that strands the class."""
    stored = await _stored(client, name="detach.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="ready", title="Only audio")

    refused = await client.patch(f"/api/v1/listening/{created['id']}", json={"media_asset_id": None})
    assert refused.status_code == 422, refused.text
    assert "publish" in error_of(refused)["message"] or "draft" in error_of(refused)["message"]

    still = await client.get(f"/api/v1/listening/{created['id']}")
    assert still.json()["audio"]["id"] == stored["id"], "the refusal changed nothing"


async def test_content_thrown_away_does_not_hold_a_file_forever(client):
    """A trashed listening must not block the trash of its own recording.

    Otherwise the library would fill with files only deleted lessons referenced, and the
    teacher could never clear either of them.
    """
    stored = await _stored(client, name="lesson.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="ready", title="Going together")
    assert (await client.delete(f"/api/v1/listening/{created['id']}")).status_code == 200

    trashed = await client.delete(f"{TEACHER_PATH}/{stored['id']}")
    assert trashed.status_code == 200, trashed.text


async def test_restore_brings_the_file_back_and_says_so_in_the_log(client):
    stored = await _stored(client, name="back.mp3", content=MP3_BYTES)
    await client.delete(f"{TEACHER_PATH}/{stored['id']}")

    restored = await client.post(f"{TEACHER_PATH}/{stored['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["state"] == "available"

    trashed_rows = await _audits("media.trashed")
    restored_rows = await _audits("media.restored")
    assert [row["target_id"] for row in trashed_rows] == [stored["id"]]
    assert [row["target_id"] for row in restored_rows] == [stored["id"]]
    assert restored_rows[0]["before"]["deleted_at"], "a restore records what it undid"


async def test_restoring_collides_with_a_copy_made_in_the_meantime(client):
    """The unique index covers live rows, so the clash must be a sentence, not a 500."""
    first = await _stored(client, name="clash.mp3", content=MP3_BYTES)
    await client.delete(f"{TEACHER_PATH}/{first['id']}")
    await _stored(client, name="clash-again.mp3", content=MP3_BYTES)

    refused = await client.post(f"{TEACHER_PATH}/{first['id']}/restore")
    assert refused.status_code == 409, refused.text
    assert error_of(refused)["code"] == "asset_exists"
    assert (await _asset(client, first["id"]))["state"] == "trashed"


async def test_trashing_an_asset_twice_is_the_second_one_a_no_op(client):
    """The trash is a state, not a counter: a repeat returns the row it describes."""
    stored = await _stored(client, name="twice.mp3", content=MP3_BYTES)
    assert (await client.delete(f"{TEACHER_PATH}/{stored['id']}")).status_code == 200
    again = await client.delete(f"{TEACHER_PATH}/{stored['id']}")
    assert again.status_code == 200, again.text
    assert again.json()["state"] == "trashed"


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


async def test_the_bank_and_the_trash_are_two_different_answers(client):
    keep = await _stored(client, name="keep.png", content=PNG_BYTES)
    drop = await _stored(client, name="drop.mp3", content=MP3_BYTES)
    await client.delete(f"{TEACHER_PATH}/{drop['id']}")

    bank = await client.get(f"{TEACHER_PATH}?view=bank")
    assert bank.status_code == 200
    assert [item["id"] for item in bank.json()["items"]] == [keep["id"]]
    assert bank.json()["total"] == 1

    trash = await client.get(f"{TEACHER_PATH}?view=trash")
    assert [item["id"] for item in trash.json()["items"]] == [drop["id"]]
    assert trash.json()["items"][0]["state"] == "trashed"

    everything = await client.get(f"{TEACHER_PATH}?view=all")
    assert everything.json()["total"] == 2

    images = await client.get(f"{TEACHER_PATH}?kind=image")
    assert [item["id"] for item in images.json()["items"]] == [keep["id"]]
    uploads = await client.get(f"{TEACHER_PATH}?view=all&source_origin=upload")
    assert uploads.json()["total"] == 2, "every file here arrived by uploading"
    imported = await client.get(f"{TEACHER_PATH}?view=all&source_origin=imported")
    assert imported.json()["total"] == 0, "the import screen (Phase 8) owns that origin"


async def test_a_teacher_searches_the_library_by_its_own_words(client):
    await _stored(client, name="class-photo.png", content=PNG_BYTES, label="Grade 5 whiteboard")
    await _stored(client, name="other.mp3", content=MP3_BYTES, label="Listening one")

    for query, expected in (
        ("whiteboard", 1),  # the teacher's caption
        ("class-photo", 1),  # the file they uploaded
        ("audio/mpeg", 1),  # what the bytes turned out to be
        ("zulu-nothing-here", 0),
    ):
        resp = await client.get(f"{TEACHER_PATH}?q={query}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == expected, query


async def test_the_library_refuses_an_unknown_view_kind_sort_or_order(client):
    for query, word in (
        ("?view=everything", "view"),
        ("?kind=spreadsheet", "kind"),
        ("?sort=password_hash", "sort"),
        ("?order=sideways", "order"),
    ):
        resp = await client.get(TEACHER_PATH + query)
        assert resp.status_code == 422, f"{query} -> {resp.status_code}"
        assert word in error_of(resp)["message"]


async def test_a_list_page_never_repeats_or_skips_a_row(client):
    ids = []
    for index in range(4):
        made = await _stored(client, name=f"one-{index}.png", content=PNG_BYTES[: 20 + index])
        ids.append(made["id"])

    first = await client.get(f"{TEACHER_PATH}?sort=size_bytes&order=asc&page=1&page_size=2")
    second = await client.get(f"{TEACHER_PATH}?sort=size_bytes&order=asc&page=2&page_size=2")
    assert first.json()["total"] == 4
    seen = [item["id"] for item in first.json()["items"]] + [
        item["id"] for item in second.json()["items"]
    ]
    assert sorted(seen) == sorted(ids), "paging must cover every row exactly once"
    sizes = [item["size_bytes"] for item in first.json()["items"]]
    assert sizes == sorted(sizes)


async def test_a_list_row_carries_the_reference_count_that_blocks_its_trash(client):
    stored = await _stored(client, name="used.mp3", content=MP3_BYTES)
    await _new_listening(client, stored["id"], status="ready", title="Counts")

    items = (await client.get(f"{TEACHER_PATH}?view=bank")).json()["items"]
    row = next(item for item in items if item["id"] == stored["id"])
    assert row["reference_count"] == 1


# --------------------------------------------------------------------------- #
# Metadata a player measured
# --------------------------------------------------------------------------- #


async def test_a_caption_and_a_measurement_are_written_by_one_patch(client):
    stored = await _stored(client, name="clip.mp3", content=MP3_BYTES)
    assert (await _asset(client, stored["id"]))["duration_seconds"] is None

    patched = await client.patch(
        f"{TEACHER_PATH}/{stored['id']}",
        json={"label": "Track 1", "duration_seconds": 41.5, "width": 640, "height": 480},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["label"], body["duration_seconds"]) == ("Track 1", 41.5)
    assert (body["width"], body["height"]) == (640, 480)
    assert body["mime_type"] == "audio/mpeg"

    rows = await _audits("media.updated")
    assert len(rows) == 1 and rows[0]["target_id"] == stored["id"]
    assert rows[0]["after"]["duration_seconds"] == 41.5
    assert rows[0]["before"]["duration_seconds"] is None


async def test_the_measurement_reaches_the_listening_that_uses_the_file(client):
    """One asset, one number: the duration is not copied onto the exercise."""
    stored = await _stored(client, name="clip.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="ready", title="Timed")
    await client.patch(f"{TEACHER_PATH}/{stored['id']}", json={"duration_seconds": 12.25})

    detail = await client.get(f"/api/v1/listening/{created['id']}")
    assert detail.json()["audio"]["duration_seconds"] == 12.25


async def test_a_browser_cannot_retype_what_a_file_is(client):
    stored = await _stored(client, name="clip.mp3", content=MP3_BYTES)

    claimed = await client.patch(f"{TEACHER_PATH}/{stored['id']}", json={"mime_type": "image/png"})
    assert claimed.status_code == 422, claimed.text
    assert (await _asset(client, stored["id"]))["mime_type"] == "audio/mpeg"

    empty = await client.patch(f"{TEACHER_PATH}/{stored['id']}", json={})
    assert empty.status_code == 422
    assert "nothing to change" in error_of(empty)["message"]

    blank = await client.patch(f"{TEACHER_PATH}/{stored['id']}", json={"label": "   "})
    assert blank.status_code == 422

    huge = await client.patch(
        f"{TEACHER_PATH}/{stored['id']}", json={"duration_seconds": 90000}
    )
    assert huge.status_code == 422, "a length past a day is a mistake, not a lesson"


async def test_a_trashed_asset_is_edited_from_the_trash_not_behind_it(client):
    stored = await _stored(client, name="clip.mp3", content=MP3_BYTES)
    await client.delete(f"{TEACHER_PATH}/{stored['id']}")

    refused = await client.patch(f"{TEACHER_PATH}/{stored['id']}", json={"label": "Renamed"})
    assert refused.status_code == 422, refused.text
    assert "restore" in error_of(refused)["message"]
    assert (await _asset(client, stored["id"]))["label"] is None


async def test_an_unknown_asset_id_is_a_404_on_every_route(client, session_factory):
    missing = str(uuid.uuid4())
    for path in (
        f"{TEACHER_PATH}/{missing}",
        f"{TEACHER_PATH}/{missing}/content",
    ):
        resp = await client.get(path)
        assert resp.status_code == 404, path
        assert error_of(resp)["code"] == "not_found"
    restore = await client.post(f"{TEACHER_PATH}/{missing}/restore")
    assert restore.status_code == 404
    patch = await client.patch(f"{TEACHER_PATH}/{missing}", json={"label": "ghost"})
    assert patch.status_code == 404

    # A learner asks the student route with a learner session: an admin there is a
    # permission question (401/403), not a "does this file exist" one, and this test is
    # about the 404 the library gives for an id nobody has.
    learner = await _learner_session(client, session_factory, "learner-ghost")
    as_student = await learner.get(f"{STUDENT_PATH}/{missing}/content")
    assert as_student.status_code == 404, as_student.text
    assert error_of(as_student)["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Bulk
# --------------------------------------------------------------------------- #


async def test_bulk_answers_for_every_asset_it_was_handed(client):
    loose = await _stored(client, name="loose.png", content=PNG_BYTES)
    held = await _stored(client, name="held.mp3", content=MP3_BYTES)
    await _new_listening(client, held["id"], status="ready", title="Held")
    missing = str(uuid.uuid4())

    resp = await client.post(
        f"{TEACHER_PATH}/bulk",
        json={"asset_ids": [loose["id"], held["id"], missing], "action": "trash"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "trash"
    assert body["updated"] == [loose["id"]]
    assert [item["id"] for item in body["refused"]] == [held["id"]]
    assert "1 listening exercise" in body["refused"][0]["reason"]
    assert body["not_found"] == [missing]
    assert (await _asset(client, loose["id"]))["state"] == "trashed"
    assert (await _asset(client, held["id"]))["state"] == "available"


async def test_bulk_reports_a_second_trash_as_a_refusal_not_a_success(client):
    stored = await _stored(client, name="twice.png", content=PNG_BYTES)
    await client.delete(f"{TEACHER_PATH}/{stored['id']}")

    again = await client.post(
        f"{TEACHER_PATH}/bulk", json={"asset_ids": [stored["id"]], "action": "trash"}
    )
    assert again.json()["updated"] == []
    assert again.json()["refused"] == [{"id": stored["id"], "reason": "already in the trash"}]

    back = await client.post(
        f"{TEACHER_PATH}/bulk", json={"asset_ids": [stored["id"]], "action": "restore"}
    )
    assert back.json()["updated"] == [stored["id"]]

    already_live = await client.post(
        f"{TEACHER_PATH}/bulk", json={"asset_ids": [stored["id"]], "action": "restore"}
    )
    assert already_live.json()["refused"] == [{"id": stored["id"], "reason": "not in the trash"}]


async def test_the_bulk_route_only_offers_the_two_actions_the_library_has(client):
    stored = await _stored(client, name="once.png", content=PNG_BYTES)
    resp = await client.post(
        f"{TEACHER_PATH}/bulk", json={"asset_ids": [stored["id"]], "action": "delete_forever"}
    )
    assert resp.status_code == 422, resp.text
    detail = error_of(resp)
    assert detail["code"] == "validation_failed"
    assert "trash" in detail["message"] + str(detail.get("fields"))

    empty = await client.post(f"{TEACHER_PATH}/bulk", json={"asset_ids": [], "action": "trash"})
    assert empty.status_code == 422, "a bulk action needs at least one id"


# --------------------------------------------------------------------------- #
# Meta and the surface itself
# --------------------------------------------------------------------------- #


async def test_the_upload_screen_gets_its_rules_from_the_code_that_enforces_them(client):
    resp = await client.get(f"{TEACHER_PATH}/meta")
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    settings = get_settings()
    assert {item["mime_type"] for item in meta["formats"]} == {
        fmt.mime for fmt in media_types.accepted_formats()
    }
    assert meta["kinds"] == list(media_service.KINDS)
    assert meta["max_upload_mb"] == {
        "image": settings.max_image_upload_mb,
        "audio": settings.max_audio_upload_mb,
        "video": settings.max_video_upload_mb,
    }
    assert meta["max_upload_bytes"]["audio"] == settings.max_audio_upload_mb * 1024 * 1024
    assert meta["views"] == list(media_service.VIEWS)
    assert meta["states"] == [media_service.STATE_AVAILABLE, media_service.STATE_TRASHED]
    assert set(meta["sortable"]) == set(media_service.SORTABLE)


async def test_the_media_surface_is_admin_only_and_csrf_guarded(client, session_factory):
    anonymous = session_factory()._c
    assert (await anonymous.get(TEACHER_PATH)).status_code == 401
    assert error_of(await anonymous.get(f"{TEACHER_PATH}/meta"))["code"] == "unauthorized"

    raw = client._c
    without_csrf = await raw.post(TEACHER_PATH, files={"file": ("csrf.png", PNG_BYTES, "image/png")})
    assert without_csrf.status_code == 403, without_csrf.text
    assert error_of(without_csrf)["code"] == "csrf_failed"
    assert await _live_ids() == [], "a refused upload must not reach storage"


async def test_audit_rows_describe_the_file_and_never_its_bytes(client):
    stored = await _stored(client, name="audit.mp3", content=MP3_BYTES)
    await client.patch(f"{TEACHER_PATH}/{stored['id']}", json={"label": "Named"})
    await client.delete(f"{TEACHER_PATH}/{stored['id']}")

    uploads = await _audits("media.uploaded")
    assert len(uploads) == 1
    after = uploads[0]["after"]
    assert after["kind"] == "audio" and after["mime_type"] == "audio/mpeg"
    assert after["size_bytes"] == len(MP3_BYTES)
    assert "checksum" not in after and "storage_key" not in after
    assert not any(MP3_BYTES[8:16] in str(row).encode() for row in uploads), "no file content in the log"

    assert (await _audits("media.trashed"))[0]["after"]["filename"] == "audit.mp3"


async def test_trashing_a_listening_does_not_trash_the_file_it_used(client):
    """The exercise and the recording are two rows with two trash cans.

    Otherwise deleting one lesson would quietly remove a recording its siblings share.
    """
    stored = await _stored(client, name="shared.mp3", content=MP3_BYTES)
    first = await _new_listening(client, stored["id"], status="ready", title="Lesson one")
    await _new_listening(client, stored["id"], status="ready", title="Lesson two")

    assert (await client.delete(f"/api/v1/listening/{first['id']}")).status_code == 200
    detail = await _asset(client, stored["id"])
    assert detail["state"] == "available"
    assert detail["referenced_by"]["listenings"] == 1, "the trashed lesson stopped counting"


async def test_the_library_still_holds_the_file_after_every_listening_is_gone(client):
    """Soft delete only removes a file from the library, never from storage.

    The Phase 11 purge is the one that decides an object may stop existing, and it has
    to find these bytes still here to do it.
    """
    stored = await _stored(client, name="orphan.mp3", content=MP3_BYTES)
    created = await _new_listening(client, stored["id"], status="ready", title="Only one")
    await client.delete(f"/api/v1/listening/{created['id']}")

    key = (await _live_rows())[0].storage_key
    assert media_service.get_storage().exists(key) is True
