"""Listening items over HTTP, against real Postgres and real MinIO (Phase 5).

`tests/test_passage_rules.py` settles the listening arithmetic offline: which provenance
words this endpoint may write, what a cue list needs to survive, that an empty recording
cannot be published, how the audit row reshapes a transcript into a length. What only a
live database and a live object store can answer is the behaviour of the edges:

* the recording is the library's row and this one only names it - the payload carries one
  server-written path, never a storage key, and the same file can be the recording of two
  lessons;
* `transcript_source` is written from what the request actually contained, and a client
  that claims its transcript was imported is refused rather than believed;
* the playback rules reach the learner exactly as the teacher set them, and the words stay
  out of the payload until `show_transcript` says otherwise - in the teacher's preview too;
* the publish veto is asked over HTTP as well: on its own, row by row inside a bulk
  action, and for the state no click can produce (a lesson still naming a file the library
  has thrown away), which is why the guard exists at all;
* a block's seconds are a question boundary that travels to the player, so a block about
  seconds 40 to 70 is answered against those seconds and not the whole file.

Questions come in through `/api/v1/questions` and files through `/api/v1/media`, so every
assertion here is about what a browser can actually do to a real row.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
import pytest_asyncio
from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core import enums, security
from app.core.database import SessionLocal
from app.models.content import Listening, MediaAsset, Question, QuestionVersion
from app.models.ops import AuditLog
from app.schemas.passage import MAX_BODY_CHARACTERS, MAX_REPLAY_LIMIT
from app.services import listening_service, media_service, passage_service

#: A string that only ever belongs in a teacher-only place: the explanation handed out
#: after grading, the private note, a block's authoring config. It shows up in a learner
#: payload only if a projection is wrong. It is deliberately NOT an option's text - a
#: learner has to read every option to answer, so hiding one would hide nothing.
KEY = "ZULU-LISTENING-KEY"

PASSAGE_PATH = "/api/v1/listening"
STUDENT_PATH = "/api/v1/student/listening"
MEDIA_PATH = "/api/v1/media"
READING_PATH = "/api/v1/reading"

TRANSCRIPT = "Good morning. Today we talk about the train station at night."
# 13 and 18 characters: the two lengths the audit row is expected to report in place of
# the words themselves.
SHORT_WORDS = "one two three"
LONG_WORDS = "one two three four"

#: Long enough to be a real recording's head, distinct enough that a range test could name
#: its bytes. The sniffer only ever reads the first kilobytes.
MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(40))
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(50))


def choice_body(prompt: str = "What did the speaker say about the station?", **over) -> dict:
    body = {
        "type": "multiple_choice",
        "prompt": prompt,
        "config": {
            "options": [
                {"text": "it closed", "correct": True},
                {"text": "it stayed open"},
                {"text": "it moved"},
            ]
        },
        "explanation": f"{KEY}: the speaker said the station shut, so the first one is right.",
        "teacher_notes": f"{KEY} again, in the notes nobody but the teacher reads.",
        "score": 2,
        "level": "A2",
        "learning_language": "en",
    }
    body.update(over)
    return body


# --------------------------------------------------------------------------- #
# Fixtures and short names for the scenarios
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture(autouse=True)
async def leave_no_objects_behind(clean_db, storage_ready):
    """Delete every object this test put in the bucket, before its rows are forgotten.

    The rows are truncated by the next test's setup, so the keys have to be read while
    they are still here. `storage_ready` makes a dead MinIO an infrastructure failure
    rather than a wall of 500s: every recording here is a round trip to the object store.
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


