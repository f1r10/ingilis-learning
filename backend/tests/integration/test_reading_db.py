"""Reading passages over HTTP, against real Postgres (Phase 5).

The offline rule tests (`tests/test_passage_rules.py`) settle the arithmetic: what a
`Range` is not, how a word is counted, which set id belongs to which passage. What only a
live database can prove is the behaviour of the edges:

* a text's `word_count` and `excerpt` are produced by the server from the body it was
  handed, on a real row, and a client that sends its own count is refused;
* filing a question under a set moves or unfiles it, reports which, and writes **no**
  question version - grouping is not a content edit, and an immutable history that grew a
  version every time somebody dragged a box would stop being evidence;
* a learner is served ready texts only, with the sets that contain answerable questions,
  the set's authoring config withheld, and no answer key anywhere in the payload;
* trashing a text leaves its twenty exercises in the bank;
* the lifecycle words, the list filters and the bulk answers behave the way the teacher's
  screen needs them to, per row.

Questions are created through `/api/v1/questions` rather than inserted, so the binding
rules that Phase 3 wrote are exercised by the same door a browser uses.
"""
from __future__ import annotations

import uuid

from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core import enums
from app.core.database import SessionLocal
from app.models.content import Question, QuestionVersion, Reading
from app.services import passage_service, reading_service

#: A string that only ever belongs in a teacher-only field - the explanation that is
#: handed out after grading and the private notes. If it appears anywhere in a
#: learner-facing payload, the test that looked for it fails loudly. It is deliberately
#: NOT the text of an option: a learner has to be able to read what they are being asked
#: to tick, and a sentinel they must tick would prove nothing about the answer key.
KEY = "ZULU-READING-KEY"

PASSAGE_PATH = "/api/v1/reading"
STUDENT_PATH = "/api/v1/student/reading"

NINE_WORDS = "The quick brown fox jumps over the lazy dog."
OTHER_BODY = "Another text entirely, about a different lesson."


def choice_body(prompt: str = "Which word means 'kite'?", **over) -> dict:
    body = {
        "type": "multiple_choice",
        "prompt": prompt,
        "config": {
            "options": [
                {"text": "apple", "correct": True},
                {"text": "table"},
                {"text": "window"},
            ]
        },
        "explanation": f"{KEY}: an apple is the fruit, so the other two are wrong.",
        "teacher_notes": f"{KEY} again, in the notes nobody but the teacher reads.",
        "score": 2,
        "level": "A2",
        "learning_language": "en",
    }
    body.update(over)
    return body


async def _create(client, **over) -> dict:
    body = {"title": "A day at the market", "body": NINE_WORDS, "language": "en"}
    body.update(over)
    resp = await client.post(PASSAGE_PATH, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _detail(client, reading_id: str) -> dict:
    resp = await client.get(f"{PASSAGE_PATH}/{reading_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows(*, view: str = "bank") -> list[dict]:
    async with SessionLocal() as db:
        stmt = select(Reading)
        if view == "bank":
            stmt = stmt.where(Reading.deleted_at.is_(None))
        elif view == "trash":
            stmt = stmt.where(Reading.deleted_at.is_not(None))
        return list((await db.execute(stmt.order_by(Reading.created_at))).scalars().all())


async def _question(client, reading_id: str, *, prompt: str, **over) -> dict:
    body = choice_body(prompt, context_kind="reading_bound", reading_id=reading_id)
    body.update(over)
    resp = await client.post("/api/v1/questions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _set(client, reading_id: str, title: str, **over) -> dict:
    resp = await client.post(f"{PASSAGE_PATH}/{reading_id}/sets", json={"title": title, **over})
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


async def _learner(client, session_factory, username: str):
    created = await client.post(
        "/api/v1/students", json={"name": "Read", "surname": "Away", "username": username}
    )
    assert created.status_code == 201, created.text
    learner = session_factory()
    await learner.login_student(created.json()["access_key"])
    return learner


async def _ready_with_questions(client, *, title: str = "Ready text", drafts: int = 0):
    """One published text, two answerable questions under a set, and `drafts` unfiled drafts."""
    reading = await _create(client, title=title)
    questions = [
        await _question(client, reading["id"], prompt=f"Ask {index}") for index in range(2)
    ]
    for index in range(drafts):
        await _question(client, reading["id"], prompt=f"Draft {index}", status="draft")
    set_row = await _set(client, reading["id"], "Comprehension")
    await _file(client, set_row["id"], *[item["id"] for item in questions])
    return reading, questions, set_row


# --------------------------------------------------------------------------- #
# What the server derives
# --------------------------------------------------------------------------- #


async def test_the_word_count_is_the_servers_count_of_the_body(client):
    created = await _create(client)
    assert created["word_count"] == len(NINE_WORDS.split()) == 9

    row = (await _rows())[0]
    assert row.word_count == created["word_count"], "the column and the payload agree"

    listed = await client.get(PASSAGE_PATH)
    assert listed.json()["items"][0]["word_count"] == 9


async def test_editing_the_text_recounts_it_in_the_same_request(client):
    created = await _create(client)
    patched = await client.patch(
        f"{PASSAGE_PATH}/{created['id']}", json={"body": "one two three four five"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["word_count"] == 5
    assert (await _detail(client, created["id"]))["word_count"] == 5

    shorter = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"body": "only one"})
    assert shorter.json()["word_count"] == 2


async def test_a_client_may_not_report_its_own_word_count(client):
    """`extra="forbid"`: accepting it would make the number beside the text a rumour."""
    claimed = await client.post(
        PASSAGE_PATH, json={"title": "T", "body": NINE_WORDS, "word_count": 9999}
    )
    assert claimed.status_code == 422, claimed.text
    assert error_of(claimed)["code"] == "validation_failed"

    created = await _create(client)
    patched = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"word_count": 3})
    assert patched.status_code == 422, patched.text
    assert (await _detail(client, created["id"]))["word_count"] == 9, "the refusal changed nothing"


async def test_status_is_not_a_patch_field(client):
    """Lifecycle has its own endpoint, which can then refuse the trash as a status."""
    created = await _create(client)
    refused = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"status": "draft"})
    assert refused.status_code == 422, refused.text


