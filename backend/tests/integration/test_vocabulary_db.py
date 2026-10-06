"""The vocabulary bank over HTTP, against real Postgres (Phase 4).

What the offline rule tests cannot prove: that the duplicate-word rule survives two
writers at the same moment, that the trash really is a soft delete the status survives,
that filters, sorting and pagination agree with the rows in the table, that bulk answers
for every id it was handed, and - the one that matters most here - that a teacher's
private note never reaches a learner's screen, not even through the learner's search box.

Every refusal is checked against the table as well as the response: a 409 that still
wrote a row would look like a clean refusal from outside.
"""
from __future__ import annotations

import asyncio
import uuid

from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core import enums
from app.core.database import SessionLocal
from app.models.content import MediaAsset, VocabularyEntry, vocabulary_tag
from app.models.identity import Student
from app.models.ops import AuditLog

# Text that only ever belongs in the teacher's private note. Wherever it shows up in a
# learner payload or a learner's search results, the test looking for it fails.
TEACHER_ONLY = "ZULU-PRIVATE-TEACHER-NOTE"


def word_body(**over) -> dict:
    body = {
        "word": "improve",
        "learning_language": "en",
        "definition": "to make something better",
        "ipa": "/ɪmˈpruːv/",
        "part_of_speech": "verb",
        "level": "B1",
        "synonyms": ["better", "enhance"],
        "antonyms": ["worsen"],
        "notes": f"Bring up the comparative form - {TEACHER_ONLY}",
        "translations": [
            {"language": "az", "value": "təkmilləşdirmək"},
            {"language": "ru", "value": "улучшать"},
        ],
        "examples": [
            {
                "sentence": "She wants to improve her English.",
                "language": "en",
                "translation": "O ingilis dilini təkmilləşdirmək istəyir.",
            }
        ],
    }
    body.update(over)
    return body


async def _create(client, **over) -> dict:
    resp = await client.post("/api/v1/vocabulary", json=word_body(**over))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _detail(client, entry_id: str) -> dict:
    resp = await client.get(f"/api/v1/vocabulary/{entry_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _new_tag(client, name: str) -> str:
    resp = await client.post("/api/v1/tags", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _new_media_asset() -> str:
    async with SessionLocal() as db:
        row = MediaAsset(
            kind="audio", storage_key="vocabulary/test-pronunciation.mp3", mime_type="audio/mpeg"
        )
        db.add(row)
        await db.commit()
        return str(row.id)


async def _learner(client, session_factory, username: str):
    """A logged-in learner, created through the teacher's own screen."""
    created = await client.post(
        "/api/v1/students", json={"name": "Learner", "surname": "Card", "username": username}
    )
    assert created.status_code == 201, created.text
    learner = session_factory()
    await learner.login_student(created.json()["access_key"])
    return created.json(), learner


async def _learner_session(session_factory, access_key: str):
    learner = session_factory()
    await learner.login_student(access_key)
    return learner


async def _force_student_status(student_id: str, status: enums.StudentStatus) -> None:
    """Set the status column without going through the status endpoint.

    The endpoint also ends the learner's sessions, so this is the only way to reach the
    dependency's own guard: an account that stopped being active while a session for it
    is still alive.
    """
    async with SessionLocal() as db:
        student = await db.get(Student, uuid.UUID(student_id))
        assert student is not None
        student.status = status
        await db.commit()


async def _live_words(word: str | None = None, learning_language: str = "en") -> list[str]:
    """The live bank as the table holds it, ignoring capitalisation in the response."""
    async with SessionLocal() as db:
        stmt = select(VocabularyEntry.word).where(
            VocabularyEntry.learning_language == learning_language,
            VocabularyEntry.deleted_at.is_(None),
        )
        if word is not None:
            stmt = stmt.where(func.lower(VocabularyEntry.word) == word.lower())
        return sorted((await db.execute(stmt)).scalars().all(), key=str.lower)


async def _count_live() -> int:
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(func.count()).select_from(VocabularyEntry).where(VocabularyEntry.deleted_at.is_(None))
            )
        ).scalar_one()


async def _tag_links(entry_id: str) -> list[str]:
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(vocabulary_tag.c.tag_id).where(
                        vocabulary_tag.c.vocabulary_entry_id == uuid.UUID(entry_id)
                    )
                )
            ).scalars().all()
        )


async def _set_learning_languages(client, codes: list[str]) -> None:
    resp = await client.put(
        "/api/v1/settings/ui", json={"category": "i18n", "values": {"learning_languages": codes}}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["learning_languages"]) == sorted(codes)


# --------------------------------------------------------------------------- #
# Meta, auth, CSRF
# --------------------------------------------------------------------------- #


async def test_the_meta_endpoint_describes_the_bank_the_editor_builds_from(client):
    resp = await client.get("/api/v1/vocabulary/meta")
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["learning_languages"] == ["en"]
    assert meta["translation_languages"] == ["az", "ru", "tr"]
    # An example may be written in the language being learned or in a language the
    # class is taught in, so the two sets merge for the example picker.
    assert meta["example_languages"] == ["az", "en", "ru", "tr"]
    assert meta["levels"] == ["A1", "A2", "B1", "B2", "C1", "C2"]
    assert "verb" in meta["parts_of_speech"]
    assert meta["statuses"] == ["draft", "ready", "archived"], "trash is a deletion, not a status"


async def test_the_bank_is_teacher_only(client, session_factory):
    anonymous = session_factory()._c
    for path in ("/api/v1/vocabulary", "/api/v1/vocabulary/meta"):
        resp = await anonymous.get(path)
        assert resp.status_code == 401, f"GET {path} -> {resp.status_code}"
        assert error_of(resp)["code"] == "unauthorized"
    refused = await anonymous.post("/api/v1/vocabulary", json=word_body())
    assert refused.status_code == 401
    assert error_of(refused)["code"] == "unauthorized"

    _profile, learner = await _learner(client, session_factory, "vocab-no-write")
    for path in ("/api/v1/vocabulary", "/api/v1/vocabulary/meta"):
        assert (await learner.get(path)).status_code == 401, f"a learner reached {path}"
    write = await learner.post("/api/v1/vocabulary", json=word_body(word="intruder"))
    assert write.status_code == 401, write.text
    assert await _count_live() == 0, "a refused write must leave no row"


async def test_bank_writes_need_the_csrf_header(client):
    resp = await client._c.post("/api/v1/vocabulary", json=word_body())
    assert resp.status_code == 403
    assert error_of(resp)["code"] == "csrf_failed"
    assert await _count_live() == 0


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