async def _stored(client, *, name: str = "recording.mp3", content: bytes = MP3_BYTES) -> dict:
    resp = await client.post(MEDIA_PATH, files={"file": (name, content, "application/octet-stream")})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _create(client, *, title: str = "The night train", asset_id: str | None = None,
                  transcript: str | None = TRANSCRIPT, cues: list | None = None, **over) -> dict:
    body = {
        "title": title,
        "media_asset_id": asset_id,
        "language": "en",
        "transcript": transcript,
        "transcript_timestamps": cues if cues is not None else [],
    }
    body.update(over)
    resp = await client.post(PASSAGE_PATH, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _detail(client, listening_id: str) -> dict:
    resp = await client.get(f"{PASSAGE_PATH}/{listening_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows(*, view: str = "bank") -> list[Listening]:
    async with SessionLocal() as db:
        stmt = select(Listening)
        if view == "bank":
            stmt = stmt.where(Listening.deleted_at.is_(None))
        elif view == "trash":
            stmt = stmt.where(Listening.deleted_at.is_not(None))
        return list((await db.execute(stmt.order_by(Listening.created_at))).scalars().all())


async def _library_row(asset_id: str) -> MediaAsset:
    async with SessionLocal() as db:
        row = await db.get(MediaAsset, uuid.UUID(asset_id))
    assert row is not None
    return row


async def _throw_out_the_file(client, asset_id: str) -> None:
    """Put the library's row in the state no click can reach.

    A live lesson holds its file, so `/media` refuses to trash it while a listening names
    it - which is exactly why `refuse_publish`'s second branch and the learner's
    `has_audio: false` look like dead code. They are not: the Phase 11 purge and a
    trash-then-attach race both produce the pair, and the guards have to be tested against
    it. This is the only honest way to get there from an HTTP suite.
    """
    async with SessionLocal() as db:
        asset = await db.get(MediaAsset, uuid.UUID(asset_id))
        assert asset is not None
        asset.deleted_at = security.utcnow()
        await db.commit()


async def _question(client, listening_id: str, *, prompt: str, **over) -> dict:
    body = choice_body(prompt, context_kind="listening_bound", listening_id=listening_id)
    body.update(over)
    resp = await client.post("/api/v1/questions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _set(client, listening_id: str, title: str, **over) -> dict:
    resp = await client.post(f"{PASSAGE_PATH}/{listening_id}/sets", json={"title": title, **over})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _file(client, set_id: str, *question_ids: str) -> dict:
    resp = await client.post(
        f"{PASSAGE_PATH}/sets/{set_id}/questions", json={"question_ids": list(question_ids)}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _versions(question_id: str) -> tuple[int, int]:
    """(version rows for this question, the row's own `current_version`)."""
    async with SessionLocal() as db:
        count = (
            await db.execute(
                select(func.count()).where(QuestionVersion.question_id == uuid.UUID(question_id))
            )
        ).scalar_one()
        row = await db.get(Question, uuid.UUID(question_id))
    assert row is not None
    return int(count), row.current_version


async def _audits(action: str) -> list[AuditLog]:
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.action == action)
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )


async def _learner(client, session_factory, username: str):
    created = await client.post(
        "/api/v1/students", json={"name": "Listen", "surname": "Away", "username": username}
    )
    assert created.status_code == 201, created.text
    learner = session_factory()
    await learner.login_student(created.json()["access_key"])
    return learner


async def _ready_with_block(client, *, title: str = "Ready lesson", drafts: int = 0):
    """One published recording, two answerable questions in one block, `drafts` loose drafts."""
    stored = await _stored(client, name=f"{title}.mp3")
    created = await _create(client, title=title, asset_id=stored["id"])
    answered = [
        await _question(client, created["id"], prompt=f"Q {index}") for index in range(2)
    ]
    for index in range(drafts):
        await _question(client, created["id"], prompt=f"Draft {index}", status="draft")
    set_row = await _set(client, created["id"], "First block", start_seconds=0, end_seconds=30)
    await _file(client, set_row["id"], *[question["id"] for question in answered])
    return created, answered, set_row


# --------------------------------------------------------------------------- #
# A recording is a reference, not a copy
# --------------------------------------------------------------------------- #


async def test_a_lesson_names_a_file_it_does_not_own(client):
    """The payload hands out one server-written path, and nothing about the bucket."""
    stored = await _stored(client, name="night.mp3")
    created = await _create(client, asset_id=stored["id"])

    audio = created["audio"]
    assert audio["id"] == stored["id"]
    assert audio["kind"] == "audio" and audio["mime_type"] == "audio/mpeg"
    assert audio["state"] == "available"
    assert audio["content_url"] == f"/media/{stored['id']}/content"
    assert "://" not in audio["content_url"], "no object-store address is handed to a browser"
    assert set(audio) == {
        "id",
        "kind",
        "mime_type",
        "label",
        "duration_seconds",
        "state",
        "content_url",
    }, audio
    for word in ("storage_key", "checksum", "bucket"):
        assert word not in created["audio"] and word not in str(created)
    assert created["source_file_id"] is None, "only the Phase 8 importer writes provenance"


async def test_the_same_recording_can_serve_two_lessons(client):
    """One file, two exercises: the library still answers "is anything using this?"."""
    stored = await _stored(client, name="shared.mp3")
    first = await _create(client, title="Lesson one", asset_id=stored["id"])
    second = await _create(client, title="Lesson two", asset_id=stored["id"])
    assert first["audio"]["id"] == second["audio"]["id"]

    detail = await client.get(f"{MEDIA_PATH}/{stored['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["referenced_by"]["listenings"] == 2

    refused = await client.delete(f"{MEDIA_PATH}/{stored['id']}")
    assert refused.status_code == 409, refused.text
    assert error_of(refused)["code"] == "asset_in_use"
    assert "2 listening exercises" in error_of(refused)["message"]

    assert (await client.delete(f"{PASSAGE_PATH}/{first['id']}")).status_code == 200
    after = await client.get(f"{MEDIA_PATH}/{stored['id']}")
    assert after.json()["referenced_by"]["listenings"] == 1, "a trashed lesson stops counting"


async def test_what_the_player_measured_reaches_the_lesson_row(client):
    """The backend has no codec, so the duration the browser reports is the one shown.

    It travels to three places from one write: the editor's audio block, the teacher's row,
    and the learner's player - and it is the asset's number rather than a field a teacher
    could type onto the listening and get wrong.
    """
    stored = await _stored(client, name="timed.mp3")
    created = await _create(client, asset_id=stored["id"])
    assert created["audio"]["duration_seconds"] is None

    measured = await client.patch(
        f"{MEDIA_PATH}/{stored['id']}", json={"duration_seconds": 42.5, "label": "Unit 4 recording"}
    )
    assert measured.status_code == 200, measured.text

    detail = await _detail(client, created["id"])
    assert detail["audio"]["duration_seconds"] == 42.5
    assert detail["audio"]["label"] == "Unit 4 recording"

    row = (await client.get(f"{PASSAGE_PATH}?q={created['title']}")).json()["items"][0]
    assert (row["has_audio"], row["audio_state"], row["duration_seconds"]) == (
        True,
        "available",
        42.5,
    )


async def test_a_listening_cannot_attach_a_picture(client):
    """The wrong shelf is refused in the words a teacher acts on, before any row is written."""
    stored = await _stored(client, name="photo.png", content=PNG_BYTES)
    refused = await client.post(
        PASSAGE_PATH, json={"title": "Wrong shelf", "media_asset_id": stored["id"]}
    )
    assert refused.status_code == 422, refused.text
    message = error_of(refused)["message"]
    assert "a listening item uses an audio file" in message, message
    assert "this one is image" in message, message
    assert await _rows() == []


async def test_a_file_that_is_not_in_the_library_is_not_a_recording(client):
    ghost = str(uuid.uuid4())
    refused = await client.post(
        PASSAGE_PATH, json={"title": "No such file", "media_asset_id": ghost}
    )
    assert refused.status_code == 422, refused.text
    assert "no file with that id is in the library" in error_of(refused)["message"]
    assert await _rows() == []


async def test_a_file_in_the_trash_cannot_be_chosen_for_a_new_lesson(client):
    """The foreign key would have accepted it, and a class would have found out later."""
    stored = await _stored(client, name="gone.mp3")
    assert (await client.delete(f"{MEDIA_PATH}/{stored['id']}")).status_code == 200

    refused = await client.post(
        PASSAGE_PATH, json={"title": "Unplayable", "media_asset_id": stored["id"]}
    )
    assert refused.status_code == 422, refused.text
    assert "that file is in the trash" in error_of(refused)["message"]
    assert await _rows() == []


async def test_a_lesson_can_be_the_words_alone(client):
    """A transcript is enough to publish: a dictation exercise has no file at all."""
    created = await _create(client, title="Dictation", transcript="Write what you hear.")

    assert created["audio"] is None
    assert created["transcript"] == "Write what you hear."

    row = (await client.get(f"{PASSAGE_PATH}?q=Dictation")).json()["items"][0]
    assert (row["has_audio"], row["audio_state"], row["duration_seconds"]) == (False, None, None)
    assert row["has_transcript"] is True


# --------------------------------------------------------------------------- #
# The transcript and where it came from
# --------------------------------------------------------------------------- #


async def test_the_provenance_says_what_this_endpoint_was_actually_sent(client):
    # The lesson keeps a file so that emptying its words is a provenance question rather
    # than a publishability one: a recording with something to hear may lose its text.
    hearing = (await _stored(client, name="provenance.mp3"))["id"]
    typed = await _create(client, title="Typed", transcript="Words from the teacher.", asset_id=hearing)
    silent = await _create(client, title="Silent", transcript=None, asset_id=hearing)
    assert typed["transcript_source"] == "manual"
    assert silent["transcript_source"] == "absent"

    cleared = await client.patch(f"{PASSAGE_PATH}/{typed['id']}", json={"transcript": "   "})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["transcript"] is None
    assert cleared.json()["transcript_source"] == "absent"

    rewritten = await client.patch(
        f"{PASSAGE_PATH}/{typed['id']}", json={"transcript": "Words again."}
    )
    assert rewritten.json()["transcript_source"] == "manual"


async def test_a_client_cannot_claim_a_transcript_was_imported_or_automatic(client):
    """Only the module that produced the text may say it produced it.

    A browser allowed to send `imported` would leave a transcript with no honest
    provenance, and the Phase 8 review screen would be auditing a claim.
    """
    for claim in ("imported", "auto", "manual", "absent"):
        refused = await client.post(
            PASSAGE_PATH,
            json={"title": "Claimed", "transcript": "Words.", "transcript_source": claim},
        )
        assert refused.status_code == 422, f"{claim} -> {refused.status_code}"
    assert await _rows() == [], "a refused create must not reach the table"

    made = await _create(client)
    refused = await client.patch(
        f"{PASSAGE_PATH}/{made['id']}", json={"transcript_source": "auto"}
    )
    assert refused.status_code == 422, refused.text


async def test_a_cue_list_is_stored_exactly_as_it_arrives(client):
    """Phase 8 and Phase 12 write this shape too, so nothing here re-serialises it."""
    cues = [
        {"start": 0, "end": 2.5, "text": "Good morning."},
        {"start": 2.5, "text": "Today we talk", "speaker": "narrator"},
    ]
    created = await _create(client, transcript=TRANSCRIPT, cues=cues)
    assert created["transcript_timestamps"] == cues

    detail = await _detail(client, created["id"])
    assert detail["transcript_timestamps"] == cues


async def test_clearing_the_words_clears_the_lines_that_highlighted_them(client):
    """Timestamps describe text that has to be there to be highlighted.

    The lesson is given a file first: a ready recording may lose its transcript, while one
    with neither words nor audio would be refused for a different reason entirely.
    """
    cues = [{"start": 0, "text": "Good morning."}]
    created = await _create(
        client,
        transcript=TRANSCRIPT,
        cues=cues,
        asset_id=(await _stored(client, name="cues.mp3"))["id"],
    )

    cleared = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"transcript": None})
    assert cleared.json()["transcript"] is None
    assert cleared.json()["transcript_timestamps"] == [], "no cues without words"

    # The words come back; the cues were the teacher's to re-type, not to resurrect.
    rewritten = await client.patch(
        f"{PASSAGE_PATH}/{created['id']}", json={"transcript": TRANSCRIPT}
    )
    assert rewritten.json()["transcript"] == TRANSCRIPT
    assert rewritten.json()["transcript_timestamps"] == []


async def test_an_edit_that_did_not_touch_the_words_keeps_their_lines(client):
    cues = [{"start": 12, "text": "the train station"}]
    created = await _create(client, transcript=TRANSCRIPT, cues=cues)

    renamed = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"title": "Renamed"})
    assert renamed.json()["transcript_timestamps"] == cues


