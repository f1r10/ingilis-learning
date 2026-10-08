"""Practice over HTTP, against real Postgres (Phase 6).

`tests/test_practice_rules.py` settles the pure rules - reachability, shuffling, the totals
a summary is built from. What only a live database and a real learner session can prove is
the part that has to hold under concurrency and across requests:

* a run is a token this backend issued, and every answer is authorised against the catalog
  behind that token rather than against the body the learner sent;
* the events in `activity_event` are the *only* record - no practice session table, no exam
  row, no question version written by practising;
* the feedback timing really withholds a verdict and really hands it over on finish;
* a retry is a second event, and the newest one is what a summary counts;
* one learner cannot read, answer or finish another learner's run;
* content that cannot be served is skipped and counted, never served half-finished and never
  silently dropped.
"""
from __future__ import annotations

import asyncio
import uuid

from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core.database import SessionLocal
from app.models.activity import ActivityEvent, Favorite
from app.models.assessment import Exam
from app.models.content import Question, QuestionVersion

CATALOGS = "/api/v1/catalogs"
PRACTICE = "/api/v1/student/practice"
FAVORITES = "/api/v1/student/favorites"
QUESTIONS = "/api/v1/questions"
VOCABULARY = "/api/v1/vocabulary"
READING = "/api/v1/reading"

#: A string that only ever belongs in a teacher-only field. Its appearance anywhere in a
#: learner payload fails the test that looked for it.
KEY = "ZULU-PRACTICE-KEY"


# --------------------------------------------------------------------------- #
# Content the teacher writes
# --------------------------------------------------------------------------- #


def choice_body(prompt: str = "Which word means 'kite'?", *, correct: int = 0, **over) -> dict:
    """A ready multiple-choice question.

    `explanation` is deliberately plain text: under `instant` timing the learner is shown it,
    so it is not a place to hide the sentinel. The sentinel goes in `teacher_notes`, which no
    learner projection carries.
    """
    options = [{"text": "apple"}, {"text": "table"}, {"text": "window"}]
    options[correct]["correct"] = True
    body = {
        "type": "multiple_choice",
        "prompt": prompt,
        "config": {"options": options},
        "explanation": "An apple is the fruit.",
        "teacher_notes": f"{KEY}: watch for the 'table' distractor.",
        "score": 2,
        "level": "A2",
        "learning_language": "en",
    }
    body.update(over)
    return body


def essay_body(prompt: str = "Describe your morning.") -> dict:
    return {
        "type": "essay",
        "prompt": prompt,
        "config": {"min_words": 5, "guidance": "Three sentences is plenty."},
        "teacher_notes": KEY,
        "score": 4,
        "level": "B1",
        "learning_language": "en",
    }


def word_body(word: str = "improve", **over) -> dict:
    body = {
        "word": word,
        "learning_language": "en",
        "definition": "to make something better",
        "part_of_speech": "verb",
        "level": "B1",
        "notes": f"{KEY}: bring up the comparative.",
        "translations": [
            {"language": "az", "value": "təkmilləşdirmək"},
            {"language": "ru", "value": "улучшать"},
        ],
    }
    body.update(over)
    return body


async def _post(client, path: str, body: dict, *, expect: int = 201) -> dict:
    resp = await client.post(path, json=body)
    assert resp.status_code == expect, f"{path} -> {resp.status_code} {resp.text}"
    return resp.json()


async def _question(client, prompt: str = "Which word means 'kite'?", **over) -> dict:
    return await _post(client, QUESTIONS, choice_body(prompt, **over))


async def _essay(client, prompt: str = "Describe your morning.") -> dict:
    return await _post(client, QUESTIONS, essay_body(prompt))


async def _word(client, word: str = "improve", **over) -> dict:
    return await _post(client, VOCABULARY, word_body(word, **over))


async def _reading(client, title: str = "A day at the market") -> dict:
    return await _post(client, READING, {"title": title, "body": "The market opens at six.", "language": "en"})


async def _set(client, reading_id: str, title: str) -> dict:
    return await _post(client, f"{READING}/{reading_id}/sets", {"title": title})


async def _file(client, set_id: str, *question_ids: str) -> dict:
    return await _post(client, f"{READING}/sets/{set_id}/questions", {"question_ids": list(question_ids)}, expect=200)


async def _reading_question(client, reading_id: str, prompt: str) -> dict:
    return await _question(client, prompt, context_kind="reading_bound", reading_id=reading_id)


async def _reference(kind: str, ref_id: str, **config) -> dict:
    return {"kind": kind, "ref_id": ref_id, "config": config}


async def _catalog(client, name: str = "Spring practice", **over) -> dict:
    """A catalog in the order the teacher built it, so a step order can be asserted."""
    body = {"name": name, "learning_language": "en", "level": "B1", "shuffle_default": False}
    body.update(over)
    return await _post(client, CATALOGS, body)


async def _published(client, name: str = "Spring practice", *items: dict, **over) -> dict:
    catalog = await _catalog(client, name, **over)
    if items:
        await _post(client, f"{CATALOGS}/{catalog['id']}/items", {"items": list(items)})
    return await _post(
        client, f"{CATALOGS}/{catalog['id']}/status", {"status": "ready"}, expect=200
    )


async def _status(client, path: str, status: str) -> dict:
    return await _post(client, f"{path}/status", {"status": status}, expect=200)


# --------------------------------------------------------------------------- #
# The learner
# --------------------------------------------------------------------------- #


async def _learner(client, session_factory, username: str):
    """A real student row, a real access key and a logged-in session of their own."""
    body = await _post(client, "/api/v1/students", {"name": "Ada", "surname": username, "username": username})
    session = session_factory()
    await session.login_student(body["access_key"])
    return session