async def test_a_row_shows_the_opening_of_the_text_without_shipping_it(client):
    long = "word " * 400  # 400 words, 2000 characters
    created = await _create(client, title="A very long text", body=long)

    listed = await client.get(PASSAGE_PATH)
    item = next(row for row in listed.json()["items"] if row["id"] == created["id"])
    assert len(item["excerpt"]) <= reading_service.EXCERPT_CHARACTERS + 1, item["excerpt"]
    assert item["excerpt"].endswith("…")
    assert item["excerpt"].startswith("word word")

    detail = await _detail(client, created["id"])
    assert detail["body"] == long.strip(), "the editor still gets the whole text"


async def test_the_excerpt_flattens_the_paragraphs_a_teacher_wrote(client):
    created = await _create(client, body="First paragraph.\n\n\nSecond paragraph here.")
    listed = await client.get(PASSAGE_PATH)
    item = next(row for row in listed.json()["items"] if row["id"] == created["id"])
    assert item["excerpt"] == "First paragraph. Second paragraph here."


# --------------------------------------------------------------------------- #
# Field rules
# --------------------------------------------------------------------------- #


async def test_a_language_is_stored_in_the_shape_the_filters_use(client):
    created = await _create(client, language="  EN ")
    assert created["language"] == "en"
    listed = await client.get(f"{PASSAGE_PATH}?language=EN")
    assert [row["id"] for row in listed.json()["items"]] == [created["id"]], "case cannot split a filter"


async def test_a_language_the_platform_has_not_enabled_is_refused(client):
    refused = await client.post(
        PASSAGE_PATH, json={"title": "Deutsch", "body": NINE_WORDS, "language": "de"}
    )
    assert refused.status_code == 422, refused.text
    message = error_of(refused)["message"]
    assert "enabled learning language" in message and "available:" in message


async def test_a_text_may_have_no_language_yet(client):
    created = await _create(client, language=None)
    assert created["language"] is None
    assert (await _detail(client, created["id"]))["language"] is None


async def test_only_a_layout_the_learner_screen_can_render_is_accepted(client):
    default = await _create(client)
    assert default["layout"] == "above", "a text without a layout is the ordinary one"

    for layout in passage_service.LAYOUTS:
        made = await _create(client, title=f"Layout {layout}", layout=layout)
        assert made["layout"] == layout

    bogus = await client.post(
        PASSAGE_PATH, json={"title": "Sideways", "body": NINE_WORDS, "layout": "sidebar"}
    )
    assert bogus.status_code == 422, bogus.text
    assert "layout must be one of" in error_of(bogus)["message"]

    edited = await client.patch(f"{PASSAGE_PATH}/{default['id']}", json={"layout": "tabbed"})
    assert edited.json()["layout"] == "tabbed"


async def test_blank_title_or_text_is_refused(client):
    base = {"title": "T", "body": NINE_WORDS}
    for over in ({"title": "   "}, {"title": ""}, {"body": "   "}, {"body": ""}):
        refused = await client.post(PASSAGE_PATH, json={**base, **over})
        assert refused.status_code == 422, over


async def test_a_patch_needs_to_say_something(client):
    created = await _create(client)
    empty = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={})
    assert empty.status_code == 422, empty.text
    assert "nothing to change" in error_of(empty)["message"]


async def test_an_edit_that_clears_the_title_is_refused_rather_than_stored(client):
    created = await _create(client)
    blank = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"title": "   "})
    assert blank.status_code == 422, blank.text
    assert (await _detail(client, created["id"]))["title"] == "A day at the market"


# --------------------------------------------------------------------------- #
# Lifecycle, and what the trash is
# --------------------------------------------------------------------------- #


async def test_trash_is_refused_as_a_status_because_it_is_a_deletion(client):
    created = await _create(client)
    refused = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "trash"})
    assert refused.status_code == 422, refused.text
    assert "not a status" in error_of(refused)["message"]


async def test_the_three_lifecycle_words_move(client):
    created = await _create(client, status="draft")
    assert created["status"] == "draft"

    published = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "ready"})
    assert published.json()["status"] == "ready"

    archived = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "archived"})
    assert archived.json()["status"] == "archived"

    bogus = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "published"})
    assert bogus.status_code == 422, bogus.text
    assert "status must be one of" in error_of(bogus)["message"]


async def test_publishing_a_text_needs_no_questions(client):
    """Unlike a recording, a text with nothing under it is still something to read."""
    created = await _create(client, status="draft", title="Just a text")
    published = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "ready"})
    assert published.status_code == 200, published.text
    assert published.json()["question_count"] == 0


