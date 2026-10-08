"""Exam authoring, assignment and the paper's lifecycle over HTTP (Phase 7).

`tests/test_exam_rules.py` settles the words, the schema refusals and the pure arithmetic. What
only a live Postgres can prove is the half that is about rows arriving in a certain order:

* an item pins the version that was current when it was added, and editing the question in the
  bank afterwards does not move the paper - which is the whole difference between an exam and a
  catalog;
* the composition freezes the moment somebody sits it, while the rules stay editable;
* the unique indexes behind one-assignment-per-target are what make the service's guards true
  when two writes land in the same moment;
* a learner's list is one row per paper even when both they and their group were named;
* nothing a learner is sent carries a question's answer key or the teacher's notes.

The sitting itself - the clock, the marks, the review queue - is `test_attempt_db.py`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core.database import SessionLocal
from app.models.assessment import ExamAssignment, ExamAttempt, ExamItem

EXAMS = "/api/v1/exams"
QUESTIONS = "/api/v1/questions"
STUDENTS = "/api/v1/students"
GROUPS = "/api/v1/groups"
READING = "/api/v1/reading"
STUDENT_EXAMS = "/api/v1/student/exams"
ATTEMPTS = "/api/v1/student/attempts"

#: A string that only ever belongs in a teacher-only field of a question. Its appearance anywhere
#: in a learner payload fails the test that looked for it.
KEY = "ZULU-EXAM-KEY"


# --------------------------------------------------------------------------- #
# Content the teacher writes
# --------------------------------------------------------------------------- #


def choice_body(prompt: str = "Which word means 'kite'?", *, correct: int = 0, **over) -> dict:
    """A ready multiple-choice question worth 2.

    `explanation` stays plain text because the paper's own switches can hand it to a learner;
    the sentinel lives in `teacher_notes`, which no learner projection carries.
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


def exam_body(title: str, **over) -> dict:
    body = {
        "title": title,
        "learning_language": "en",
        "level": "B1",
        "duration_minutes": 30,
        "max_attempts": 1,
        "passing_score": 50,
    }
    body.update(over)
    return body


async def _post(client, path: str, body: dict, *, expect: int = 201) -> dict:
    resp = await client.post(path, json=body)
    assert resp.status_code == expect, f"POST {path} -> {resp.status_code} {resp.text}"
    return resp.json()


async def _patch(client, path: str, body: dict) -> dict:
    resp = await client.patch(path, json=body)
    assert resp.status_code == 200, f"PATCH {path} -> {resp.status_code} {resp.text}"
    return resp.json()


async def _get(client, path: str) -> dict:
    resp = await client.get(path)
    assert resp.status_code == 200, f"GET {path} -> {resp.status_code} {resp.text}"
    return resp.json()


async def _question(client, prompt: str = "Which word means 'kite'?", **over) -> dict:
    return await _post(client, QUESTIONS, choice_body(prompt, **over))


async def _exam(client, title: str | None = None, **over) -> dict:
    return await _post(client, EXAMS, exam_body(title or f"Paper {uuid.uuid4().hex[:8]}", **over))


async def _detail(client, exam_id: str) -> dict:
    return await _get(client, f"{EXAMS}/{exam_id}")


async def _blockers(client, exam_id: str) -> list[str]:
    """The rules the editor says are stopping this paper, as their codes."""
    return [row["code"] for row in (await _detail(client, exam_id))["publish_blockers"]]


async def _add(client, exam_id: str, *items: dict, expect: int = 201) -> dict:
    return await _post(client, f"{EXAMS}/{exam_id}/items", {"items": list(items)}, expect=expect)


def _ref(question_id: str, **over) -> dict:
    body = {"kind": "question", "ref_id": question_id}
    body.update(over)
    return body


def _passage_ref(kind: str, ref_id: str, **over) -> dict:
    body = {"kind": kind, "ref_id": ref_id}
    body.update(over)
    return body


async def _section(client, exam_id: str, title: str = "Part one", **over) -> dict:
    return await _post(client, f"{EXAMS}/{exam_id}/sections", {"title": title, **over})


async def _publish(client, exam_id: str, status: str = "active", *, expect: int = 200) -> dict:
    return await _post(client, f"{EXAMS}/{exam_id}/status", {"status": status}, expect=expect)


async def _paper(client, *questions: dict, **over) -> dict:
    """A draft paper holding the given questions, assigned to nobody."""
    exam = await _exam(client, **over)
    if questions:
        await _add(client, exam["id"], *(_ref(row["id"]) for row in questions))
    return await _detail(client, exam["id"])


async def _live_paper(client, *questions: dict, **over) -> dict:
    """A published paper: the state an assignment can actually be made against."""
    exam = await _paper(client, *questions, **over)
    return await _publish(client, exam["id"])


async def _learner(client, session_factory, username: str):
    """A real student row, a real access key and a logged-in session of their own."""
    created = await _post(client, STUDENTS, {"name": "Ada", "surname": username, "username": username})
    session = session_factory()
    await session.login_student(created["access_key"])
    return session, created["student"]["id"]


async def _assign(client, exam_id: str, *, students: list[str] | None = None, groups: list[str] | None = None) -> dict:
    return await _post(
        client,
        f"{EXAMS}/{exam_id}/assignments",
        {"student_ids": students or [], "group_ids": groups or []},
    )


async def _sit_once(client, session_factory, exam: dict, username: str = "sitter") -> dict:
    """Assign a learner, open their sitting and hand it in, through their own routes."""
    learner, student_id = await _learner(client, session_factory, username)
    await _assign(client, exam["id"], students=[student_id])
    started = await _post(learner, f"{STUDENT_EXAMS}/{exam['id']}/start", {}, expect=201)
    await _post(learner, f"{ATTEMPTS}/submit", {"token": started["token"]}, expect=200)
    return await _detail(client, exam["id"])