@pytest.mark.parametrize(
    "cues,phrase",
    [
        (["a bare line of text"], "each transcript line must be an object"),
        ([{"start": "0:12", "text": "not seconds"}], "must be a number of seconds"),
        ([{"start": -1, "text": "before the file began"}], "cannot be negative"),
    ],
)
async def test_a_cue_list_is_refused_for_its_nonsense(client, cues, phrase):
    """The shape is not policed, but time has to be time."""
    refused = await client.post(
        PASSAGE_PATH, json={"title": "Bad cues", "transcript": TRANSCRIPT, "transcript_timestamps": cues}
    )
    assert refused.status_code == 422, refused.text
    assert phrase in error_of(refused)["message"]
    assert await _rows() == []

    made = await _create(client)
    on_patch = await client.patch(
        f"{PASSAGE_PATH}/{made['id']}", json={"transcript_timestamps": cues}
    )
    assert on_patch.status_code == 422, on_patch.text
    assert phrase in error_of(on_patch)["message"], "the patch route maps the same sentence"


async def test_the_transcript_ceiling_is_the_number_the_editor_is_told(client):
    """The cap is inclusive: the longest allowed transcript is saved, and one more is not."""
    at_the_cap = "word " * (MAX_BODY_CHARACTERS // 5)
    assert len(at_the_cap) == MAX_BODY_CHARACTERS
    made = await _create(client, title="At the cap", transcript=at_the_cap)
    assert made["transcript"] == at_the_cap.strip()

    refused = await client.post(
        PASSAGE_PATH, json={"title": "Too long", "transcript": at_the_cap + "d"}
    )
    assert refused.status_code == 422, refused.text
    assert len(await _rows()) == 1, "the refused create wrote no second row"


# --------------------------------------------------------------------------- #
# The playback rules
# --------------------------------------------------------------------------- #


async def test_the_player_is_given_the_rules_the_teacher_set(client, session_factory):
    """These are the conditions of the exercise, not a player's preferences."""
    stored = await _stored(client, name="rules.mp3")
    created = await _create(
        client,
        asset_id=stored["id"],
        replay_limit=2,
        allow_pause=False,
        allow_seek=False,
        show_transcript=True,
    )
    assert (created["replay_limit"], created["allow_pause"], created["allow_seek"]) == (2, False, False)

    learner = await _learner(client, session_factory, "learner-rules")
    served = (await learner.get(f"{STUDENT_PATH}/{created['id']}")).json()
    assert served["replay_limit"] == 2
    assert served["allow_pause"] is False and served["allow_seek"] is False
    assert served["show_transcript"] is True

    plain = await _create(client, title="Plain", asset_id=stored["id"])
    assert (plain["replay_limit"], plain["allow_pause"], plain["allow_seek"]) == (None, True, True)
    assert plain["show_transcript"] is False


async def test_the_replay_ceiling_is_the_number_the_meta_endpoint_reports(client):
    stored = await _stored(client, name="replay.mp3")
    ceiling = await _create(client, title="Ceiling", asset_id=stored["id"], replay_limit=MAX_REPLAY_LIMIT)
    assert ceiling["replay_limit"] == MAX_REPLAY_LIMIT

    for limit in (MAX_REPLAY_LIMIT + 1, -1):
        refused = await client.post(
            PASSAGE_PATH, json={"title": "Out of range", "replay_limit": limit}
        )
        assert refused.status_code == 422, f"{limit} -> {refused.status_code}"

    made = await _create(client, title="Loose", asset_id=stored["id"])
    raised = await client.patch(f"{PASSAGE_PATH}/{made['id']}", json={"replay_limit": MAX_REPLAY_LIMIT + 1})
    assert raised.status_code == 422, raised.text


async def test_the_status_word_is_not_a_patch_field_and_never_trash(client):
    created = await _create(client)
    for body in ({"status": "draft"}, {"status": "ready"}, {"status": "trash"}):
        refused = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json=body)
        assert refused.status_code == 422, f"{body} -> {refused.status_code}"

    empty = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={})
    assert empty.status_code == 422, empty.text
    assert "nothing to change" in error_of(empty)["message"]

    for word, phrase in (("trash", "not a status"), ("published", "status must be one of")):
        refused = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": word})
        assert refused.status_code == 422, word
        assert phrase in error_of(refused)["message"]


async def test_a_blank_title_is_refused_and_a_padded_one_is_saved_trimmed(client):
    created = await _create(client)
    padded = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"title": "  Padded title  "})
    assert padded.status_code == 200, padded.text
    assert padded.json()["title"] == "Padded title"

    blank = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"title": "   "})
    assert blank.status_code == 422, blank.text
    assert "a recording needs a title" in error_of(blank)["message"]


# --------------------------------------------------------------------------- #
# Nothing that cannot be heard is published
# --------------------------------------------------------------------------- #


async def test_a_recording_with_nothing_to_hear_cannot_be_published(client):
    empty = await _create(client, title="Empty", transcript=None, status="draft")

    refused = await client.post(f"{PASSAGE_PATH}/{empty['id']}/status", json={"status": "ready"})
    assert refused.status_code == 422, refused.text
    message = error_of(refused)["message"]
    assert "needs audio or a transcript before it can be published" in message
    assert "leave it as a draft" in message
    assert (await _detail(client, empty["id"]))["status"] == "draft"

    # a file alone is enough, and so are the words alone
    stored = await _stored(client, name="enough.mp3")
    await client.patch(f"{PASSAGE_PATH}/{empty['id']}", json={"media_asset_id": stored["id"]})
    published = await client.post(f"{PASSAGE_PATH}/{empty['id']}/status", json={"status": "ready"})
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "ready"


async def test_a_lesson_cannot_be_edited_into_silence_after_it_is_public(client):
    """The publish rule is checked on the way back in, not only on the way out."""
    stored = await _stored(client, name="live.mp3")
    ready = await _create(client, title="Playing now", asset_id=stored["id"], transcript=None)
    assert ready["status"] == "ready"

    refused = await client.patch(f"{PASSAGE_PATH}/{ready['id']}", json={"media_asset_id": None})
    assert refused.status_code == 422, refused.text
    assert "published" in error_of(refused)["message"]

    still = await _detail(client, ready["id"])
    assert still["audio"]["id"] == stored["id"], "the refusal wrote nothing"