async def test_a_word_and_every_child_of_it_round_trips(client):
    tag_id = await _new_tag(client, "study list 1")
    created = await _create(client, tag_ids=[tag_id])

    assert created["word"] == "improve"
    assert created["learning_language"] == "en"
    assert created["status"] == "ready"
    assert created["deleted_at"] is None
    assert created["synonyms"] == ["better", "enhance"]
    assert created["notes"] == f"Bring up the comparative form - {TEACHER_ONLY}"
    assert created["source_file_id"] is None, "a hand-written entry has no import provenance"
    assert [row["language"] for row in created["translations"]] == ["az", "ru"]
    assert created["translations"][0]["value"] == "təkmilləşdirmək"
    assert created["examples"][0]["sentence"] == "She wants to improve her English."
    assert created["tags"] == [{"id": tag_id, "name": "study list 1"}]

    # The edit screen reads the same shape back, so a patch can resend what it saw.
    assert (await _detail(client, created["id"])) == created

    listed = await client.get("/api/v1/vocabulary")
    assert listed.status_code == 200
    row = listed.json()["items"][0]
    assert row["translation_languages"] == ["az", "ru"]
    assert row["example_count"] == 1
    assert row["tag_names"] == ["study list 1"]
    assert row["has_audio"] is False and row["has_source"] is False
    assert "notes" not in row, "a list row must not carry the teacher's private note"


async def test_a_blank_word_or_language_is_refused(client):
    for bad in ({"word": "   "}, {"word": ""}, {"learning_language": "  "}, {"learning_language": None}):
        resp = await client.post("/api/v1/vocabulary", json=word_body(**bad))
        assert resp.status_code == 422, f"{bad} -> {resp.status_code}: {resp.text}"
    assert await _count_live() == 0


async def test_a_word_is_only_accepted_in_a_language_the_platform_teaches(client):
    resp = await client.post("/api/v1/vocabulary", json=word_body(learning_language="de"))
    assert resp.status_code == 422
    assert "learning language" in error_of(resp)["message"]
    assert await _count_live() == 0


async def test_the_learning_language_is_normalised_before_it_is_stored(client):
    """`EN`, ` en ` and `en` are one language, so they must be one row in the bank."""
    created = await _create(client, learning_language=" EN ")
    assert created["learning_language"] == "en"
    duplicate = await client.post("/api/v1/vocabulary", json=word_body(learning_language="en"))
    assert duplicate.status_code == 409
    assert error_of(duplicate)["code"] == "word_exists"


async def test_a_translation_may_not_repeat_or_answer_the_learning_language(client):
    repeated = await client.post(
        "/api/v1/vocabulary",
        json=word_body(
            translations=[{"language": "az", "value": "bir"}, {"language": "AZ", "value": "iki"}]
        ),
    )
    assert repeated.status_code == 422
    assert "one meaning per language" in error_of(repeated)["message"]

    mirror = await client.post(
        "/api/v1/vocabulary", json=word_body(translations=[{"language": "en", "value": "yourself"}])
    )
    assert mirror.status_code == 422
    assert "cannot be translated into" in error_of(mirror)["message"]

    unknown = await client.post(
        "/api/v1/vocabulary", json=word_body(translations=[{"language": "de", "value": "verbessern"}])
    )
    assert unknown.status_code == 422
    assert "translation language" in error_of(unknown)["message"]
    assert await _count_live() == 0


async def test_an_example_is_written_in_a_language_the_class_has(client):
    resp = await client.post(
        "/api/v1/vocabulary",
        json=word_body(examples=[{"sentence": "Ich verbessere mich.", "language": "de"}]),
    )
    assert resp.status_code == 422
    assert "neither a learning nor a translation language" in error_of(resp)["message"]

    # An example with no language at all is allowed: plenty of banks carry the sentence
    # only, and the teacher can label it later.
    unlabelled = await client.post(
        "/api/v1/vocabulary", json=word_body(word="label", examples=[{"sentence": "We label later."}])
    )
    assert unlabelled.status_code == 201, unlabelled.text


async def test_synonym_lists_are_cleaned_on_the_way_in(client):
    created = await _create(
        client, synonyms=["  Run ", "run", "", "   ", "sprint", "Dash", "dash"], antonyms=[" ", "fall"]
    )
    assert created["synonyms"] == ["Run", "sprint", "Dash"]
    assert created["antonyms"] == ["fall"]


async def test_an_unknown_audio_asset_or_tag_is_named_not_a_server_error(client):
    resp = await client.post("/api/v1/vocabulary", json=word_body(audio_asset_id=str(uuid.uuid4())))
    assert resp.status_code == 422
    assert "does not exist" in error_of(resp)["message"]

    tagged = await client.post("/api/v1/vocabulary", json=word_body(tag_ids=[str(uuid.uuid4())]))
    assert tagged.status_code == 422
    assert "tag" in error_of(tagged)["message"]
    assert await _count_live() == 0


async def test_a_word_can_carry_a_pronunciation_recording(client):
    asset_id = await _new_media_asset()
    created = await _create(client, audio_asset_id=asset_id)
    assert created["audio_asset_id"] == asset_id
    assert (await client.get("/api/v1/vocabulary")).json()["items"][0]["has_audio"] is True
    only_audio = await client.get("/api/v1/vocabulary", params={"has_audio": "true"})
    assert [row["id"] for row in only_audio.json()["items"]] == [created["id"]]
    assert (await client.get("/api/v1/vocabulary", params={"has_audio": "false"})).json()["items"] == []


# --------------------------------------------------------------------------- #
# The duplicate rule
# --------------------------------------------------------------------------- #


async def test_the_same_word_twice_is_refused_however_it_is_capitalised(client):
    """A learner's word list holds one card for "run"; so does the bank."""
    await _create(client, word="improve")
    for variant in ("Improve", "IMPROVE", " improve "):
        resp = await client.post("/api/v1/vocabulary", json=word_body(word=variant))
        assert resp.status_code == 409, f"'{variant}' -> {resp.status_code}"
        assert error_of(resp)["code"] == "word_exists"
    assert await _live_words("improve") == ["improve"]


async def test_the_same_word_in_two_learning_languages_is_two_words(client):
    await _set_learning_languages(client, ["en", "az"])
    english = await _create(client, word="read", learning_language="en")
    # 'read' written as an Azerbaijani word is a different card, and its meanings go
    # into the other languages - an entry is never translated into itself.
    azerbaijani = await _create(
        client,
        word="read",
        learning_language="az",
        translations=[{"language": "ru", "value": "читать"}],
        examples=[],
    )
    assert english["id"] != azerbaijani["id"]
    assert await _live_words("read", "en") == ["read"]
    assert await _live_words("read", "az") == ["read"]

    duplicate = await client.post(
        "/api/v1/vocabulary", json=word_body(word="Read", learning_language="en")
    )
    assert duplicate.status_code == 409