async def _count(model) -> int:
    async with SessionLocal() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _pinned(exam_id: str) -> list[tuple[str, str | None, int, float | None]]:
    """The item rows themselves: what each line points at and which version it is frozen to.

    Read from the table rather than from the API because the pin is the fact under test, and
    the teacher's read deliberately reports the version's *number*, not its id.
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ExamItem)
                .where(ExamItem.exam_id == uuid.UUID(exam_id))
                .order_by(ExamItem.position, ExamItem.created_at)
            )
        ).scalars().all()
    return [
        (str(row.ref_id), str(row.question_version_id) if row.question_version_id else None, row.position, row.points)
        for row in rows
    ]


def _future(minutes: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _past(minutes: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# --------------------------------------------------------------------------- #
# Writing a paper
# --------------------------------------------------------------------------- #


async def test_a_new_paper_is_a_draft_with_the_rules_the_teacher_set(client) -> None:
    exam = await _exam(client, "October test", duration_minutes=45, max_attempts=2, feedback_timing="instant")
    assert exam["status"] == "draft"
    assert exam["duration_minutes"] == 45
    assert exam["max_attempts"] == 2
    assert exam["feedback_timing"] == "instant"
    assert exam["item_count"] == 0
    assert exam["points"] == 0.0
    assert exam["composition_locked"] is False
    assert exam["publish_blockers"], "an empty paper is not publishable and must say so"


async def test_a_paper_cannot_be_born_live(client) -> None:
    """The lifecycle endpoint is the only door to a state a learner can see, because publishing
    is where the composition is checked - and a body that could set `active` would skip it."""
    resp = await client.post(EXAMS, json=exam_body("Shortcut", status="active"))
    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert err["code"] == "validation_failed"
    assert "draft or a scheduled paper" in err["message"]


async def test_a_title_belongs_to_one_paper_at_a_time(client) -> None:
    first = await _exam(client, "Term one test")
    resp = await client.post(EXAMS, json=exam_body("Term one test"))
    assert resp.status_code == 409, resp.text
    assert error_of(resp)["code"] == "exam_name_taken"

    # A case change is the same title to a teacher scanning a list for the paper they wrote.
    resp = await client.post(EXAMS, json=exam_body("TERM ONE TEST"))
    assert resp.status_code == 409, resp.text
    other = await _exam(client, "A different paper")
    resp = await client.patch(f"{EXAMS}/{other['id']}", json={"title": "term one test"})
    assert resp.status_code == 409, resp.text

    # The paper that already holds the name may change its own case; renaming frees it.
    assert (await _patch(client, f"{EXAMS}/{first['id']}", {"title": "Term One Test"}))["title"] == "Term One Test"
    await _patch(client, f"{EXAMS}/{first['id']}", {"title": "Term one test (retake)"})
    assert (await _exam(client, "TERM ONE TEST"))["title"] == "TERM ONE TEST"


async def test_the_bin_frees_a_title_and_a_restore_then_refuses(client) -> None:
    """A binned paper is out of the teacher's way, so its name is usable again - and the moment
    they try to bring it back, the collision has to be named rather than silently tolerated."""
    binned = await _exam(client, "Mocks")
    await client.delete(f"{EXAMS}/{binned['id']}")
    replacement = await _exam(client, "Mocks")

    resp = await client.post(f"{EXAMS}/{binned['id']}/restore", json={})
    assert resp.status_code == 409, resp.text
    assert error_of(resp)["code"] == "exam_name_taken"

    await client.delete(f"{EXAMS}/{replacement['id']}")
    restored = await _post(client, f"{EXAMS}/{binned['id']}/restore", {}, expect=200)
    assert restored["restored"] is True
    assert (await _detail(client, binned["id"]))["deleted_at"] is None


async def test_timestamps_written_without_a_timezone_are_read_as_utc(client) -> None:
    exam = await _exam(client, available_from="2026-11-02T09:00:00")
    assert exam["available_from"].endswith("+00:00"), exam["available_from"]


async def test_a_closing_time_before_the_opening_one_is_refused(client) -> None:
    resp = await client.post(
        EXAMS,
        json=exam_body(
            "Backwards", available_from="2026-11-02T09:00:00Z", available_to="2026-11-01T09:00:00Z"
        ),
    )
    assert resp.status_code == 422, resp.text
    assert "after the opening time" in error_of(resp)["message"]


async def test_a_switch_rule_is_refused_until_it_means_something(client) -> None:
    """Watching without deciding what happens is a rule that does nothing, and a limit with no
    watching is a number no learner will ever meet. Both are refused on the way in."""
    resp = await client.post(EXAMS, json=exam_body("Watched", monitor_tab_switch=True))
    assert resp.status_code == 422, resp.text
    assert "needs an action" in error_of(resp)["message"]

    resp = await client.post(EXAMS, json=exam_body("Limited", tab_switch_limit=3))
    assert resp.status_code == 422, resp.text
    assert "monitoring is on" in error_of(resp)["message"]

    whole = await _exam(
        client, "Counted", monitor_tab_switch=True, tab_switch_action="warn", tab_switch_limit=3
    )
    assert whole["tab_switch_limit"] == 3


# --------------------------------------------------------------------------- #
# Versions, marks and the composition lock
# --------------------------------------------------------------------------- #


async def test_an_item_pins_the_version_that_was_current_when_it_was_added(client) -> None:
    question = await _question(client)
    exam = await _paper(client, question)
    item = exam["items"][0]
    assert item["version"] == question["current_version"] == 1
    assert item["state"] == "ready"
    assert item["serveable"] is True
    assert item["question_score"] == 2.0
    assert item["effective_points"] == 2.0
    assert item["points"] is None, "the line's own mark is unset until the teacher gives it one"

    rows = await _pinned(exam["id"])
    assert len(rows) == 1
    assert rows[0][0] == question["id"]
    assert rows[0][1], "an item reached the paper without a version pinned under it"


async def test_editing_the_question_in_the_bank_does_not_move_the_paper(client) -> None:
    """Invariant 3 with a row behind it: a paper is not a view onto the bank. The teacher can fix
    a typo while an exam week is running, and the papers already written keep the version they
    were composed against."""
    question = await _question(client, "Which word means 'kite'?")
    exam = await _paper(client, question)
    before = await _pinned(exam["id"])

    await _patch(client, f"{QUESTIONS}/{question['id']}", {"prompt": "Which word means 'kite'? (edited)"})
    moved = await _get(client, f"{QUESTIONS}/{question['id']}")
    assert moved["current_version"] == 2, "the edit did not produce a version at all"

    after = await _detail(client, exam["id"])
    assert [row["version"] for row in after["items"]] == [1], "the paper followed the bank"
    assert after["items"][0]["title"] == "Which word means 'kite'?", "the paper showed the live text"
    assert await _pinned(exam["id"]) == before, "the pin itself moved"


async def test_a_line_s_mark_is_the_teacher_s_and_can_be_given_back_to_the_question(client) -> None:
    """`points` overrides the pinned version's score, and an explicit null removes the override -
    the one edit that is not a number, and the reason the body has to tell "unset" from "absent"."""
    exam = await _paper(client, await _question(client))  # worth 2 in the bank
    item_id = exam["items"][0]["id"]

    marked = await _patch(client, f"{EXAMS}/items/{item_id}", {"points": 5})
    assert marked["points"] == 5.0
    assert marked["effective_points"] == 5.0
    assert (await _detail(client, exam["id"]))["points"] == 5.0

    shared = await _patch(client, f"{EXAMS}/items/{item_id}", {"points": None})
    assert shared["points"] is None
    assert shared["effective_points"] == 2.0, "the line stopped being worth what its question is worth"


async def test_the_mark_of_a_line_falls_back_to_the_pinned_version_not_to_the_live_question(client) -> None:
    """A teacher re-values a question after it is on a paper: the paper keeps the version's number,
    because that is the number the answers were graded against."""
    question = await _question(client)
    exam = await _paper(client, question)
    await _patch(client, f"{QUESTIONS}/{question['id']}", {"score": 9})

    after = await _detail(client, exam["id"])
    assert after["items"][0]["question_score"] == 2.0
    assert after["items"][0]["effective_points"] == 2.0
    assert after["points"] == 2.0
    assert after["items"][0]["current_version"] == 2, "the editor lost the news that the bank moved on"


async def test_a_line_can_be_moved_out_of_its_part_again(client) -> None:
    """Naming a part is how a paper is written and naming null is how it is unwritten; a body that
    could not say null would leave a misplaced question in the part forever."""
    exam = await _exam(client)
    section = await _section(client, exam["id"], "Part one")
    added = await _add(client, exam["id"], _ref((await _question(client))["id"], section_id=section["id"]))
    item_id = added["item_ids"][0]

    filled = await _detail(client, exam["id"])
    # The part's own count and marks, before anything is moved: a paper whose parts all report
    # nothing is a teacher looking at a paper they cannot check.
    assert filled["sections"][0]["item_count"] == 1
    assert filled["sections"][0]["points"] == filled["items"][0]["effective_points"]

    moved = await _patch(client, f"{EXAMS}/items/{item_id}", {"section_id": None})
    assert moved["section_id"] is None
    detail = await _detail(client, exam["id"])
    assert detail["sections"][0]["item_count"] == 0
    assert detail["sections"][0]["points"] == 0
    assert detail["items"][0]["section_id"] is None


async def test_naming_a_part_that_belongs_to_another_paper_is_refused(client) -> None:
    mine = await _exam(client)
    theirs = await _exam(client)
    foreign = await _section(client, theirs["id"], "Not yours")

    resp = await client.post(
        f"{EXAMS}/{mine['id']}/items",
        json={"items": [_ref((await _question(client))["id"], section_id=foreign["id"])]},
    )
    assert resp.status_code == 422, resp.text
    assert "different exam" in error_of(resp)["message"]
    assert await _count(ExamItem) == 0, "a refusal that still wrote a line"


async def test_the_same_question_cannot_be_asked_twice_on_one_paper(client) -> None:
    question = await _question(client)
    exam = await _paper(client, question)
    resp = await client.post(f"{EXAMS}/{exam['id']}/items", json={"items": [_ref(question["id"])]})
    assert resp.status_code == 409, resp.text
    assert "already asks" in error_of(resp)["message"]
    assert len((await _detail(client, exam["id"]))["items"]) == 1


async def test_a_question_that_has_left_the_bank_is_counted_out_not_added(client) -> None:
    """Content that cannot be served is named in the answer rather than written onto the paper -
    and when it is the only thing asked for, the whole request is refused instead of answering
    "added 0" as if the teacher had written something."""
    gone = await _question(client, "Gone?")
    await client.delete(f"{QUESTIONS}/{gone['id']}")
    exam = await _exam(client)

    result = await _add(client, exam["id"], _ref((await _question(client, "Kept?"))["id"]), _ref(gone["id"]))
    assert result["added"] == 1
    assert result["skipped"] == [{"ref_id": gone["id"], "reason": "not in the bank"}]

    resp = await client.post(f"{EXAMS}/{exam['id']}/items", json={"items": [_ref(gone["id"])]})
    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert err["code"] == "exam_nothing_added"
    # The reason travels as `params`, not only inside an English sentence, so the teacher's own
    # language can say it.
    assert err["params"]["detail"] == "not in the bank", err


async def test_the_composition_freezes_the_first_time_somebody_sits_it(
    client, session_factory
) -> None:
    """Not the paper - the composition. A learner's answers are keyed to these lines, and a mark
    that moves under a result is a rewrite rather than a correction."""
    exam = await _live_paper(client, await _question(client))
    learner, student_id = await _learner(client, session_factory, "locked")
    await _assign(client, exam["id"], students=[student_id])
    await _post(learner, f"{STUDENT_EXAMS}/{exam['id']}/start", {}, expect=201)

    item_id = exam["items"][0]["id"]
    assert (await _detail(client, exam["id"]))["composition_locked"] is True
    # Each rule says which part of the paper it froze, because the teacher's next move differs:
    # a locked mark is not a locked section.
    for path, body, verb, code in (
        (f"{EXAMS}/items/{item_id}", {"points": 9}, "patch", "exam_marks_locked"),
        (f"{EXAMS}/{exam['id']}/sections", {"title": "Too late"}, "post", "exam_sections_locked"),
        (
            f"{EXAMS}/{exam['id']}/items",
            {"items": [_ref((await _question(client, "New?"))["id"])]},
            "post",
            "exam_questions_locked",
        ),
    ):
        resp = await getattr(client, verb)(path, json=body)
        assert resp.status_code == 409, f"{verb.upper()} {path} -> {resp.status_code} {resp.text}"
        assert error_of(resp)["code"] == code, resp.text

    removed = await client.delete(f"{EXAMS}/items/{item_id}")
    assert removed.status_code == 409, removed.text
    assert error_of(removed)["code"] == "exam_questions_locked"
    reorder = await client.post(
        f"{EXAMS}/{exam['id']}/items/reorder", json={"item_ids": [item_id]}
    )
    assert reorder.status_code == 409, reorder.text
    assert error_of(reorder)["code"] == "exam_item_order_locked"


async def test_the_rules_a_sitting_runs_on_can_be_changed_after_learners_have_sat_it(
    client, session_factory
) -> None:
    """The lock is about the composition, not about the paper: the rules of a sitting were copied
    into it when it opened, so editing them reaches next week's learners and leaves this one's
    result alone."""
    exam = await _sit_once(client, session_factory, await _live_paper(client, await _question(client)))
    assert exam["composition_locked"] is True
    assert exam["attempt_count"] == 1

    updated = await _patch(client, f"{EXAMS}/{exam['id']}", {"duration_minutes": 60, "passing_score": 60})
    assert updated["duration_minutes"] == 60
    assert updated["passing_score"] == 60.0
    assert updated["composition_locked"] is True, "a rule edit unlocked the paper it belongs to"


async def test_a_line_taken_off_a_paper_does_not_take_the_question_with_it(client) -> None:
    """Grouping is not a content edit: the reference goes, the bank keeps its question."""
    question = await _question(client)
    exam = await _paper(client, question)
    removed = await client.delete(f"{EXAMS}/items/{exam['items'][0]['id']}")
    assert removed.status_code == 200, removed.text
    assert (await _detail(client, exam["id"]))["item_count"] == 0
    assert await _count(ExamItem) == 0
    assert (await _get(client, f"{QUESTIONS}/{question['id']}"))["id"] == question["id"]


# --------------------------------------------------------------------------- #
# Passages on a paper
# --------------------------------------------------------------------------- #


async def test_naming_a_text_adds_the_questions_that_belong_to_it(client) -> None:
    """A passage is not an item: it reaches the paper through its questions, and each of them
    carries the text with it as context."""
    reading = await _post(client, READING, {"title": "A day at the market", "body": "The market opens at six.", "language": "en"})
    first = await _question(client, "What opens at six?", context_kind="reading_bound", reading_id=reading["id"])
    second = await _question(client, "Where is the market?", context_kind="reading_bound", reading_id=reading["id"])

    exam = await _paper(client, title="Reading paper")
    result = await _add(client, exam["id"], _passage_ref("reading", reading["id"]))
    assert result["added"] == 2

    detail = await _detail(client, exam["id"])
    assert [row["ref_id"] for row in detail["items"]] == [first["id"], second["id"]], (
        "the paper's lines are not the passage's questions, in the order the text asks them"
    )
    assert [row["position"] for row in detail["items"]] == [0, 1]
    assert all(row["version"] == 1 for row in detail["items"]), "a passage's questions must pin their own versions"
    assert all(row["serveable"] is True for row in detail["items"])
    assert {row["context_id"] for row in detail["items"]} == {reading["id"]}
    assert {row["context_title"] for row in detail["items"]} == {"A day at the market"}
    assert len({row["id"] for row in detail["items"]}) == 2, "two questions reached the paper as one line"


async def test_a_draft_question_under_a_ready_text_is_left_off_the_paper(client) -> None:
    """`serveable` is a chain, and a passage is added as the questions that are ready under it -
    not as everything that has ever been filed against its id."""
    reading = await _post(client, READING, {"title": "Two texts", "body": "One ready, one not.", "language": "en"})
    ready = await _question(client, "Ready one?", context_kind="reading_bound", reading_id=reading["id"])
    await _question(client, "Draft one?", status="draft", context_kind="reading_bound", reading_id=reading["id"])

    exam = await _exam(client)
    result = await _add(client, exam["id"], _passage_ref("reading", reading["id"]))
    assert result["added"] == 1
    detail = await _detail(client, exam["id"])
    assert [row["ref_id"] for row in detail["items"]] == [ready["id"]]


async def test_a_question_whose_text_went_back_to_draft_is_shown_and_counted_out(client) -> None:
    """The chain breaks from either end: a ready question under a text the teacher un-published
    cannot be served, and a learner must never be handed a question about a text they have not
    been shown. The editor says which line is broken instead of dropping it."""
    reading = await _post(client, READING, {"title": "Unpublished later", "body": "A text on its own.", "language": "en"})
    question = await _question(client, "About the text?", context_kind="reading_bound", reading_id=reading["id"])
    exam = await _paper(client, question)

    await _post(client, f"{READING}/{reading['id']}/status", {"status": "draft"}, expect=200)
    detail = await _detail(client, exam["id"])
    assert detail["items"][0]["state"] == "draft"
    assert detail["items"][0]["serveable"] is False
    assert detail["publish_blockers"], "the editor lost the reason the chain broke"

    resp = await client.post(f"{EXAMS}/{exam['id']}/status", json={"status": "active"})
    assert resp.status_code == 422, resp.text
    assert "not ready for learners" in error_of(resp)["message"]


async def test_a_text_with_no_ready_questions_cannot_be_put_on_a_paper(client) -> None:
    """Naming an empty text is refused in words; a paper that silently gained nothing would leave
    the teacher believing the questions were there."""
    reading = await _post(client, READING, {"title": "No questions yet", "body": "A text on its own.", "language": "en"})
    exam = await _exam(client)
    resp = await client.post(f"{EXAMS}/{exam['id']}/items", json={"items": [_passage_ref("reading", reading["id"])]})
    assert resp.status_code == 422, resp.text
    assert "no ready questions" in error_of(resp)["message"]
    assert (await _detail(client, exam["id"]))["item_count"] == 0


async def test_a_draft_text_cannot_be_added_as_a_shortcut(client) -> None:
    reading = await _post(
        client, READING, {"title": "Still writing", "body": "An unfinished text.", "language": "en", "status": "draft"}
    )
    await _question(client, "About it?", context_kind="reading_bound", reading_id=reading["id"])
    exam = await _exam(client)
    resp = await client.post(f"{EXAMS}/{exam['id']}/items", json={"items": [_passage_ref("reading", reading["id"])]})
    assert resp.status_code == 422, resp.text
    assert "not ready for learners" in error_of(resp)["message"]


# --------------------------------------------------------------------------- #
# The lifecycle, in the order a teacher meets it
# --------------------------------------------------------------------------- #


async def test_an_empty_paper_cannot_be_published_and_says_what_is_missing(client) -> None:
    exam = await _exam(client)
    resp = await client.post(f"{EXAMS}/{exam['id']}/status", json={"status": "active"})
    assert resp.status_code == 422, resp.text
    assert "no questions on it yet" in error_of(resp)["message"]
    assert (await _detail(client, exam["id"]))["status"] == "draft"


async def test_a_paper_holding_a_deleted_question_cannot_be_published(client) -> None:
    question = await _question(client)
    exam = await _paper(client, question)
    await client.delete(f"{QUESTIONS}/{question['id']}")
    resp = await client.post(f"{EXAMS}/{exam['id']}/status", json={"status": "active"})
    assert resp.status_code == 422, resp.text
    assert "not ready for learners" in error_of(resp)["message"]
    detail = await _detail(client, exam["id"])
    assert detail["publish_blockers"], "the editor lost the reason the endpoint just gave"
    # The note above the form and the refusal below it name the same rule, so a teacher who acts
    # on the note is not surprised by the refusal.
    assert detail["publish_blockers"][0]["code"] == error_of(resp)["code"]
    assert detail["items"][0]["state"] == "trashed"


async def test_a_scheduled_paper_needs_an_opening_time_before_it_can_be_issued(
    client, session_factory
) -> None:
    exam = await _paper(client, await _question(client), status="scheduled")
    # Somebody is already on the paper, so the list below can be about one rule. A scheduled paper
    # with nobody assigned is stopped by the audience warning too, and an exact list would then be
    # reporting both at once - `test_an_unassigned_paper_publishes_and_the_editor_says_it_has_nobody`
    # is the test that speaks about the audience.
    _, student_id = await _learner(client, session_factory, "scheduled")
    await _assign(client, exam["id"], students=[student_id])
    resp = await client.post(f"{EXAMS}/{exam['id']}/status", json={"status": "active"})
    assert resp.status_code == 422, resp.text
    assert "opening time" in error_of(resp)["message"]
    assert await _blockers(client, exam["id"]) == ["exam_schedule_needs_opening_time"]

    await _patch(client, f"{EXAMS}/{exam['id']}", {"available_from": _future(30)})
    assert (await _publish(client, exam["id"]))["status"] == "active"


async def test_an_unassigned_paper_publishes_and_the_editor_says_it_has_nobody(
    client, session_factory
) -> None:
    """Handing a paper out and forgetting to name the class is a mistake the screen points at, but
    a teacher is entitled to issue first and assign after - so it is a warning carried in the
    blockers, not a refusal that blocks the door."""
    exam = await _paper(client, await _question(client))
    published = await _publish(client, exam["id"])
    assert published["status"] == "active"
    assert "exam_no_audience" in [row["code"] for row in published["publish_blockers"]]

    learner, _ = await _learner(client, session_factory, "later")
    assert (await _get(learner, STUDENT_EXAMS))["total"] == 0, "an issued paper reached a stranger"


async def test_pressing_hand_out_with_nobody_chosen_is_refused(client) -> None:
    """The screen keeps the button off until somebody is picked, so a request that arrives with no
    names is not a teacher's mistake in the form - it is a hand-out that would have written
    nothing and called it a success."""
    exam = await _live_paper(client, await _question(client))
    resp = await client.post(
        f"{EXAMS}/{exam['id']}/assignments", json={"student_ids": [], "group_ids": []}
    )
    assert resp.status_code == 422, resp.text
    assert error_of(resp)["code"] == "exam_no_audience"
    # The code and its words, not the schema's English complaint: the rule is a rule of the
    # school, so it arrives as one.
    assert "nobody" in error_of(resp)["message"]
    assert (await _get(client, f"{EXAMS}/{exam['id']}/assignments"))["items"] == []


async def test_a_paper_only_moves_along_the_lifecycle_the_table_allows(client) -> None:
    exam = await _paper(client, await _question(client))
    resp = await client.post(f"{EXAMS}/{exam['id']}/status", json={"status": "finished"})
    assert resp.status_code == 422, resp.text
    assert "cannot be moved" in error_of(resp)["message"]

    await _publish(client, exam["id"])
    await _publish(client, exam["id"], "archived")
    resp = await client.post(f"{EXAMS}/{exam['id']}/status", json={"status": "active"})
    assert resp.status_code == 422, f"an archived paper went straight back out: {resp.text}"
    assert (await _publish(client, exam["id"], "draft"))["status"] == "draft"


async def test_a_live_paper_cannot_be_binned_while_learners_can_still_open_it(client) -> None:
    exam = await _live_paper(client, await _question(client))
    resp = await client.delete(f"{EXAMS}/{exam['id']}")
    assert resp.status_code == 422, resp.text
    assert "finish or archive" in error_of(resp)["message"]
    await _publish(client, exam["id"], "finished")
    assert (await client.delete(f"{EXAMS}/{exam['id']}")).status_code == 200


async def test_binning_a_paper_leaves_the_sittings_it_already_holds_alone(client, session_factory) -> None:
    """A result a learner has been given is not a thing a teacher can delete by accident. The bin
    moves the paper; the attempts keep their own blueprint and their own numbers."""
    exam = await _sit_once(client, session_factory, await _live_paper(client, await _question(client)))
    attempts_before = await _count(ExamAttempt)
    assert attempts_before == 1

    await _publish(client, exam["id"], "finished")
    await client.delete(f"{EXAMS}/{exam['id']}")
    assert await _count(ExamAttempt) == attempts_before
    assert (await _get(client, f"{EXAMS}/{exam['id']}/attempts"))["total"] == attempts_before
    assert (await _detail(client, exam["id"]))["deleted_at"] is not None


async def test_a_copy_of_a_paper_starts_as_a_draft_with_the_same_versions_pinned(client) -> None:
    """This is the answer to "I want to change the questions on a sat exam": edit the copy. The
    original keeps the composition its sittings were graded against."""
    exam = await _paper(client, await _question(client), title="Original paper")
    await _section(client, exam["id"], "Part one")
    await _publish(client, exam["id"])
    exam = await _detail(client, exam["id"])
    copy = await _post(client, f"{EXAMS}/{exam['id']}/clone", {}, expect=201)

    assert copy["status"] == "draft"
    assert copy["title"] == "Original paper (copy)"
    assert await _pinned(copy["id"]) == await _pinned(exam["id"]), "the copy re-resolved the versions"
    assert [row["title"] for row in copy["sections"]] == ["Part one"]
    assert copy["sections"][0]["id"] != exam["sections"][0]["id"], "the copy shares the original's part"
    assert (await _detail(client, exam["id"]))["status"] == "active", "the copy was cut out of the original"


# --------------------------------------------------------------------------- #
# Who the paper is for
# --------------------------------------------------------------------------- #


async def test_a_draft_paper_has_nothing_to_hand_out(client, session_factory) -> None:
    """Assigning is the act of issuing, and a paper that is still being written has no composition
    to issue - so the refusal happens before any row is written."""
    _, student_id = await _learner(client, session_factory, "drafted")
    exam = await _paper(client, await _question(client))
    resp = await client.post(
        f"{EXAMS}/{exam['id']}/assignments", json={"student_ids": [student_id], "group_ids": []}
    )
    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert err["code"] == "exam_draft_not_assignable"
    assert "publish it" in err["message"]
    assert await _count(ExamAssignment) == 0


async def test_a_paper_is_assigned_to_a_student_or_a_group_and_the_row_names_which(client, session_factory) -> None:
    exam = await _live_paper(client, await _question(client))
    _, student_id = await _learner(client, session_factory, "named")
    group = await _post(client, GROUPS, {"name": "Saturday class"})
    assigned = await _assign(client, exam["id"], students=[student_id], groups=[group["id"]])
    assert assigned["created"] == 2
    assert assigned["already_assigned"] == []

    rows = (await _get(client, f"{EXAMS}/{exam['id']}/assignments"))["items"]
    assert {row["kind"] for row in rows} == {"student", "group"}
    named = next(row for row in rows if row["kind"] == "student")
    assert named["name"] == "Ada named", "a list of ids is a list the teacher cannot check"
    assert named["reachable"] is True
    assert next(row for row in rows if row["kind"] == "group")["name"] == "Saturday class"


async def test_naming_the_same_learner_twice_is_one_assignment_even_from_two_writes(client, session_factory) -> None:
    """The service refuses the duplicate in words and the partial unique index behind it refuses
    it in the table, so a teacher double-clicking "Assign" cannot produce two rows that a
    learner's list then has to guess between."""
    exam = await _live_paper(client, await _question(client))
    _, student_id = await _learner(client, session_factory, "twice")
    await _assign(client, exam["id"], students=[student_id])

    again = await _assign(client, exam["id"], students=[student_id])
    assert again["created"] == 0
    assert again["already_assigned"] == ["Ada twice"]
    assert await _count(ExamAssignment) == 1

    group = await _post(client, GROUPS, {"name": "Racing class"})
    first, second = await asyncio.gather(
        *(
            client.post(f"{EXAMS}/{exam['id']}/assignments", json={"student_ids": [], "group_ids": [group["id"]]})
            for _ in range(2)
        )
    )
    codes = {first.status_code, second.status_code}
    assert 201 in codes, f"neither concurrent write landed: {codes}"
    assert codes <= {201, 422}, f"the race answered in a code this API does not define: {codes}"
    assert await _count(ExamAssignment) == 2, "the same group was written onto the paper twice"