async def test_a_bulk_publish_refuses_the_silent_lesson_and_not_the_other_one(client):
    silent = await _create(client, title="Silent one", transcript=None, status="draft")
    spoken = await _create(client, title="Has words", transcript="Some words to read.", status="draft")
    ghost = str(uuid.uuid4())

    resp = await client.post(
        f"{PASSAGE_PATH}/bulk",
        json={
            "passage_ids": [silent["id"], spoken["id"], ghost],
            "action": "status",
            "status": "ready",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == [spoken["id"]]
    assert [item["id"] for item in body["refused"]] == [silent["id"]]
    assert "audio or a transcript" in body["refused"][0]["reason"]
    assert body["not_found"] == [ghost]
    assert (await _detail(client, silent["id"]))["status"] == "draft"


async def test_a_thrown_out_file_is_reported_to_the_teacher_and_hidden_from_a_learner(
    client, session_factory
):
    """Two screens, two honest answers about the same unavailable recording."""
    stored = await _stored(client, name="purged.mp3")
    created = await _create(client, title="Purged lesson", asset_id=stored["id"])
    await _throw_out_the_file(client, stored["id"])

    detail = await _detail(client, created["id"])
    assert detail["audio"]["state"] == "trashed"
    assert detail["audio"]["content_url"] == f"/media/{stored['id']}/content"
    teacher_read = await client.get(f"{MEDIA_PATH}/{stored['id']}/content")
    assert teacher_read.status_code == 200, "the trash screen still plays what it is about to lose"

    learner = await _learner(client, session_factory, "learner-purged")
    row = (await learner.get(STUDENT_PATH)).json()["items"][0]
    assert (row["has_audio"], row["duration_seconds"]) == (False, None), "no promise of a recording"
    served = await learner.get(f"{STUDENT_PATH}/{created['id']}")
    assert served.status_code == 200, served.text
    assert served.json()["audio"] is None
    assert served.json()["transcript"] is None, "show_transcript is still off"
    refused = await learner.get(f"/api/v1/student/media/{stored['id']}/content")
    assert refused.status_code == 404, refused.text
    assert error_of(refused)["code"] == "not_found", "the library's state is not a learner's business"


async def test_a_lesson_naming_a_thrown_out_file_cannot_be_published(client):
    stored = await _stored(client, name="restore-me.mp3")
    draft = await _create(client, title="Needs its file", asset_id=stored["id"], status="draft")
    await _throw_out_the_file(client, stored["id"])

    refused = await client.post(f"{PASSAGE_PATH}/{draft['id']}/status", json={"status": "ready"})
    assert refused.status_code == 422, refused.text
    message = error_of(refused)["message"]
    assert "is in the trash" in message and "attach another" in message

    # the sentence's own advice works: let the file go, publish with the words
    detached = await client.patch(f"{PASSAGE_PATH}/{draft['id']}", json={"media_asset_id": None})
    assert detached.status_code == 200, detached.text
    published = await client.post(f"{PASSAGE_PATH}/{draft['id']}/status", json={"status": "ready"})
    assert published.status_code == 200, published.text
    assert published.json()["transcript"] == TRANSCRIPT


# --------------------------------------------------------------------------- #
# Trash, restore, and what a soft delete touches
# --------------------------------------------------------------------------- #


async def test_a_trashed_lesson_is_edited_from_the_trash_and_not_behind_it(client):
    stored = await _stored(client, name="trashed-lesson.mp3")
    created = await _create(client, title="Archived then thrown", asset_id=stored["id"])
    await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "archived"})

    assert (await client.delete(f"{PASSAGE_PATH}/{created['id']}")).status_code == 200

    detail = await _detail(client, created["id"])
    assert detail["deleted_at"] is not None and detail["status"] == "archived"
    assert detail["audio"]["id"] == stored["id"], "the lesson still names its recording"

    edited = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"title": "Renamed"})
    assert edited.status_code == 404, edited.text

    refused = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "ready"})
    assert refused.status_code == 422, refused.text
    assert "restore the recording from the trash" in error_of(refused)["message"]

    blocks = await client.get(f"{PASSAGE_PATH}/{created['id']}/sets")
    assert blocks.status_code == 200, "the trash screen has to show what comes back with it"

    restored = await client.post(f"{PASSAGE_PATH}/{created['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "archived", "a restore returns the state it left"


async def test_trashing_a_lesson_leaves_the_file_and_the_exercises_alone(client):
    """The exercise and the recording are two rows with two trash cans."""
    stored = await _stored(client, name="kept.mp3")
    created = await _create(client, title="Two questions", asset_id=stored["id"])
    questions = [
        await _question(client, created["id"], prompt=f"Q {index}") for index in range(2)
    ]
    block = await _set(client, created["id"], "Block", start_seconds=0, end_seconds=10)
    await _file(client, block["id"], *[question["id"] for question in questions])

    assert (await client.delete(f"{PASSAGE_PATH}/{created['id']}")).status_code == 200

    asset = await client.get(f"{MEDIA_PATH}/{stored['id']}")
    assert asset.json()["state"] == "available"
    assert asset.json()["referenced_by"]["listenings"] == 0

    # with the lesson gone the file is free to go too, and the questions were never its
    for question in questions:
        row = await client.get(f"/api/v1/questions/{question['id']}")
        assert row.status_code == 200, "a lesson's trash is not a question's"
    assert (await client.delete(f"{MEDIA_PATH}/{stored['id']}")).status_code == 200


async def test_a_second_trash_is_a_no_op_rather_than_a_new_timestamp(client):
    created = await _create(client)
    first = await client.delete(f"{PASSAGE_PATH}/{created['id']}")
    assert first.status_code == 200, first.text
    before = (await _rows(view="trash"))[0].deleted_at

    again = await client.delete(f"{PASSAGE_PATH}/{created['id']}")
    assert again.status_code == 200, again.text
    assert (await _rows(view="trash"))[0].deleted_at == before, "nothing was re-trashed"


# --------------------------------------------------------------------------- #
# Blocks: a set, and a slice of the recording
# --------------------------------------------------------------------------- #


async def test_a_block_can_be_a_slice_of_the_recording_and_the_learner_is_told(client, session_factory):
    """The interval is a question boundary: the stored bytes are untouched."""
    stored = await _stored(client, name="long.mp3")
    created = await _create(client, title="Forty to seventy", asset_id=stored["id"])
    whole = await _set(client, created["id"], "Whole recording")
    slice_row = await _set(
        client, created["id"], "The difficult part", start_seconds=40, end_seconds=70
    )
    question = await _question(client, created["id"], prompt="What happens at forty seconds?")
    await _file(client, slice_row["id"], question["id"])

    blocks = (await client.get(f"{PASSAGE_PATH}/{created['id']}/sets")).json()["items"]
    assert [(item["title"], item["start_seconds"], item["end_seconds"]) for item in blocks] == [
        ("Whole recording", None, None),
        ("The difficult part", 40.0, 70.0),
    ]
    assert blocks[1]["question_count"] == 1

    moved = await client.patch(
        f"{PASSAGE_PATH}/sets/{slice_row['id']}", json={"start_seconds": 41.5}
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["start_seconds"], moved.json()["end_seconds"]) == (41.5, 70.0)

    learner = await _learner(client, session_factory, "learner-intervals")
    served = (await learner.get(f"{STUDENT_PATH}/{created['id']}")).json()
    assert [
        (item["title"], item["start_seconds"], item["end_seconds"]) for item in served["sets"]
    ] == [("The difficult part", 41.5, 70.0)], "a block with nothing answerable is not served"
    assert served["sets"][0]["questions"][0]["id"] == question["id"]

    bytes_after = await client.get(f"{MEDIA_PATH}/{stored['id']}/content")
    assert bytes_after.content == MP3_BYTES, "the file itself was never cut"
    assert whole["end_seconds"] is None, "the other block still covers the whole recording"


async def test_a_block_must_end_after_it_starts(client):
    created = await _create(client)
    for pair in ({"start_seconds": 30, "end_seconds": 30}, {"start_seconds": 40, "end_seconds": 20}):
        refused = await client.post(
            f"{PASSAGE_PATH}/{created['id']}/sets", json={"title": "Bad slice", **pair}
        )
        assert refused.status_code == 422, refused.text
        assert "the block must end after it starts" in error_of(refused)["message"]
    assert (await client.get(f"{PASSAGE_PATH}/{created['id']}/sets")).json()["items"] == []

    made = await _set(client, created["id"], "Open at the end", start_seconds=15)
    assert made["start_seconds"] == 15 and made["end_seconds"] is None

    refused = await client.patch(f"{PASSAGE_PATH}/sets/{made['id']}", json={"end_seconds": 5})
    assert refused.status_code == 200, refused.text
    assert refused.json()["end_seconds"] == 5, "one-sided patches are the editor's, not a rule"


async def test_a_reading_question_is_not_a_listening_question(client):
    """The two kinds file into two tables, and a stranger is refused by its prompt."""
    stored = await _stored(client, name="kind.mp3")
    listening = await _create(client, title="Listen", asset_id=stored["id"])
    reading = (
        await client.post(READING_PATH, json={"title": "Read", "body": "A short text here."})
    ).json()
    question = await _question(client, listening["id"], prompt="About the recording")
    block = await _set(client, listening["id"], "Block")

    stranger = (
        await client.post(
            "/api/v1/questions",
            json=choice_body(
                "About the text", context_kind="reading_bound", reading_id=reading["id"]
            ),
        )
    ).json()

    refused = await client.post(
        f"{PASSAGE_PATH}/sets/{block['id']}/questions", json={"question_ids": [stranger["id"]]}
    )
    assert refused.status_code == 422, refused.text
    message = error_of(refused)["message"]
    assert "the recording it belongs to" in message, message
    assert "About the text" in message, "the refusal names the question, not just the id"
    empty = await _detail(client, listening["id"])
    assert empty["sets"][0]["questions"] == [], "the refused filing wrote nothing"

    accepted = await _file(client, block["id"], question["id"])
    assert accepted["question_count"] == 1
    assert [item["id"] for item in accepted["questions"]] == [question["id"]]


async def test_filing_moves_a_question_between_blocks_and_reports_where_it_went(client):
    stored = await _stored(client, name="moved.mp3")
    created = await _create(client, title="Two blocks", asset_id=stored["id"])
    questions = [
        await _question(client, created["id"], prompt=f"Q {index}") for index in range(3)
    ]
    first = await _set(client, created["id"], "First", start_seconds=0, end_seconds=20)
    second = await _set(client, created["id"], "Second", start_seconds=20, end_seconds=45)

    await _file(client, first["id"], questions[0]["id"], questions[1]["id"])
    moved = await _file(client, second["id"], questions[1]["id"], questions[2]["id"])
    assert moved["moved"] == [{"question_id": questions[1]["id"], "from_title": "First"}]
    # `unfiled` answers "what did this block lose?", and this block lost nothing. The
    # question that left `First` was moved into this one, which is the other report.
    assert moved["unfiled"] == []

    detail = await _detail(client, created["id"])
    assert [item["question_count"] for item in detail["sets"]] == [1, 2]
    assert detail["unfiled"] == [], "a question that moved is still filed, just elsewhere"

    # Leaving one out of the block that holds it is what makes a question unfiled.
    dropped = await _file(client, second["id"], questions[2]["id"])
    assert dropped["unfiled"] == [questions[1]["id"]], "omitted is unfiled, never deleted"
    still_there = await client.get(f"/api/v1/questions/{questions[1]['id']}")
    assert still_there.status_code == 200, "the pool is not the trash"


async def test_deleting_a_block_returns_its_questions_to_the_pool(client):
    stored = await _stored(client, name="pool.mp3")
    created = await _create(client, title="Blockless", asset_id=stored["id"])
    questions = [
        await _question(client, created["id"], prompt=f"Q {index}") for index in range(2)
    ]
    block = await _set(client, created["id"], "To be removed", start_seconds=0, end_seconds=5)
    await _file(client, block["id"], *[question["id"] for question in questions])

    removed = await client.delete(f"{PASSAGE_PATH}/sets/{block['id']}")
    assert removed.status_code == 200, removed.text
    # The answer counts what it handed back; the ids are proven where a teacher sees them -
    # in the lesson's own pool, still bound and still editable.
    assert removed.json()["returned_to_pool"] == len(questions)

    detail = await _detail(client, created["id"])
    assert detail["sets"] == []
    assert {item["id"] for item in detail["unfiled"]} == {question["id"] for question in questions}


async def test_the_blocks_come_back_in_the_order_the_teacher_left_them(client):
    stored = await _stored(client, name="order.mp3")
    created = await _create(client, title="Ordered", asset_id=stored["id"])
    blocks = [await _set(client, created["id"], f"Block {index}") for index in range(3)]
    assert [item["position"] for item in blocks] == [0, 1, 2]

    partial = await client.post(
        f"{PASSAGE_PATH}/{created['id']}/sets/reorder", json={"set_ids": [blocks[0]["id"]]}
    )
    assert partial.status_code == 422, partial.text
    assert "every set must be listed in the new order" in error_of(partial)["message"]

    foreign = await client.post(
        f"{PASSAGE_PATH}/sets/{blocks[0]['id']}/questions", json={"question_ids": [str(uuid.uuid4())]}
    )
    assert foreign.status_code == 422, foreign.text
    assert "not in the bank" in error_of(foreign)["message"]

    other = await _create(client, title="Another lesson", asset_id=stored["id"])
    other_block = await _set(client, other["id"], "Elsewhere")
    mixed = await client.post(
        f"{PASSAGE_PATH}/{created['id']}/sets/reorder",
        json={"set_ids": [blocks[0]["id"], blocks[1]["id"], blocks[2]["id"], other_block["id"]]},
    )
    assert mixed.status_code == 422, mixed.text
    assert "do not belong to this recording" in error_of(mixed)["message"]

    flipped = await client.post(
        f"{PASSAGE_PATH}/{created['id']}/sets/reorder",
        json={"set_ids": [blocks[2]["id"], blocks[0]["id"], blocks[1]["id"]]},
    )
    assert flipped.status_code == 200, flipped.text
    assert [item["title"] for item in flipped.json()["items"]] == [
        "Block 2",
        "Block 0",
        "Block 1",
    ]


async def test_filing_never_bumps_the_immutable_history_of_a_question(client):
    """Grouping is not a content edit, and history that grew on every drag stops being evidence."""
    stored = await _stored(client, name="history.mp3")
    created = await _create(client, title="History", asset_id=stored["id"])
    question = await _question(client, created["id"], prompt="Moved about")
    first = await _set(client, created["id"], "First")
    second = await _set(client, created["id"], "Second")

    before_rows, before_version = await _versions(question["id"])
    await _file(client, first["id"], question["id"])
    await _file(client, second["id"], question["id"])
    await _file(client, second["id"])
    after_rows, after_version = await _versions(question["id"])
    assert (after_rows, after_version) == (before_rows, before_version)

    await client.patch(f"/api/v1/questions/{question['id']}", json={"prompt": "Rewritten prompt"})
    edited_rows, edited_version = await _versions(question["id"])
    assert edited_rows == before_rows + 1 and edited_version == before_version + 1


# --------------------------------------------------------------------------- #
# What a learner is shown
# --------------------------------------------------------------------------- #


async def test_a_learner_is_handed_ready_recordings_only(client, session_factory):
    learner = await _learner(client, session_factory, "learner-ready")
    stored = await _stored(client, name="visible.mp3")

    draft = await _create(client, title="Not finished", asset_id=stored["id"], status="draft")
    ready = await _create(client, title="Play me", asset_id=stored["id"])
    archived = await _create(client, title="Last year", asset_id=stored["id"])
    await client.post(f"{PASSAGE_PATH}/{archived['id']}/status", json={"status": "archived"})
    trashed = await _create(client, title="Throwing away", asset_id=stored["id"])
    await client.delete(f"{PASSAGE_PATH}/{trashed['id']}")

    for item in (draft, archived, trashed):
        refused = await learner.get(f"{STUDENT_PATH}/{item['id']}")
        assert refused.status_code == 404, f"{item['title']} -> {refused.status_code}"
        assert error_of(refused)["code"] == "not_found"

    listed = await learner.get(STUDENT_PATH)
    assert [row["id"] for row in listed.json()["items"]] == [ready["id"]]
    assert listed.json()["items"][0]["question_count"] == 0, "nothing is filed under it yet"


async def test_the_words_stay_hidden_until_the_teacher_shows_them(client, session_factory):
    """A listening whose text is on the screen is a reading, and the teacher decides."""
    stored = await _stored(client, name="hidden.mp3")
    hidden = await _create(
        client,
        title="Hold the words back",
        asset_id=stored["id"],
        transcript="The station closes at eleven.",
        cues=[{"start": 0, "text": "The station closes"}],
    )

    learner = await _learner(client, session_factory, "learner-hidden")
    served = await learner.get(f"{STUDENT_PATH}/{hidden['id']}")
    assert served.status_code == 200, served.text
    body = served.json()
    assert body["show_transcript"] is False
    assert body["transcript"] is None and body["transcript_timestamps"] == []
    assert "eleven" not in served.text, "the sentence is not in the payload at all"

    preview = await client.get(f"{PASSAGE_PATH}/{hidden['id']}/preview")
    shown = preview.json()
    # One projection, two doors. The only thing allowed to differ is which door the file
    # comes through: a preview that handed out the student path would show the teacher an
    # empty player, because an admin session cannot fetch it.
    assert shown["audio"]["content_url"] == f"/media/{stored['id']}/content"
    assert body["audio"]["content_url"] == f"/student/media/{stored['id']}/content"
    shown["audio"]["content_url"] = body["audio"]["content_url"]
    assert shown == body, "a preview that leaks the words is not a preview"

    opened = await client.patch(f"{PASSAGE_PATH}/{hidden['id']}", json={"show_transcript": True})
    assert opened.json()["transcript"] == "The station closes at eleven."
    shown = (await learner.get(f"{STUDENT_PATH}/{hidden['id']}")).json()
    assert shown["transcript"] == "The station closes at eleven."
    assert shown["transcript_timestamps"] == [{"start": 0, "text": "The station closes"}]


async def test_no_answer_key_or_authoring_note_reaches_a_learner_through_a_recording(
    client, session_factory
):
    created, questions, block = await _ready_with_block(client, title="Key test")
    await client.patch(
        f"{PASSAGE_PATH}/sets/{block['id']}", json={"config": {"notes_for_me": KEY}}
    )

    teacher = (await client.get(f"{PASSAGE_PATH}/{created['id']}/sets")).json()["items"]
    assert teacher[0]["config"] == {"notes_for_me": KEY}

    learner = await _learner(client, session_factory, "learner-no-key")
    served = await learner.get(f"{STUDENT_PATH}/{created['id']}")
    assert KEY not in served.text, served.text
    assert '"correct"' not in served.text
    assert '"explanation"' not in served.text
    assert '"teacher_notes"' not in served.text
    item = served.json()["sets"][0]["questions"][0]
    # Every option arrives, with its text: the key is which one is right, not which words
    # exist. A projection that dropped the texts would make the question unanswerable.
    assert [option["text"] for option in item["config"]["options"]] == [
        "it closed",
        "it stayed open",
        "it moved",
    ]
    assert set(item["config"]["options"][0]) == {"index", "text"}, item["config"]["options"][0]
    assert item["answer_widget"] and item["explanation_available"] is True
    assert "config" not in served.json()["sets"][0], "the authoring bucket stays with the teacher"


async def test_a_learner_never_sees_a_draft_question_or_an_empty_block(client, session_factory):
    created, questions, _block = await _ready_with_block(client, title="Draft mix", drafts=2)
    empty = await _set(client, created["id"], "Nothing filed here")

    learner = await _learner(client, session_factory, "learner-drafts")
    body = (await learner.get(f"{STUDENT_PATH}/{created['id']}")).json()
    assert [item["title"] for item in body["sets"]] == ["First block"], empty
    assert [item["id"] for item in body["sets"][0]["questions"]] == [
        question["id"] for question in questions
    ]
    assert body["unfiled"] == [], "a draft in the pool is unfinished work, not content"

    listed = (await learner.get(STUDENT_PATH)).json()["items"][0]
    assert listed["question_count"] == 2, "the row counts what a learner can answer"


async def test_the_learners_player_reads_the_bytes_through_the_student_path(
    client, session_factory
):
    """One path for a learner, authorised by their own session on every request."""
    stored = await _stored(client, name="playable.mp3")
    created = await _create(client, title="Playable", asset_id=stored["id"])

    learner = await _learner(client, session_factory, "learner-player")
    served = (await learner.get(f"{STUDENT_PATH}/{created['id']}")).json()
    assert served["audio"]["content_url"] == f"/student/media/{stored['id']}/content"
    assert set(served["audio"]) == {
        "id",
        "kind",
        "mime_type",
        "duration_seconds",
        "width",
        "height",
        "content_url",
    }, served["audio"]
    for word in ("original_filename", "label", "checksum", "storage_key", "referenced_by"):
        assert word not in served["audio"]

    whole = await learner.get(f"/api/v1/student/media/{stored['id']}/content")
    assert whole.status_code == 200, whole.text
    assert whole.content == MP3_BYTES
    assert whole.headers["content-type"] == "audio/mpeg"

    piece = await learner.get(
        f"/api/v1/student/media/{stored['id']}/content", headers={"Range": "bytes=0-9"}
    )
    assert piece.status_code == 206 and piece.content == MP3_BYTES[:10]

    teacher_path = await learner.get(f"/api/v1/media/{stored['id']}/content")
    assert teacher_path.status_code == 401, "the library's own route is not a learner's"


async def test_a_learner_cannot_open_the_editors_surface(client, session_factory):
    learner = await _learner(client, session_factory, "learner-editor")
    assert error_of(await learner.get(PASSAGE_PATH))["code"] == "unauthorized"
    assert (await learner.get(f"{PASSAGE_PATH}/meta")).status_code == 401
    assert (
        await learner.post(PASSAGE_PATH, json={"title": "T", "transcript": "Words."})
    ).status_code == 401
    assert (
        await learner.post(
            f"{PASSAGE_PATH}/bulk", json={"passage_ids": [str(uuid.uuid4())], "action": "trash"}
        )
    ).status_code == 401, "a learner is refused before the body is ever read"


async def test_the_student_list_accepts_only_the_sorts_it_offers(client, session_factory):
    learner = await _learner(client, session_factory, "learner-sort")
    bogus = await learner.get(f"{STUDENT_PATH}?sort=checksum")
    assert bogus.status_code == 422, bogus.text
    assert "sort must be one of" in error_of(bogus)["message"]

    meta = await learner.get(f"{STUDENT_PATH}/meta")
    assert meta.status_code == 200, meta.text
    assert set(meta.json()["sortable"]) == set(listening_service.SORTABLE)
    for word in ("views", "transcript_sources", "statuses", "max_replay_limit"):
        assert word not in meta.json(), "a learner changes none of these"


# --------------------------------------------------------------------------- #
# The teacher's list
# --------------------------------------------------------------------------- #


async def test_a_recording_is_found_by_its_title_by_its_words_and_by_nothing_else(client):
    stored = await _stored(client, name="search.mp3")
    station = await _create(
        client, title="At the station", asset_id=stored["id"], transcript="The train is late."
    )
    await client.patch(f"{PASSAGE_PATH}/{station['id']}", json={"level": "B1"})

    for query, expected in (("station", 1), ("is late", 1), ("train", 1), ("harbour", 0), ("B1", 0)):
        resp = await client.get(f"{PASSAGE_PATH}?q={query}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == expected, query

    by_audio = await client.get(f"{PASSAGE_PATH}?has_audio=true")
    assert by_audio.json()["total"] == 1
    assert (await client.get(f"{PASSAGE_PATH}?has_audio=false")).json()["total"] == 0
    words_only = await _create(client, title="No file", transcript="Only the words.")
    assert [item["id"] for item in (await client.get(f"{PASSAGE_PATH}?has_audio=false")).json()["items"]] == [
        words_only["id"]
    ]

    shown = await client.get(f"{PASSAGE_PATH}?show_transcript=true")
    assert shown.json()["total"] == 0
    await client.patch(f"{PASSAGE_PATH}/{station['id']}", json={"show_transcript": True})
    assert [item["id"] for item in (await client.get(f"{PASSAGE_PATH}?show_transcript=true")).json()["items"]] == [
        station["id"]
    ]

    by_status = await client.get(f"{PASSAGE_PATH}?status=draft")
    assert by_status.json()["total"] == 0
    await client.post(f"{PASSAGE_PATH}/{words_only['id']}/status", json={"status": "draft"})
    assert [item["id"] for item in (await client.get(f"{PASSAGE_PATH}?status=draft")).json()["items"]] == [
        words_only["id"]
    ]


async def test_the_bank_and_the_trash_are_two_different_answers(client):
    stored = await _stored(client, name="views.mp3")
    keep = await _create(client, title="Keep", asset_id=stored["id"])
    drop = await _create(client, title="Drop", asset_id=stored["id"])
    await client.delete(f"{PASSAGE_PATH}/{drop['id']}")

    assert [item["id"] for item in (await client.get(f"{PASSAGE_PATH}?view=bank")).json()["items"]] == [
        keep["id"]
    ]
    trash = (await client.get(f"{PASSAGE_PATH}?view=trash")).json()["items"]
    assert [item["id"] for item in trash] == [drop["id"]]
    assert trash[0]["deleted_at"] is not None
    assert (await client.get(f"{PASSAGE_PATH}?view=all")).json()["total"] == 2


async def test_the_library_refuses_an_unknown_view_sort_or_order(client):
    await _create(client)
    for query, word in (
        ("?view=everything", "view"),
        ("?sort=media_asset_id", "sort"),
        ("?order=sideways", "order"),
        ("?status=recycled", "status"),
    ):
        resp = await client.get(PASSAGE_PATH + query)
        assert resp.status_code == 422, f"{query} -> {resp.status_code}"
        assert word in error_of(resp)["message"]

    not_a_boolean = await client.get(f"{PASSAGE_PATH}?has_audio=maybe")
    assert not_a_boolean.status_code == 422, not_a_boolean.text
    error = error_of(not_a_boolean)
    assert error["code"] == "validation_failed"
    assert any("has_audio" in str(field["loc"]) for field in error["fields"]), error


async def test_a_list_page_never_repeats_or_skips_a_row(client):
    stored = await _stored(client, name="paged.mp3")
    ids = []
    for index in range(5):
        made = await _create(client, title=f"Lesson {index}", asset_id=stored["id"])
        ids.append(made["id"])

    pages = [
        await client.get(f"{PASSAGE_PATH}?sort=title&order=asc&page={page}&page_size=2")
        for page in (1, 2, 3)
    ]
    seen = [item["id"] for resp in pages for item in resp.json()["items"]]
    assert sorted(seen) == sorted(ids), "paging must cover every row exactly once"
    assert pages[0].json()["total"] == 5
    assert [item["title"] for item in pages[0].json()["items"]] == ["Lesson 0", "Lesson 1"]
    for resp in pages:
        assert resp.json()["page_size"] == 2


async def test_a_rows_counts_are_the_blocks_and_questions_the_editor_shows(client):
    stored = await _stored(client, name="counts.mp3")
    created = await _create(client, title="Counted", asset_id=stored["id"])
    questions = [
        await _question(client, created["id"], prompt=f"Q {index}") for index in range(3)
    ]
    one = await _set(client, created["id"], "One", start_seconds=0, end_seconds=15)
    two = await _set(client, created["id"], "Two", start_seconds=15, end_seconds=30)
    await _file(client, one["id"], questions[0]["id"], questions[1]["id"])
    await _file(client, two["id"], questions[2]["id"])

    row = (await client.get(f"{PASSAGE_PATH}?q=Counted")).json()["items"][0]
    assert (row["set_count"], row["question_count"]) == (2, 3)
    assert row["status"] == "ready" and row["transcript_source"] == "manual"

    await client.delete(f"{PASSAGE_PATH}/sets/{two['id']}")
    after = (await client.get(f"{PASSAGE_PATH}?q=Counted")).json()["items"][0]
    assert (after["set_count"], after["question_count"]) == (1, 3), "unfiled is not deleted"


async def test_bulk_answers_per_row_and_never_loses_a_lesson(client):
    stored = await _stored(client, name="bulk.mp3")
    ready = await _create(client, title="Already ready", asset_id=stored["id"])
    draft = await _create(client, title="Still draft", asset_id=stored["id"], status="draft")
    other = await _create(client, title="Other draft", asset_id=stored["id"], status="draft")

    published = await client.post(
        f"{PASSAGE_PATH}/bulk",
        json={"passage_ids": [ready["id"], draft["id"]], "action": "status", "status": "ready"},
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["action"] == "status"
    assert body["updated"] == [ready["id"], draft["id"]]
    assert body["refused"] == [] and body["not_found"] == []

    levelled = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [other["id"]], "action": "set_level", "level": "B2"}
    )
    assert levelled.json()["updated"] == [other["id"]]
    assert (await _detail(client, other["id"]))["level"] == "B2"

    trashed = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [draft["id"], other["id"]], "action": "trash"}
    )
    assert trashed.json()["updated"] == [draft["id"], other["id"]]

    twice = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [draft["id"]], "action": "trash"}
    )
    assert twice.json()["updated"] == []
    assert twice.json()["refused"] == [{"id": draft["id"], "reason": "already in the trash"}]

    restored = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [draft["id"]], "action": "restore"}
    )
    assert restored.json()["updated"] == [draft["id"]]

    ghost = str(uuid.uuid4())
    absent = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [ghost], "action": "trash"}
    )
    assert absent.json()["not_found"] == [ghost]


