"""The question bank over HTTP, against real Postgres.

What the offline engine tests cannot prove: that a version row really is immutable in
the database, that the trash keeps a status, that filters and pagination agree with the
rows, that every write path is guarded by the session and CSRF, and that an answer key
never reaches the learner-facing endpoint.
"""
from __future__ import annotations

import json
import uuid

from helpers import error_of
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.content import Listening, MediaAsset, Question, QuestionVersion, Reading
from app.models.ops import AuditLog

# Text that only ever belongs inside an answer key. If it shows up anywhere in a
# learner-facing payload, the test that looked for it fails loudly.
SECRET = "ZULU-ANSWER-KEY"


def choice_body(**over) -> dict:
    body = {
        "type": "multiple_choice",
        "prompt": "Which word means 'kite'?",
        "config": {
            "options": [
                {"text": "apple"},
                {"text": SECRET, "correct": True},
                {"text": "table"},
            ]
        },
        "score": 2,
        "level": "A2",
        "learning_language": "en",
        "difficulty": 3,
    }
    body.update(over)
    return body


def minimal_bodies() -> dict[str, dict]:
    """At least one valid payload per registered type, so no type is only tested offline."""
    return {
        "multiple_choice": choice_body(),
        "multi_select": {
            "type": "multi_select",
            "prompt": "Pick the fruits.",
            "config": {
                "options": [
                    {"text": "pear", "correct": True},
                    {"text": "hammer"},
                    {"text": "plum", "correct": True},
                    {"text": "ruler"},
                ]
            },
            "partial_scoring": {"mode": "partial"},
        },
        "true_false": {
            "type": "true_false",
            "prompt": "Is this right?",
            "config": {"statement": "Baku is on the Caspian coast.", "correct": True},
        },
        "short_answer": {
            "type": "short_answer",
            "prompt": "What is the opposite of 'young'?",
            "config": {"accepted": [SECRET], "max_characters": 30, "hint": "one word"},
        },
        "gap_fill": {
            "type": "gap_fill",
            "prompt": "Complete the sentence.",
            "config": {
                "text": "He ___ the newspaper every ___",
                "blanks": [{"accepted": ["reads"]}, {"accepted": ["evening", SECRET], "label": "time"}],
                "word_bank": ["reads", "evening", "runs"],
            },
        },
        "matching": {
            "type": "matching",
            "prompt": "Match the words.",
            "config": {
                "pairs": [{"left": "sea", "right": "derya"}, {"left": "land", "right": "torpaq"}],
                "extra_rights": ["rabitə"],
            },
        },
        "ordering": {
            "type": "ordering",
            "prompt": "Put the story in order.",
            "config": {"items": ["The end", "In the beginning", "Once upon a time"]},
        },
        "translation": {
            "type": "translation",
            "prompt": "Translate into Azerbaijani.",
            "config": {"source": "Good morning", "accepted": [SECRET], "max_words": 6},
        },
        "essay": {
            "type": "essay",
            "prompt": "Write about your last holiday.",
            "config": {"min_words": 40, "max_words": 300, "guidance": "Use past tenses."},
            "score": 10,
        },
    }


def secret_material(question_type: str) -> list[str]:
    """The strings that are answer-key material for this type.

    For a choice question the key is which option is marked correct, not the option
    text (a learner has to see the texts to answer), so those types are covered by the
    structural assertions in `test_preview_never_carries_an_answer_key` instead.
    """
    keys = {
        "short_answer": [SECRET],  # the accepted spellings
        "gap_fill": [SECRET],  # an accepted variant of a blank
        "translation": [SECRET],  # the accepted translation
    }
    return keys.get(question_type, [])


async def _create(client, body) -> dict:
    resp = await client.post("/api/v1/questions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _row(question_id: str) -> dict:
    async with SessionLocal() as db:
        row = await db.get(Question, uuid.UUID(question_id))
        assert row is not None, f"question {question_id} is gone from the database"
        return {
            "status": row.status.value,
            "type": row.type,
            "prompt": row.prompt,
            "score": row.score,
            "level": row.level,
            "learning_language": row.learning_language,
            "current_version": row.current_version,
            "deleted_at": row.deleted_at,
            "config": dict(row.config or {}),
        }


async def _version_rows(question_id: str) -> list[dict]:
    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(QuestionVersion)
                    .where(QuestionVersion.question_id == uuid.UUID(question_id))
                    .order_by(QuestionVersion.version)
                )
            )
            .scalars()
            .all()
        )
        return [{"version": row.version, "snapshot": dict(row.snapshot), "note": row.change_note} for row in rows]


async def _new_reading(**over) -> str:
    fields = {"title": "A quiet harbour", "body": "The boats waited.", "language": "en", "level": "B1"}
    fields.update(over)
    async with SessionLocal() as db:
        row = Reading(**fields)
        db.add(row)
        await db.commit()
        return str(row.id)


async def _new_listening(**over) -> str:
    fields = {"title": "At the station", "transcript": "The train is late."}
    fields.update(over)
    async with SessionLocal() as db:
        row = Listening(**fields)
        db.add(row)
        await db.commit()
        return str(row.id)


async def _new_media_asset(**over) -> str:
    fields = {"kind": "image", "storage_key": "question-bank/test-image.png", "mime_type": "image/png"}
    fields.update(over)
    async with SessionLocal() as db:
        row = MediaAsset(**fields)
        db.add(row)
        await db.commit()
        return str(row.id)