async def test_a_group_assignment_reaches_its_members_and_a_direct_one_alongside_is_still_one_paper(
    client, session_factory
) -> None:
    """A learner named twice - once by themselves and once through their class - sits one paper.
    Their list is a list of papers, not a list of assignment rows."""
    exam = await _live_paper(client, await _question(client))
    learner, student_id = await _learner(client, session_factory, "grouped")
    group = await _post(client, GROUPS, {"name": "Class 4B"})
    await _post(client, f"{GROUPS}/{group['id']}/members", {"student_id": student_id})
    await _assign(client, exam["id"], students=[student_id], groups=[group["id"]])

    # The teacher's picker marks a learner who already gets the paper through their class, so the
    # group's row has to name the learners it reaches, not just the class.
    rows = (await _get(client, f"{EXAMS}/{exam['id']}/assignments"))["items"]
    by_group = next(row for row in rows if row["kind"] == "group")
    assert student_id in by_group["member_ids"]
    assert student_id in next(row for row in rows if row["kind"] == "student")["member_ids"]

    listed = await _get(learner, STUDENT_EXAMS)
    assert listed["total"] == 1, "one paper reached the learner's list twice"
    assert listed["items"][0]["available"] is True
    assert listed["items"][0]["availability_reason"] == "open"


async def test_a_learner_never_sees_a_paper_somebody_else_was_handed(client, session_factory) -> None:
    exam = await _live_paper(client, await _question(client))
    other, _ = await _learner(client, session_factory, "notme")
    assert (await _get(other, STUDENT_EXAMS))["total"] == 0
    resp = await other.get(f"{STUDENT_EXAMS}/{exam['id']}")
    assert resp.status_code == 404, f"the brief told a stranger the paper exists: {resp.text}"
    start = await other.post(f"{STUDENT_EXAMS}/{exam['id']}/start", json={})
    assert start.status_code == 404, start.text