async def test_bulk_has_no_entry_point_for_editing_the_words(client):
    created = await _create(client)
    for action, extra in (
        ("retitle", {"title": "x"}),
        ("set_transcript", {"transcript": "x"}),
        ("purge", {}),
    ):
        refused = await client.post(
            f"{PASSAGE_PATH}/bulk", json={"passage_ids": [created["id"]], "action": action, **extra}
        )
        assert refused.status_code == 422, action
        assert "action must be one of" in error_of(refused)["message"]

    no_status = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [created["id"]], "action": "status"}
    )
    assert no_status.status_code == 422, no_status.text
    assert "needs a status" in error_of(no_status)["message"]

    empty = await client.post(f"{PASSAGE_PATH}/bulk", json={"passage_ids": [], "action": "trash"})
    assert empty.status_code == 422


# --------------------------------------------------------------------------- #
# Meta, gates, audit
# --------------------------------------------------------------------------- #


async def test_the_meta_endpoint_is_the_vocabulary_the_endpoints_enforce(client):
    resp = await client.get(f"{PASSAGE_PATH}/meta")
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["statuses"] == list(passage_service.SETTABLE_STATUSES)
    assert meta["views"] == list(passage_service.VIEWS)
    assert set(meta["sortable"]) == set(listening_service.SORTABLE)
    assert meta["transcript_sources"] == [source.value for source in enums.TranscriptSource]
    assert meta["max_replay_limit"] == MAX_REPLAY_LIMIT
    assert meta["max_body_characters"] == MAX_BODY_CHARACTERS
    assert "layouts" not in meta, "a recording has no text layout to pick"

    for code in meta["learning_languages"]:
        made = await _create(client, title=f"Lang {code}", language=code)
        assert made["language"] == code