async def test_a_trashed_text_is_still_readable_and_editable_only_from_the_trash(client):
    """The trash screen has to show what is about to be lost, and say how to get it back.

    Reading it works, editing its body answers 404 (the row is not in the bank), and a
    status change answers with the sentence that names the restore button.
    """
    created = await _create(client, title="Gone for now")
    trashed = await client.delete(f"{PASSAGE_PATH}/{created['id']}")
    assert trashed.status_code == 200, trashed.text
    assert trashed.json()["status"] == "ready", "the trash does not overwrite the lifecycle"

    shown = await client.get(f"{PASSAGE_PATH}/{created['id']}")
    assert shown.status_code == 200, shown.text
    assert shown.json()["deleted_at"] is not None

    refused = await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "draft"})
    assert refused.status_code == 422, refused.text
    assert "restore" in error_of(refused)["message"]

    edited = await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"title": "Renamed"})
    assert edited.status_code == 404, edited.text

    restored = await client.post(f"{PASSAGE_PATH}/{created['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    assert restored.json()["status"] == "ready", "a restore returns the state it left"


async def test_a_second_trash_is_a_no_op_rather_than_a_new_timestamp(client):
    created = await _create(client)
    first = await client.delete(f"{PASSAGE_PATH}/{created['id']}")
    assert first.status_code == 200, first.text
    before = (await _rows(view="trash"))[0].deleted_at

    again = await client.delete(f"{PASSAGE_PATH}/{created['id']}")
    assert again.status_code == 200, again.text
    assert (await _rows(view="trash"))[0].deleted_at == before, "nothing was re-trashed"


async def test_a_status_change_touches_the_status_and_nothing_else(client):
    reading, questions, set_row = await _ready_with_questions(client)
    body_before = (await _detail(client, reading["id"]))["body"]

    archived = await client.post(f"{PASSAGE_PATH}/{reading['id']}/status", json={"status": "archived"})
    after = archived.json()
    assert after["status"] == "archived"
    assert after["body"] == body_before
    assert after["word_count"] == 9
    assert [item["id"] for item in after["sets"]] == [set_row["id"]]
    assert [item["id"] for item in after["sets"][0]["questions"]] == [
        item["id"] for item in questions
    ], "publishing a text is not a way of publishing the questions under it"


# --------------------------------------------------------------------------- #
# Questions, sets, and the difference between the two
# --------------------------------------------------------------------------- #


async def test_trashing_a_text_leaves_its_questions_in_the_bank(client):
    """Twenty exercises belong to the bank, not to the one passage they were written for."""
    reading, questions, _ = await _ready_with_questions(client)
    assert (await client.delete(f"{PASSAGE_PATH}/{reading['id']}")).status_code == 200

    for item in questions:
        still = await client.get(f"/api/v1/questions/{item['id']}")
        assert still.status_code == 200, still.text
        assert still.json()["status"] == "ready"
        assert still.json()["reading_id"] == reading["id"]


async def test_a_question_bound_to_a_text_is_visible_before_any_set_exists(client):
    created = await _create(client)
    question = await _question(client, created["id"], prompt="Loose question")

    detail = await _detail(client, created["id"])
    assert detail["set_count"] == 0 and detail["question_count"] == 1
    assert [item["id"] for item in detail["unfiled"]] == [question["id"]]
    assert detail["unfiled"][0]["prompt"] == "Loose question"


async def test_assigning_replaces_the_set_and_reports_what_left(client):
    reading = await _create(client)
    a, b, c = [
        await _question(client, reading["id"], prompt=f"Q {index}") for index in range(3)
    ]
    set_row = await _set(client, reading["id"], "First block")

    filled = await _file(client, set_row["id"], a["id"], b["id"])
    assert [item["id"] for item in filled["questions"]] == [a["id"], b["id"]]
    assert [item["position"] for item in filled["questions"]] == [0, 1]
    assert filled["unfiled"] == [] and filled["moved"] == []

    replaced = await _file(client, set_row["id"], b["id"], c["id"])
    assert [item["id"] for item in replaced["questions"]] == [b["id"], c["id"]]
    assert replaced["unfiled"] == [a["id"]], "an omitted question is unfiled, never deleted"
    assert replaced["moved"] == []

    still_there = await client.get(f"/api/v1/questions/{a['id']}")
    assert still_there.status_code == 200, "the pool is not the trash"
    detail = await _detail(client, reading["id"])
    assert [item["id"] for item in detail["unfiled"]] == [a["id"]]


async def test_a_question_moves_from_a_sibling_block_and_the_answer_names_it(client):
    """Dragging a question between blocks is a move, and a silent one reads as a lost save."""
    reading = await _create(client)
    question = await _question(client, reading["id"], prompt="Traveller")
    first = await _set(client, reading["id"], "Vocabulary")
    second = await _set(client, reading["id"], "Grammar")

    await _file(client, first["id"], question["id"])
    moved = await _file(client, second["id"], question["id"])

    assert moved["moved"] == [{"question_id": question["id"], "from_title": "Vocabulary"}]
    left = (await client.get(f"{PASSAGE_PATH}/{reading['id']}/sets")).json()["items"]
    assert [item["question_count"] for item in left] == [0, 1]


async def test_grouping_writes_no_question_version(client):
    """A question's history is content change. Filing it under a block is not one.

    If it were, `QuestionVersion` would stop being evidence about what a learner was
    asked, and become a log of the teacher's mouse.
    """
    reading = await _create(client)
    question = await _question(client, reading["id"], prompt="Stable content")
    before_rows, before_number = await _versions(question["id"])

    set_row = await _set(client, reading["id"], "Block")
    await _file(client, set_row["id"], question["id"])
    await _file(client, set_row["id"])  # taken out again
    await _file(client, set_row["id"], question["id"])

    after_rows, after_number = await _versions(question["id"])
    assert (after_rows, after_number) == (before_rows, before_number)

    # ...and an actual content edit still does append one, so the rule is not "nothing works".
    edited = await client.patch(
        f"/api/v1/questions/{question['id']}",
        json={"prompt": "Rewritten content"},
    )
    assert edited.status_code == 200, edited.text
    grown_rows, grown_number = await _versions(question["id"])
    assert grown_rows == before_rows + 1 and grown_number == before_number + 1


async def test_a_question_from_another_text_cannot_be_filed_here(client):
    mine = await _create(client, title="Text one")
    theirs = await _create(client, title="Text two")
    stranger = await _question(client, theirs["id"], prompt="Belongs elsewhere")
    set_row = await _set(client, mine["id"], "Block")

    refused = await client.post(
        f"{PASSAGE_PATH}/sets/{set_row['id']}/questions", json={"question_ids": [stranger["id"]]}
    )
    assert refused.status_code == 422, refused.text
    assert "Belongs elsewhere" in error_of(refused)["message"]
    assert (await client.get(f"{PASSAGE_PATH}/{mine['id']}/sets")).json()["items"][0]["question_count"] == 0


async def test_a_question_that_is_not_in_the_bank_is_named_not_guessed(client):
    reading = await _create(client)
    set_row = await _set(client, reading["id"], "Block")
    ghost = str(uuid.uuid4())

    refused = await client.post(
        f"{PASSAGE_PATH}/sets/{set_row['id']}/questions", json={"question_ids": [ghost]}
    )
    assert refused.status_code == 422, refused.text
    assert ghost in error_of(refused)["message"]


async def test_a_trashed_question_cannot_be_filed(client):
    reading = await _create(client)
    question = await _question(client, reading["id"], prompt="On its way out")
    set_row = await _set(client, reading["id"], "Block")
    assert (await client.delete(f"/api/v1/questions/{question['id']}")).status_code == 200

    refused = await client.post(
        f"{PASSAGE_PATH}/sets/{set_row['id']}/questions", json={"question_ids": [question["id"]]}
    )
    assert refused.status_code == 422, refused.text
    assert "trash" in error_of(refused)["message"]


async def test_the_same_question_named_twice_in_one_request_is_refused(client):
    """A repeated id is a client bug; answering 200 for one copy would misreport the save."""
    reading = await _create(client)
    question = await _question(client, reading["id"], prompt="Duplicated")
    set_row = await _set(client, reading["id"], "Block")

    refused = await client.post(
        f"{PASSAGE_PATH}/sets/{set_row['id']}/questions",
        json={"question_ids": [question["id"], question["id"]]},
    )
    assert refused.status_code == 422, refused.text
    assert "cannot appear twice" in str(refused.json())


async def test_deleting_a_set_returns_its_questions_to_the_pool(client):
    reading, questions, set_row = await _ready_with_questions(client)

    dropped = await client.delete(f"{PASSAGE_PATH}/sets/{set_row['id']}")
    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["returned_to_pool"] == 2

    detail = await _detail(client, reading["id"])
    assert detail["sets"] == []
    assert detail["question_count"] == 2, "the exercises are still bound to the text"
    assert {item["id"] for item in detail["unfiled"]} == {item["id"] for item in questions}


async def test_sets_are_numbered_by_the_server_in_the_order_they_arrived(client):
    reading = await _create(client)
    made = [await _set(client, reading["id"], f"Block {index}") for index in range(3)]
    assert [item["position"] for item in made] == [0, 1, 2]

    listed = (await client.get(f"{PASSAGE_PATH}/{reading['id']}/sets")).json()["items"]
    assert [item["title"] for item in listed] == ["Block 0", "Block 1", "Block 2"]


async def test_reordering_needs_the_whole_list(client):
    reading = await _create(client)
    blocks = [await _set(client, reading["id"], f"Block {index}") for index in range(3)]
    ids = [item["id"] for item in blocks]

    partial = await client.post(
        f"{PASSAGE_PATH}/{reading['id']}/sets/reorder", json={"set_ids": [ids[2], ids[0]]}
    )
    assert partial.status_code == 422, partial.text
    assert "every set must be listed" in error_of(partial)["message"]

    stranger = await client.post(
        f"{PASSAGE_PATH}/{reading['id']}/sets/reorder",
        json={"set_ids": [ids[0], ids[1], str(uuid.uuid4())]},
    )
    assert stranger.status_code == 422, stranger.text
    assert "do not belong to this reading" in error_of(stranger)["message"]

    reordered = await client.post(
        f"{PASSAGE_PATH}/{reading['id']}/sets/reorder", json={"set_ids": [ids[2], ids[0], ids[1]]}
    )
    assert reordered.status_code == 200, reordered.text
    assert [item["position"] for item in reordered.json()["items"]] == [0, 1, 2]
    assert [item["title"] for item in reordered.json()["items"]] == [
        "Block 2",
        "Block 0",
        "Block 1",
    ]


async def test_a_set_is_addressed_by_its_own_id(client):
    """The browser editing one block never names the text above it, and cannot point at
    another text's block by sending a mismatched pair."""
    reading = await _create(client)
    set_row = await _set(client, reading["id"], "Original", instructions="Choose the right word.")
    assert set_row["title"] == "Original"

    renamed = await client.patch(
        f"{PASSAGE_PATH}/sets/{set_row['id']}", json={"title": "Renamed", "instructions": None}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Renamed"
    assert renamed.json()["instructions"] is None

    empty = await client.patch(f"{PASSAGE_PATH}/sets/{set_row['id']}", json={})
    assert empty.status_code == 422, empty.text
    assert "nothing to change" in error_of(empty)["message"]

    missing = await client.patch(f"{PASSAGE_PATH}/sets/{uuid.uuid4()}", json={"title": "Ghost"})
    assert missing.status_code == 404, missing.text


async def test_a_set_holds_its_own_order_of_questions_across_two_blocks(client):
    reading = await _create(client)
    questions = [
        await _question(client, reading["id"], prompt=f"Q {index}") for index in range(4)
    ]
    one = await _set(client, reading["id"], "One")
    two = await _set(client, reading["id"], "Two")

    await _file(client, one["id"], questions[0]["id"], questions[1]["id"])
    await _file(client, two["id"], questions[2]["id"], questions[3]["id"])

    listed = (await client.get(f"{PASSAGE_PATH}/{reading['id']}/sets")).json()["items"]
    assert [[q["position"] for q in item["questions"]] for item in listed] == [[0, 1], [0, 1]]
    assert [item["question_count"] for item in listed] == [2, 2]


async def test_a_filed_question_cannot_be_re_pointed_from_the_question_screen(client):
    """The binding and the filing are two rows that have to agree.

    Re-binding from the question screen would leave a set holding a question about a text
    it is no longer attached to, so the refusal says where it is filed and what to do.
    """
    first = await _create(client, title="Text one")
    second = await _create(client, title="Text two")
    question = await _question(client, first["id"], prompt="Filed already")
    set_row = await _set(client, first["id"], "Block")
    await _file(client, set_row["id"], question["id"])

    refused = await client.patch(
        f"/api/v1/questions/{question['id']}",
        json={"context_kind": "reading_bound", "reading_id": second["id"]},
    )
    assert refused.status_code == 422, refused.text
    message = error_of(refused)["message"]
    assert "filed under" in message and "remove it from that set" in message

    # take it out of the block, and the same re-bind is a normal edit
    await _file(client, set_row["id"])
    moved = await client.patch(
        f"/api/v1/questions/{question['id']}",
        json={"context_kind": "reading_bound", "reading_id": second["id"]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["reading_id"] == second["id"]


async def test_a_question_cannot_be_bound_to_a_trashed_text(client):
    created = await _create(client)
    assert (await client.delete(f"{PASSAGE_PATH}/{created['id']}")).status_code == 200

    refused = await client.post(
        "/api/v1/questions",
        json=choice_body("Too late", context_kind="reading_bound", reading_id=created["id"]),
    )
    assert refused.status_code == 422, refused.text
    assert "restore it" in error_of(refused)["message"]


async def test_a_sets_questions_survive_its_own_trash_and_return(client):
    """Membership waits out the question's trash: restoring the exercise puts it back."""
    reading = await _create(client)
    question = await _question(client, reading["id"], prompt="Temporarily gone")
    set_row = await _set(client, reading["id"], "Block")
    await _file(client, set_row["id"], question["id"])

    assert (await client.delete(f"/api/v1/questions/{question['id']}")).status_code == 200
    during = (await client.get(f"{PASSAGE_PATH}/{reading['id']}/sets")).json()["items"]
    assert during[0]["question_count"] == 0, "a deleted exercise is not shown as a live one"

    restored = await client.post(f"/api/v1/questions/{question['id']}/restore")
    assert restored.status_code == 200, restored.text
    after = (await client.get(f"{PASSAGE_PATH}/{reading['id']}/sets")).json()["items"]
    assert [item["id"] for item in after[0]["questions"]] == [question["id"]]


# --------------------------------------------------------------------------- #
# The learner's reading
# --------------------------------------------------------------------------- #


async def test_a_learner_is_shown_ready_texts_only(client, session_factory):
    learner = await _learner(client, session_factory, "learner-reading")

    draft = await _create(client, status="draft", title="Not finished")
    ready = await _create(client, title="Read me")
    archived = await _create(client, title="Finished unit")
    await client.post(f"{PASSAGE_PATH}/{archived['id']}/status", json={"status": "archived"})
    trashed = await _create(client, title="Throwing this away")
    await client.delete(f"{PASSAGE_PATH}/{trashed['id']}")

    for item in (draft, archived, trashed):
        refused = await learner.get(f"{STUDENT_PATH}/{item['id']}")
        assert refused.status_code == 404, f"{item['title']} -> {refused.status_code}"
        assert error_of(refused)["code"] == "not_found", "a draft must not be describable"

    shown = await learner.get(f"{STUDENT_PATH}/{ready['id']}")
    assert shown.status_code == 200, shown.text

    listed = await learner.get(STUDENT_PATH)
    assert [item["id"] for item in listed.json()["items"]] == [ready["id"]]


async def test_a_learners_reading_carries_the_text_once(client, session_factory):
    """The body is the block above the questions, not a field of every question."""
    reading, questions, set_row = await _ready_with_questions(client)
    learner = await _learner(client, session_factory, "learner-once")

    detail = await learner.get(f"{STUDENT_PATH}/{reading['id']}")
    body = detail.json()
    assert body["body"] == NINE_WORDS
    assert len(body["sets"]) == 1
    served = body["sets"][0]["questions"]
    assert [item["id"] for item in served] == [item["id"] for item in questions]
    for item in served:
        assert "context" not in item, "the text is not repeated inside its own questions"
        assert NINE_WORDS not in str(item), item


async def test_no_answer_key_reaches_a_learner_through_a_reading(client, session_factory):
    reading, _questions, _set = await _ready_with_questions(client)
    learner = await _learner(client, session_factory, "learner-no-key")

    detail = await learner.get(f"{STUDENT_PATH}/{reading['id']}")
    payload = detail.text
    assert KEY not in payload, "an explanation and a private note are not a learner's page"
    assert '"correct"' not in payload
    assert '"explanation"' not in payload
    assert '"teacher_notes"' not in payload
    for item in detail.json()["sets"][0]["questions"]:
        # The options must arrive whole - a tick list with the right answer removed is not
        # a question, it is a hole. Only the *key* is taken out.
        assert [option["text"] for option in item["config"]["options"]] == ["apple", "table", "window"]
        assert set(item["config"]["options"][0]) == {"index", "text"}, item["config"]["options"][0]
        assert item["answer_widget"]
        assert item["explanation_available"] is True, "the learner is told one is coming"


async def test_a_sets_authoring_config_stays_with_the_teacher(client, session_factory):
    reading = await _create(client)
    set_row = await _set(client, reading["id"], "Block", config={"notes_for_me": KEY})
    question = await _question(client, reading["id"], prompt="Answerable")
    await _file(client, set_row["id"], question["id"])

    teacher = (await client.get(f"{PASSAGE_PATH}/{reading['id']}/sets")).json()["items"]
    assert teacher[0]["config"] == {"notes_for_me": KEY}

    learner = await _learner(client, session_factory, "learner-no-config")
    student = await learner.get(f"{STUDENT_PATH}/{reading['id']}")
    assert student.status_code == 200, student.text
    assert "config" not in student.json()["sets"][0], student.json()["sets"][0]
    assert KEY not in student.text


async def test_a_learner_never_sees_a_draft_question_or_an_empty_block(client, session_factory):
    reading = await _create(client)
    answered = await _question(client, reading["id"], prompt="Answerable")
    await _question(client, reading["id"], prompt="Still a draft", status="draft")

    empty = await _set(client, reading["id"], "Nothing filed here")
    filled = await _set(client, reading["id"], "One question")
    await _file(client, empty["id"])
    await _file(client, filled["id"], answered["id"])

    learner = await _learner(client, session_factory, "learner-ready-only")
    body = (await learner.get(f"{STUDENT_PATH}/{reading['id']}")).json()
    assert [item["title"] for item in body["sets"]] == ["One question"], "a block of drafts is not an exercise"
    assert [q["id"] for q in body["sets"][0]["questions"]] == [answered["id"]]
    assert body["unfiled"] == [], "a draft in the pool is unfinished work, not content"

    listed = await learner.get(STUDENT_PATH)
    assert listed.json()["items"][0]["question_count"] == 1, "the row counts what can be answered"


async def test_the_teacher_preview_and_the_student_route_agree(client, session_factory):
    """What a teacher previews is literally what a class would get, from one projection."""
    reading, _questions, _set = await _ready_with_questions(client)
    learner = await _learner(client, session_factory, "learner-preview")

    preview = await client.get(f"{PASSAGE_PATH}/{reading['id']}/preview")
    served = await learner.get(f"{STUDENT_PATH}/{reading['id']}")
    assert preview.status_code == served.status_code == 200
    assert preview.json() == served.json()

    draft_preview = await client.get(f"{PASSAGE_PATH}/{reading['id']}/preview")
    assert KEY not in draft_preview.text, "a preview is still not an answer key"


async def test_a_learner_cannot_open_the_editors_surface(client, session_factory):
    learner = await _learner(client, session_factory, "learner-editor")
    assert error_of(await learner.get(PASSAGE_PATH))["code"] == "unauthorized"
    assert (await learner.get(f"{PASSAGE_PATH}/meta")).status_code == 401
    assert (await learner.post(PASSAGE_PATH, json={"title": "T", "body": NINE_WORDS})).status_code == 401


async def test_the_student_list_accepts_only_the_sorts_it_offers(client, session_factory):
    learner = await _learner(client, session_factory, "learner-sort")
    bogus = await learner.get(f"{STUDENT_PATH}?sort=checksum")
    assert bogus.status_code == 422, bogus.text
    assert "sort must be one of" in error_of(bogus)["message"]

    meta = await learner.get(f"{STUDENT_PATH}/meta")
    assert meta.status_code == 200, meta.text
    assert set(meta.json()["sortable"]) == set(reading_service.SORTABLE)
    assert "views" not in meta.json() and "layouts" not in meta.json(), "a learner sets no layout"


# --------------------------------------------------------------------------- #
# Listing, bulk, meta
# --------------------------------------------------------------------------- #


async def test_the_bank_and_the_trash_are_two_different_answers(client):
    keep = await _create(client, title="Keep")
    drop = await _create(client, title="Drop")
    await client.delete(f"{PASSAGE_PATH}/{drop['id']}")

    bank = await client.get(f"{PASSAGE_PATH}?view=bank")
    assert [item["id"] for item in bank.json()["items"]] == [keep["id"]]
    trash = await client.get(f"{PASSAGE_PATH}?view=trash")
    assert [item["id"] for item in trash.json()["items"]] == [drop["id"]]
    assert trash.json()["items"][0]["deleted_at"] is not None
    assert (await client.get(f"{PASSAGE_PATH}?view=all")).json()["total"] == 2


async def test_a_text_is_found_by_its_title_by_its_prose_and_by_nothing_else(client):
    market = await _create(client, title="Saturday market", body="Buy bread and milk today.")
    await _create(client, title="Weather", body="It will rain on Tuesday.")

    for query, expected in (("Saturday", 1), ("milk", 1), ("tues", 1), ("zebra", 0)):
        resp = await client.get(f"{PASSAGE_PATH}?q={query}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == expected, query

    by_status = await client.get(f"{PASSAGE_PATH}?status=draft")
    assert by_status.json()["total"] == 0
    await client.post(f"{PASSAGE_PATH}/{market['id']}/status", json={"status": "draft"})
    assert [item["id"] for item in (await client.get(f"{PASSAGE_PATH}?status=draft")).json()["items"]] == [
        market["id"]
    ]

    by_level = await client.get(f"{PASSAGE_PATH}?level=B1")
    assert by_level.json()["total"] == 0
    await client.patch(f"{PASSAGE_PATH}/{market['id']}", json={"level": "B1"})
    assert [item["id"] for item in (await client.get(f"{PASSAGE_PATH}?level=B1")).json()["items"]] == [
        market["id"]
    ]

    by_layout = await client.get(f"{PASSAGE_PATH}?layout=split")
    assert by_layout.json()["total"] == 0
    await client.patch(f"{PASSAGE_PATH}/{market['id']}", json={"layout": "split"})
    after = await client.get(f"{PASSAGE_PATH}?layout=split")
    assert after.json()["total"] == 1


async def test_the_library_refuses_an_unknown_view_sort_or_order(client):
    await _create(client)
    for query, word in (
        ("?view=everything", "view"),
        ("?sort=password_hash", "sort"),
        ("?order=sideways", "order"),
        ("?status=recycled", "status"),
        ("?layout=sidebar", "layout"),
    ):
        resp = await client.get(PASSAGE_PATH + query)
        assert resp.status_code == 422, f"{query} -> {resp.status_code}"
        assert word in error_of(resp)["message"]


async def test_a_list_page_never_repeats_or_skips_a_row(client):
    ids = []
    for index in range(5):
        made = await _create(client, title=f"Text {index}", body="one two " * (index + 1))
        ids.append(made["id"])

    first = await client.get(f"{PASSAGE_PATH}?sort=word_count&order=asc&page=1&page_size=2")
    second = await client.get(f"{PASSAGE_PATH}?sort=word_count&order=asc&page=2&page_size=2")
    third = await client.get(f"{PASSAGE_PATH}?sort=word_count&order=asc&page=3&page_size=2")
    seen = [
        item["id"]
        for resp in (first, second, third)
        for item in resp.json()["items"]
    ]
    assert sorted(seen) == sorted(ids), "paging must cover every row exactly once"
    assert first.json()["total"] == 5
    counts = [item["word_count"] for resp in (first, second, third) for item in resp.json()["items"]]
    assert counts == sorted(counts)


async def test_a_rows_counts_are_the_sets_and_questions_the_editor_shows(client):
    reading = await _create(client)
    questions = [
        await _question(client, reading["id"], prompt=f"Q {index}") for index in range(3)
    ]
    one = await _set(client, reading["id"], "One")
    two = await _set(client, reading["id"], "Two")
    await _file(client, one["id"], questions[0]["id"], questions[1]["id"])
    await _file(client, two["id"], questions[2]["id"])

    item = (await client.get(f"{PASSAGE_PATH}?q={reading['title']}")).json()["items"][0]
    assert (item["set_count"], item["question_count"]) == (2, 3)

    await client.delete(f"{PASSAGE_PATH}/sets/{two['id']}")
    after = (await client.get(f"{PASSAGE_PATH}?q={reading['title']}")).json()["items"][0]
    assert (after["set_count"], after["question_count"]) == (1, 3), "unfiled is not deleted"


async def test_bulk_answers_per_row_and_never_loses_a_text(client):
    ready = await _create(client, title="Already ready", status="ready")
    draft = await _create(client, title="Still draft", status="draft")
    other = await _create(client, title="Other draft", status="draft")

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
        f"{PASSAGE_PATH}/bulk",
        json={"passage_ids": [other["id"]], "action": "set_level", "level": "B2"},
    )
    assert levelled.json()["updated"] == [other["id"]]
    assert (await _detail(client, other["id"]))["level"] == "B2"

    trashed = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [draft["id"], other["id"]], "action": "trash"}
    )
    assert trashed.json()["updated"] == [draft["id"], other["id"]]
    assert {
        item["id"] for item in (await client.get(f"{PASSAGE_PATH}?view=trash")).json()["items"]
    } == {draft["id"], other["id"]}, "both rows are in the trash and neither was copied"

    twice = await client.post(
        f"{PASSAGE_PATH}/bulk", json={"passage_ids": [draft["id"]], "action": "trash"}
    )
    assert twice.json()["updated"] == []
    assert twice.json()["refused"] == [{"id": draft["id"], "reason": "already in the trash"}]

    status_on_trash = await client.post(
        f"{PASSAGE_PATH}/bulk",
        json={"passage_ids": [draft["id"], ready["id"]], "action": "status", "status": "archived"},
    )
    body = status_on_trash.json()
    assert body["updated"] == [ready["id"]], "one trashed text does not stop the other being archived"
    assert "trash" in body["refused"][0]["reason"]

    ghost = str(uuid.uuid4())
    absent = await client.post(f"{PASSAGE_PATH}/bulk", json={"passage_ids": [ghost], "action": "trash"})
    assert absent.json()["not_found"] == [ghost]


async def test_bulk_has_no_entry_point_for_editing_prose(client):
    """A body is content, and content that changes in bulk belongs to the review pipeline."""
    created = await _create(client)
    for action, extra in (("retitle", {"title": "x"}), ("set_body", {"body": "x"}), ("purge", {})):
        refused = await client.post(
            f"{PASSAGE_PATH}/bulk", json={"passage_ids": [created["id"]], "action": action, **extra}
        )
        assert refused.status_code == 422, action
        assert "action must be one of" in error_of(refused)["message"]

    empty = await client.post(f"{PASSAGE_PATH}/bulk", json={"passage_ids": [], "action": "trash"})
    assert empty.status_code == 422


async def test_the_meta_endpoint_is_the_vocabulary_the_endpoints_enforce(client):
    resp = await client.get(f"{PASSAGE_PATH}/meta")
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["layouts"] == list(passage_service.LAYOUTS)
    assert meta["statuses"] == list(passage_service.SETTABLE_STATUSES)
    assert meta["views"] == list(passage_service.VIEWS)
    assert set(meta["sortable"]) == set(reading_service.SORTABLE)
    assert meta["max_body_characters"] > 0
    assert meta["max_sets"] > 0
    assert meta["excerpt_characters"] == reading_service.EXCERPT_CHARACTERS

    # every language it offers is a language a create request will accept
    for code in meta["learning_languages"]:
        made = await _create(client, title=f"Lang {code}", language=code)
        assert made["language"] == code


async def test_the_surface_is_admin_only_and_writes_are_csrf_guarded(client, session_factory):
    anonymous = session_factory()._c
    assert (await anonymous.get(PASSAGE_PATH)).status_code == 401
    assert (await anonymous.get(f"{PASSAGE_PATH}/meta")).status_code == 401

    raw = client._c
    without_csrf = await raw.post(PASSAGE_PATH, json={"title": "T", "body": NINE_WORDS})
    assert without_csrf.status_code == 403, without_csrf.text
    assert error_of(without_csrf)["code"] == "csrf_failed"
    assert await _rows() == [], "a refused write must not create a row"


async def test_an_unknown_id_is_a_404_on_every_route(client):
    missing = str(uuid.uuid4())
    for path in (
        f"{PASSAGE_PATH}/{missing}",
        f"{PASSAGE_PATH}/{missing}/sets",
        f"{PASSAGE_PATH}/{missing}/preview",
    ):
        resp = await client.get(path)
        assert resp.status_code == 404, path
        assert error_of(resp)["code"] == "not_found"

    assert (await client.patch(f"{PASSAGE_PATH}/{missing}", json={"title": "Ghost"})).status_code == 404
    assert (await client.delete(f"{PASSAGE_PATH}/{missing}")).status_code == 404
    assert (await client.post(f"{PASSAGE_PATH}/{missing}/restore")).status_code == 404
    assert (
        await client.post(f"{PASSAGE_PATH}/{missing}/status", json={"status": "draft"})
    ).status_code == 404
    assert (
        await client.post(f"{PASSAGE_PATH}/{missing}/sets", json={"title": "Ghost block"})
    ).status_code == 404


# --------------------------------------------------------------------------- #
# The audit trail
# --------------------------------------------------------------------------- #


async def test_audit_rows_record_the_shape_of_the_change_not_the_prose(client):
    created = await _create(client, title="Audited", body="one two three")
    await client.patch(f"{PASSAGE_PATH}/{created['id']}", json={"body": "one two three four five"})
    await client.post(f"{PASSAGE_PATH}/{created['id']}/status", json={"status": "draft"})

    from app.models.ops import AuditLog

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog.action, AuditLog.before, AuditLog.after).where(
                    AuditLog.target_type == "reading"
                )
            )
        ).all()

    by_action = {action: (before, after) for action, before, after in rows}
    assert {"reading.created", "reading.updated", "reading.status.changed"} <= set(by_action)

    _before, after = by_action["reading.created"]
    assert after["word_count"] == 3 and after["status"] == "ready"
    assert "body" not in after, "an editor history must not be a second copy of the library"
    assert "one two three" not in str(rows), "the prose itself is not logged"

    before, after = by_action["reading.updated"]
    assert (before["word_count"], after["word_count"]) == (3, 5)

    before, after = by_action["reading.status.changed"]
    assert (before["status"], after["status"]) == ("ready", "draft")