async def test_withdrawing_an_assignment_takes_the_paper_off_the_learner_s_list(client, session_factory) -> None:
    exam = await _live_paper(client, await _question(client))
    learner, student_id = await _learner(client, session_factory, "withdrawn")
    await _assign(client, exam["id"], students=[student_id])
    assert (await _get(learner, STUDENT_EXAMS))["total"] == 1

    rows = (await _get(client, f"{EXAMS}/{exam['id']}/assignments"))["items"]
    removed = await client.delete(f"{EXAMS}/assignments/{rows[0]['id']}")
    assert removed.status_code == 200, removed.text
    assert (await _get(learner, STUDENT_EXAMS))["total"] == 0


async def test_a_paper_that_is_only_a_plan_is_off_the_learner_s_list_until_it_is_issued(
    client, session_factory
) -> None:
    """Assigning ahead of time is normal - a teacher schedules a paper and names the class. What a
    learner may not see is a paper that has not been issued yet, or one that has been put away:
    the only reason the ladder has for either is `closed`, which reads as a missed deadline."""
    exam = await _paper(client, await _question(client), status="scheduled")
    learner, student_id = await _learner(client, session_factory, "early")
    await _assign(client, exam["id"], students=[student_id])
    assert (await _get(learner, STUDENT_EXAMS))["total"] == 0, "a scheduled paper is not an issued one"

    # A plan needs an opening time before it can be issued at all.
    resp = await client.post(f"{EXAMS}/{exam['id']}/status", json={"status": "active"})
    assert resp.status_code == 422, resp.text
    assert "opening time" in error_of(resp)["message"]

    await _patch(client, f"{EXAMS}/{exam['id']}", {"available_from": _past(60)})
    await _publish(client, exam["id"])
    assert (await _get(learner, STUDENT_EXAMS))["total"] == 1
    await _publish(client, exam["id"], "finished")
    await _publish(client, exam["id"], "archived")
    assert (await _get(learner, STUDENT_EXAMS))["total"] == 0, "a put-away paper is still on the list"