async def test_a_language_the_admin_did_not_enable_is_refused_by_name(client):
    refused = await client.post(
        PASSAGE_PATH, json={"title": "Wrong language", "language": "zz", "transcript": "Words."}
    )
    assert refused.status_code == 422, refused.text
    message = error_of(refused)["message"]
    assert "not an enabled learning language" in message and "available:" in message
    assert await _rows() == []

    made = await _create(client, title="Normalised", language="  EN ")
    assert made["language"] == "en"
    loose = await _create(client, title="None yet", language=None)
    assert loose["language"] is None


async def test_the_surface_is_admin_only_and_writes_are_csrf_guarded(client, session_factory):
    anonymous = session_factory()._c
    assert (await anonymous.get(PASSAGE_PATH)).status_code == 401
    assert (await anonymous.get(f"{PASSAGE_PATH}/meta")).status_code == 401

    raw = client._c
    without_csrf = await raw.post(PASSAGE_PATH, json={"title": "T", "transcript": "Words."})
    assert without_csrf.status_code == 403, without_csrf.text
    assert error_of(without_csrf)["code"] == "csrf_failed"
    assert await _rows() == [], "a refused write must not create a row"


async def test_an_unknown_id_is_a_404_on_every_route(client, session_factory):
    missing = str(uuid.uuid4())
    for path in (
        f"{PASSAGE_PATH}/{missing}",
        f"{PASSAGE_PATH}/{missing}/sets",
        f"{PASSAGE_PATH}/{missing}/preview",
    ):
        resp = await client.get(path)
        assert resp.status_code == 404, path
        assert error_of(resp)["code"] == "not_found"

    for call in (
        client.patch(f"{PASSAGE_PATH}/{missing}", json={"title": "Ghost"}),
        client.delete(f"{PASSAGE_PATH}/{missing}"),
        client.post(f"{PASSAGE_PATH}/{missing}/restore"),
        client.post(f"{PASSAGE_PATH}/{missing}/status", json={"status": "draft"}),
        client.post(f"{PASSAGE_PATH}/{missing}/sets", json={"title": "Ghost block"}),
        client.post(f"{PASSAGE_PATH}/{missing}/sets/reorder", json={"set_ids": [missing]}),
        client.patch(f"{PASSAGE_PATH}/sets/{missing}", json={"title": "Ghost"}),
        client.delete(f"{PASSAGE_PATH}/sets/{missing}"),
        client.post(f"{PASSAGE_PATH}/sets/{missing}/questions", json={"question_ids": []}),
    ):
        assert (await call).status_code == 404

    learner = await _learner(client, session_factory, "learner-ghost")
    assert error_of(await learner.get(f"{STUDENT_PATH}/{missing}"))["code"] == "not_found"