async def _new_topic(client, name: str, parent_id: str | None = None) -> str:
    resp = await client.post("/api/v1/topics", json={"name": name, "parent_id": parent_id})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _new_tag(client, name: str) -> str:
    resp = await client.post("/api/v1/tags", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _detail(client, question_id: str) -> dict:
    resp = await client.get(f"/api/v1/questions/{question_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Registry, auth and CSRF
# --------------------------------------------------------------------------- #


async def test_type_registry_describes_every_type_to_the_editor(client):
    resp = await client.get("/api/v1/questions/types")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(body["items"]) == 9
    by_type = {item["type"]: item for item in body["items"]}
    assert set(by_type) == set(minimal_bodies())
    for key, item in by_type.items():
        assert item["group"] and item["answer_widget"]
        assert "label" not in item, "the screen puts the code into the learner's language"
        assert item["config_schema"]["type"] == "object"
        assert item["config_schema"]["properties"]
        assert isinstance(item["gradable_automatically"], bool)
        assert item["supports_partial"] == (key in ("multi_select", "gap_fill", "matching", "ordering"))


async def test_the_bank_is_admin_only(client, session_factory):
    anonymous = session_factory()._c
    for path in ("/api/v1/questions", "/api/v1/questions/types", "/api/v1/topics", "/api/v1/tags"):
        resp = await anonymous.get(path)
        assert resp.status_code == 401, f"GET {path} -> {resp.status_code}"
        assert error_of(resp)["code"] == "unauthorized"
    # A POST with no session cookie is not a CSRF risk, so it reaches the auth layer
    # and is refused there - the answer must still be 401, not a silent 403.
    created = await anonymous.post("/api/v1/questions", json=choice_body())
    assert created.status_code == 401
    assert error_of(created)["code"] == "unauthorized"

    # Students are created by an admin; the learner then logs in on their own session.
    student = await client.post(
        "/api/v1/students", json={"name": "Ada", "surname": "L", "username": "ada-bank"}
    )
    assert student.status_code == 201, student.text
    student_session = session_factory()
    await student_session.login_student(student.json()["access_key"])
    for path in ("/api/v1/questions", "/api/v1/questions/types", "/api/v1/topics", "/api/v1/tags"):
        resp = await student_session.get(path)
        assert resp.status_code == 401, f"a student reached {path}"
    refused = await student_session.post("/api/v1/questions", json=choice_body())
    assert refused.status_code == 401
    assert (await client.get("/api/v1/questions")).json()["total"] == 0, "a refused write must leave no row"


async def test_bank_writes_require_the_csrf_header(client):
    await client.login_admin()
    resp = await client._c.post("/api/v1/questions", json=choice_body())
    assert resp.status_code == 403
    assert error_of(resp)["code"] == "csrf_failed"
    # Nothing was written by the rejected request.
    assert (await client.get("/api/v1/questions")).json()["total"] == 0


# --------------------------------------------------------------------------- #
# Creation and validation
# --------------------------------------------------------------------------- #


async def test_every_registered_type_can_be_created_and_read_back(client):
    for question_type, body in minimal_bodies().items():
        created = await _create(client, body)
        assert created["type"] == question_type
        assert created["current_version"] == 1
        assert created["status"] == "ready"
        assert created["deleted_at"] is None

        assert (await _detail(client, created["id"]))["config"] == created["config"]

        rows = (await client.get(f"/api/v1/questions/{created['id']}/versions")).json()["items"]
        assert [row["version"] for row in rows] == [1]
        assert rows[0]["snapshot"]["snapshot_schema"] == 1
        assert rows[0]["snapshot"]["question"]["type"] == question_type

        listed = await client.get("/api/v1/questions", params={"type": question_type})
        assert [item["id"] for item in listed.json()["items"]] == [created["id"]]


async def test_config_is_canonicalised_before_it_is_stored(client):
    created = await _create(
        client,
        {
            "type": "gap_fill",
            "prompt": "Fill the gap.",
            "config": {"text": "I ___ a letter", "blanks": [{"accepted": ["wrote"]}]},
        },
    )
    config = created["config"]
    assert config["word_bank"] == []
    assert set(config["normalization"]) == {
        "case_insensitive",
        "trim",
        "collapse_spaces",
        "ignore_punctuation",
        "ignore_articles",
        "ignore_diacritics",
    }
    assert config["blanks"][0]["label"] is None
    stored = await _row(created["id"])
    assert stored["config"] == config


async def test_an_invalid_config_is_refused_with_the_engine_message(client):
    resp = await client.post(
        "/api/v1/questions",
        json={"type": "multiple_choice", "prompt": "No correct option.", "config": {"options": [{"text": "a"}, {"text": "b"}]}},
    )
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "validation_failed"
    assert "exactly one correct" in err["message"]


async def test_an_unknown_type_is_refused(client):
    resp = await client.post(
        "/api/v1/questions", json={"type": "guess_the_word", "prompt": "x", "config": {"words": []}}
    )
    assert resp.status_code == 422
    assert "Unknown question type" in error_of(resp)["message"]


async def test_unknown_taxonomy_and_media_references_are_refused(client):
    missing = str(uuid.uuid4())
    # The two refusals are worded differently on purpose: a taxonomy id is just not there,
    # while a missing file has a next step the teacher can take.
    cases = (
        ({"topic_ids": [missing]}, f"topic '{missing}' does not exist"),
        ({"tag_ids": [missing]}, f"tag '{missing}' does not exist"),
        ({"media_asset_id": missing}, "no file with that id is in the library"),
    )
    for extra, phrase in cases:
        resp = await client.post("/api/v1/questions", json={**choice_body(), **extra})
        assert resp.status_code == 422, extra
        assert phrase in error_of(resp)["message"], error_of(resp)["message"]

    edited = await _create(client, choice_body())
    resp = await client.patch(f"/api/v1/questions/{edited['id']}", json={"topic_ids": [missing]})
    assert resp.status_code == 422
    assert "topic" in error_of(resp)["message"]
    # The refused patch left the question exactly as it was, at its original version.
    assert (await _row(edited["id"]))["current_version"] == 1

    assert (await client.get("/api/v1/questions", params={"view": "all"})).json()["total"] == 1, (
        "a refused create must not leave a row"
    )


async def test_scoring_knobs_must_suit_the_type(client):
    resp = await client.post("/api/v1/questions", json={**choice_body(), "partial_scoring": {"mode": "partial"}})
    assert resp.status_code == 422
    assert "no partial credit" in error_of(resp)["message"]

    resp = await client.post("/api/v1/questions", json={**choice_body(), "negative_scoring": {"penalty": 0.5}})
    assert resp.status_code == 422
    assert "no wrong answers to penalise" in error_of(resp)["message"]

    assert (await client.post("/api/v1/questions", json={**choice_body(), "score": 0})).status_code == 422
    assert (await client.post("/api/v1/questions", json={**choice_body(), "difficulty": 11})).status_code == 422

    allowed = await _create(
        client,
        {
            **minimal_bodies()["multi_select"],
            "partial_scoring": {"mode": "all_or_nothing"},
            "negative_scoring": {"penalty": 0.25},
        },
    )
    assert allowed["partial_scoring"] == {"mode": "all_or_nothing"}
    assert allowed["negative_scoring"] == {"penalty": 0.25}


async def test_a_question_is_created_at_version_one_and_written_once(client):
    created = await _create(client, choice_body(change_note="First draft"))
    rows = await _version_rows(created["id"])
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["note"] == "First draft"
    assert rows[0]["snapshot"]["question"]["prompt"] == choice_body()["prompt"]


# --------------------------------------------------------------------------- #
# Versions: append-only history
# --------------------------------------------------------------------------- #


async def test_an_edit_appends_a_version_and_leaves_the_older_one_untouched(client):
    created = await _create(client, choice_body())
    edited = await client.patch(
        f"/api/v1/questions/{created['id']}",
        json={"prompt": "Which word means 'kitten'?", "change_note": "Fixed the prompt"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["current_version"] == 2
    assert edited.json()["prompt"] == "Which word means 'kitten'?"

    rows = await _version_rows(created["id"])
    assert [row["version"] for row in rows] == [1, 2]
    # Version 1 is the question as it was, not a view of the current row.
    assert rows[0]["snapshot"]["question"]["prompt"] == choice_body()["prompt"]
    assert rows[1]["snapshot"]["question"]["prompt"] == "Which word means 'kitten'?"

    through_api = await client.get(f"/api/v1/questions/{created['id']}/versions/1")
    assert through_api.json()["snapshot"] == rows[0]["snapshot"]
    # Version 1 is the creation itself, so its note is the creation note - not the
    # note of the edit that produced version 2.
    assert through_api.json()["change_note"] == "Created"
    assert (await client.get(f"/api/v1/questions/{created['id']}/versions/2")).json()["change_note"] == "Fixed the prompt"
    assert through_api.json()["question_id"] == created["id"]
    assert through_api.json()["version"] == 1


async def test_an_edit_that_changes_nothing_does_not_version(client):
    created = await _create(client, choice_body())
    unchanged = await client.patch(
        f"/api/v1/questions/{created['id']}",
        json={
            "prompt": created["prompt"],
            "config": created["config"],
            "score": created["score"],
            "level": created["level"],
            "difficulty": created["difficulty"],
            "learning_language": created["learning_language"],
            "explanation": created["explanation"],
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["current_version"] == 1
    assert len(await _version_rows(created["id"])) == 1

    assert (await client.patch(f"/api/v1/questions/{created['id']}", json={})).json()["current_version"] == 1


async def test_classification_and_lifecycle_edits_do_not_version(client):
    topic = await _new_topic(client, "Vocabulary")
    created = await _create(client, choice_body())
    assigned = await client.post(f"/api/v1/questions/{created['id']}/taxonomy", json={"topic_ids": [topic]})
    assert assigned.status_code == 200
    assert [t["name"] for t in assigned.json()["topics"]] == ["Vocabulary"]
    assert assigned.json()["current_version"] == 1
    assert (await _row(created["id"]))["current_version"] == 1

    status = await client.post(f"/api/v1/questions/{created['id']}/status", json={"status": "archived"})
    assert status.json()["status"] == "archived"
    assert status.json()["current_version"] == 1
    assert len(await _version_rows(created["id"])) == 1


async def test_changing_the_type_without_its_config_is_refused(client):
    created = await _create(client, choice_body())
    resp = await client.patch(f"/api/v1/questions/{created['id']}", json={"type": "essay"})
    assert resp.status_code == 422
    assert "config" in error_of(resp)["message"]
    assert (await _row(created["id"]))["current_version"] == 1


async def test_replacing_the_type_and_config_versions_the_question(client):
    created = await _create(client, choice_body())
    resp = await client.patch(
        f"/api/v1/questions/{created['id']}",
        json={"type": "short_answer", "config": {"accepted": ["forty"]}, "change_note": "Rebuilt as an open question"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "short_answer"
    assert body["current_version"] == 2
    assert body["config"]["accepted"] == ["forty"]
    assert body["config"]["normalization"]["case_insensitive"] is True

    rows = await _version_rows(created["id"])
    assert rows[0]["snapshot"]["question"]["type"] == "multiple_choice"
    assert rows[1]["snapshot"]["question"]["type"] == "short_answer"
    assert rows[1]["note"] == "Rebuilt as an open question"


async def test_retyping_clears_the_scoring_knobs_the_new_type_has_not(client):
    """What the editor really sends: type, its config, and both scoring objects.

    A patch that leaves `partial_scoring` out keeps the previous type's value on the row,
    and a question type with no partial credit may not carry one - so without the empty
    objects a teacher could never retype a question away from a gradable type.
    """
    bodies = minimal_bodies()
    created = await _create(
        client,
        {
            **bodies["multi_select"],
            "partial_scoring": {"mode": "all_or_nothing"},
            "negative_scoring": {"penalty": 0.25},
        },
    )
    stored = await _detail(client, created["id"])
    assert stored["partial_scoring"] == {"mode": "all_or_nothing"}
    assert stored["negative_scoring"] == {"penalty": 0.25}

    half_written = await client.patch(
        f"/api/v1/questions/{created['id']}", json={"type": "essay", "config": bodies["essay"]["config"]}
    )
    assert half_written.status_code == 422, half_written.text
    assert "no partial credit" in error_of(half_written)["message"]
    assert (await _row(created["id"]))["current_version"] == 1, "a refused patch writes no version"

    retyped = await client.patch(
        f"/api/v1/questions/{created['id']}",
        json={"type": "essay", "config": bodies["essay"]["config"], "partial_scoring": {}, "negative_scoring": {}},
    )
    assert retyped.status_code == 200, retyped.text
    stored = await _detail(client, created["id"])
    assert stored["type"] == "essay"
    assert stored["partial_scoring"] == {}
    assert stored["negative_scoring"] == {}

    rows = await _version_rows(created["id"])
    assert rows[0]["snapshot"]["question"]["negative_scoring"] == {"penalty": 0.25}, "v1 stays frozen"
    assert rows[1]["snapshot"]["question"]["partial_scoring"] == {}


async def test_versions_are_contiguous_and_never_reused(client):
    created = await _create(client, choice_body())
    for index in range(3):
        resp = await client.patch(f"/api/v1/questions/{created['id']}", json={"teacher_notes": f"note {index}"})
        assert resp.json()["current_version"] == index + 2
    rows = await _version_rows(created["id"])
    assert [row["version"] for row in rows] == [1, 2, 3, 4]

    listed = (await client.get(f"/api/v1/questions/{created['id']}/versions")).json()
    assert listed["total"] == 4
    # Newest first, because that is the order a history panel shows.
    assert [row["version"] for row in listed["items"]] == [4, 3, 2, 1]

    missing = await client.get(f"/api/v1/questions/{created['id']}/versions/99")
    assert missing.status_code == 404
    assert error_of(missing)["code"] == "not_found"


async def test_a_snapshot_freezes_the_taxonomy_it_was_taken_with(client):
    topic = await _new_topic(client, "Grammar")
    tag = await _new_tag(client, "irregular")
    created = await _create(client, choice_body(topic_ids=[topic], tag_ids=[tag]))
    first = (await client.get(f"/api/v1/questions/{created['id']}/versions/1")).json()["snapshot"]
    assert [t["name"] for t in first["topics"]] == ["Grammar"]
    assert [t["name"] for t in first["tags"]] == ["irregular"]

    await client.post(f"/api/v1/questions/{created['id']}/taxonomy", json={"topic_ids": [], "tag_ids": []})
    again = (await client.get(f"/api/v1/questions/{created['id']}/versions/1")).json()["snapshot"]
    assert again == first, "version 1 must not follow the live classification"
    assert (await _detail(client, created["id"]))["topics"] == []


# --------------------------------------------------------------------------- #
# Status, trash, restore, clone
# --------------------------------------------------------------------------- #


async def test_lifecycle_status_transitions(client):
    created = await _create(client, choice_body(status="draft"))
    assert created["status"] == "draft"
    for target in ("ready", "archived", "draft"):
        resp = await client.post(f"/api/v1/questions/{created['id']}/status", json={"status": target})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == target

    bogus = await client.post(f"/api/v1/questions/{created['id']}/status", json={"status": "published"})
    assert bogus.status_code == 422
    message = error_of(bogus)["message"]
    assert "status must be one of" in message
    # The message lists the real vocabulary, without offering trash as an option.
    assert "draft" in message and "ready" in message and "archived" in message
    assert "trash" not in message, "trash is reachable through DELETE, not through a status"


async def test_status_endpoint_refuses_trash_as_a_status(client):
    created = await _create(client, choice_body())
    resp = await client.post(f"/api/v1/questions/{created['id']}/status", json={"status": "trash"})
    assert resp.status_code == 422
    assert "trash is a deletion, not a status" in error_of(resp)["message"]
    row = await _row(created["id"])
    assert row["deleted_at"] is None and row["status"] == "ready"


async def test_trash_keeps_the_status_and_restore_is_exact(client):
    created = await _create(client, choice_body(status="archived"))
    trashed = await client.delete(f"/api/v1/questions/{created['id']}")
    assert trashed.status_code == 200, trashed.text
    assert trashed.json()["status"] == "archived"

    row = await _row(created["id"])
    assert row["status"] == "archived" and row["deleted_at"] is not None and row["current_version"] == 1

    assert (await client.get("/api/v1/questions", params={"view": "bank"})).json()["total"] == 0
    trash_view = (await client.get("/api/v1/questions", params={"view": "trash"})).json()
    assert [item["id"] for item in trash_view["items"]] == [created["id"]]
    assert trash_view["items"][0]["status"] == "archived"
    assert (await client.get("/api/v1/questions", params={"view": "all"})).json()["total"] == 1

    # A trashed question is not editable, but it is still readable by id.
    assert (await client.get(f"/api/v1/questions/{created['id']}", params={"include_trash": False})).status_code == 404
    assert (await _detail(client, created["id"]))["deleted_at"] is not None
    assert (await client.patch(f"/api/v1/questions/{created['id']}", json={"prompt": "x"})).status_code == 404
    assert (await client.post(f"/api/v1/questions/{created['id']}/status", json={"status": "ready"})).status_code == 404
    assert (await client.post(f"/api/v1/questions/{created['id']}/clone")).status_code == 404
    assert (await client.get(f"/api/v1/questions/{created['id']}/preview")).status_code == 404
    assert (await client.post(f"/api/v1/questions/{created['id']}/taxonomy", json={"topic_ids": []})).status_code == 404

    again = await client.delete(f"/api/v1/questions/{created['id']}")
    assert again.status_code == 200, "trashing twice is not an error"
    assert (await _row(created["id"]))["current_version"] == 1

    restored = await client.post(f"/api/v1/questions/{created['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert restored.json()["status"] == "archived", "restore must return the status the question had"
    assert (await _row(created["id"]))["deleted_at"] is None
    assert (await client.post(f"/api/v1/questions/{created['id']}/restore")).json()["status"] == "archived"

    # Now it is editable again, and the status endpoint accepts it.
    assert (await client.patch(f"/api/v1/questions/{created['id']}", json={"prompt": "Back"})).json()["current_version"] == 2


async def test_unknown_and_malformed_ids_are_404_not_500(client):
    assert (await client.get(f"/api/v1/questions/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/api/v1/questions/not-a-uuid")).status_code == 422
    assert (await client.post(f"/api/v1/questions/{uuid.uuid4()}/grade", json={"response": {"option_index": 0}})).status_code == 404
    assert (await client.delete(f"/api/v1/questions/{uuid.uuid4()}")).status_code == 404
    assert (await client.post(f"/api/v1/questions/{uuid.uuid4()}/restore")).status_code == 404
    assert (await client.get(f"/api/v1/questions/{uuid.uuid4()}/versions")).status_code == 404


async def test_cloning_produces_an_independent_question_at_version_one(client):
    topic = await _new_topic(client, "Reading skills")
    tag = await _new_tag(client, "exam-style")
    original = await _create(client, choice_body(topic_ids=[topic], tag_ids=[tag], explanation="because it fits"))
    clone = await client.post(f"/api/v1/questions/{original['id']}/clone")
    assert clone.status_code == 201, clone.text
    body = clone.json()
    assert body["id"] != original["id"]
    assert body["cloned_from"] == original["id"]
    assert body["current_version"] == 1
    assert body["status"] == "draft"
    assert body["config"] == original["config"]
    assert body["explanation"] == "because it fits"
    assert [t["name"] for t in body["topics"]] == ["Reading skills"]
    assert [t["name"] for t in body["tags"]] == ["exam-style"]

    # The clone's history belongs to the clone.
    assert [row["version"] for row in await _version_rows(body["id"])] == [1]
    assert (await _version_rows(original["id"]))[0]["snapshot"]["question"]["id"] == original["id"]

    edited = await client.patch(f"/api/v1/questions/{body['id']}", json={"prompt": "A different prompt"})
    assert edited.json()["current_version"] == 2
    assert (await _row(original["id"]))["prompt"] == choice_body()["prompt"]
    assert (await _row(original["id"]))["current_version"] == 1

    ready_clone = await client.post(f"/api/v1/questions/{original['id']}/clone", params={"status": "ready"})
    assert ready_clone.json()["status"] == "ready"
    bad = await client.post(f"/api/v1/questions/{original['id']}/clone", params={"status": "trashed"})
    assert bad.status_code == 422


# --------------------------------------------------------------------------- #
# Listing: filters, search, sort, pagination
# --------------------------------------------------------------------------- #


async def _library(client) -> dict:
    """A small, deliberately varied bank; returns the ids it created."""
    ids: dict[str, str] = {}
    ids["mc_a2"] = (await _create(client, choice_body(prompt="Which word means kit?")))["id"]
    ids["mc_b1"] = (await _create(client, choice_body(prompt="Which verb fits?", level="B1", status="draft")))["id"]
    ids["sa"] = (await _create(client, minimal_bodies()["short_answer"]))["id"]
    ids["essay"] = (await _create(client, minimal_bodies()["essay"]))["id"]
    ids["topic"] = await _new_topic(client, "Verbs")
    ids["tag"] = await _new_tag(client, "phrasal")
    await client.post(
        f"/api/v1/questions/{ids['mc_b1']}/taxonomy", json={"topic_ids": [ids["topic"]], "tag_ids": [ids["tag"]]}
    )
    await client.patch(f"/api/v1/questions/{ids['essay']}", json={"teacher_notes": "needs review"})
    return ids


async def test_filters_narrow_the_bank(client):
    ids = await _library(client)

    async def ids_for(**params) -> list[str]:
        body = (await client.get("/api/v1/questions", params=params)).json()
        return sorted(item["id"] for item in body["items"])

    assert await ids_for(type="multiple_choice") == sorted([ids["mc_a2"], ids["mc_b1"]])
    assert await ids_for(status="draft") == [ids["mc_b1"]]
    assert await ids_for(level="A2") == [ids["mc_a2"]]
    assert await ids_for(learning_language="en") == sorted([ids["mc_a2"], ids["mc_b1"]])
    assert await ids_for(context_kind="independent") == sorted([ids["mc_a2"], ids["mc_b1"], ids["sa"], ids["essay"]])
    assert await ids_for(topic_id=ids["topic"]) == [ids["mc_b1"]]
    assert await ids_for(tag_id=ids["tag"]) == [ids["mc_b1"]]
    assert await ids_for(has_media=False) == sorted([ids["mc_a2"], ids["mc_b1"], ids["sa"], ids["essay"]])
    assert await ids_for(has_media=True) == []
    assert await ids_for(view="trash") == []
    assert await ids_for(view="all") == sorted([ids["mc_a2"], ids["mc_b1"], ids["sa"], ids["essay"]])
    # A trashed question leaves the bank but does not disappear from it.
    await client.delete(f"/api/v1/questions/{ids['sa']}")
    assert await ids_for(view="bank") == sorted([ids["mc_a2"], ids["mc_b1"], ids["essay"]])
    assert await ids_for(view="trash") == [ids["sa"]]

    combined = await client.get("/api/v1/questions", params={"type": "multiple_choice", "level": "B1"})
    assert [item["id"] for item in combined.json()["items"]] == [ids["mc_b1"]]


async def test_search_reaches_prompt_explanation_and_notes(client):
    ids = await _library(client)
    assert (await client.get("/api/v1/questions", params={"q": "verb"})).json()["total"] == 1
    assert (await client.get("/api/v1/questions", params={"q": "VERB"})).json()["total"] == 1
    assert (await client.get("/api/v1/questions", params={"q": "needs review"})).json()["total"] == 1
    assert (await client.get("/api/v1/questions", params={"q": "no such text"})).json()["total"] == 0

    await client.patch(f"/api/v1/questions/{ids['sa']}", json={"explanation": "Opposite of young is old."})
    assert (await client.get("/api/v1/questions", params={"q": "Opposite of young"})).json()["total"] == 1


async def test_summary_rows_carry_what_the_table_shows(client):
    topic = await _new_topic(client, "Verbs")
    tag = await _new_tag(client, "phrasal")
    created = await _create(client, choice_body(topic_ids=[topic], tag_ids=[tag]))
    row = (await client.get("/api/v1/questions")).json()["items"][0]
    assert row["topic_names"] == ["Verbs"]
    assert row["tag_names"] == ["phrasal"]
    assert row["has_media"] is False
    assert row["current_version"] == 1
    assert row["score"] == 2
    assert row["id"] == created["id"]
    assert set(row) == {
        "id", "type", "prompt", "status", "context_kind", "level", "difficulty",
        "learning_language", "score", "current_version", "topic_names", "tag_names",
        "has_media", "created_at", "updated_at", "deleted_at",
    }


async def test_sorting_is_stable_and_paginated_without_gaps(client):
    for prompt in ("Zebra", "apple", "Moon", "apple"):
        await _create(client, choice_body(prompt=prompt))
    await _create(client, choice_body(prompt="Quiet"))
    expected = ["apple", "apple", "Moon", "Quiet", "Zebra"]

    seen: list[str] = []
    for page in (1, 2, 3):
        body = (await client.get("/api/v1/questions", params={"sort": "prompt", "order": "asc", "page": page, "page_size": 2})).json()
        assert body["total"] == 5
        assert body["page"] == page
        seen += [item["prompt"] for item in body["items"]]
    assert sorted(seen, key=str.lower) == expected

    desc = (await client.get("/api/v1/questions", params={"sort": "prompt", "order": "desc"})).json()
    assert [item["prompt"] for item in desc["items"]] == list(reversed(seen))

    clamped = await client.get("/api/v1/questions", params={"page_size": 5000})
    assert clamped.json()["page_size"] == 200
    tiny = await client.get("/api/v1/questions", params={"page_size": 0})
    assert tiny.json()["page_size"] == 1
    # A nonsense page number is clamped, not turned into a negative SQL OFFSET.
    negative = await client.get("/api/v1/questions", params={"page_size": -5, "page": -3})
    assert (negative.json()["page"], negative.json()["page_size"]) == (1, 1)
    assert negative.json()["total"] == 5

    unsortable = await client.get("/api/v1/questions", params={"sort": "current_version"})
    assert unsortable.status_code == 422, "only the columns the table can order by are accepted"


async def test_bad_listing_parameters_are_refused_not_ignored(client):
    for params in (
        {"sort": "password"},
        {"sort": "config"},
        {"order": "sideways"},
        {"view": "everything"},
        {"type": "not_a_type"},
        {"status": "published"},
        {"context_kind": "with_video"},
        {"topic_id": "not-a-uuid"},
    ):
        resp = await client.get("/api/v1/questions", params=params)
        assert resp.status_code == 422, params
        assert error_of(resp)["code"] == "validation_failed"


# --------------------------------------------------------------------------- #
# Bulk work
# --------------------------------------------------------------------------- #


async def test_bulk_actions_apply_to_every_listed_id(client):
    first = await _create(client, choice_body(prompt="First", status="draft"))
    second = await _create(client, choice_body(prompt="Second", status="draft"))
    topic = await _new_topic(client, "Bulk")
    tag = await _new_tag(client, "bulk-checked")

    resp = await client.post(
        "/api/v1/questions/bulk",
        json={"question_ids": [first["id"], second["id"]], "action": "status", "status": "ready"},
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["updated"]) == sorted([first["id"], second["id"]])
    assert (await _row(first["id"]))["status"] == "ready"
    assert (await _row(second["id"]))["status"] == "ready"

    for action, extra, field, expected in (
        ("add_topic", {"topic_id": topic}, "topics", ["Bulk"]),
        ("add_tag", {"tag_id": tag}, "tags", ["bulk-checked"]),
        ("remove_topic", {"topic_id": topic}, "topics", []),
        ("remove_tag", {"tag_id": tag}, "tags", []),
    ):
        resp = await client.post(
            "/api/v1/questions/bulk", json={"question_ids": [first["id"]], "action": action, **extra}
        )
        assert resp.json()["updated"] == [first["id"]], action
        names = [item["name"] for item in (await _detail(client, first["id"]))[field]]
        assert names == expected, action

    resp = await client.post(
        "/api/v1/questions/bulk", json={"question_ids": [first["id"], second["id"]], "action": "set_level", "level": "B2"}
    )
    assert resp.json()["updated"] == [first["id"], second["id"]]
    assert (await _row(first["id"]))["level"] == "B2"

    resp = await client.post(
        "/api/v1/questions/bulk", json={"question_ids": [first["id"]], "action": "set_language", "learning_language": "az"}
    )
    assert (await _row(first["id"]))["learning_language"] == "az"

    trashed = await client.post("/api/v1/questions/bulk", json={"question_ids": [second["id"]], "action": "trash"})
    assert trashed.json()["updated"] == [second["id"]]
    assert (await _row(second["id"]))["deleted_at"] is not None
    assert (await _row(second["id"]))["status"] == "ready", "bulk trash keeps the status too"

    restored = await client.post("/api/v1/questions/bulk", json={"question_ids": [second["id"]], "action": "restore"})
    assert restored.json()["updated"] == [second["id"]]
    assert (await _row(second["id"]))["deleted_at"] is None


async def test_bulk_reports_per_id_instead_of_a_silent_partial_win(client):
    good = await _create(client, choice_body(prompt="Good"))
    trashed = await _create(client, choice_body(prompt="Trashed"))
    await client.delete(f"/api/v1/questions/{trashed['id']}")
    gone = str(uuid.uuid4())

    resp = await client.post(
        "/api/v1/questions/bulk",
        json={"question_ids": [good["id"], trashed["id"], gone], "action": "status", "status": "archived"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == [good["id"]]
    assert body["not_found"] == [gone]
    assert [entry["id"] for entry in body["refused"]] == [trashed["id"]]
    assert "trash" in body["refused"][0]["reason"]
    assert (await _row(good["id"]))["status"] == "archived"
    assert (await _row(trashed["id"]))["status"] == "ready", "a refused row must not change"


async def test_bulk_needs_the_argument_its_action_uses(client):
    created = await _create(client, choice_body())
    ids = [created["id"]]
    for payload in (
        {"question_ids": ids, "action": "status"},
        {"question_ids": ids, "action": "add_topic"},
        {"question_ids": ids, "action": "remove_topic"},
        {"question_ids": ids, "action": "add_tag"},
        {"question_ids": ids, "action": "remove_tag"},
        {"question_ids": ids, "action": "set_level"},
        {"question_ids": ids, "action": "set_language"},
    ):
        resp = await client.post("/api/v1/questions/bulk", json=payload)
        assert resp.status_code == 422, payload
        assert "need" in error_of(resp)["message"]
        assert (await _detail(client, created["id"]))["status"] == "ready"

    unknown = await client.post("/api/v1/questions/bulk", json={"question_ids": ids, "action": "delete_forever"})
    assert unknown.status_code == 422
    empty = await client.post("/api/v1/questions/bulk", json={"question_ids": [], "action": "trash"})
    assert empty.status_code == 422

    for action, field in (("add_topic", "topic"), ("add_tag", "tag")):
        resp = await client.post(
            "/api/v1/questions/bulk",
            json={"question_ids": ids, "action": action, f"{field}_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 422, action
        assert field in error_of(resp)["message"]


async def test_bulk_taxonomy_does_not_duplicate_links(client):
    topic = await _new_topic(client, "Once")
    created = await _create(client, choice_body(topic_ids=[topic]))
    for _ in range(3):
        await client.post(
            "/api/v1/questions/bulk", json={"question_ids": [created["id"]], "action": "add_topic", "topic_id": topic}
        )
    assert [t["name"] for t in (await _detail(client, created["id"]))["topics"]] == ["Once"]


# --------------------------------------------------------------------------- #
# Context relationships, preview and grading
# --------------------------------------------------------------------------- #


async def test_context_rules_are_enforced(client):
    reading = await _new_reading()
    listening = await _new_listening()

    loose = await client.post("/api/v1/questions", json={**choice_body(), "context_kind": "reading_bound"})
    assert loose.status_code == 422
    assert "needs a reading_id" in error_of(loose)["message"]

    invented = await client.post(
        "/api/v1/questions", json={**choice_body(), "context_kind": "reading_bound", "reading_id": str(uuid.uuid4())}
    )
    assert invented.status_code == 422
    assert "does not exist" in error_of(invented)["message"]

    both = await client.post(
        "/api/v1/questions",
        json={**choice_body(), "context_kind": "reading_bound", "reading_id": reading, "listening_id": listening},
    )
    assert both.status_code == 422
    assert "must not also carry" in error_of(both)["message"]

    assert (await client.post("/api/v1/questions", json={**choice_body(), "reading_id": reading})).status_code == 422

    bound = await _create(
        client, choice_body(context_kind="reading_bound", reading_id=reading, prompt="Answer from the text.")
    )
    assert bound["context_kind"] == "reading_bound"
    assert bound["reading_id"] == reading

    preview = (await client.get(f"/api/v1/questions/{bound['id']}/preview")).json()
    assert preview["context"]["kind"] == "reading_bound"
    assert preview["context"]["reading"]["body"] == "The boats waited."

    heard = await _create(client, choice_body(context_kind="listening_bound", listening_id=listening))
    heard_view = (await client.get(f"/api/v1/questions/{heard['id']}/preview")).json()
    assert heard_view["context"]["listening"]["transcript"] is None
    assert heard_view["context"]["listening"]["show_transcript"] is False

    revealed_id = await _new_listening(show_transcript=True)
    revealed = await _create(client, choice_body(context_kind="listening_bound", listening_id=revealed_id))
    shown = (await client.get(f"/api/v1/questions/{revealed['id']}/preview")).json()
    assert shown["context"]["listening"]["transcript"] == "The train is late."

    # An edit cannot quietly detach a bound question from its passage.
    detach = await client.patch(f"/api/v1/questions/{bound['id']}", json={"reading_id": None})
    assert detach.status_code == 422
    assert "needs a reading_id" in error_of(detach)["message"]
    moved = await client.patch(f"/api/v1/questions/{bound['id']}", json={"context_kind": "independent"})
    assert moved.status_code == 422


async def test_a_bound_question_can_be_filtered_by_its_context(client):
    first = await _new_reading()
    second = await _new_reading(title="Another passage")
    bound = await _create(client, choice_body(context_kind="reading_bound", reading_id=first))
    await _create(client, choice_body(context_kind="reading_bound", reading_id=second))

    rows = (await client.get("/api/v1/questions", params={"reading_id": first})).json()["items"]
    assert [row["id"] for row in rows] == [bound["id"]]
    assert (await client.get("/api/v1/questions", params={"context_kind": "listening_bound"})).json()["total"] == 0


def _json_keys(payload) -> set[str]:
    """Every dict key anywhere inside a JSON body, however deeply nested."""
    if isinstance(payload, dict):
        found = set(payload)
        for value in payload.values():
            found |= _json_keys(value)
        return found
    if isinstance(payload, list):
        return set().union(*[_json_keys(value) for value in payload]) if payload else set()
    return set()


# Keys that belong to an answer key, a private note or the version machinery. None of
# them may appear in a learner-facing payload at any depth.
FORBIDDEN_LEARNER_KEYS = {
    "correct",
    "accepted",
    "answer",
    "answer_key",
    "pairs",
    "extra_rights",
    "items",
    "explanation",
    "teacher_notes",
    "current_version",
    "snapshot",
    "partial_scoring",
    "negative_scoring",
}


async def test_preview_never_carries_an_answer_key(client):
    for question_type, body in minimal_bodies().items():
        created = await _create(client, body)
        preview = await client.get(f"/api/v1/questions/{created['id']}/preview")
        assert preview.status_code == 200, question_type
        text = json.dumps(preview.json())
        for secret in secret_material(question_type):
            assert secret not in text, f"{question_type} leaked its key"
        leaked = _json_keys(preview.json()) & FORBIDDEN_LEARNER_KEYS
        assert not leaked, f"{question_type} preview carries {sorted(leaked)}"

    created = await _create(client, choice_body(explanation="Because the key says so.", teacher_notes="private note"))
    preview = (await client.get(f"/api/v1/questions/{created['id']}/preview")).json()
    assert preview["explanation_available"] is True
    assert "explanation" not in preview
    assert "teacher_notes" not in preview
    assert "current_version" not in preview
    assert preview["answer_widget"] == "single_option"
    assert preview["requires_manual_grading"] is False
    assert all(set(option) == {"index", "text"} for option in preview["config"]["options"])

    essay = await _create(client, minimal_bodies()["essay"])
    essay_view = (await client.get(f"/api/v1/questions/{essay['id']}/preview")).json()
    assert essay_view["requires_manual_grading"] is True
    assert essay_view["answer_widget"] == "essay"
    assert essay_view["config"]["guidance"] == "Use past tenses."


async def test_preview_presents_matching_and_ordering_without_positions(client):
    authored_pairs = minimal_bodies()["matching"]["config"]["pairs"]
    matching = await _create(client, minimal_bodies()["matching"])
    view = (await client.get(f"/api/v1/questions/{matching['id']}/preview")).json()["config"]
    assert {item["text"] for item in view["lefts"]} == {pair["left"] for pair in authored_pairs}
    assert {item["text"] for item in view["rights"]} == {"derya", "torpaq", "rabitə"}
    for item in view["lefts"] + view["rights"]:
        # Nothing but an opaque handle and the text: no index, no partner, and no
        # marker that would identify the distractor.
        assert set(item) == {"ref", "text"}
        assert len(item["ref"]) == 12

    partners = {pair["left"]: pair["right"] for pair in authored_pairs}
    straight = sum(
        1 for left, right in zip(view["lefts"], view["rights"], strict=False) if partners.get(left["text"]) == right["text"]
    )
    assert straight == 0, "pairing the two columns as they are displayed must score nothing"

    # The same element always carries the same handle, and the order never changes, so
    # a saved answer can be re-graded later without storing anything extra.
    again = (await client.get(f"/api/v1/questions/{matching['id']}/preview")).json()["config"]
    assert again == view

    ordering = await _create(client, minimal_bodies()["ordering"])
    tokens = (await client.get(f"/api/v1/questions/{ordering['id']}/preview")).json()["config"]["tokens"]
    authored_order = minimal_bodies()["ordering"]["config"]["items"]
    assert {token["text"] for token in tokens} == set(authored_order)
    assert all(set(token) == {"ref", "text"} for token in tokens)
    # Reading the tokens in the order they are shown must not reproduce the story.
    aligned = sum(1 for position, token in enumerate(tokens) if authored_order.index(token["text"]) == position)
    assert aligned < len(tokens)


async def test_grading_endpoint_scores_live_and_frozen_versions(client):
    created = await _create(client, choice_body())
    key_index = 1

    right = await client.post(f"/api/v1/questions/{created['id']}/grade", json={"response": {"option_index": key_index}})
    assert right.status_code == 200, right.text
    assert (right.json()["score"], right.json()["correct"], right.json()["version"]) == (2.0, True, 1)

    wrong = await client.post(f"/api/v1/questions/{created['id']}/grade", json={"response": {"option_index": 0}})
    assert (wrong.json()["score"], wrong.json()["correct"]) == (0.0, False)

    await client.patch(
        f"/api/v1/questions/{created['id']}",
        json={"config": {"options": [{"text": "apple", "correct": True}, {"text": "kite"}, {"text": "table"}]}},
    )
    after_edit = await client.post(f"/api/v1/questions/{created['id']}/grade", json={"response": {"option_index": key_index}})
    assert (after_edit.json()["score"], after_edit.json()["version"]) == (0.0, 2)

    # The same answer graded against frozen version 1 keeps the marks it earned then,
    # which is what an attempt from last week needs.
    as_then = await client.post(
        f"/api/v1/questions/{created['id']}/grade",
        params={"at_version": 1},
        json={"response": {"option_index": key_index}},
    )
    assert (as_then.json()["score"], as_then.json()["correct"], as_then.json()["version"]) == (2.0, True, 1)

    missing = await client.post(
        f"/api/v1/questions/{created['id']}/grade", params={"at_version": 77}, json={"response": {"option_index": 0}}
    )
    assert missing.status_code == 404
    assert error_of(missing)["code"] == "not_found"


async def test_grading_a_matching_answer_through_the_published_refs_works(client):
    created = await _create(client, minimal_bodies()["matching"])
    view = (await client.get(f"/api/v1/questions/{created['id']}/preview")).json()["config"]
    left_of = {item["text"]: item["ref"] for item in view["lefts"]}
    right_of = {item["text"]: item["ref"] for item in view["rights"]}

    answer = {
        "pairs": [
            {"left_ref": left_of["sea"], "right_ref": right_of["derya"]},
            {"left_ref": left_of["land"], "right_ref": right_of["torpaq"]},
        ]
    }
    resp = await client.post(f"/api/v1/questions/{created['id']}/grade", json={"response": answer})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["score"], resp.json()["correct"]) == (1.0, True)

    nonsense = await client.post(
        f"/api/v1/questions/{created['id']}/grade",
        json={"response": {"pairs": [{"left_ref": "0" * 12, "right_ref": "1" * 12} for _ in range(2)]}},
    )
    assert (nonsense.json()["correct"], nonsense.json()["detail"]["reason"]) == (False, "no_valid_answer")


async def test_grading_an_essay_asks_for_a_marker_instead_of_guessing(client):
    created = await _create(client, minimal_bodies()["essay"])
    resp = await client.post(
        f"/api/v1/questions/{created['id']}/grade", json={"response": {"text": " ".join(["holiday"] * 50)}}
    )
    body = resp.json()
    assert (body["score"], body["correct"], body["requires_manual"]) == (0.0, None, True)
    assert body["detail"]["word_count"] == 50


async def test_a_question_with_media_is_reported_to_the_learner(client):
    asset_id = await _new_media_asset()
    created = await _create(client, choice_body(media_asset_id=asset_id))
    assert created["media_asset_id"] == asset_id
    preview = (await client.get(f"/api/v1/questions/{created['id']}/preview")).json()
    # The preview panel is the learner's projection - a player, not the library's row, and
    # nothing that says where the file lives. Only the door is the teacher's own, because
    # the browser reading this answer holds an admin session and cannot use a student one.
    assert preview["media"]["id"] == asset_id
    assert preview["media"]["content_url"] == f"/media/{asset_id}/content"
    assert preview["media"]["content_url"].startswith("/media/"), "not a storage address"
    assert set(preview["media"]) == {"id", "kind", "mime_type", "duration_seconds", "width", "height", "content_url"}
    assert "media_asset_id" not in preview, "the asset id is the teacher's bookkeeping"
    assert (await client.get("/api/v1/questions", params={"has_media": True})).json()["total"] == 1
    assert (await client.get("/api/v1/questions")).json()["items"][0]["has_media"] is True
    assert created["source_file_id"] is None
    assert created["extraction_method"] is None


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #


async def test_bank_writes_are_audited(client):
    created = await _create(client, choice_body())
    await client.patch(f"/api/v1/questions/{created['id']}", json={"prompt": "Edited prompt"})
    await client.post(f"/api/v1/questions/{created['id']}/status", json={"status": "draft"})
    await client.post(f"/api/v1/questions/{created['id']}/taxonomy", json={"topic_ids": []})
    clone = await client.post(f"/api/v1/questions/{created['id']}/clone")
    assert clone.status_code == 201, clone.text
    await client.delete(f"/api/v1/questions/{created['id']}")
    await client.post(f"/api/v1/questions/{created['id']}/restore")
    await client.post(
        "/api/v1/questions/bulk", json={"question_ids": [created["id"]], "action": "set_level", "level": "B1"}
    )

    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.target_id == uuid.UUID(created["id"]))))
            .scalars()
            .all()
        )
        # A clone is a new question, so its own history is written under its own id.
        clone_rows = (
            (await db.execute(select(AuditLog).where(AuditLog.target_id == uuid.UUID(clone.json()["id"]))))
            .scalars()
            .all()
        )
        # Bulk is one call over a set of questions, so it is audited as one aggregate
        # event rather than as a fabricated per-question row.
        bulk_rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "question.bulk.set_level"))).scalars().all()
        )

    actions = sorted(row.action for row in rows)
    assert actions == [
        "question.created",
        "question.restored",
        "question.status.changed",
        "question.taxonomy.assigned",
        "question.trashed",
        "question.updated",
    ]
    assert [row.action for row in clone_rows] == ["question.cloned"]
    assert clone_rows[0].detail == created["id"], "the clone records what it came from"
    by_action = {row.action: row for row in rows}
    assert all(row.actor_type == "admin" and row.actor_id is not None for row in rows)
    assert all(row.target_type == "question" for row in rows)
    assert by_action["question.created"].after == {"type": "multiple_choice", "status": "ready"}
    assert by_action["question.updated"].after["versioned"] is True
    assert by_action["question.updated"].before["prompt"] == choice_body()["prompt"]
    assert by_action["question.status.changed"].before == {"status": "ready"}
    assert by_action["question.status.changed"].after == {"status": "draft"}
    assert by_action["question.trashed"].after["status"] == "draft"
    assert by_action["question.restored"].before["deleted_at"]
    assert by_action["question.restored"].after == {"status": "draft"}
    assert by_action["question.taxonomy.assigned"].after == {"topics": 0, "tags": 0}

    assert len(bulk_rows) == 1
    assert bulk_rows[0].target_id is None
    assert bulk_rows[0].after == {"requested": 1, "updated": 1, "refused": 0, "not_found": 0}