async def test_a_paper_pulled_back_while_a_learner_is_sitting_it_stays_on_their_list(
    client, session_factory
) -> None:
    """The list rule keeps an unissued paper out of a class's way; it is not a licence to lose a
    sitting that is already open. A teacher archiving a paper mid-session must not cost the
    learner the door back to their own paper."""
    exam = await _live_paper(client, await _question(client))
    learner, student_id = await _learner(client, session_factory, "pulled")
    await _assign(client, exam["id"], students=[student_id])
    started = await _post(learner, f"{STUDENT_EXAMS}/{exam['id']}/start", {}, expect=201)

    await _publish(client, exam["id"], "archived")
    row = (await _get(learner, STUDENT_EXAMS))["items"][0]
    assert row["status"] == "archived", "the list lost the word the teacher's screen shows"
    assert row["available"] is False
    assert row["resume_token"] == started["token"]

    resumed = await _post(learner, f"{STUDENT_EXAMS}/{exam['id']}/start", {}, expect=201)
    assert resumed["resumed"] is True
    assert resumed["token"] == started["token"], "a second sitting was opened on a pulled-back paper"


# --------------------------------------------------------------------------- #
# The teacher's list
# --------------------------------------------------------------------------- #


async def test_the_bank_the_bin_and_all_are_three_views_of_one_table(client) -> None:
    await _live_paper(client, await _question(client), title="Sat last week")
    await _exam(client, title="Still writing")
    binned = await _exam(client, title="Mistake")
    await client.delete(f"{EXAMS}/{binned['id']}")

    bank = await _get(client, f"{EXAMS}?view=bank")
    trash = await _get(client, f"{EXAMS}?view=trash")
    everything = await _get(client, f"{EXAMS}?view=all")
    assert {row["title"] for row in trash["items"]} == {"Mistake"}
    assert "Mistake" not in {row["title"] for row in bank["items"]}
    assert {"Mistake", "Sat last week", "Still writing"} <= {row["title"] for row in everything["items"]}
    issued = next(row for row in bank["items"] if row["title"] == "Sat last week")
    assert issued["status"] == "active"
    assert issued["item_count"] == 1
    assert (await _get(client, f"{EXAMS}?status=active"))["total"] == 1
    resp = await client.get(f"{EXAMS}?view=trash&status=draft")
    assert resp.status_code == 422, resp.text
    assert "chosen by its bin" in error_of(resp)["message"]