async def test_audit_rows_describe_the_lesson_and_never_its_words(client):
    stored = await _stored(client, name="audited.mp3")
    created = await _create(
        client,
        title="Audited",
        asset_id=stored["id"],
        transcript=SHORT_WORDS,
        cues=[{"start": 0, "text": "one"}],
    )
    await client.patch(
        f"{PASSAGE_PATH}/{created['id']}",
        json={"media_asset_id": None, "transcript": LONG_WORDS},
    )
    await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "draft"})

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog.action, AuditLog.before, AuditLog.after).where(
                    AuditLog.target_type == "listening"
                )
            )
        ).all()
    by_action = {action: (before, after) for action, before, after in rows}
    assert {"listening.created", "listening.updated", "listening.status.changed"} <= set(by_action)

    _before, after = by_action["listening.created"]
    assert after["has_audio"] is True
    assert after["transcript_characters"] == 13 and after["cue_lines"] == 1
    assert after["status"] == "ready" and "transcript" not in after
    assert "media_asset_id" not in after, "an id tells whoever reads the history nothing"
    assert "one two three" not in str(rows), "the words themselves are not logged"
    assert stored["id"] not in str(rows), "and neither is the file it named"

    before, after = by_action["listening.updated"]
    assert (before["has_audio"], after["has_audio"]) == (True, False)
    assert (before["transcript_characters"], after["transcript_characters"]) == (13, 18)

    before, after = by_action["listening.status.changed"]
    assert (before["status"], after["status"]) == ("ready", "draft")