async def test_a_set_is_audited_by_its_own_id_and_the_assignment_by_its_effect(client):
    reading = await _create(client)
    question = await _question(client, reading["id"], prompt="Assigned once")
    set_row = await _set(client, reading["id"], "Block")
    await _file(client, set_row["id"], question["id"])

    from app.models.ops import AuditLog

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                # `created_at`, never `id`: a primary key from gen_random_uuid() has no
                # order to it, and an audit trail read in the wrong order tells a story
                # that never happened.
                select(AuditLog)
                .where(AuditLog.target_type == "reading_question_set")
                .order_by(AuditLog.created_at, AuditLog.action)
            )
        ).scalars().all()

    assert [row.action for row in rows] == ["reading.set.created", "reading.set.assigned"]
    assigned = rows[1].after
    assert assigned["questions"] == [question["id"]]
    assert assigned["unfiled"] == [] and assigned["moved_in"] == []
    assert str(reading["id"]) not in str(assigned), "the set row is the target, not the text"


async def test_the_enum_words_the_service_writes_are_the_ones_the_api_reports(client):
    """A status column that drifts from the payloads would show a teacher an empty chip."""
    reading = await _create(client)
    assert enums.ContentStatus(reading["status"]) is enums.ContentStatus.READY

    detail = await _detail(client, reading["id"])
    assert detail["status"] == reading["status"]
    row = (await _rows())[0]
    assert row.status.value == reading["status"]