async def _run(learner, catalog_id: str, *, body: dict | None = None, **params) -> dict:
    """Open a run. `shuffle` travels in the body, `language` as a query, as the API declares."""
    resp = await learner.post(
        f"{PRACTICE}/catalogs/{catalog_id}/run", json=body, params=params or None
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _answer(learner, session_id: str, question_id: str, response=None, **over) -> dict:
    body = {"session_id": session_id, "question_id": question_id, "response": response}
    body.update(over)
    resp = await learner.post(f"{PRACTICE}/answer", json=body)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    return resp.json()


async def _events(*, event_type: str | None = None) -> list[ActivityEvent]:
    async with SessionLocal() as db:
        stmt = select(ActivityEvent).order_by(ActivityEvent.occurred_at, ActivityEvent.id)
        if event_type is not None:
            stmt = stmt.where(ActivityEvent.event_type == event_type)
        return list((await db.execute(stmt)).scalars().all())


async def _count(model) -> int:
    async with SessionLocal() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


def _prompts(run: dict) -> list[str]:
    return [step["view"]["prompt"] for step in run["steps"] if step["kind"] == "question"]


# --------------------------------------------------------------------------- #
# What a learner may see
# --------------------------------------------------------------------------- #


async def test_a_learner_sees_a_catalog_only_when_its_whole_folder_chain_is_published(
    client, session_factory
):
    question = await _question(client)
    folder = await _catalog(client, "Unit 1")
    child = await _published(client, "Week two", await _reference("question", question["id"]), parent_id=folder["id"])
    learner = await _learner(client, session_factory, "practise-chain")

    listed = (await learner.get(f"{PRACTICE}/catalogs")).json()
    assert listed["total"] == 0, "a published lesson inside a draft folder must not be offered"

    refused = await learner.get(f"{PRACTICE}/catalogs/{child['id']}")
    assert refused.status_code == 404, refused.text
    assert error_of(refused)["code"] == "not_found"

    await _status(client, f"{CATALOGS}/{folder['id']}", "ready")
    listed = (await learner.get(f"{PRACTICE}/catalogs")).json()
    assert [row["name"] for row in listed["items"]] == ["Unit 1", "Week two"]
    assert listed["items"][1]["runs"] == 0


async def test_an_unpublished_or_binned_catalog_is_a_plain_404_to_the_learner(client, session_factory):
    """Not "unpublished": a learner is not told what a teacher has not finished."""
    question = await _question(client)
    reference = await _reference("question", question["id"])
    learner = await _learner(client, session_factory, "practise-draft")

    draft = await _catalog(client, "Still being written")
    await _post(client, f"{CATALOGS}/{draft['id']}/items", {"items": [reference]})
    for path in (f"{PRACTICE}/catalogs/{draft['id']}", f"{PRACTICE}/catalogs/{draft['id']}/known"):
        resp = await learner.get(path)
        assert resp.status_code == 404, f"{path} -> {resp.status_code} {resp.text}"
    assert (await learner.post(f"{PRACTICE}/catalogs/{draft['id']}/run")).status_code == 404

    published = await _published(client, "Week two", reference)
    assert (await learner.get(f"{PRACTICE}/catalogs/{published['id']}")).status_code == 200
    assert (await client.delete(f"{CATALOGS}/{published['id']}")).status_code == 200
    assert (await learner.get(f"{PRACTICE}/catalogs/{published['id']}")).status_code == 404
    assert (await learner.post(f"{PRACTICE}/catalogs/{published['id']}/run")).status_code == 404

    missing = await learner.get(f"{PRACTICE}/catalogs/{uuid.uuid4()}")
    assert missing.status_code == 404, missing.text


async def test_the_learner_list_carries_no_lifecycle_and_counts_their_own_runs(
    client, session_factory
):
    question = await _question(client)
    catalog = await _published(client, "Week two", await _reference("question", question["id"]))
    learner = await _learner(client, session_factory, "practise-list")

    await _run(learner, catalog["id"])
    await _run(learner, catalog["id"])
    row = (await learner.get(f"{PRACTICE}/catalogs")).json()["items"][0]
    assert row["runs"] == 2
    assert row["item_count"] == 1 and row["counts"] == {"question": 1}
    assert "status" not in row and "parent_name" not in row, row
    assert KEY not in str(row)

    detail = (await learner.get(f"{PRACTICE}/catalogs/{catalog['id']}")).json()
    assert [run["finished"] for run in detail["runs"]] == [False, False]
    assert detail["runs"][0]["answered"] == 0


async def test_the_learner_filters_and_sorting_are_validated_not_guessed(client, session_factory):
    await _published(client, "Week two", await _reference("question", (await _question(client))["id"]))
    learner = await _learner(client, session_factory, "practise-filters")

    found = (await learner.get(f"{PRACTICE}/catalogs", params={"q": "week"})).json()
    assert found["total"] == 1
    assert (await learner.get(f"{PRACTICE}/catalogs", params={"q": "nothing"})).json()["total"] == 0
    assert (await learner.get(f"{PRACTICE}/catalogs", params={"level": "b1"})).json()["total"] == 1
    assert (await learner.get(f"{PRACTICE}/catalogs", params={"language": "en"})).json()["total"] == 1

    bad_sort = await learner.get(f"{PRACTICE}/catalogs", params={"sort": "score"})
    assert bad_sort.status_code == 422, bad_sort.text
    assert "sort must be one of" in error_of(bad_sort)["message"]

    meta = (await learner.get(f"{PRACTICE}/meta")).json()
    assert meta["known_states"] == ["known", "learning"]
    assert meta["favorite_kinds"] == ["question", "vocabulary"]
    assert meta["feedback_timings"] == ["instant", "after_session"]
    assert "en" in meta["learning_languages"] and "B1" in meta["levels"]


# --------------------------------------------------------------------------- #
# Opening a run
# --------------------------------------------------------------------------- #


async def test_a_run_serves_the_catalog_in_order_with_no_answer_key_in_it(client, session_factory):
    first = await _question(client, "First?")
    second = await _word(client, "improve")
    catalog = await _published(
        client,
        "Week two",
        await _reference("question", first["id"]),
        await _reference("vocabulary", second["id"]),
    )
    learner = await _learner(client, session_factory, "practise-serve")

    resp = await learner.post(f"{PRACTICE}/catalogs/{catalog['id']}/run")
    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert KEY not in resp.text, "a teacher note must never reach a learner"
    assert run["session_id"] and len(run["session_id"]) == 32
    assert run["catalog_id"] == catalog["id"] and run["catalog_name"] == "Week two"
    assert run["shuffle"] is False and run["skipped_count"] == 0
    assert run["feedback_timing"] == "instant" and run["known_states_enabled"] is False
    assert [step["position"] for step in run["steps"]] == [0, 1]
    assert [step["kind"] for step in run["steps"]] == ["question", "vocabulary"]
    assert _prompts(run) == ["First?"]

    view = run["steps"][0]["view"]
    assert [option["text"] for option in view["config"]["options"]] == ["apple", "table", "window"]
    assert all("correct" not in option for option in view["config"]["options"]), view["config"]
    assert view["answer_widget"] == "single_option"
    assert view["explanation_available"] is True and "explanation" not in view
    assert run["steps"][1]["view"]["word"] == "improve"
    assert "notes" not in run["steps"][1]["view"], run["steps"][1]["view"]


async def test_unservable_references_are_skipped_and_the_learner_is_told_how_many(
    client, session_factory
):
    ready = await _question(client, "Ready?")
    draft = await _question(client, "Still being written?")
    word = await _word(client, "gone")
    catalog = await _published(
        client,
        "Week two",
        await _reference("question", ready["id"]),
        await _reference("question", draft["id"]),
        await _reference("vocabulary", word["id"]),
    )
    learner = await _learner(client, session_factory, "practise-skip")
    assert (await _run(learner, catalog["id"]))["skipped_count"] == 0

    # Both holes are made after the catalog was published: the teacher sees them, and the
    # learner is told how many steps were left out rather than being served a short run.
    await _status(client, f"{QUESTIONS}/{draft['id']}", "draft")
    assert (await client.delete(f"{VOCABULARY}/{word['id']}")).status_code == 200
    teacher_view = (await client.get(f"{CATALOGS}/{catalog['id']}")).json()
    assert teacher_view["unavailable_count"] == 2

    run = await _run(learner, catalog["id"])
    assert run["skipped_count"] == 2
    assert _prompts(run) == ["Ready?"]
    assert [step["position"] for step in run["steps"]] == [0], "the served steps are renumbered from zero"


async def test_a_shuffled_run_keeps_every_step_and_its_order_comes_from_the_run(
    client, session_factory
):
    ids = [(await _question(client, f"Q{index}?"))["id"] for index in range(6)]
    catalog = await _published(
        client, "Week two", *[await _reference("question", question_id) for question_id in ids]
    )
    learner = await _learner(client, session_factory, "practise-shuffle")

    straight = await _run(learner, catalog["id"], body={"shuffle": False})
    assert [step["ref_id"] for step in straight["steps"]] == ids
    assert straight["shuffle"] is False

    mixed = await _run(learner, catalog["id"], body={"shuffle": True})
    assert mixed["shuffle"] is True
    assert sorted(step["ref_id"] for step in mixed["steps"]) == sorted(ids), "a shuffle reorders, it never drops"
    assert [step["position"] for step in mixed["steps"]] == list(range(6))

    starts = await _events(event_type="practice_run_start")
    seeds = [row.payload["shuffle_seed"] for row in starts if row.payload.get("shuffle")]
    assert len(seeds) == 1 and seeds[0], "the seed is on the start event, so the order can be rebuilt"
    assert [row.payload["shuffle"] for row in starts] == [False, True]


async def test_a_run_can_be_picked_up_in_the_order_it_was_opened_in(client, session_factory):
    """The resume half of the stored shuffle seed.

    A learner who closes the tab at step seven must come back to the same step seven, so the
    order is served again from the seed rather than drawn afresh - and re-reading a run may
    not open a second one, which would double the run count on their own history.
    """
    ids = [(await _question(client, f"Q{index}?"))["id"] for index in range(5)]
    catalog = await _published(
        client, "Week two", *[await _reference("question", question_id) for question_id in ids]
    )
    learner = await _learner(client, session_factory, "practise-resume")

    mixed = await _run(learner, catalog["id"], body={"shuffle": True})
    opened = len(await _events(event_type="practice_run_start"))

    resp = await learner.get(f"{PRACTICE}/runs/{mixed['session_id']}/steps")
    assert resp.status_code == 200, resp.text
    again = resp.json()
    assert KEY not in resp.text, "a resume is a learner surface too"
    assert again["session_id"] == mixed["session_id"]
    assert again["catalog_name"] == "Week two" and again["shuffle"] is True
    assert [step["ref_id"] for step in again["steps"]] == [step["ref_id"] for step in mixed["steps"]]
    assert [step["position"] for step in again["steps"]] == list(range(5))
    assert len(await _events(event_type="practice_run_start")) == opened

    # The token the resume hands back is the one the answers belong to.
    answered = await _answer(learner, again["session_id"], again["steps"][0]["ref_id"], {"option_index": 0})
    assert answered["recorded"] is True and answered["correct"] is True


async def test_an_unshuffled_run_resumes_in_the_catalogs_own_order(client, session_factory):
    ids = [(await _question(client, f"Q{index}?"))["id"] for index in range(3)]
    catalog = await _published(
        client, "Week two", *[await _reference("question", question_id) for question_id in ids]
    )
    learner = await _learner(client, session_factory, "practise-resume-plain")

    run = await _run(learner, catalog["id"], body={"shuffle": False})
    again = (await learner.get(f"{PRACTICE}/runs/{run['session_id']}/steps")).json()
    assert [step["ref_id"] for step in again["steps"]] == ids
    assert again["shuffle"] is False and again["skipped_count"] == 0


async def test_a_resumed_run_counts_what_stopped_being_servable(client, session_factory):
    ready = await _question(client, "Ready?")
    taken_back = await _question(client, "Taken back for editing?")
    catalog = await _published(
        client,
        "Week two",
        await _reference("question", ready["id"]),
        await _reference("question", taken_back["id"]),
    )
    learner = await _learner(client, session_factory, "practise-resume-hole")

    run = await _run(learner, catalog["id"])
    assert run["skipped_count"] == 0
    await _status(client, f"{QUESTIONS}/{taken_back['id']}", "draft")

    again = (await learner.get(f"{PRACTICE}/runs/{run['session_id']}/steps")).json()
    assert again["skipped_count"] == 1, "the learner is told the run is shorter, not shown a blank step"
    assert _prompts(again) == ["Ready?"]

    refused = await learner.post(
        f"{PRACTICE}/answer",
        json={"session_id": run["session_id"], "question_id": taken_back["id"], "response": {"option_index": 0}},
    )
    assert refused.status_code == 404


async def test_a_run_still_shows_its_exercises_after_its_catalog_is_archived(client, session_factory):
    """Read-only after the fact: the verdicts and the exercises stay the learner's."""
    question = await _question(client)
    catalog = await _published(client, "Week two", await _reference("question", question["id"]))
    learner = await _learner(client, session_factory, "practise-resume-archived")

    run = await _run(learner, catalog["id"])
    await _status(client, f"{CATALOGS}/{catalog['id']}", "archived")

    again = (await learner.get(f"{PRACTICE}/runs/{run['session_id']}/steps")).json()
    assert [step["ref_id"] for step in again["steps"]] == [question["id"]]
    answered = await learner.post(
        f"{PRACTICE}/answer",
        json={"session_id": run["session_id"], "question_id": question["id"], "response": {"option_index": 0}},
    )
    assert answered.status_code == 404


async def test_the_steps_of_a_run_are_read_only_by_the_learner_they_were_issued_to(client, session_factory):
    question = await _question(client)
    catalog = await _published(client, "Week two", await _reference("question", question["id"]))
    learner = await _learner(client, session_factory, "practise-resume-owner")
    other = await _learner(client, session_factory, "practise-resume-stranger")
    run = await _run(learner, catalog["id"])

    assert (await other.get(f"{PRACTICE}/runs/{run['session_id']}/steps")).status_code == 404
    assert (await learner.get(f"{PRACTICE}/runs/{'0' * 32}/steps")).status_code == 404
    assert (await learner.get(f"{PRACTICE}/runs/not-a-token/steps")).status_code == 404
    assert len(await _events(event_type="practice_run_start")) == 1


async def test_a_word_card_is_narrowed_to_the_language_the_learner_asked_for(client, session_factory):
    word = await _word(client)
    catalog = await _published(client, "Week two", await _reference("vocabulary", word["id"]))
    learner = await _learner(client, session_factory, "practise-language")

    every = await _run(learner, catalog["id"])
    assert [row["language"] for row in every["steps"][0]["view"]["translations"]] == ["az", "ru"]

    one = await _run(learner, catalog["id"], language="az")
    assert [row["value"] for row in one["steps"][0]["view"]["translations"]] == ["təkmilləşdirmək"]


async def test_a_ready_question_bound_to_a_draft_passage_is_neither_served_nor_answerable(
    client, session_factory
):
    """The second half of serveability: a published question must not carry an unpublished text."""
    reading = await _reading(client)
    question = await _reading_question(client, reading["id"], "What opens at six?")
    catalog = await _published(
        client, "Week two", await _reference("question", question["id"]), await _reference("reading", reading["id"])
    )
    learner = await _learner(client, session_factory, "practise-passage")

    run = await _run(learner, catalog["id"])
    assert run["skipped_count"] == 0 and len(run["steps"]) == 2

    await _status(client, f"{READING}/{reading['id']}", "draft")
    reopened = await _run(learner, catalog["id"])
    assert reopened["skipped_count"] == 2 and reopened["steps"] == []

    refused = await learner.post(
        f"{PRACTICE}/answer", json={"session_id": run["session_id"], "question_id": question["id"], "response": {}}
    )
    assert refused.status_code == 404, refused.text


async def test_a_block_reference_serves_only_that_block_and_refuses_its_sibling(
    client, session_factory
):
    """"Do block two again" must not quietly become "do the whole text"."""
    reading = await _reading(client)
    inside = await _reading_question(client, reading["id"], "In the block?")
    outside = await _reading_question(client, reading["id"], "In the other block?")
    wanted = await _set(client, reading["id"], "Part one")
    other = await _set(client, reading["id"], "Part two")
    await _file(client, wanted["id"], inside["id"])
    await _file(client, other["id"], outside["id"])
    catalog = await _published(
        client, "Week two", await _reference("reading", reading["id"], set_id=wanted["id"])
    )
    learner = await _learner(client, session_factory, "practise-block")

    run = await _run(learner, catalog["id"])
    assert len(run["steps"]) == 1
    view = run["steps"][0]["view"]
    assert [block["title"] for block in view["sets"]] == ["Part one"]
    assert view["unfiled"] == []
    assert [question["prompt"] for question in view["sets"][0]["questions"]] == ["In the block?"]

    answered = await _answer(learner, run["session_id"], inside["id"], {"option_index": 0})
    assert answered["correct"] is True

    sibling = await learner.post(
        f"{PRACTICE}/answer",
        json={"session_id": run["session_id"], "question_id": outside["id"], "response": {"option_index": 0}},
    )
    assert sibling.status_code == 422, sibling.text
    assert "not part of this practice" in error_of(sibling)["message"]


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #


async def test_an_answer_is_graded_and_written_once_against_the_run_it_belongs_to(
    client, session_factory
):
    right = await _question(client, "Right?", correct=0)
    catalog = await _published(client, "Week two", await _reference("question", right["id"]))
    learner = await _learner(client, session_factory, "practise-answer")
    run = await _run(learner, catalog["id"])

    body = await _answer(learner, run["session_id"], right["id"], {"option_index": 0}, time_spent_seconds=12)
    assert body == {
        "recorded": True,
        "session_id": run["session_id"],
        "question_id": right["id"],
        "withheld": False,
        "correct": True,
        "score": 2.0,
        "max_score": 2.0,
        "requires_manual": False,
        "explanation": "An apple is the fruit.",
    }
    assert KEY not in str(body)

    rows = await _events(event_type="practice_answer")
    assert len(rows) == 1
    event = rows[0]
    assert event.category == "assessment"
    assert event.session_id == run["session_id"]
    assert str(event.context_id) == catalog["id"] and event.context_type == "catalog"
    assert str(event.ref_question_id) == right["id"]
    assert event.correct is True and event.time_spent_seconds == 12
    assert event.human_summary is None, "the sentence a teacher reads is rendered by their screen"
    assert event.payload["grade"]["detail"] == {"correct_index": 0, "given_index": 0}, (
        "the grader's reasoning is kept for the teacher"
    )
    assert event.payload["question_version"] == right["current_version"]
    assert event.payload["item_kind"] == "question" and event.payload["withheld"] is False


async def test_a_question_the_opened_catalog_does_not_reach_cannot_be_answered_through_it(
    client, session_factory
):
    """Otherwise "practise this catalog" would be a door to every ready question in the school."""
    inside = await _question(client, "Inside?")
    stranger = await _question(client, "Somewhere else?")
    catalog = await _published(client, "Week two", await _reference("question", inside["id"]))
    learner = await _learner(client, session_factory, "practise-scope")
    run = await _run(learner, catalog["id"])

    refused = await learner.post(
        f"{PRACTICE}/answer",
        json={"session_id": run["session_id"], "question_id": stranger["id"], "response": {"option_index": 0}},
    )
    assert refused.status_code == 422, refused.text
    assert "not part of this practice" in error_of(refused)["message"]
    assert await _events(event_type="practice_answer") == []

    missing = await learner.post(
        f"{PRACTICE}/answer",
        json={"session_id": run["session_id"], "question_id": str(uuid.uuid4()), "response": {}},
    )
    assert missing.status_code == 404, missing.text
    assert await _events(event_type="practice_answer") == []


async def test_a_run_token_belongs_to_the_learner_it_was_issued_to(client, session_factory):
    question = await _question(client)
    catalog = await _published(client, "Week two", await _reference("question", question["id"]))
    mine = await _learner(client, session_factory, "practise-mine")
    theirs = await _learner(client, session_factory, "practise-theirs")
    run = await _run(mine, catalog["id"])

    assert (await theirs.get(f"{PRACTICE}/runs/{run['session_id']}")).status_code == 404
    assert (await theirs.post(f"{PRACTICE}/runs/{run['session_id']}/finish")).status_code == 404
    stolen = await theirs.post(
        f"{PRACTICE}/answer",
        json={"session_id": run["session_id"], "question_id": question["id"], "response": {"option_index": 0}},
    )
    assert stolen.status_code == 404, stolen.text
    assert await _events(event_type="practice_answer") == [], "nothing was filed against somebody else's run"

    # A token of the right shape that this backend never issued is the same answer.
    invented = await mine.post(
        f"{PRACTICE}/answer",
        json={"session_id": "0" * 32, "question_id": question["id"], "response": {}},
    )
    assert invented.status_code == 404, invented.text
    assert error_of(invented)["code"] == "not_found"
    assert (await mine.get(f"{PRACTICE}/runs/{'0' * 32}")).status_code == 404


async def test_a_session_token_that_is_not_one_is_refused_by_the_body(client, session_factory):
    learner = await _learner(client, session_factory, "practise-token")
    for token in ("nope", "0" * 31, "Z" * 32):
        resp = await learner.post(
            f"{PRACTICE}/answer", json={"session_id": token, "question_id": str(uuid.uuid4()), "response": {}}
        )
        assert resp.status_code == 422, f"{token} -> {resp.status_code} {resp.text}"
        assert error_of(resp)["code"] == "validation_failed"
    assert await _events() == []


async def test_after_session_holds_every_verdict_until_the_run_is_finished(client, session_factory):
    right = await _question(client, "Right?", correct=0)
    wrong = await _question(client, "Wrong?", correct=1)
    catalog = await _published(
        client,
        "Week two",
        await _reference("question", right["id"]),
        await _reference("question", wrong["id"]),
        feedback_timing="after_session",
    )
    learner = await _learner(client, session_factory, "practise-withheld")
    run = await _run(learner, catalog["id"])
    assert run["feedback_timing"] == "after_session"

    first = await _answer(learner, run["session_id"], right["id"], {"option_index": 0})
    assert first["withheld"] is True
    assert first["correct"] is None and first["score"] is None and first["explanation"] is None
    assert first["recorded"] is True, "the learner is told it was written down, not left guessing"

    open_run = (await learner.get(f"{PRACTICE}/runs/{run['session_id']}")).json()
    assert open_run["answered"] == 1
    assert open_run["finished"] is False
    assert open_run["correct_count"] == 0 and open_run["incorrect_count"] == 0
    assert open_run["score"] == 0.0 and open_run["max_score"] == 0.0
    assert open_run["results"][0]["correct"] is None
    assert open_run["results"][0]["explanation"] is None

    await _answer(learner, run["session_id"], wrong["id"], {"option_index": 0})
    finished = (await learner.post(f"{PRACTICE}/runs/{run['session_id']}/finish")).json()
    assert finished["finished"] is True and finished["finished_at"]
    assert finished["answered"] == 2
    assert finished["correct_count"] == 1 and finished["incorrect_count"] == 1
    assert finished["score"] == 2.0 and finished["max_score"] == 4.0
    by_prompt = {row["prompt"]: row for row in finished["results"]}
    assert by_prompt["Right?"]["correct"] is True
    assert by_prompt["Wrong?"]["correct"] is False
    assert by_prompt["Right?"]["explanation"] == "An apple is the fruit."
    assert KEY not in str(finished)


async def test_the_newest_answer_to_a_question_is_the_one_a_summary_counts(client, session_factory):
    """Practising means trying again; a retry that was right must not be averaged away."""
    right = await _question(client, "Right?", correct=0)
    catalog = await _published(client, "Week two", await _reference("question", right["id"]))
    learner = await _learner(client, session_factory, "practise-retry")
    run = await _run(learner, catalog["id"])

    await _answer(learner, run["session_id"], right["id"], {"option_index": 2})
    # `occurred_at` is what orders the log, and Postgres stores microseconds: without a
    # moment between them two answers would be tied and "newest" would be a coin toss.
    await asyncio.sleep(0.002)
    again = await _answer(learner, run["session_id"], right["id"], {"option_index": 0})
    assert again["correct"] is True

    summary = (await learner.get(f"{PRACTICE}/runs/{run['session_id']}")).json()
    assert summary["answered"] == 1
    assert summary["correct_count"] == 1 and summary["incorrect_count"] == 0
    assert summary["score"] == 2.0 and summary["max_score"] == 2.0
    assert len(await _events(event_type="practice_answer")) == 2, "the first attempt stays in the log"


async def test_finishing_a_run_twice_answers_the_same_way_and_writes_one_event(client, session_factory):
    right = await _question(client, "Right?", correct=0)
    catalog = await _published(client, "Week two", await _reference("question", right["id"]))
    learner = await _learner(client, session_factory, "practise-finish")
    run = await _run(learner, catalog["id"])
    await _answer(learner, run["session_id"], right["id"], {"option_index": 0})

    first = (await learner.post(f"{PRACTICE}/runs/{run['session_id']}/finish")).json()
    second = (await learner.post(f"{PRACTICE}/runs/{run['session_id']}/finish")).json()
    assert first["correct_count"] == second["correct_count"] == 1
    assert first["finished_at"] == second["finished_at"]
    assert len(await _events(event_type="practice_run_finish")) == 1


async def test_an_essay_is_counted_as_needing_a_teacher_and_never_as_wrong(client, session_factory):
    essay = await _essay(client)
    right = await _question(client, "Right?", correct=0)
    catalog = await _published(
        client,
        "Week two",
        await _reference("question", essay["id"]),
        await _reference("question", right["id"]),
    )
    learner = await _learner(client, session_factory, "practise-essay")
    run = await _run(learner, catalog["id"])

    body = await _answer(learner, run["session_id"], essay["id"], {"text": "I wake at seven and make tea."})
    assert body["requires_manual"] is True
    assert body["correct"] is None, "an unmarked essay is not a marked wrong one"
    assert body["score"] == 0.0 and body["max_score"] == 4.0

    await _answer(learner, run["session_id"], right["id"], {"option_index": 0})
    summary = (await learner.post(f"{PRACTICE}/runs/{run['session_id']}/finish")).json()
    assert summary["manual_count"] == 1
    assert summary["correct_count"] == 1 and summary["incorrect_count"] == 0
    assert summary["score"] == 2.0 and summary["max_score"] == 6.0


async def test_a_run_stops_taking_answers_when_its_catalog_is_unpublished(client, session_factory):
    """A teacher taking a lesson down must not leave a run open against it."""
    right = await _question(client, "Right?", correct=0)
    catalog = await _published(client, "Week two", await _reference("question", right["id"]))
    learner = await _learner(client, session_factory, "practise-unpublished")
    run = await _run(learner, catalog["id"])
    await _answer(learner, run["session_id"], right["id"], {"option_index": 0})

    await _status(client, f"{CATALOGS}/{catalog['id']}", "archived")
    refused = await learner.post(
        f"{PRACTICE}/answer",
        json={"session_id": run["session_id"], "question_id": right["id"], "response": {"option_index": 0}},
    )
    assert refused.status_code == 404, refused.text
    assert len(await _events(event_type="practice_answer")) == 1

    # What the learner already did is theirs, so it is still readable.
    kept = (await learner.get(f"{PRACTICE}/runs/{run['session_id']}")).json()
    assert kept["answered"] == 1 and kept["correct_count"] == 1
    assert (await learner.get(f"{PRACTICE}/catalogs")).json()["total"] == 0


async def test_practising_writes_no_exam_row_and_no_question_version(client, session_factory):
    """The whole reason a run is a token: there is nothing to freeze, so nothing is frozen."""
    right = await _question(client, "Right?", correct=0)
    catalog = await _published(client, "Week two", await _reference("question", right["id"]))
    learner = await _learner(client, session_factory, "practise-no-history")
    before = await _count(QuestionVersion)

    run = await _run(learner, catalog["id"])
    await _answer(learner, run["session_id"], right["id"], {"option_index": 0})
    await learner.post(f"{PRACTICE}/runs/{run['session_id']}/finish")

    assert await _count(QuestionVersion) == before
    assert await _count(Exam) == 0
    async with SessionLocal() as db:
        assert await db.get(Question, uuid.UUID(right["id"])) is not None
    kinds = sorted({row.event_type for row in await _events()})
    assert kinds == ["practice_answer", "practice_run_finish", "practice_run_start"], kinds


# --------------------------------------------------------------------------- #
# Known states and favorites
# --------------------------------------------------------------------------- #


async def test_known_and_learning_marks_are_last_one_wins(client, session_factory):
    word = await _word(client)
    catalog = await _published(
        client, "Week two", await _reference("vocabulary", word["id"]), known_states_enabled=True
    )
    learner = await _learner(client, session_factory, "practise-marks")

    marked = await learner.post(f"{PRACTICE}/catalogs/{catalog['id']}/known", json={"ref_id": word["id"], "state": "known"})
    assert marked.status_code == 200, marked.text
    assert marked.json() == {"ref_id": word["id"], "state": "known"}

    states = (await learner.get(f"{PRACTICE}/catalogs/{catalog['id']}/known")).json()
    assert states["enabled"] is True and states["word_count"] == 1
    assert states["counts"] == {"known": 1, "learning": 0, "unmarked": 0}
    assert states["items"] == [{"ref_id": word["id"], "state": "known"}]

    await asyncio.sleep(0.002)
    await learner.post(f"{PRACTICE}/catalogs/{catalog['id']}/known", json={"ref_id": word["id"], "state": "learning"})
    states = (await learner.get(f"{PRACTICE}/catalogs/{catalog['id']}/known")).json()
    assert states["counts"] == {"known": 0, "learning": 1, "unmarked": 0}
    assert states["items"] == [{"ref_id": word["id"], "state": "learning"}]
    assert len(await _events(event_type="practice_mark_known")) == 1, "the first mark stays in the log"
    assert len(await _events(event_type="practice_mark_learning")) == 1


async def test_marks_are_only_taken_where_the_teacher_turned_them_on(client, session_factory):
    word = await _word(client)
    closed = await _published(client, "No marks here", await _reference("vocabulary", word["id"]))
    learner = await _learner(client, session_factory, "practise-marks-off")

    refused = await learner.post(
        f"{PRACTICE}/catalogs/{closed['id']}/known", json={"ref_id": word["id"], "state": "known"}
    )
    assert refused.status_code == 422, refused.text
    assert "does not use known / learning marks" in error_of(refused)["message"]
    assert (await learner.get(f"{PRACTICE}/catalogs/{closed['id']}/known")).json()["enabled"] is False

    opened = await client.patch(f"{CATALOGS}/{closed['id']}", json={"known_states_enabled": True})
    assert opened.status_code == 200, opened.text
    assert opened.json()["known_states_enabled"] is True
    assert (
        await learner.post(f"{PRACTICE}/catalogs/{closed['id']}/known", json={"ref_id": word["id"], "state": "known"})
    ).status_code == 200


async def test_a_mark_on_a_word_outside_the_practice_is_refused(client, session_factory):
    inside = await _word(client, "improve")
    outside = await _word(client, "develop")
    catalog = await _published(
        client, "Week two", await _reference("vocabulary", inside["id"]), known_states_enabled=True
    )
    learner = await _learner(client, session_factory, "practise-marks-scope")

    refused = await learner.post(
        f"{PRACTICE}/catalogs/{catalog['id']}/known", json={"ref_id": outside["id"], "state": "known"}
    )
    assert refused.status_code == 422, refused.text
    assert "not part of this practice" in error_of(refused)["message"]

    missing = await learner.post(
        f"{PRACTICE}/catalogs/{catalog['id']}/known", json={"ref_id": str(uuid.uuid4()), "state": "known"}
    )
    assert missing.status_code == 404, missing.text
    assert await _events(event_type="practice_mark_known") == []

    unknown_state = await learner.post(
        f"{PRACTICE}/catalogs/{catalog['id']}/known", json={"ref_id": inside["id"], "state": "mastered"}
    )
    assert unknown_state.status_code == 422, unknown_state.text


async def test_saving_the_same_thing_twice_leaves_one_row(client, session_factory):
    """A double click on a heart is not a failure and must not be answered as one."""
    question = await _question(client)
    learner = await _learner(client, session_factory, "practise-favorites")
    body = {"kind": "question", "ref_id": question["id"]}

    first = await learner.post(FAVORITES, json=body)
    assert first.status_code == 201, first.text
    assert first.json()["added"] is True and first.json()["ok"] is True

    second = await learner.post(FAVORITES, json=body)
    assert second.status_code == 201, second.text
    assert second.json() == {"ok": True, "added": False, "kind": "question", "ref_id": question["id"]}

    async with SessionLocal() as db:
        assert int((await db.execute(select(func.count()).select_from(Favorite))).scalar_one()) == 1
    assert len(await _events(event_type="favorite_add")) == 1, "an already-saved item is not a new event"


async def test_the_review_list_names_each_saved_thing_from_its_own_row(client, session_factory):
    question = await _question(client, "Which word means 'kite'?")
    word = await _word(client, "improve")
    learner = await _learner(client, session_factory, "practise-favorites-list")
    assert (await learner.post(FAVORITES, json={"kind": "question", "ref_id": question["id"]})).status_code == 201
    assert (await learner.post(FAVORITES, json={"kind": "vocabulary", "ref_id": word["id"]})).status_code == 201

    body = (await learner.get(FAVORITES)).json()
    assert body["total"] == 2
    by_kind = {row["kind"]: row for row in body["items"]}
    assert by_kind["question"]["title"] == question["prompt"]
    assert by_kind["question"]["detail"] == "multiple_choice"
    assert by_kind["question"]["available"] is True
    assert by_kind["vocabulary"]["title"] == "improve"
    assert by_kind["vocabulary"]["detail"] == "verb"
    assert KEY not in str(body)

    await _status(client, f"{QUESTIONS}/{question['id']}", "draft")
    after = {row["kind"]: row for row in (await learner.get(FAVORITES)).json()["items"]}
    assert after["question"]["available"] is False, "shown as unavailable, not opened into a 404"
    assert after["vocabulary"]["available"] is True


async def test_removing_something_that_is_not_there_is_answered_not_failed(client, session_factory):
    question = await _question(client)
    learner = await _learner(client, session_factory, "practise-favorites-remove")
    body = {"kind": "question", "ref_id": question["id"]}

    absent = await learner.post(f"{FAVORITES}/remove", json=body)
    assert absent.status_code == 200, absent.text
    assert absent.json() == {"ok": True, "removed": False, "kind": "question", "ref_id": question["id"]}

    await learner.post(FAVORITES, json=body)
    removed = await learner.post(f"{FAVORITES}/remove", json=body)
    assert removed.json()["removed"] is True
    assert (await learner.get(FAVORITES)).json()["total"] == 0
    assert len(await _events(event_type="favorite_remove")) == 1

    again = await learner.post(f"{FAVORITES}/remove", json=body)
    assert again.json()["removed"] is False
    assert len(await _events(event_type="favorite_remove")) == 1


async def test_a_favorite_may_only_point_at_content_a_learner_can_use(client, session_factory):
    learner = await _learner(client, session_factory, "practise-favorites-scope")
    draft = await _question(client, "Still being written?")
    await _status(client, f"{QUESTIONS}/{draft['id']}", "draft")

    refused = await learner.post(FAVORITES, json={"kind": "question", "ref_id": draft["id"]})
    assert refused.status_code == 404, refused.text
    assert error_of(refused)["code"] == "not_found"

    missing = await learner.post(FAVORITES, json={"kind": "vocabulary", "ref_id": str(uuid.uuid4())})
    assert missing.status_code == 404, missing.text
    assert await _count(Favorite) == 0

    unknown_kind = await learner.post(FAVORITES, json={"kind": "reading", "ref_id": str(uuid.uuid4())})
    assert unknown_kind.status_code == 422, unknown_kind.text
    assert "kind must be one of" in error_of(unknown_kind)["message"]


# --------------------------------------------------------------------------- #
# Permissions and the CSRF contract
# --------------------------------------------------------------------------- #


async def test_a_teacher_session_cannot_reach_the_practice_surface(client, session_factory):
    catalog = await _published(
        client, "Week two", await _reference("question", (await _question(client))["id"])
    )
    for path in (f"{PRACTICE}/meta", f"{PRACTICE}/catalogs", f"{PRACTICE}/catalogs/{catalog['id']}", FAVORITES):
        resp = await client.get(path)
        assert resp.status_code == 401, f"an admin reached {path}: {resp.status_code}"
    assert await _events() == []


async def test_practice_writes_need_the_csrf_header(client, session_factory):
    catalog = await _published(
        client, "Week two", await _reference("question", (await _question(client))["id"])
    )
    learner = await _learner(client, session_factory, "practise-csrf")

    opened = await learner._c.post(f"{PRACTICE}/catalogs/{catalog['id']}/run")
    assert opened.status_code == 403
    assert error_of(opened)["code"] == "csrf_failed"
    assert await _events() == []


async def test_an_anonymous_visitor_gets_nothing_from_the_practice_surface(session_factory):
    stranger = session_factory()
    catalog_id = str(uuid.uuid4())
    token = "0" * 32
    routes = (
        ("get", f"{PRACTICE}/meta", None),
        ("get", f"{PRACTICE}/catalogs", None),
        ("get", f"{PRACTICE}/catalogs/{catalog_id}", None),
        ("get", f"{PRACTICE}/catalogs/{catalog_id}/known", None),
        ("get", f"{PRACTICE}/runs/{token}", None),
        ("get", f"{PRACTICE}/runs/{token}/steps", None),
        ("get", FAVORITES, None),
        ("post", f"{PRACTICE}/catalogs/{catalog_id}/run", {}),
        ("post", f"{PRACTICE}/answer", {"session_id": token, "question_id": str(uuid.uuid4())}),
        ("post", f"{PRACTICE}/runs/{token}/finish", None),
        ("post", f"{PRACTICE}/catalogs/{catalog_id}/known", {"ref_id": str(uuid.uuid4()), "state": "known"}),
        ("post", FAVORITES, {"kind": "question", "ref_id": str(uuid.uuid4())}),
        ("post", f"{FAVORITES}/remove", {"kind": "question", "ref_id": str(uuid.uuid4())}),
    )
    for method, path, body in routes:
        call = getattr(stranger, method)
        resp = await call(path) if body is None else await call(path, json=body)
        assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"
    assert await _events() == []