async def test_a_block_is_audited_by_its_own_id_and_the_filing_by_its_effect(client):
    stored = await _stored(client, name="set-audit.mp3")
    created = await _create(client, title="Set audit", asset_id=stored["id"])
    question = await _question(client, created["id"], prompt="Filed once")
    block = await _set(client, created["id"], "Block", start_seconds=5, end_seconds=15)
    await _file(client, block["id"], question["id"])
    await client.patch(f"{PASSAGE_PATH}/sets/{block['id']}", json={"title": "Renamed block"})
    await client.patch(f"{PASSAGE_PATH}/sets/{block['id']}", json={"start_seconds": 7.5})

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.target_type == "listening_question_set")
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ).scalars().all()

    assert [row.action for row in rows] == [
        "listening.set.created",
        "listening.set.assigned",
        "listening.set.updated",
        "listening.set.updated",
    ]
    assert {str(row.target_id) for row in rows} == {block["id"]}, "the block is its own audit target"

    created_row, assigned = rows[0], rows[1]
    assert (created_row.after["title"], created_row.after["position"]) == ("Block", 0)
    assert assigned.after["questions"] == [question["id"]]
    assert assigned.after["unfiled"] == [] and assigned.after["moved_in"] == []
    assert (rows[2].before["title"], rows[2].after["title"]) == ("Block", "Renamed block")
    assert (rows[3].before["start_seconds"], rows[3].after["start_seconds"]) == (5.0, 7.5)
    assert str(created["id"]) not in str(assigned.after), "the set is the target, not the lesson"


async def test_the_enum_words_the_service_writes_are_the_ones_the_api_reports(client):
    """A column that drifts from the payloads would show a teacher an empty chip."""
    stored = await _stored(client, name="enum.mp3")
    created = await _create(client, title="Enum", asset_id=stored["id"])

    assert enums.ContentStatus(created["status"]) is enums.ContentStatus.READY
    assert enums.TranscriptSource(created["transcript_source"]) is enums.TranscriptSource.MANUAL

    row = (await _rows())[0]
    assert row.status.value == created["status"]
    assert row.transcript_source == created["transcript_source"]

    # A lesson with neither audio nor words is a draft by law, so the provenance word is
    # read from a row that was allowed to exist rather than from a refused write.
    silent = await _create(client, title="Silent source", transcript=None, status="draft")
    assert enums.TranscriptSource(silent["transcript_source"]) is enums.TranscriptSource.ABSENT
    second_row = [item for item in await _rows() if item.id == uuid.UUID(silent["id"])][0]
    assert second_row.transcript_source == "absent"