async def test_a_search_finds_a_paper_by_title_in_any_case(client) -> None:
    await _exam(client, "Autumn mock exam")
    await _exam(client, "Spring mock exam")
    found = await _get(client, f"{EXAMS}?q={uuid.uuid4().hex[:8]}")
    assert found["total"] == 0
    mocks = await _get(client, f"{EXAMS}?q=MOCK")
    assert {row["title"] for row in mocks["items"]} == {"Autumn mock exam", "Spring mock exam"}
    only_en = await _get(client, f"{EXAMS}?language=de")
    assert only_en["total"] == 0


async def test_the_list_sorts_and_pages_without_losing_rows(client) -> None:
    for index in range(4):
        await _exam(client, title=f"Series {index}")
    page_one = await _get(client, f"{EXAMS}?page_size=2&page=1&sort=title&order=asc")
    page_two = await _get(client, f"{EXAMS}?page_size=2&page=2&sort=title&order=asc")
    assert page_one["total"] == 4
    assert len(page_one["items"]) == 2 and len(page_two["items"]) == 2
    assert [row["title"] for row in page_one["items"]] == ["Series 0", "Series 1"]
    seen = {row["id"] for row in page_one["items"]} | {row["id"] for row in page_two["items"]}
    assert len(seen) == 4, "the same paper appeared on two pages"

    for bad in ("sort=nonsense", "view=nonsense", "order=sideways", "status=nonsense"):
        resp = await client.get(f"{EXAMS}?{bad}")
        assert resp.status_code == 422, f"{bad} -> {resp.status_code} {resp.text}"