async def test_renaming_a_word_onto_another_live_word_is_refused(client):
    await _create(client, word="improve")
    second = await _create(client, word="enhance", translations=[], examples=[])
    resp = await client.patch(f"/api/v1/vocabulary/{second['id']}", json={"word": "Improve"})
    assert resp.status_code == 409
    assert error_of(resp)["code"] == "word_exists"
    assert (await _detail(client, second["id"]))["word"] == "enhance", (
        "the refused rename must not have been stored"
    )


async def test_editing_a_definition_never_reads_as_a_new_word(client):
    created = await _create(client)
    resp = await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"definition": "to get better"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["definition"] == "to get better"
    assert await _live_words("improve") == ["improve"]


async def test_two_concurrent_creates_of_one_word_leave_exactly_one_row(client):
    """The rule the service cannot enforce on its own.

    Both requests look, see no 'decide', and insert. Only the partial unique index can
    see the other transaction, so one must lose - and the loser has to be answered in
    words a teacher can act on, not with a 500.
    """
    outcomes = await asyncio.gather(
        client.post("/api/v1/vocabulary", json=word_body(word="decide")),
        client.post("/api/v1/vocabulary", json=word_body(word="decide")),
    )
    statuses = sorted(resp.status_code for resp in outcomes)
    assert statuses == [201, 409], [(r.status_code, r.text) for r in outcomes]
    loser = next(r for r in outcomes if r.status_code == 409)
    assert error_of(loser)["code"] == "word_exists"
    assert await _live_words("decide") == ["decide"]


# --------------------------------------------------------------------------- #
# Patching children
# --------------------------------------------------------------------------- #


async def test_a_patch_replaces_the_whole_translation_set_never_merges_it(client):
    created = await _create(client)
    resp = await client.patch(
        f"/api/v1/vocabulary/{created['id']}",
        json={"translations": [{"language": "tr", "value": "geliştirmek"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["language"] for row in body["translations"]] == ["tr"], (
        "a patch that sent one meaning must not leave the two old ones behind"
    )
    assert body["examples"] == created["examples"], "untouched children survive"


async def test_a_patch_that_says_nothing_about_children_keeps_them(client):
    created = await _create(client)
    body = (await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"level": "B2"})).json()
    assert [row["language"] for row in body["translations"]] == ["az", "ru"]
    assert len(body["examples"]) == 1


async def test_an_empty_translation_list_clears_the_meanings(client):
    created = await _create(client)
    body = (await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"translations": []})).json()
    assert body["translations"] == []


async def test_a_blank_meaning_costs_the_teacher_nothing(client):
    """Validation happens before anything is deleted.

    One empty row in the set must not leave the entry holding fewer meanings than it did
    with the request rejected on top of it.
    """
    created = await _create(client)
    resp = await client.patch(
        f"/api/v1/vocabulary/{created['id']}",
        json={
            "translations": [
                {"language": "az", "value": "təkmilləşdirmək"},
                {"language": "ru", "value": "  "},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    assert "empty" in error_of(resp)["message"]
    kept = await _detail(client, created["id"])
    assert [row["language"] for row in kept["translations"]] == ["az", "ru"]


async def test_an_empty_example_sentence_is_refused(client):
    created = await _create(client)
    resp = await client.patch(
        f"/api/v1/vocabulary/{created['id']}", json={"examples": [{"sentence": "   "}]}
    )
    assert resp.status_code == 422
    assert "example needs a sentence" in error_of(resp)["message"]
    assert len((await _detail(client, created["id"]))["examples"]) == 1


async def test_a_word_cannot_be_left_without_a_language(client):
    created = await _create(client)
    resp = await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"learning_language": "   "})
    assert resp.status_code == 422
    assert "learning language" in error_of(resp)["message"]
    assert (await _detail(client, created["id"]))["learning_language"] == "en"


async def test_a_null_word_is_a_clean_422_not_a_crash(client):
    created = await _create(client)
    resp = await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"word": None})
    assert resp.status_code == 422, resp.text
    assert "needs a word" in error_of(resp)["message"]


async def test_a_patch_body_cannot_set_a_status(client):
    """Lifecycle moves through the status endpoint, where the trash rule lives."""
    created = await _create(client)
    resp = await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"status": "draft"})
    assert resp.status_code == 422, resp.text
    assert (await _detail(client, created["id"]))["status"] == "ready"


async def test_a_definition_can_be_cleared_back_to_nothing(client):
    created = await _create(client)
    body = (await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"definition": "   "})).json()
    assert body["definition"] is None
    assert body["notes"] == f"Bring up the comparative form - {TEACHER_ONLY}", (
        "clearing one field must not blank the fields the patch never mentioned"
    )


# --------------------------------------------------------------------------- #
# Lifecycle: draft, ready, archived, trash, restore
# --------------------------------------------------------------------------- #