async def test_a_list_row_carries_the_numbers_the_teacher_picks_the_paper_by(client, session_factory) -> None:
    """Item counts, marks, who has it, how many sat it and what is still waiting - all without the
    list opening the paper, because the list is the screen a teacher lives on."""
    exam = await _live_paper(client, await _question(client), title="Counted paper")
    _, student_id = await _learner(client, session_factory, "counter")
    await _assign(client, exam["id"], students=[student_id])
    learner, second_id = await _learner(client, session_factory, "counter2")
    await _assign(client, exam["id"], students=[second_id])

    row = next(row for row in (await _get(client, f"{EXAMS}?view=bank"))["items"] if row["title"] == "Counted paper")
    assert row["item_count"] == 1
    assert row["points"] == 2.0
    assert row["attempt_count"] == 0
    assert row["submitted_count"] == 0
    assert row["review_count"] == 0

    started = await _post(learner, f"{STUDENT_EXAMS}/{exam['id']}/start", {}, expect=201)
    await _post(learner, f"{ATTEMPTS}/submit", {"token": started["token"]}, expect=200)
    row = next(row for row in (await _get(client, f"{EXAMS}?view=bank"))["items"] if row["title"] == "Counted paper")
    assert row["attempt_count"] == 1
    assert row["submitted_count"] == 1
    assert row["assigned_count"] == 2


async def test_bulk_editing_reports_the_papers_it_refused_and_why(client) -> None:
    """A teacher who selected twelve and got nine needs to know which three, and why."""
    ready = await _paper(client, await _question(client), title="Ready one")
    empty = await _exam(client, title="Empty one")
    missing = str(uuid.uuid4())

    result = await _post(
        client,
        f"{EXAMS}/bulk",
        {"exam_ids": [ready["id"], empty["id"], missing], "action": "status", "status": "active"},
        expect=200,
    )
    assert result["updated"] == [ready["id"]]
    assert result["refused"][0]["id"] == empty["id"]
    assert "no questions" in result["refused"][0]["reason"]
    assert result["not_found"] == [missing]
    assert (await _detail(client, ready["id"]))["status"] == "active"
    assert (await _detail(client, empty["id"]))["status"] == "draft"


async def test_bulk_trashing_and_restoring_move_the_same_rows_the_single_endpoints_do(client) -> None:
    exam = await _exam(client, title="Bulk bin")
    await _post(client, f"{EXAMS}/bulk", {"exam_ids": [exam["id"]], "action": "trash"}, expect=200)
    assert (await _detail(client, exam["id"]))["deleted_at"] is not None
    await _post(client, f"{EXAMS}/bulk", {"exam_ids": [exam["id"]], "action": "restore"}, expect=200)
    assert (await _detail(client, exam["id"]))["deleted_at"] is None

    resp = await client.post(f"{EXAMS}/bulk", json={"exam_ids": [exam["id"]], "action": "status"})
    assert resp.status_code == 422, resp.text
    assert "status to set" in error_of(resp)["message"]
    resp = await client.post(f"{EXAMS}/bulk", json={"exam_ids": [exam["id"]], "action": "delete"})
    assert resp.status_code == 422, resp.text


async def test_bulk_refuses_to_bin_a_paper_a_learner_can_still_open(client) -> None:
    """The same rule as the single endpoint, reached from the toolbar: a selection is not a licence
    to take a live paper away."""
    exam = await _live_paper(client, await _question(client))
    result = await _post(client, f"{EXAMS}/bulk", {"exam_ids": [exam["id"]], "action": "trash"}, expect=200)
    assert result["updated"] == []
    assert "finish or archive" in result["refused"][0]["reason"]
    assert (await _detail(client, exam["id"]))["deleted_at"] is None


# --------------------------------------------------------------------------- #
# The preview, and what a learner is never shown
# --------------------------------------------------------------------------- #