async def test_a_draft_is_published_from_the_status_endpoint(client):
    created = await _create(client, word="drafted", status="draft")
    assert created["status"] == "draft"
    resp = await client.post(f"/api/v1/vocabulary/{created['id']}/status", json={"status": "ready"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


async def test_an_unknown_status_is_answered_with_the_ones_that_exist(client):
    created = await _create(client)
    resp = await client.post(f"/api/v1/vocabulary/{created['id']}/status", json={"status": "published"})
    assert resp.status_code == 422
    assert "draft" in error_of(resp)["message"]


async def test_trash_is_never_a_status(client):
    created = await _create(client)
    resp = await client.post(f"/api/v1/vocabulary/{created['id']}/status", json={"status": "trash"})
    assert resp.status_code == 422
    assert "deletion, not a status" in error_of(resp)["message"]
    assert (await _detail(client, created["id"]))["deleted_at"] is None


async def test_the_trash_keeps_the_status_and_the_other_views(client):
    created = await _create(client, word="quiet", status="draft")
    resp = await client.delete(f"/api/v1/vocabulary/{created['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["trashed"] == created["id"]

    row = await _detail(client, created["id"])
    assert row["deleted_at"] is not None
    assert row["status"] == "draft", "the trash records a deletion; it never overwrites a state"

    assert (await client.get("/api/v1/vocabulary", params={"view": "bank"})).json()["items"] == []
    trash = await client.get("/api/v1/vocabulary", params={"view": "trash"})
    assert [item["id"] for item in trash.json()["items"]] == [created["id"]]
    assert trash.json()["total"] == 1
    assert (await client.get("/api/v1/vocabulary", params={"view": "all"})).json()["total"] == 1

    # Deleting twice changes nothing further - no second timestamp, no error.
    again = await client.delete(f"/api/v1/vocabulary/{created['id']}")
    assert again.status_code == 200
    assert (await _detail(client, created["id"]))["deleted_at"] == row["deleted_at"]


async def test_a_trashed_word_is_closed_for_editing_but_open_for_restoring(client):
    created = await _create(client, word="locked")
    await client.delete(f"/api/v1/vocabulary/{created['id']}")

    assert (await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"level": "C1"})).status_code == 404
    status = await client.post(f"/api/v1/vocabulary/{created['id']}/status", json={"status": "ready"})
    assert status.status_code == 404
    taxonomy = await client.post(f"/api/v1/vocabulary/{created['id']}/taxonomy", json={"tag_ids": []})
    assert taxonomy.status_code == 404

    restored = await client.post(f"/api/v1/vocabulary/{created['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    assert restored.json()["status"] == "ready", "a draft stays a draft - restore is not a publish"


async def test_restoring_a_word_that_was_replaced_is_refused_not_a_server_error(client):
    """The unique index covers live rows, so a naive restore would violate it.

    Telling the teacher is the whole job: the entry has to stay in the trash exactly as
    it was, so nothing is lost whichever way they choose to merge the two.
    """
    created = await _create(client, word="replace")
    await client.delete(f"/api/v1/vocabulary/{created['id']}")
    newcomer = await _create(client, word="replace", definition="the newer row")

    resp = await client.post(f"/api/v1/vocabulary/{created['id']}/restore")
    assert resp.status_code == 409, resp.text
    message = error_of(resp)["message"]
    assert "merge" in message and "trash" in message, message

    row = await _detail(client, created["id"])
    assert row["deleted_at"] is not None, "a refused restore must leave the entry in the trash"
    assert row["definition"] == "to make something better"
    assert (await _detail(client, newcomer["id"]))["definition"] == "the newer row"
    assert await _live_words("replace") == ["replace"]


async def test_restoring_a_word_that_never_left_changes_nothing(client):
    created = await _create(client, word="never-gone")
    resp = await client.post(f"/api/v1/vocabulary/{created['id']}/restore")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == created["id"]


async def test_an_unknown_entry_id_is_a_404_in_every_shape(client):
    missing = str(uuid.uuid4())
    assert (await client.get(f"/api/v1/vocabulary/{missing}")).status_code == 404
    assert (await client.patch(f"/api/v1/vocabulary/{missing}", json={"level": "A1"})).status_code == 404
    assert (await client.delete(f"/api/v1/vocabulary/{missing}")).status_code == 404
    assert (await client.post(f"/api/v1/vocabulary/{missing}/restore")).status_code == 404
    assert (
        await client.post(f"/api/v1/vocabulary/{missing}/status", json={"status": "ready"})
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/vocabulary/{missing}/taxonomy", json={"tag_ids": []})
    ).status_code == 404
    assert not missing.replace("-", "").isalpha(), "the id used here must look like an id"
    assert (await client.get("/api/v1/vocabulary/not-an-id")).status_code == 422


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


async def _seed_bank(client) -> dict:
    """Four words with deliberately different shapes.

    Levels A1 / A2 / none / B1, one tag, one private note (on 'run'), a draft, and one
    translation language each except 'jog', which carries two - so every filter has a
    different answer and a shared language only the teacher can still see.
    """
    tag_id = await _new_tag(client, "week one")
    created = {
        "answer": await _create(
            client,
            word="answer",
            level="A1",
            part_of_speech="verb",
            definition="to say something back to someone",
            notes=None,
            tag_ids=[tag_id],
            translations=[{"language": "az", "value": "cavab"}],
            examples=[{"sentence": "Answer the question.", "language": "en"}],
        ),
        "carry": await _create(
            client,
            word="carry",
            level="A2",
            part_of_speech="noun",
            definition="to move something from one place to another",
            notes=None,
            translations=[{"language": "ru", "value": "нести"}],
            examples=[],
        ),
        "run": await _create(
            client,
            word="run",
            part_of_speech="verb",
            definition="to move quickly on foot",
            level=None,
            translations=[{"language": "tr", "value": "koşmak"}],
        ),
        "jog": await _create(
            client,
            word="jog",
            status="draft",
            definition="to run slowly for exercise",
            notes=None,
            examples=[],
            translations=[{"language": "az", "value": "cisiq"}, {"language": "ru", "value": "trot"}],
        ),
    }
    return {"ids": {word: row["id"] for word, row in created.items()}, "tag_id": tag_id}


async def test_the_filters_answer_with_exactly_the_rows_they_describe(client):
    seeded = await _seed_bank(client)

    async def words(**params) -> list[str]:
        resp = await client.get("/api/v1/vocabulary", params=params)
        assert resp.status_code == 200, f"{params} -> {resp.status_code}: {resp.text}"
        return sorted(row["word"] for row in resp.json()["items"])

    assert await words() == ["answer", "carry", "jog", "run"]

    assert await words(q="run") == ["jog", "run"], "contains reaches the definition as well as the word"
    assert await words(q="run", q_kind="word_only") == ["run"], (
        "word_only never matches a definition or a teacher's note"
    )
    assert await words(q="RUN", q_kind="exact") == ["run"], "exact is case-insensitive"
    assert await words(q="an", q_kind="starts_with") == ["answer"]
    assert await words(q="nothing like this") == []

    assert await words(level="A1") == ["answer"]
    assert await words(part_of_speech="NOUN") == ["carry"], "part of speech is not case sensitive"
    assert await words(status="draft") == ["jog"]
    assert await words(status="ready") == ["answer", "carry", "run"]
    assert await words(learning_language="en") == ["answer", "carry", "jog", "run"]
    assert await words(learning_language="az") == []
    # 'jog' is a draft but a live row: the teacher's translation filter still finds it,
    # the learner's never does.
    assert await words(translation_language="az") == ["answer", "jog"]
    assert await words(translation_language="ru") == ["carry", "jog"]
    assert await words(translation_language="tr") == ["run"]
    assert await words(translation_language="de") == []
    assert await words(tag_id=seeded["tag_id"]) == ["answer"]
    assert await words(has_audio=True) == [], "nothing in this bank has pronunciation audio yet"
    assert await words(has_audio=False) == ["answer", "carry", "jog", "run"]

    assert seeded["ids"]


async def test_a_rejected_read_names_the_option_that_is_wrong(client):
    await _seed_bank(client)
    for params, needle in (
        ({"view": "archive"}, "view"),
        ({"q": "x", "q_kind": "regex"}, "q_kind"),
        ({"sort": "notes"}, "sort"),
        ({"order": "sideways"}, "order"),
        ({"status": "published"}, "status"),
    ):
        resp = await client.get("/api/v1/vocabulary", params=params)
        assert resp.status_code == 422, f"{params} -> {resp.status_code}"
        assert needle in error_of(resp)["message"]
    assert await _count_live() == 4, "a rejected read must not touch a row"


async def test_sorting_and_pagination_never_repeat_or_drop_a_row(client):
    await _seed_bank(client)

    asc = (await client.get("/api/v1/vocabulary", params={"sort": "word", "order": "asc"})).json()
    assert [row["word"] for row in asc["items"]] == ["answer", "carry", "jog", "run"]
    desc = (await client.get("/api/v1/vocabulary", params={"sort": "word", "order": "desc"})).json()
    assert [row["word"] for row in desc["items"]] == ["run", "jog", "carry", "answer"]

    seen: list[str] = []
    sizes: list[int] = []
    for page in (1, 2, 3):
        body = (
            await client.get("/api/v1/vocabulary", params={"sort": "word", "page": page, "page_size": 2})
        ).json()
        assert body["total"] == 4 and body["page"] == page and body["page_size"] == 2
        sizes.append(len(body["items"]))
        seen.extend(row["word"] for row in body["items"])
    assert sizes == [2, 2, 0]
    assert seen == ["answer", "carry", "jog", "run"], f"paging repeated or dropped a row: {seen}"

    # A column with blanks sorts them last, so the unfinished rows never fall off the
    # bottom of the page a teacher is working through.
    levels = (await client.get("/api/v1/vocabulary", params={"sort": "level", "order": "asc"})).json()
    assert [row["level"] for row in levels["items"]] == ["A1", "A2", "B1", None]

    capped = (await client.get("/api/v1/vocabulary", params={"page_size": 5000})).json()
    assert capped["page_size"] == 200, "the page size is capped, not handed to the database as typed"

    clamped = (await client.get("/api/v1/vocabulary", params={"page": 0, "page_size": 0})).json()
    assert clamped["page"] == 1 and clamped["page_size"] == 1


async def test_a_teachers_private_note_stays_on_the_teacher_side(client, session_factory):
    await _seed_bank(client)

    teacher = await client.get("/api/v1/vocabulary", params={"q": TEACHER_ONLY})
    assert teacher.json()["total"] == 1, "the teacher's own search does reach the notes"

    _profile, learner = await _learner(client, session_factory, "vocab-no-notes")
    student = await learner.get("/api/v1/student/vocabulary", params={"q": TEACHER_ONLY})
    assert student.status_code == 200, student.text
    assert student.json()["total"] == 0, "a note the teacher wrote for herself is not study material"
    assert student.json()["items"] == []


# --------------------------------------------------------------------------- #
# The learner surface
# --------------------------------------------------------------------------- #


async def test_a_learner_sees_only_published_and_alive_words(client, session_factory):
    seeded = await _seed_bank(client)
    archived = await _create(client, word="walk", examples=[])
    await client.post(f"/api/v1/vocabulary/{archived['id']}/status", json={"status": "archived"})
    trashed = await _create(client, word="swim", examples=[])
    await client.delete(f"/api/v1/vocabulary/{trashed['id']}")

    _profile, learner = await _learner(client, session_factory, "vocab-learner")
    body = await learner.get("/api/v1/student/vocabulary")
    assert body.status_code == 200, body.text
    listed = body.json()
    assert sorted(row["word"] for row in listed["items"]) == ["answer", "carry", "run"], (
        "a draft, an archived word and a trashed word are not study material"
    )
    assert listed["total"] == 3
    row = next(item for item in listed["items"] if item["word"] == "answer")
    assert row["translation_languages"] == ["az"] and row["example_count"] == 1
    assert "notes" not in row

    for word, entry_id in (("jog", seeded["ids"]["jog"]), ("walk", archived["id"]), ("swim", trashed["id"])):
        detail = await learner.get(f"/api/v1/student/vocabulary/{entry_id}")
        assert detail.status_code == 404, f"a learner was served the {word} card"
        assert error_of(detail)["code"] == "not_found"

    ready = await learner.get(f"/api/v1/student/vocabulary/{seeded['ids']['run']}")
    assert ready.status_code == 200, ready.text


async def test_a_study_card_hands_over_exactly_what_a_learner_needs(client, session_factory):
    created = await _create(client, tag_ids=[await _new_tag(client, "week one")])
    _profile, learner = await _learner(client, session_factory, "vocab-card-shape")
    card = await learner.get(f"/api/v1/student/vocabulary/{created['id']}")
    assert card.status_code == 200, card.text
    body = card.json()

    assert set(body) == {
        "id",
        "word",
        "learning_language",
        "level",
        "part_of_speech",
        "ipa",
        "definition",
        "synonyms",
        "antonyms",
        "translations",
        "examples",
        "has_audio",
        "tags",
    }
    assert body["word"] == "improve"
    assert body["definition"] == "to make something better"
    assert body["tags"] == [{"id": created["tags"][0]["id"], "name": "week one"}]
    assert body["has_audio"] is False
    for hidden in ("notes", "source_file_id", "audio_asset_id", "deleted_at", "status", "created_at"):
        assert hidden not in body, f"the study card leaked the teacher field '{hidden}'"
    assert not any(TEACHER_ONLY in str(value) for value in body.values()), "the note leaked inside a child row"


async def test_a_learner_can_narrow_a_card_to_one_language(client, session_factory):
    created = await _create(
        client,
        word="bright",
        translations=[
            {"language": "az", "value": "işıqlı"},
            {"language": "ru", "value": "яркий"},
            {"language": "tr", "value": "parlak"},
        ],
        examples=[
            {"sentence": "The room is bright.", "language": "en"},
            {"sentence": "Oda çox işıqlı idi.", "language": "az"},
            {"sentence": "Oda çox işıqlı idi.", "language": None},
            {"sentence": "Odna komnata svetlaya.", "language": "ru"},
        ],
    )
    _profile, learner = await _learner(client, session_factory, "vocab-language-filter")

    everything = await learner.get(f"/api/v1/student/vocabulary/{created['id']}")
    assert [row["language"] for row in everything.json()["translations"]] == ["az", "ru", "tr"]

    azerbaijani = await learner.get(
        f"/api/v1/student/vocabulary/{created['id']}", params={"language": " AZ "}
    )
    assert azerbaijani.status_code == 200, azerbaijani.text
    body = azerbaijani.json()
    assert [row["language"] for row in body["translations"]] == ["az"]
    # A learner reading the card in Azerbaijani still needs the example in the language
    # the word is written in, and one the teacher never labelled.
    assert [row["language"] for row in body["examples"]] == ["en", "az", None]

    empty = await learner.get(
        f"/api/v1/student/vocabulary/{created['id']}", params={"language": "tr"}
    )
    assert [row["language"] for row in empty.json()["examples"]] == ["en", None]


async def test_the_learner_list_filters_are_theirs_to_use(client, session_factory):
    seeded = await _seed_bank(client)
    _profile, learner = await _learner(client, session_factory, "vocab-list-filters")

    async def words(**params) -> list[str]:
        resp = await learner.get("/api/v1/student/vocabulary", params=params)
        assert resp.status_code == 200, f"{params} -> {resp.status_code}: {resp.text}"
        return sorted(row["word"] for row in resp.json()["items"])

    assert await words(q="an") == ["answer"], "a learner's search is a word search"
    assert await words(level="A2") == ["carry"]
    assert await words(tag_id=seeded["tag_id"]) == ["answer"]
    assert await words(translation_language="ru") == ["carry"]
    assert await words(learning_language="en") == ["answer", "carry", "run"]
    assert await words(learning_language="az") == []

    desc = await learner.get("/api/v1/student/vocabulary", params={"sort": "word", "order": "desc"})
    assert [row["word"] for row in desc.json()["items"]] == ["run", "carry", "answer"]

    bad_sort = await learner.get("/api/v1/student/vocabulary", params={"sort": "notes"})
    assert bad_sort.status_code == 422
    assert "sort" in error_of(bad_sort)["message"]
    capped = (await learner.get("/api/v1/student/vocabulary", params={"page_size": 400})).json()
    assert capped["page_size"] == 100


async def test_the_learner_surface_needs_a_learner(client):
    anonymous = await client._c.get("/api/v1/student/vocabulary")
    assert anonymous.status_code == 401
    admin_on_the_student_surface = await client.get("/api/v1/student/vocabulary")
    assert admin_on_the_student_surface.status_code == 401, (
        "an admin cookie is not a learner session: the two surfaces stay separate"
    )
    created = await _create(client, word="closed")
    assert (
        await client.get(f"/api/v1/student/vocabulary/{created['id']}")
    ).status_code == 401


async def test_a_disabled_learner_is_not_served_cards(client, session_factory):
    profile, learner = await _learner(client, session_factory, "vocab-disabled")
    await _create(client, word="sensitive")
    assert (await learner.get("/api/v1/student/vocabulary")).status_code == 200

    # A session that outlives the account - the guard below the API's own bookkeeping,
    # reached here by writing the column without ending the session.
    await _force_student_status(profile["student"]["id"], enums.StudentStatus.DISABLED)
    refused = await learner.get("/api/v1/student/vocabulary")
    assert refused.status_code == 403, refused.text
    assert error_of(refused)["code"] == "forbidden"
    await _force_student_status(profile["student"]["id"], enums.StudentStatus.ACTIVE)

    # The teacher's own route is stricter: disabling also ends the learner's live
    # sessions, so the honest answer to the old cookie is that the session is gone.
    resp = await client.post(f"/api/v1/students/{profile['student']['id']}/status", json={"status": "disabled"})
    assert resp.status_code == 200, resp.text
    ended = await learner.get("/api/v1/student/vocabulary")
    assert ended.status_code == 401, ended.text
    assert error_of(ended)["code"] == "unauthorized"

    await client.post(f"/api/v1/students/{profile['student']['id']}/status", json={"status": "active"})
    assert (await learner.get("/api/v1/student/vocabulary")).status_code == 401, (
        "coming back to active must not resurrect a session the platform ended"
    )
    back = await _learner_session(session_factory, profile["access_key"])
    body = await back.get("/api/v1/student/vocabulary")
    assert body.status_code == 200, body.text
    assert body.json()["total"] == 1


# --------------------------------------------------------------------------- #
# The teacher's preview, and the options a learner filters by
# --------------------------------------------------------------------------- #


async def test_the_teachers_preview_is_the_learners_card(client, session_factory):
    """One projection, two doors.

    The preview is worth having only if it is the learner's card; a second copy of the
    rules would let a broken card pass for a good one on the teacher's screen.
    """
    created = await _create(client)
    _profile, learner = await _learner(client, session_factory, "vocab-preview-parity")

    preview = await client.get(f"/api/v1/vocabulary/{created['id']}/preview")
    assert preview.status_code == 200, preview.text
    card = await learner.get(f"/api/v1/student/vocabulary/{created['id']}")
    assert card.status_code == 200, card.text
    assert preview.json() == card.json(), "the teacher was shown a card no learner can get"

    assert "notes" not in preview.json()
    assert TEACHER_ONLY not in preview.text, "the preview panel leaked the private note"


async def test_a_draft_previews_for_the_teacher_who_wrote_it(client, session_factory):
    """The teacher previews a draft to decide whether to publish it; a learner cannot."""
    draft = await _create(client, word="almost", status="draft")
    preview = await client.get(f"/api/v1/vocabulary/{draft['id']}/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["word"] == "almost"

    _profile, learner = await _learner(client, session_factory, "vocab-preview-draft")
    assert (await learner.get(f"/api/v1/student/vocabulary/{draft['id']}")).status_code == 404
    assert (await learner.get(f"/api/v1/vocabulary/{draft['id']}/preview")).status_code == 401, (
        "the preview belongs to the teacher's surface, not the learner's"
    )


async def test_the_preview_narrows_to_a_language_the_way_a_card_does(client):
    created = await _create(
        client,
        word="sharp",
        translations=[{"language": "az", "value": "sürtmə"}, {"language": "ru", "value": "острый"}],
    )
    narrowed = await client.get(f"/api/v1/vocabulary/{created['id']}/preview", params={"language": " RU "})
    assert narrowed.status_code == 200, narrowed.text
    assert [row["language"] for row in narrowed.json()["translations"]] == ["ru"]


async def test_a_trashed_word_previews_and_an_unknown_one_does_not(client):
    created = await _create(client, word="buried")
    assert (await client.delete(f"/api/v1/vocabulary/{created['id']}")).status_code == 200
    assert (await client.get(f"/api/v1/vocabulary/{created['id']}/preview")).status_code == 200, (
        "the teacher previews a word in the trash to decide whether to restore it"
    )
    missing = await client.get(f"/api/v1/vocabulary/{uuid.uuid4()}/preview")
    assert missing.status_code == 404
    assert error_of(missing)["code"] == "not_found"


async def test_the_learner_gets_the_options_behind_its_own_filters(client, session_factory):
    """The learner's pickers come from the server too, and carry nothing extra."""
    _profile, learner = await _learner(client, session_factory, "vocab-learner-meta")
    resp = await learner.get("/api/v1/student/vocabulary/meta")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"learning_languages", "translation_languages", "levels"}, (
        "a learner picks a language and a level; lifecycle states are not the learner's to set"
    )
    assert body["levels"] == ["A1", "A2", "B1", "B2", "C1", "C2"]
    assert sorted(body["translation_languages"]) == ["az", "ru", "tr"]

    # A literal path, so it must not be read as a word id on the way in.
    assert (await client._c.get("/api/v1/student/vocabulary/meta")).status_code == 401
    assert (await learner.get("/api/v1/vocabulary/meta")).status_code == 401, (
        "the teacher's meta is not the learner's door"
    )


async def test_the_learners_pickers_follow_the_settings_the_teacher_edits(client, session_factory):
    _profile, learner = await _learner(client, session_factory, "vocab-meta-settings")
    before = (await learner.get("/api/v1/student/vocabulary/meta")).json()
    assert before["learning_languages"] == ["en"]

    await _set_learning_languages(client, ["en", "az"])
    after = (await learner.get("/api/v1/student/vocabulary/meta")).json()
    # In the order the teacher saved it: the first language is the one the class works
    # in, and a picker that sorted it alphabetically would lose that.
    assert after["learning_languages"] == ["en", "az"], (
        "the learner's filter list is the same configuration the teacher changed"
    )


# --------------------------------------------------------------------------- #
# Bulk work
# --------------------------------------------------------------------------- #


async def test_bulk_answers_for_every_id_it_was_given(client):
    published = await _create(client, word="bulk one")
    unpublished = await _create(client, word="bulk two")
    await client.delete(f"/api/v1/vocabulary/{unpublished['id']}")
    vanished = str(uuid.uuid4())

    resp = await client.post(
        "/api/v1/vocabulary/bulk",
        json={"entry_ids": [published["id"], unpublished["id"], vanished], "action": "status", "status": "draft"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "status"
    assert body["updated"] == [published["id"]]
    assert [item["id"] for item in body["refused"]] == [unpublished["id"]]
    assert "trash" in body["refused"][0]["reason"]
    assert body["not_found"] == [vanished]

    assert (await _detail(client, published["id"]))["status"] == "draft"
    assert (await _detail(client, unpublished["id"]))["status"] == "ready", (
        "the row that was skipped keeps the state it had"
    )


async def test_bulk_trash_then_restore_reports_a_collision_per_row(client):
    first = await _create(client, word="alpha")
    second = await _create(client, word="beta")
    trashed = await _create(client, word="gamma")
    await client.delete(f"/api/v1/vocabulary/{trashed['id']}")

    trash = await client.post(
        "/api/v1/vocabulary/bulk", json={"entry_ids": [first["id"], second["id"]], "action": "trash"}
    )
    assert sorted(trash.json()["updated"]) == sorted([first["id"], second["id"]])
    assert [item["id"] for item in trash.json()["refused"]] == []

    # Re-add 'alpha' by hand, then try to restore the two at once: only 'beta' can come
    # back, and the answer has to say why the other one could not.
    await _create(client, word="alpha", definition="the replacement")
    restore = await client.post(
        "/api/v1/vocabulary/bulk",
        json={"entry_ids": [first["id"], second["id"], trashed["id"]], "action": "restore"},
    )
    assert restore.status_code == 200, restore.text
    body = restore.json()
    assert sorted(body["updated"]) == sorted([second["id"], trashed["id"]])
    assert [item["id"] for item in body["refused"]] == [first["id"]]
    assert "merge" in body["refused"][0]["reason"]
    assert (await _detail(client, first["id"]))["deleted_at"] is not None
    assert (await _detail(client, second["id"]))["deleted_at"] is None


async def test_bulk_skips_a_row_that_is_already_in_the_trash(client):
    created = await _create(client, word="gone twice")
    await client.delete(f"/api/v1/vocabulary/{created['id']}")
    resp = await client.post(
        "/api/v1/vocabulary/bulk", json={"entry_ids": [created["id"]], "action": "trash"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == []
    assert "already in the trash" in resp.json()["refused"][0]["reason"]


async def test_bulk_tag_actions_do_not_stack(client):
    created = await _create(client, word="tagged")
    tag_id = await _new_tag(client, "bulk tag")
    payload = {"entry_ids": [created["id"]], "action": "add_tag", "tag_id": tag_id}

    for _ in range(2):
        resp = await client.post("/api/v1/vocabulary/bulk", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["updated"] == [created["id"]]
    assert [str(link) for link in await _tag_links(created["id"])] == [tag_id]
    assert (await _detail(client, created["id"]))["tags"][0]["name"] == "bulk tag"

    removed = await client.post(
        "/api/v1/vocabulary/bulk", json={"entry_ids": [created["id"]], "action": "remove_tag", "tag_id": tag_id}
    )
    assert removed.json()["updated"] == [created["id"]]
    assert await _tag_links(created["id"]) == []
    assert (await _detail(client, created["id"]))["tags"] == []


async def test_bulk_needs_what_its_action_needs(client):
    created = await _create(client, word="needy")
    cases = [
        ({"entry_ids": [created["id"]], "action": "status"}, "needs a status"),
        ({"entry_ids": [created["id"]], "action": "add_tag"}, "need a tag_id"),
        ({"entry_ids": [created["id"]], "action": "set_level"}, "needs a level"),
        ({"entry_ids": [created["id"]], "action": "status", "status": "trash"}, "deletion, not a status"),
    ]
    for payload, needle in cases:
        resp = await client.post("/api/v1/vocabulary/bulk", json=payload)
        assert resp.status_code == 422, f"{payload} -> {resp.status_code}: {resp.text}"
        assert needle in error_of(resp)["message"], f"{needle!r} missing from {error_of(resp)['message']!r}"

    # An action the service does not implement is refused by the request body itself,
    # so it never reaches a row.
    invented = await client.post("/api/v1/vocabulary/bulk", json={"entry_ids": [created["id"]], "action": "purge"})
    assert invented.status_code == 422, invented.text
    assert "action" in invented.text

    unknown_tag = await client.post(
        "/api/v1/vocabulary/bulk",
        json={"entry_ids": [created["id"]], "action": "add_tag", "tag_id": str(uuid.uuid4())},
    )
    assert unknown_tag.status_code == 422
    assert "does not exist" in error_of(unknown_tag)["message"]

    empty = await client.post("/api/v1/vocabulary/bulk", json={"entry_ids": [], "action": "trash"})
    assert empty.status_code == 422

    too_many = await client.post(
        "/api/v1/vocabulary/bulk",
        json={"entry_ids": [str(uuid.uuid4()) for _ in range(501)], "action": "trash"},
    )
    assert too_many.status_code == 422, "a selection has a ceiling, so one request cannot lock the bank"
    assert (await _detail(client, created["id"]))["status"] == "ready"


async def test_bulk_level_action_clears_a_level_the_same_way_a_teacher_can(client):
    created = await _create(client, word="leveled", level="B2")
    resp = await client.post(
        "/api/v1/vocabulary/bulk", json={"entry_ids": [created["id"]], "action": "set_level", "level": " "}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == [created["id"]]
    assert (await _detail(client, created["id"]))["level"] is None


# --------------------------------------------------------------------------- #
# Taxonomy and the tags screen
# --------------------------------------------------------------------------- #


async def test_assigning_taxonomy_replaces_the_whole_set(client):
    created = await _create(client, tag_ids=[await _new_tag(client, "old list")])
    keep = await _new_tag(client, "new list")
    resp = await client.post(f"/api/v1/vocabulary/{created['id']}/taxonomy", json={"tag_ids": [keep, keep]})
    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()["tags"]] == [keep]

    cleared = await client.post(f"/api/v1/vocabulary/{created['id']}/taxonomy", json={"tag_ids": []})
    assert cleared.json()["tags"] == []
    unknown = await client.post(
        f"/api/v1/vocabulary/{created['id']}/taxonomy", json={"tag_ids": [str(uuid.uuid4())]}
    )
    assert unknown.status_code == 422
    assert "does not exist" in error_of(unknown)["message"]


async def test_the_tag_list_counts_the_word_bank_separately_from_the_questions(client):
    tag_id = await _new_tag(client, "shared tag")
    created = await _create(client, word="labelled", tag_ids=[tag_id])
    question = await client.post(
        "/api/v1/questions",
        json={
            "type": "multiple_choice",
            "prompt": "Which one means 'labelled'?",
            "config": {"options": [{"text": "a", "correct": True}, {"text": "b"}]},
            "tag_ids": [tag_id],
        },
    )
    assert question.status_code == 201, question.text

    listed = await client.get("/api/v1/tags")
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json()["items"] if item["id"] == tag_id)
    assert row["question_count"] == 1
    assert row["vocabulary_count"] == 1, "the teacher must see which bank is holding the tag"

    edited = await client.patch(f"/api/v1/tags/{tag_id}", json={"color": "#123456"})
    assert edited.json()["vocabulary_count"] == 1

    refused = await client.delete(f"/api/v1/tags/{tag_id}")
    assert refused.status_code == 409
    message = error_of(refused)["message"]
    assert "question" in message and "vocabulary" in message, message

    # Let the question go first, then trash the word: a trashed entry still carries its
    # label, because restoring it has to bring the label back, so the trash never frees
    # a tag. Unlabelling does.
    await client.post(f"/api/v1/questions/{question.json()['id']}/taxonomy", json={"tag_ids": []})
    await client.delete(f"/api/v1/vocabulary/{created['id']}")
    held = error_of(await client.delete(f"/api/v1/tags/{tag_id}"))["message"]
    assert "vocabulary" in held and "question" not in held, held

    await client.post(f"/api/v1/vocabulary/{created['id']}/restore")
    await client.post(f"/api/v1/vocabulary/{created['id']}/taxonomy", json={"tag_ids": []})
    freed = next(item for item in (await client.get("/api/v1/tags")).json()["items"] if item["id"] == tag_id)
    assert freed["question_count"] == 0 and freed["vocabulary_count"] == 0
    gone = await client.delete(f"/api/v1/tags/{tag_id}")
    assert gone.status_code == 200, gone.text


async def test_a_tag_used_only_by_the_word_bank_is_named_as_such(client):
    tag_id = await _new_tag(client, "word list only")
    await _create(client, word="held", tag_ids=[tag_id])
    resp = await client.delete(f"/api/v1/tags/{tag_id}")
    assert resp.status_code == 409
    message = error_of(resp)["message"]
    assert "vocabulary entry" in message and "question" not in message, message


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #


async def test_every_vocabulary_write_leaves_an_audit_trail(client):
    created = await _create(client, word="watched")
    tag_id = await _new_tag(client, "watched list")

    await client.patch(f"/api/v1/vocabulary/{created['id']}", json={"level": "B2"})
    await client.post(f"/api/v1/vocabulary/{created['id']}/status", json={"status": "draft"})
    await client.delete(f"/api/v1/vocabulary/{created['id']}")
    await client.post(f"/api/v1/vocabulary/{created['id']}/restore")
    await client.post(f"/api/v1/vocabulary/{created['id']}/taxonomy", json={"tag_ids": [tag_id]})
    await client.post(
        "/api/v1/vocabulary/bulk", json={"entry_ids": [created["id"]], "action": "set_level", "level": "C1"}
    )

    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.target_id == uuid.UUID(created["id"]))))
            .scalars()
            .all()
        )
        # A bulk call is one operation over a set of entries, so it is not filed
        # against any single one of them.
        bulk_rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action.like("vocabulary.bulk.%"))))
            .scalars()
            .all()
        )
    by_action = {row.action: row for row in rows}
    assert len(rows) == 6 == len(by_action), "one write, one entry in the trail"
    assert sorted(by_action) == [
        "vocabulary.created",
        "vocabulary.restored",
        "vocabulary.status.changed",
        "vocabulary.taxonomy.assigned",
        "vocabulary.trashed",
        "vocabulary.updated",
    ]
    assert by_action["vocabulary.created"].after["word"] == "watched"
    assert by_action["vocabulary.updated"].after["fields"] == ["level"]
    assert by_action["vocabulary.status.changed"].before["status"] == "ready"
    assert by_action["vocabulary.status.changed"].after["status"] == "draft"
    assert by_action["vocabulary.trashed"].after["word"] == "watched"
    assert by_action["vocabulary.restored"].after["status"] == "draft", (
        "the restore records the state the word came back in"
    )
    assert len(bulk_rows) == 1, "a set is audited as one call, not one row per entry"
    assert bulk_rows[0].target_id is None
    assert bulk_rows[0].action == "vocabulary.bulk.set_level"
    assert bulk_rows[0].after == {
        "requested": 1,
        "updated": 1,
        "refused": 0,
        "not_found": 0,
    }, "the counts have to say what actually happened"


async def test_a_refused_write_leaves_no_audit_lie(client):
    created = await _create(client, word="honest")
    duplicate = await client.post("/api/v1/vocabulary", json=word_body(word="Honest"))
    assert duplicate.status_code == 409

    async with SessionLocal() as db:
        actions = (
            (
                await db.execute(
                    select(AuditLog.action).where(AuditLog.action.like("vocabulary.%"))
                )
            )
            .scalars()
            .all()
        )
    assert actions.count("vocabulary.created") == 1, f"the rejected request was audited as a success: {actions}"
    assert created["word"] == "honest"