async def test_the_preview_deals_the_paper_the_learner_will_get(client) -> None:
    """Reads only. A preview that wrote a blueprint would put a paper into the history that
    nobody sat, and a teacher who looks at the same layout twice must see the same one."""
    questions = [await _question(client, f"Question {index}?") for index in range(6)]
    exam = await _paper(client, *questions, shuffle_questions=True)

    first = await _get(client, f"{EXAMS}/{exam['id']}/preview")
    assert first["item_count"] == 6
    assert first["points"] == 12.0
    assert first["duration_minutes"] == 30
    assert [row["position"] for row in first["items"]] == list(range(6))
    assert {row["exam_item_id"] for row in first["items"]} == {row["id"] for row in exam["items"]}

    again = await _get(client, f"{EXAMS}/{exam['id']}/preview?seed={first['seed']}")
    assert [row["exam_item_id"] for row in again["items"]] == [
        row["exam_item_id"] for row in first["items"]
    ], "the same seed dealt a different paper"

    orders = set()
    for _ in range(8):
        draw = await _get(client, f"{EXAMS}/{exam['id']}/preview")
        orders.add(tuple(row["exam_item_id"] for row in draw["items"]))
    assert len(orders) > 1, "a paper marked as shuffled was dealt in one order every time"
    assert await _count(ExamAttempt) == 0, "a preview opened a sitting"


async def test_a_bad_seed_is_refused_in_words(client) -> None:
    exam = await _paper(client, await _question(client))
    resp = await client.get(f"{EXAMS}/{exam['id']}/preview", params={"seed": "not-hex"})
    assert resp.status_code == 422, resp.text
    assert "32 hexadecimal" in error_of(resp)["message"]


async def test_the_preview_names_what_it_left_out(client) -> None:
    """A preview that silently dropped an unservable line would be a paper that looks fine and
    then serves less than the teacher wrote."""
    question = await _question(client)
    exam = await _paper(client, question)
    await _post(client, f"{QUESTIONS}/{question['id']}/status", {"status": "draft"}, expect=200)

    preview = await _get(client, f"{EXAMS}/{exam['id']}/preview")
    assert preview["item_count"] == 0
    assert preview["skipped"][0]["reason"] == "draft"
    assert preview["skipped"][0]["exam_item_id"] == exam["items"][0]["id"]


async def test_a_teacher_s_notes_never_reach_a_learner(client, session_factory) -> None:
    """Checked on every payload the learner's own routes hand out, in one sitting: the list, the
    brief, the paper they answer, the answer's own reply, the result and the history."""
    question = await _question(client)
    exam = await _live_paper(client, question, title="Leak check")
    learner, student_id = await _learner(client, session_factory, "leaky")
    await _assign(client, exam["id"], students=[student_id])

    started = await _post(learner, f"{STUDENT_EXAMS}/{exam['id']}/start", {}, expect=201)
    token = started["token"]
    item_id = exam["items"][0]["id"]
    payloads = [
        await _post(
            learner,
            f"{ATTEMPTS}/answer",
            {"token": token, "exam_item_id": item_id, "response": {"option_index": 2}},
            expect=200,
        ),
        await _get(learner, STUDENT_EXAMS),
        await _get(learner, f"{STUDENT_EXAMS}/{exam['id']}"),
        await _get(learner, f"{ATTEMPTS}/{token}/steps"),
        await _get(learner, f"{ATTEMPTS}/{token}"),
        await _post(learner, f"{ATTEMPTS}/submit", {"token": token}, expect=200),
        await _get(learner, f"{ATTEMPTS}/{token}/result"),
        await _get(learner, f"{STUDENT_EXAMS}/{exam['id']}/attempts"),
    ]
    for body in payloads:
        assert KEY not in str(body), f"the teacher's notes reached a learner payload: {list(body)}"

    # The answer key itself is not in the paper they were dealt, either.
    steps = await _get(learner, f"{ATTEMPTS}/{token}/steps")
    assert "correct" not in str([row["view"] for row in steps["steps"]])
    assert not any(row.get("saved", {}).get("correct") for row in steps["steps"])


async def test_the_meta_endpoint_gives_the_editor_the_words_the_service_uses(client) -> None:
    meta = await _get(client, f"{EXAMS}/meta")
    assert meta["statuses"] == ["draft", "scheduled", "active", "finished", "archived"]
    assert [row["kind"] for row in meta["kinds"]] == ["question", "reading", "listening"]
    assert meta["feedback_timings"] == ["instant", "after_session"]
    assert meta["result_visibilities"] == ["immediate", "after_close", "after_approval", "hidden"]
    assert meta["tab_switch_actions"] == ["warn", "flag", "auto_submit"]
    assert meta["views"] == ["bank", "trash", "all"]
    assert meta["bulk_actions"] == ["status", "trash", "restore"]
    assert "en" in meta["languages"], "the editor cannot offer a language the API accepts"
    assert meta["limits"]["max_items"] == 200

    modes = {row["mode"]: row for row in meta["grading_modes"]}
    assert set(modes) == {"automatic", "manual", "ai_assisted"}
    assert modes["ai_assisted"]["available"] is False, "an assistant that does not exist yet was offered"
    assert "available" not in modes["automatic"], "a working mode was flagged as a caveat"

    # The lists are codes, so the editor can say them in the teacher's own interface language.
    # A prose label here would be English in an Azerbaijani, Russian or Turkish classroom.
    assert all("label" not in row for row in meta["kinds"])
    assert all("label" not in row for row in meta["grading_modes"])


async def test_the_learner_meta_endpoint_advertises_the_words_their_routes_return(
    client, session_factory
) -> None:
    """The learner's screens branch on these strings. A value that appears in a payload but not
    here would be a word no locale has a translation for."""
    learner, student_id = await _learner(client, session_factory, "metas")
    meta = await _get(learner, f"{STUDENT_EXAMS}/meta")
    assert set(meta) == {
        "statuses",
        "exam_statuses",
        "availability_reasons",
        "result_states",
        "close_reasons",
        "notices",
    }
    assert meta["close_reasons"] == ["submitted", "expired", "tab_limit"]

    exam = await _live_paper(client, await _question(client))
    await _assign(client, exam["id"], students=[student_id])
    row = (await _get(learner, STUDENT_EXAMS))["items"][0]
    assert row["status"] in meta["exam_statuses"], "the list carries a word the locale was never given"
    assert row["availability_reason"] in meta["availability_reasons"]

    started = await _post(learner, f"{STUDENT_EXAMS}/{exam['id']}/start", {}, expect=201)
    assert started["resumed"] is False, "a first sitting reported itself as a picked-up one"
    sitting = await _get(learner, f"{ATTEMPTS}/{started['token']}")
    assert sitting["status"] in meta["statuses"]
    assert sitting["rules"]["feedback_timing"] == exam["feedback_timing"]

    result = await _post(learner, f"{ATTEMPTS}/submit", {"token": started["token"]}, expect=200)
    assert result["status"] in meta["statuses"]
    assert result["state"] in meta["result_states"]
    assert (await _get(learner, f"{ATTEMPTS}/{started['token']}/steps"))["status"] in meta["statuses"]
