"""The sitting itself over HTTP: the clock, the marks, the review queue (Phase 7).

`tests/test_attempt_rules.py` settles the pure arithmetic - which words mean what, how a deadline
is read, which ladder a result walks. `test_exam_db.py` settles the paper: versions, composition,
assignment. What only a live Postgres and a real HTTP round-trip can prove is the part where a
learner, a timer and a teacher meet:

* the token is the whole relationship - another learner's token reads as no sitting at all, and
  no request a learner makes names an attempt id;
* the deadline is the server's. It survives a second tab, a second device, a submitted batch and
  a browser that claims a different time;
* a paper is graded from the version it was dealt, from the marks written in its own blueprint,
  and closing it goes through one path whoever pressed the button;
* what the learner is told is the paper's policy, not a fact about this code: `withheld` while
  they sit, `hidden`/`closed`/`awaiting_teacher`/`shown` afterwards;
* an answer the engine cannot decide becomes a row of work for a teacher, and the teacher's mark
  moves the total the learner already has.

Every test here drives the learner's own routes; the direct database writes exist only to move
the server's clock forward, which is the one thing no client can do.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core.database import SessionLocal
from app.models.activity import ActivityEvent
from app.models.assessment import AttemptAnswer, ExamAttempt, ManualReview
from app.models.ops import AuditLog
from app.services import attempt_service

EXAMS = "/api/v1/exams"
GRADING = "/api/v1/grading"
QUESTIONS = "/api/v1/questions"
STUDENTS = "/api/v1/students"
STUDENT_EXAMS = "/api/v1/student/exams"
ATTEMPTS = "/api/v1/student/attempts"

#: Only ever written into a teacher-only field. Its appearance in a learner payload fails the
#: test that looked for it.
KEY = "ZULU-ATTEMPT-KEY"


# --------------------------------------------------------------------------- #
# Building a paper and handing it to somebody
# --------------------------------------------------------------------------- #


def choice_body(prompt: str, *, correct: int = 0, **over) -> dict:
    options = [{"text": "apple"}, {"text": "table"}, {"text": "window"}]
    options[correct]["correct"] = True
    body = {
        "type": "multiple_choice",
        "prompt": prompt,
        "config": {"options": options},
        "explanation": "An apple is the fruit.",
        "teacher_notes": f"{KEY}: the distractor is 'table'.",
        "score": 2,
        "level": "A2",
        "learning_language": "en",
    }
    body.update(over)
    return body


def essay_body(prompt: str = "Write about your last holiday.", **over) -> dict:
    body = {
        "type": "essay",
        "prompt": prompt,
        "config": {"min_words": 20, "max_words": 150},
        "score": 5,
        "level": "B1",
        "learning_language": "en",
    }
    body.update(over)
    return body


def select_body(prompt: str = "Which of these are fruits?", **over) -> dict:
    """A tick-many question on its default partial marking: one box of two is half its mark."""
    body = {
        "type": "multi_select",
        "prompt": prompt,
        "config": {
            "options": [
                {"text": "apple", "correct": True},
                {"text": "peach", "correct": True},
                {"text": "table"},
            ]
        },
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


async def _refused(client, method: str, path: str, body: dict | None, expect: int) -> dict:
    if method == "get":
        resp = await client.get(path)
    else:
        resp = await getattr(client, method)(path, json=body)
    assert resp.status_code == expect, f"{method.upper()} {path} -> {resp.status_code} {resp.text}"
    return error_of(resp)


async def _draft(client, *bodies: dict, title: str | None = None, **over) -> dict:
    """An unpublished paper, with one freshly written bank question per body."""
    exam = await _post(client, EXAMS, exam_body(title or f"Run {uuid.uuid4().hex[:8]}", **over))
    if bodies:
        refs = [{"kind": "question", "ref_id": (await _post(client, QUESTIONS, body))["id"]} for body in bodies]
        await _post(client, f"{EXAMS}/{exam['id']}/items", {"items": refs})
    return exam


async def _paper(client, *bodies: dict, **over) -> dict:
    """A published paper: the state a learner can actually be handed."""
    exam = await _draft(client, *bodies, **over)
    return await _status(client, exam["id"], "active")


async def _status(client, exam_id: str, status: str) -> dict:
    return await _post(client, f"{EXAMS}/{exam_id}/status", {"status": status}, expect=200)


async def _assign(client, exam_id: str, *people: dict) -> dict:
    ids = [person["id"] for person in people]
    return await _post(client, f"{EXAMS}/{exam_id}/assignments", {"student_ids": ids, "group_ids": []})


async def _grade(client, review_id: str, body: dict) -> dict:
    """A teacher's marks on one queued answer.

    Closing the paper already put the row in the queue, so this writes to a review that exists and
    answers 200 with the review and the sitting it belongs to - not 201 for a row nobody created.
    """
    return await _post(client, f"{GRADING}/answers/{review_id}/grade", body, expect=200)


async def _person(client, session_factory, username: str) -> dict:
    """A real learner with a logged-in session, the key that made it, and a way to sign in twice."""
    created = await _post(client, STUDENTS, {"name": "Ada", "surname": username, "username": username})
    session = session_factory()
    await session.login_student(created["access_key"])
    return {
        "session": session,
        "id": created["student"]["id"],
        "key": created["access_key"],
        "name": f"Ada {username}",
    }


async def _second_session(session_factory, person: dict):
    """The same learner on another device: a different cookie jar, the same access key."""
    other = session_factory()
    await other.login_student(person["key"])
    return other


async def _assigned(client, session_factory, exam_id: str, username: str, **over) -> dict:
    person = await _person(client, session_factory, username)
    await _assign(client, exam_id, person)
    person.update(await _start(client, person["session"], exam_id, **over))
    return person


async def _start(client, session, exam_id: str, *, expect: int = 201) -> dict:
    started = await _post(session, f"{STUDENT_EXAMS}/{exam_id}/start", {}, expect=expect)
    started["items"] = [row["exam_item_id"] for row in started["steps"]]
    return started


async def _answer(session, started: dict, item: str, response, **over) -> dict:
    return await _post(
        session,
        f"{ATTEMPTS}/answer",
        {"token": started["token"], "exam_item_id": item, "response": response, **over},
        expect=200,
    )


async def _submit(session, started: dict, answers: list[dict] | None = None) -> dict:
    return await _post(session, f"{ATTEMPTS}/submit", {"token": started["token"], "answers": answers}, expect=200)


async def _switch(session, started: dict) -> dict:
    """The browser saying "the page went invisible", once.

    A report is a write to the learner's own sitting, so it answers 200 with the new count and the
    notice; 201 would promise the learner a saved exam item that does not exist.
    """
    return await _post(session, f"{ATTEMPTS}/tab-switch", {"token": started["token"]}, expect=200)


def _right(started: dict, index: int = 0) -> str:
    """The exam item id of the line whose authored answer is option `index`."""
    return started["items"][index]


async def _row(token: str) -> ExamAttempt:
    """The sitting as the table holds it - the only place a status can be checked without the
    read that is under test polishing it."""
    async with SessionLocal() as db:
        return (
            await db.execute(select(ExamAttempt).where(ExamAttempt.session_id == token))
        ).scalar_one()


async def _attempt_count(exam_id: str) -> int:
    async with SessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(ExamAttempt).where(
                        ExamAttempt.exam_id == uuid.UUID(exam_id)
                    )
                )
            ).scalar_one()
        )


async def _expire(token: str, *, minutes: int = 5) -> None:
    """Put a sitting's own deadline behind the server's clock, as waiting would."""
    async with SessionLocal() as db:
        row = (await db.execute(select(ExamAttempt).where(ExamAttempt.session_id == token))).scalar_one()
        now = datetime.now(timezone.utc)
        row.started_at = now - timedelta(minutes=minutes + 90)
        row.expires_at = now - timedelta(minutes=minutes)
        await db.commit()


def _future(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _past(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# --------------------------------------------------------------------------- #
# Opening a sitting
# --------------------------------------------------------------------------- #


async def test_opening_a_paper_hands_back_a_token_the_clock_and_the_whole_paper(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), choice_body("Which is a fruit?"))
    learner = await _assigned(client, session_factory, exam["id"], "opener")
    started = learner

    assert started["resumed"] is False
    assert len(started["token"]) == 32
    assert all(char in "0123456789abcdef" for char in started["token"])
    assert started["attempt_number"] == 1
    assert started["total_items"] == 2
    assert started["points"] == 4.0
    assert started["max_attempts"] == 1, "a paper with no number written on it is sat once"
    assert 1_795 <= started["remaining_seconds"] <= 1_800, "the clock is not the paper's allowance"
    assert started["expires_at"] > started["started_at"]
    assert len(started["items"]) == 2
    assert started["rules"]["feedback_timing"] == "after_session"
    assert started["rules"]["monitor_tab_switch"] is False

    state = await _get(learner["session"], f"{ATTEMPTS}/{started['token']}")
    assert state["status"] == "in_progress"
    assert state["answered_items"] == 0
    assert state["total_items"] == 2
    assert state["server_seconds_used"] == 0

    steps = await _get(learner["session"], f"{ATTEMPTS}/{started['token']}/steps")
    assert [row["exam_item_id"] for row in steps["steps"]] == started["items"], "the order moved"
    assert all(row["view"]["prompt"] for row in steps["steps"])


async def test_the_paper_a_learner_is_given_carries_no_answer_key_and_no_teacher_notes(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "quiet")
    body = json.dumps(
        [
            await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/steps"),
            await _get(learner["session"], f"{STUDENT_EXAMS}/{exam['id']}"),
            await _get(learner["session"], STUDENT_EXAMS),
        ]
    )
    assert KEY not in body
    assert '"correct": true' not in body and '"correct":true' not in body
    assert "accepted" not in body, "an accepted-answers list reached a learner screen"


async def test_a_second_tab_gets_the_same_sitting_with_the_answers_still_in_it(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), choice_body("Which is a fruit?"))
    learner = await _assigned(client, session_factory, exam["id"], "tabber")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})

    again = await _start(client, learner["session"], exam["id"])
    assert again["token"] == learner["token"], "a second tab opened a second sitting"
    assert again["resumed"] is True
    assert await _attempt_count(exam["id"]) == 1

    saved = {row["exam_item_id"]: row["saved"] for row in again["steps"]}
    assert saved[_right(learner)] == {"option_index": 0}


async def test_a_different_device_gets_the_same_sitting_from_the_same_start_call(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "traveller")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 2})

    phone = await _second_session(session_factory, learner)
    resumed = await _start(client, phone, exam["id"])
    assert resumed["token"] == learner["token"]
    assert resumed["resumed"] is True
    assert resumed["steps"][0]["saved"] == {"option_index": 2}
    assert resumed["steps"][0]["answered"] is True


async def test_a_paper_that_was_never_handed_to_this_learner_is_not_theirs_to_open(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    stranger = await _person(client, session_factory, "stranger")

    err = await _refused(stranger["session"], "get", f"{STUDENT_EXAMS}/{exam['id']}", None, 404)
    assert err["code"] == "exam_not_assigned"
    err = await _refused(stranger["session"], "post", f"{STUDENT_EXAMS}/{exam['id']}/start", {}, 404)
    # The refusal is the same one a paper that does not exist gets, so this answers nothing about
    # anybody else's papers.
    assert err["code"] == "exam_not_assigned"
    assert err["message"] == "no exam is assigned to you under that id"
    assert (await _get(stranger["session"], STUDENT_EXAMS))["total"] == 0


async def test_a_paper_that_has_not_opened_yet_says_so_and_the_list_says_it_too(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), available_from=_future(120))
    learner = await _person(client, session_factory, "early")
    await _assign(client, exam["id"], learner)

    row = (await _get(learner["session"], STUDENT_EXAMS))["items"][0]
    assert row["available"] is False
    assert row["availability_reason"] == "not_yet"
    assert row["opens_at"]

    err = await _refused(learner["session"], "post", f"{STUDENT_EXAMS}/{exam['id']}/start", {}, 422)
    assert err["code"] == "exam_not_open_yet"
    assert err["message"] == "this exam is not open yet"


async def test_a_finished_paper_is_refused_in_the_words_that_explain_it(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _person(client, session_factory, "late")
    await _assign(client, exam["id"], learner)
    await _status(client, exam["id"], "finished")

    err = await _refused(learner["session"], "post", f"{STUDENT_EXAMS}/{exam['id']}/start", {}, 422)
    assert err["code"] == "exam_closed"
    assert err["message"] == "this exam is closed"
    row = (await _get(learner["session"], STUDENT_EXAMS))["items"][0]
    assert row["availability_reason"] == "closed"


async def test_the_first_sitting_of_a_one_attempt_paper_cannot_be_followed_by_another(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "once")
    await _submit(learner["session"], learner)

    err = await _refused(learner["session"], "post", f"{STUDENT_EXAMS}/{exam['id']}/start", {}, 422)
    assert err["code"] == "exam_one_sitting_only"
    assert err["message"] == "you have used the one sitting this exam allows"
    assert await _attempt_count(exam["id"]) == 1


async def test_a_second_attempt_is_numbered_and_the_first_stays_in_the_history(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), max_attempts=2)
    learner = await _assigned(client, session_factory, exam["id"], "retaker")
    assert learner["attempt_number"] == 1
    await _submit(learner["session"], learner)

    second = await _start(client, learner["session"], exam["id"])
    assert second["resumed"] is False
    assert second["attempt_number"] == 2
    assert second["token"] != learner["token"]

    history = await _get(learner["session"], f"{STUDENT_EXAMS}/{exam['id']}/attempts")
    assert [row["attempt_number"] for row in history["attempts"]] == [1, 2]
    assert [row["status"] for row in history["attempts"]] == ["submitted", "in_progress"]
    assert history["title"] == exam["title"]


async def test_using_every_allowed_attempt_leaves_the_learner_with_no_way_to_start(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), max_attempts=2)
    learner = await _assigned(client, session_factory, exam["id"], "twice")
    await _submit(learner["session"], learner)
    second = await _start(client, learner["session"], exam["id"])
    await _submit(learner["session"], second)

    err = await _refused(learner["session"], "post", f"{STUDENT_EXAMS}/{exam['id']}/start", {}, 422)
    assert err["code"] == "exam_no_attempts_left"
    assert err["message"] == "you have used every attempt at this exam"
    row = (await _get(learner["session"], STUDENT_EXAMS))["items"][0]
    assert row["attempts_left"] == 0
    assert row["availability_reason"] == "no_attempts_left"


async def test_a_sitting_left_past_its_deadline_is_closed_before_the_next_one_opens(
    client, session_factory
) -> None:
    """The learner who closed the laptop and never pressed anything still has one graded paper and
    a fresh numbered one, not two open sittings."""
    exam = await _paper(client, choice_body("Which word means 'kite'?"), max_attempts=2)
    learner = await _assigned(client, session_factory, exam["id"], "wanderer")
    await _expire(learner["token"])

    second = await _start(client, learner["session"], exam["id"])
    assert second["attempt_number"] == 2
    assert (await _row(learner["token"])).status.value == "auto_submitted"
    assert await _attempt_count(exam["id"]) == 2


async def test_a_sitting_without_a_time_limit_stays_open_until_it_is_handed_in(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), duration_minutes=None)
    learner = await _assigned(client, session_factory, exam["id"], "untimed")
    assert learner["expires_at"] is None
    assert learner["remaining_seconds"] is None

    state = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}")
    assert state["remaining_seconds"] is None
    assert state["status"] == "in_progress"


async def test_a_paper_that_must_finish_before_the_window_closes_loses_the_extra_minutes(
    client, session_factory
) -> None:
    exam = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        duration_minutes=60,
        must_finish_before_close=True,
        available_to=_future(10),
    )
    learner = await _assigned(client, session_factory, exam["id"], "clamped")
    assert learner["remaining_seconds"] <= 10 * 60, "the sitting kept minutes the window does not give"
    state = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}")
    assert state["duration_minutes"] == 60, "the paper's own allowance is still what it was set to"
    brief = await _get(learner["session"], f"{STUDENT_EXAMS}/{exam['id']}")
    assert brief["must_finish_before_close"] is True


# --------------------------------------------------------------------------- #
# The token, and who may use it
# --------------------------------------------------------------------------- #


async def test_a_sitting_can_only_be_answered_by_the_learner_it_was_opened_for(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    mine = await _assigned(client, session_factory, exam["id"], "owner")
    yours = await _person(client, session_factory, "thief")

    for path in (f"{ATTEMPTS}/{mine['token']}", f"{ATTEMPTS}/{mine['token']}/steps", f"{ATTEMPTS}/{mine['token']}/result"):
        err = await _refused(yours["session"], "get", path, None, 404)
        assert err["message"] == "that is not an exam attempt"

    err = await _refused(
        yours["session"],
        "post",
        f"{ATTEMPTS}/answer",
        {"token": mine["token"], "exam_item_id": _right(mine), "response": {"option_index": 0}},
        404,
    )
    assert err["message"] == "that is not an exam attempt"
    assert await _attempt_count(exam["id"]) == 1, "another learner's request opened a sitting"


async def test_a_token_of_the_wrong_shape_is_not_a_sitting(client, session_factory) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "shaper")

    # A body carries the token through a validator, so a string that could not have been issued
    # here is refused before it reaches a query.
    err = await _refused(
        learner["session"],
        "post",
        f"{ATTEMPTS}/answer",
        {"token": "ZZZZ-not-hex", "exam_item_id": _right(learner), "response": {"option_index": 0}},
        422,
    )
    assert "not an exam attempt" in err["message"]

    # A path carries it as text, so the lookup answers it - and answers it as no sitting at all.
    err = await _refused(learner["session"], "get", f"{ATTEMPTS}/zzzz-not-hex", None, 404)
    assert err["message"] == "that is not an exam attempt"
    err = await _refused(learner["session"], "get", f"{ATTEMPTS}/{uuid.uuid4().hex}/steps", None, 404)
    assert err["code"] == "attempt_not_found"


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #


async def test_an_answer_is_recorded_and_the_verdict_is_held_back_until_the_session_ends(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "holder")
    saved = await _answer(learner["session"], learner, _right(learner), {"option_index": 0})

    assert saved["recorded"] is True
    assert saved["withheld"] is True
    assert saved["notice"] == "withheld"
    assert saved["correct"] is None and saved["score"] is None
    assert saved["remaining_seconds"] <= 30 * 60

    steps = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/steps")
    assert steps["steps"][0]["saved"] == {"option_index": 0}
    assert steps["steps"][0]["answered"] is True


async def test_a_paper_that_gives_instant_feedback_says_whether_that_answer_was_right(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), feedback_timing="instant")
    learner = await _assigned(client, session_factory, exam["id"], "instant")

    right = await _answer(learner["session"], learner, _right(learner), {"option_index": 0})
    assert right["withheld"] is False and right["notice"] is None
    assert right["correct"] is True and right["score"] == 2.0 and right["max_score"] == 2.0

    wrong = await _answer(learner["session"], learner, _right(learner), {"option_index": 2})
    assert wrong["correct"] is False and wrong["score"] == 0.0


async def test_a_blank_answer_and_a_line_that_is_not_on_the_paper_are_both_refused(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "blanker")

    err = await _refused(
        learner["session"],
        "post",
        f"{ATTEMPTS}/answer",
        {"token": learner["token"], "exam_item_id": _right(learner), "response": None},
        422,
    )
    assert err["message"] == "there is nothing in that answer yet"

    err = await _refused(
        learner["session"],
        "post",
        f"{ATTEMPTS}/answer",
        {"token": learner["token"], "exam_item_id": str(uuid.uuid4()), "response": {"option_index": 0}},
        422,
    )
    assert err["message"] == "that question is not on your paper"
    assert (await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}"))["answered_items"] == 0


async def test_changing_an_answer_is_counted_and_the_time_the_learner_reports_adds_up(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), feedback_timing="instant")
    learner = await _assigned(client, session_factory, exam["id"], "ditherer")
    item = _right(learner)

    await _answer(learner["session"], learner, item, {"option_index": 0}, time_spent_seconds=12)
    first = await _answer(learner["session"], learner, item, {"option_index": 1}, time_spent_seconds=8)
    assert first["correct"] is False
    again = await _answer(learner["session"], learner, item, {"option_index": 0}, time_spent_seconds=5)
    assert again["correct"] is True

    detail = await _get(client, f"{EXAMS}/attempts/{learner['attempt_id']}")
    line = next(row for row in detail["answers"] if row["exam_item_id"] == item)
    assert line["changed_count"] == 2, "the learner moved their answer twice and the record says once"
    assert line["time_spent_seconds"] == 25
    assert line["score"] == 2.0


async def test_the_learner_s_own_clock_never_moves_the_deadline(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "watchmaker")
    before = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}")

    for stamp in (_past(600), _future(600), "the morning of the eighteenth"):
        await _answer(
            learner["session"],
            learner,
            _right(learner),
            {"option_index": 0},
            client_updated_at=stamp,
        )

    after = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}")
    assert after["expires_at"] == before["expires_at"]
    assert after["status"] == "in_progress"
    assert after["remaining_seconds"] <= before["remaining_seconds"]


# --------------------------------------------------------------------------- #
# The deadline
# --------------------------------------------------------------------------- #


async def test_a_request_that_arrives_after_the_deadline_closes_the_sitting_on_the_way_in(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "slow")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})
    await _expire(learner["token"])

    state = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}")
    assert state["status"] == "auto_submitted"
    assert state["notice"] == "expired"
    assert state["remaining_seconds"] == 0
    assert state["server_seconds_used"] >= 90 * 60

    result = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result")
    assert result["score"] == 2.0 and result["max_score"] == 2.0
    assert (await _row(learner["token"])).submitted_at is not None


async def test_a_paper_that_is_not_auto_submitted_is_left_as_expired(
    client, session_factory
) -> None:
    exam = await _paper(
        client, choice_body("Which word means 'kite'?"), auto_submit_on_expiry=False
    )
    learner = await _assigned(client, session_factory, exam["id"], "unfinished")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})
    await _expire(learner["token"])

    state = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}")
    assert state["status"] == "expired"
    assert state["rules"]["auto_submit_on_expiry"] is False
    row = await _row(learner["token"])
    assert row.score == 2.0, "an expired paper is still marked from what was stored"


async def test_answering_a_closed_or_run_out_paper_is_refused_as_a_conflict(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    handed_in = await _assigned(client, session_factory, exam["id"], "finisher")
    await _submit(handed_in["session"], handed_in)
    err = await _refused(
        handed_in["session"],
        "post",
        f"{ATTEMPTS}/answer",
        {"token": handed_in["token"], "exam_item_id": _right(handed_in), "response": {"option_index": 0}},
        409,
    )
    assert err["code"] == "attempt_already_closed"
    assert err["message"] == "this attempt is already closed"

    run_out = await _assigned(client, session_factory, exam["id"], "ranout")
    await _expire(run_out["token"])
    err = await _refused(
        run_out["session"],
        "post",
        f"{ATTEMPTS}/answer",
        {"token": run_out["token"], "exam_item_id": _right(run_out), "response": {"option_index": 0}},
        409,
    )
    assert err["code"] == "attempt_time_up"
    assert err["message"] == "your time on this exam ran out"
    assert (await _row(run_out["token"])).status.value == "auto_submitted"


# --------------------------------------------------------------------------- #
# Handing the paper in
# --------------------------------------------------------------------------- #


async def test_handing_the_paper_in_twice_gives_the_same_result_and_says_so(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), result_visibility="immediate")
    learner = await _assigned(client, session_factory, exam["id"], "doubter")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})

    first = await _submit(learner["session"], learner)
    assert first["notice"] == "closed"
    assert first["visible"] is True and first["state"] == "shown"
    assert first["score"] == 2.0

    second = await _submit(learner["session"], learner)
    assert second["notice"] == "already_submitted"
    assert second["score"] == first["score"]
    assert second["submitted_at"] == first["submitted_at"]
    assert await _attempt_count(exam["id"]) == 1


async def test_an_offline_batch_is_stored_with_the_submit_and_a_bad_line_does_not_stop_the_rest(
    client, session_factory
) -> None:
    """The tab was shut for ten minutes: everything it buffered arrives with the submit, and a
    line that no longer belongs to the paper must not throw the learner's own work away."""
    exam = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        choice_body("Which is a fruit?"),
        result_visibility="immediate",
        feedback_timing="instant",
    )
    learner = await _assigned(client, session_factory, exam["id"], "offline")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})

    submitted = await _submit(
        learner["session"],
        learner,
        answers=[
            {"exam_item_id": _right(learner, 1), "response": {"option_index": 0}},
            {"exam_item_id": str(uuid.uuid4()), "response": {"option_index": 1}},
        ],
    )
    assert submitted["status"] == "submitted"
    assert submitted["answered_items"] == 2
    assert submitted["score"] == 4.0
    assert submitted["correct_count"] == 2
    assert submitted["total_items"] == 2


async def test_a_line_that_took_part_of_its_mark_is_counted_apart_from_right_and_wrong(
    client, session_factory
) -> None:
    """One box ticked out of two is 1 of 2, and the summary has to say that.

    `correct` is a two-valued flag, so a partial line lands on the false side of it. Counting it
    with the wrong answers would put "1 wrong" next to a line that still shows "1 of 2".

    The paper shows right-and-wrong per line, because that flag is a teacher's to reveal: with
    `show_correct_answers` off a line comes back with its marks but no verdict, and this test
    would then be asserting about a payload the learner never gets to see.
    """
    exam = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        select_body(),
        result_visibility="immediate",
        feedback_timing="instant",
        show_correct_answers=True,
    )
    learner = await _assigned(client, session_factory, exam["id"], "halfer")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})
    await _answer(learner["session"], learner, _right(learner, 1), {"option_indexes": [0]})

    submitted = await _submit(learner["session"], learner)
    assert submitted["score"] == 3.0, "the choice in full plus half of what the select is worth"
    assert submitted["correct_count"] == 1
    assert submitted["partial_count"] == 1
    assert submitted["incorrect_count"] == 0
    assert submitted["lines"][1]["correct"] is False and submitted["lines"][1]["score"] == 1.0


async def test_the_marks_come_from_the_version_the_learner_was_given_not_the_one_in_the_bank_now(
    client, session_factory
) -> None:
    """A teacher fixes a question while the exam week is running. The papers already handed in are
    graded against the text the learner saw, and so is the next sitting - because the *paper* is
    pinned, not just the attempt."""
    question = await _post(client, QUESTIONS, choice_body("Which word means 'kite'?"))
    exam = await _draft(client, title="Pinned run")
    await _post(
        client, f"{EXAMS}/{exam['id']}/items", {"items": [{"kind": "question", "ref_id": question["id"]}]}
    )
    exam = await _status(client, exam["id"], "active")

    learner = await _assigned(client, session_factory, exam["id"], "pinned")
    await _patch(
        client,
        f"{QUESTIONS}/{question['id']}",
        {"config": {"options": [{"text": "apple"}, {"text": "table"}, {"text": "window", "correct": True}]}},
    )

    moved = await _get(client, f"{QUESTIONS}/{question['id']}")
    assert moved["current_version"] == 2

    saved = await _answer(learner["session"], learner, _right(learner), {"option_index": 0})
    assert saved["recorded"] is True
    submitted = await _submit(learner["session"], learner)
    assert submitted["score"] == 2.0, "the sitting was re-marked against the live bank"

    detail = await _get(client, f"{EXAMS}/attempts/{learner['attempt_id']}")
    assert [row["version"] for row in detail["answers"]] == [1]

    # A learner who sits it after the edit is asked the same thing, because the paper is pinned too.
    second = await _assigned(client, session_factory, exam["id"], "aftertheedit")
    await _answer(second["session"], second, _right(second), {"option_index": 0})
    later = await _submit(second["session"], second)
    assert later["score"] == 2.0
    their_detail = await _get(client, f"{EXAMS}/attempts/{second['attempt_id']}")
    assert [row["version"] for row in their_detail["answers"]] == [1]


# --------------------------------------------------------------------------- #
# What the learner is allowed to see, and when
# --------------------------------------------------------------------------- #


async def test_the_result_is_only_as_visible_as_the_paper_s_own_rule(client, session_factory) -> None:
    hidden = await _paper(client, choice_body("Which word means 'kite'?"), result_visibility="hidden")
    learner = await _assigned(client, session_factory, hidden["id"], "hidden")
    await _submit(learner["session"], learner)
    result = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result")
    assert result["state"] == "hidden" and result["visible"] is False
    assert result["score"] is None and result["lines"] == []

    waiting = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        result_visibility="after_close",
        available_to=_future(120),
    )
    second = await _assigned(client, session_factory, waiting["id"], "waiting")
    await _submit(second["session"], second)
    result = await _get(second["session"], f"{ATTEMPTS}/{second['token']}/result")
    assert result["state"] == "closed", "the paper is still open, so nothing has been decided"
    assert result["visible"] is False


async def test_a_result_held_until_the_paper_closes_is_shown_afterwards(
    client, session_factory
) -> None:
    exam = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        result_visibility="after_close",
        available_to=_future(120),
    )
    learner = await _assigned(client, session_factory, exam["id"], "patient")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})
    await _submit(learner["session"], learner)
    assert (await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result"))["visible"] is False

    await _patch(client, f"{EXAMS}/{exam['id']}", {"available_to": _past(1)})
    shown = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result")
    assert shown["state"] == "shown" and shown["visible"] is True
    assert shown["score"] == 2.0
    assert [row["answered"] for row in shown["lines"]] == [True]


async def test_the_result_lines_only_name_the_correct_answer_when_the_paper_allows_it(
    client, session_factory
) -> None:
    """`show_correct_answers` and `show_explanations` are frozen with the sitting: a teacher who
    turns them on mid-week reaches the learners who start after the edit, not the ones already
    handed in - which is the same rule the clock follows, and the reason a result cannot change
    underneath a learner after the fact."""
    exam = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        result_visibility="immediate",
        show_correct_answers=False,
        show_explanations=False,
    )
    learner = await _assigned(client, session_factory, exam["id"], "bare")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 2})
    result = await _submit(learner["session"], learner)
    assert result["score"] == 0.0
    assert result["show_correct_answers"] is False
    assert result["lines"][0]["correct"] is None
    assert result["lines"][0]["explanation"] is None
    assert result["lines"][0]["reviewer_note"] is None, (
        "nothing was marked by hand, so the learner has no sentence to read either"
    )

    await _patch(
        client, f"{EXAMS}/{exam['id']}", {"show_correct_answers": True, "show_explanations": True}
    )
    before = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result")
    assert before["show_correct_answers"] is False, "an already-graded sitting was re-shown new rules"
    assert before["lines"][0]["explanation"] is None

    later = await _assigned(client, session_factory, exam["id"], "informed")
    await _answer(later["session"], later, _right(later), {"option_index": 2})
    shown = await _submit(later["session"], later)
    assert shown["show_correct_answers"] is True
    assert shown["lines"][0]["correct"] is False
    assert shown["lines"][0]["explanation"] == "An apple is the fruit."


async def test_the_passing_grade_is_measured_against_the_paper_s_own_total(
    client, session_factory
) -> None:
    exam = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        choice_body("Which is a fruit?"),
        passing_score=50,
        result_visibility="immediate",
    )
    half = await _assigned(client, session_factory, exam["id"], "halfway")
    await _answer(half["session"], half, _right(half), {"option_index": 0})
    result = await _submit(half["session"], half)
    assert result["score"] == 2.0 and result["max_score"] == 4.0
    assert result["passed"] is True, "50% of the paper's own total is the pass the teacher set"

    none_ = await _assigned(client, session_factory, exam["id"], "blanked")
    result = await _submit(none_["session"], none_)
    assert result["passed"] is False and result["score"] == 0.0


# --------------------------------------------------------------------------- #
# The teacher's queue
# --------------------------------------------------------------------------- #


async def test_an_essay_waits_for_a_teacher_and_the_paper_s_verdict_waits_with_it(
    client, session_factory
) -> None:
    exam = await _paper(
        client,
        essay_body(),
        passing_score=50,
        result_visibility="immediate",
        feedback_timing="instant",
    )
    learner = await _assigned(client, session_factory, exam["id"], "writer")
    saved = await _answer(learner["session"], learner, _right(learner), {"text": "A long enough answer about the sea."})
    assert saved["requires_manual"] is True

    result = await _submit(learner["session"], learner)
    assert result["state"] == "awaiting_teacher"
    assert result["visible"] is False
    assert result["manual_count"] == 1
    assert (await _row(learner["token"])).passed is None, "a verdict was guessed before the marks arrived"

    detail = await _get(client, f"{EXAMS}/attempts/{learner['attempt_id']}")
    assert detail["passing_score"] == 50.0 and detail["passed"] is None, (
        "the teacher's screen has to tell a verdict that is still owed from a paper with no pass mark"
    )

    queue = await _get(client, f"{GRADING}/queue")
    assert queue["total"] == 1
    review = queue["items"][0]
    assert review["student_name"] == learner["name"]
    assert review["max_score"] == 5.0
    assert review["question_type"] == "essay"
    assert review["response"] == {"text": "A long enough answer about the sea."}

    graded = await _grade(
        client, review["id"], {"score": 4, "note": "Good detail, one tense slip."}
    )
    assert graded["review"]["reviewed"] is True
    assert graded["review"]["final_score"] == 4.0
    assert graded["review"]["auto_score"] is None

    after = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result")
    assert after["visible"] is True
    assert after["score"] == 4.0 and after["max_score"] == 5.0
    assert after["passed"] is True and after["manual_count"] == 0
    assert after["partial_count"] == 1 and after["incorrect_count"] == 0, (
        "a line the teacher left at 4 of 5 took part of its mark: the summary that calls it wrong "
        "contradicts the same line, which says 4 of 5"
    )
    assert after["lines"][0]["reviewer_note"] == "Good detail, one tense slip.", (
        "the sentence written with the mark is the learner's answer to \"why this mark\", and a "
        "result that shows 4 of 5 without it leaves the number to be guessed at"
    )

    emptied = await _get(client, f"{GRADING}/queue")
    assert emptied["total"] == 0
    done = await _get(client, f"{GRADING}/queue?reviewed=true")
    assert [row["final_score"] for row in done["items"]] == [4.0]


async def test_marking_is_refused_while_the_paper_is_still_being_sat_and_above_its_own_mark(
    client, session_factory
) -> None:
    """The review box only opens when the paper does: a mark written against a sitting that is
    still running would be corrected by answers that have not arrived yet."""
    exam = await _paper(client, essay_body())
    learner = await _assigned(client, session_factory, exam["id"], "still_writing")
    await _answer(learner["session"], learner, _right(learner), {"text": "Something about the sea and the wind."})

    attempt_id = uuid.UUID(learner["attempt_id"])
    async with SessionLocal() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(ManualReview).where(ManualReview.attempt_id == attempt_id)
            )
            == 0
        ), "an answer was queued for a teacher before the paper was handed in"
        answer_id = (
            await db.execute(select(AttemptAnswer.id).where(AttemptAnswer.attempt_id == attempt_id))
        ).scalar_one()
        # A row written from outside the engine: the one state the guard below is really for.
        planted = ManualReview(attempt_id=attempt_id, answer_id=answer_id, reviewed=False)
        db.add(planted)
        await db.commit()
        review_id = planted.id

    err = await _refused(client, "post", f"{GRADING}/answers/{review_id}/grade", {"score": 1}, 422)
    assert err["code"] == "grading_not_submitted_yet"
    assert err["message"] == "the learner has not handed this paper in yet"

    await _submit(learner["session"], learner)
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(ManualReview).where(ManualReview.attempt_id == attempt_id))
        ).scalars().all()
    assert len(rows) == 1, "closing the paper queued the same answer a second time"

    err = await _refused(client, "post", f"{GRADING}/answers/{review_id}/grade", {"score": 9}, 422)
    assert err["code"] == "grading_mark_above_max"
    assert "worth 5" in err["message"]
    # The two numbers the teacher needs travel beside the sentence, not inside it, so their own
    # language can put them where the grammar wants them.
    assert err["params"] == {"max": 5.0, "score": 9.0}

    graded = await _grade(client, review_id, {"score": 2, "correct": False})
    assert graded["review"]["final_score"] == 2.0
    again = await _grade(client, review_id, {"score": 3})
    assert again["review"]["final_score"] == 3.0
    assert again["attempt"]["score"] == 3.0

    async with SessionLocal() as db:
        marks = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "exam.answer.grade", AuditLog.target_id == answer_id)
                .order_by(AuditLog.created_at)
            )
        ).scalars().all()
    assert len(marks) == 2, "a re-mark was not audited beside the first mark"
    assert marks[1].before["final_score"] == 2.0
    assert marks[1].after["final_score"] == 3.0


async def test_a_paper_marked_by_hand_sends_every_answer_to_the_teacher(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), choice_body("Which is a fruit?"), grading_mode="manual")
    learner = await _assigned(client, session_factory, exam["id"], "essayist")
    for index, item in enumerate(learner["items"]):
        await _answer(learner["session"], learner, item, {"option_index": index})
    await _submit(learner["session"], learner)

    queue = await _get(client, f"{GRADING}/queue?exam_id={exam['id']}")
    assert queue["total"] == 2
    assert {row["question_type"] for row in queue["items"]} == {"multiple_choice"}
    assert all(row["reviewed"] is False for row in queue["items"])
    assert (await _row(learner["token"])).passed is None

    summary = await _get(client, f"{GRADING}/summary")
    assert summary["pending"] == 2
    assert [row["pending"] for row in summary["exams"] if row["exam_id"] == exam["id"]] == [2]
    assert [row["pending"] for row in summary["students"] if row["student_id"] == learner["id"]] == [2]

    for row in queue["items"]:
        await _grade(client, row["id"], {"score": row["max_score"]})
    result = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result")
    assert result["state"] == "shown" and result["score"] == 4.0


# --------------------------------------------------------------------------- #
# Tab switches and the sweep
# --------------------------------------------------------------------------- #


async def test_tab_switches_are_only_counted_while_the_paper_says_to_watch(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "unwatched")
    report = await _switch(learner["session"], learner)
    assert report["notice"] == "monitoring_off"
    assert report["tab_switches"] == 0
    assert (await _row(learner["token"])).tab_switch_count == 0


async def test_the_switch_limit_warns_first_and_closes_on_the_switch_after_it(
    client, session_factory
) -> None:
    warned = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        monitor_tab_switch=True,
        tab_switch_limit=1,
        tab_switch_action="warn",
    )
    learner = await _assigned(client, session_factory, warned["id"], "warner")
    first = await _switch(learner["session"], learner)
    assert first["notice"] == "tab_switched" and first["tab_switches"] == 1
    second = await _switch(learner["session"], learner)
    assert second["tab_switches"] == 2 and second["status"] == "in_progress"
    assert second["notice"] == "tab_switched", "a limit of one means one switch is tolerated"

    closed = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        monitor_tab_switch=True,
        tab_switch_limit=1,
        tab_switch_action="auto_submit",
        result_visibility="immediate",
    )
    sitter = await _assigned(client, session_factory, closed["id"], "closer")
    await _answer(sitter["session"], sitter, _right(sitter), {"option_index": 0})
    await _switch(sitter["session"], sitter)
    report = await _switch(sitter["session"], sitter)
    assert report["notice"] == "tab_limit_reached"
    assert report["auto_submitted"] is True
    assert report["status"] == "auto_submitted"

    row = await _row(sitter["token"])
    assert row.status.value == "auto_submitted"
    assert row.score == 2.0, "the paper was not graded from what had been stored"
    detail = await _get(client, f"{EXAMS}/attempts/{sitter['attempt_id']}")
    assert detail["tab_switches"] == 2
    assert detail["monitor_tab_switch"] is True
    assert detail["tab_switch_action"] == "auto_submit"
    assert detail["tab_switch_limit"] == 1


async def test_the_sweep_closes_the_papers_nobody_came_back_for(client, session_factory) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), max_attempts=5)
    gone = await _assigned(client, session_factory, exam["id"], "gone")
    away = await _assigned(client, session_factory, exam["id"], "away")
    await _answer(gone["session"], gone, _right(gone), {"option_index": 0})
    await _expire(gone["token"])
    await _expire(away["token"], minutes=1)

    async with SessionLocal() as db:
        report = await attempt_service.expire_due(db)
        await db.commit()

    assert report["closed"] >= 2
    assert report["submitted"] == report["closed"]
    assert (await _row(gone["token"])).status.value == "auto_submitted"
    assert (await _row(away["token"])).status.value == "auto_submitted"

    still_open = await _assigned(client, session_factory, exam["id"], "present")
    async with SessionLocal() as db:
        report = await attempt_service.expire_due(db)
        await db.commit()
    assert (await _row(still_open["token"])).status.value == "in_progress"
    assert report["closed"] == 0, "the sweep took a paper that was still within its time"


# --------------------------------------------------------------------------- #
# The teacher's read of the sittings
# --------------------------------------------------------------------------- #


async def test_the_teacher_s_list_of_sittings_filters_and_refuses_words_it_does_not_know(
    client, session_factory
) -> None:
    exam = await _paper(
        client,
        choice_body("Which word means 'kite'?"),
        max_attempts=3,
        monitor_tab_switch=True,
        tab_switch_limit=1,
        tab_switch_action="auto_submit",
    )
    handed_in = await _assigned(client, session_factory, exam["id"], "listed1")
    await _submit(handed_in["session"], handed_in)
    open_sitting = await _assigned(client, session_factory, exam["id"], "listed2")
    closed_by_tabs = await _assigned(client, session_factory, exam["id"], "listed3")
    await _switch(closed_by_tabs["session"], closed_by_tabs)
    await _switch(closed_by_tabs["session"], closed_by_tabs)

    whole = await _get(client, f"{EXAMS}/{exam['id']}/attempts")
    assert whole["total"] == 3
    assert whole["page"] == 1 and whole["page_size"] == 25
    assert {row["status"] for row in whole["items"]} == {"submitted", "in_progress", "auto_submitted"}
    started = [row["started_at"] for row in whole["items"]]
    assert started == sorted(started, reverse=True), "the list is not newest first"

    row = next(item for item in whole["items"] if item["token"] == open_sitting["token"])
    assert row["student_name"] == open_sitting["name"]
    assert row["total_items"] == 1 and row["answered_items"] == 0
    assert row["attempt_number"] == 1

    by_status = await _get(client, f"{EXAMS}/{exam['id']}/attempts?status=submitted")
    assert [row["status"] for row in by_status["items"]] == ["submitted"]

    by_student = await _get(client, f"{EXAMS}/{exam['id']}/attempts?student_id={handed_in['id']}")
    assert by_student["total"] == 1

    err = await _refused(client, "get", f"{EXAMS}/{exam['id']}/attempts?status=nonsense", None, 422)
    assert "in_progress" in err["message"] and "nonsense" in err["message"]

    paged = await _get(client, f"{EXAMS}/{exam['id']}/attempts?page=2&page_size=2")
    assert len(paged["items"]) == 1 and paged["total"] == 3


async def test_the_teacher_s_detail_shows_the_marks_the_learner_never_got(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), choice_body("Which is a fruit?"))
    learner = await _assigned(client, session_factory, exam["id"], "detailed")
    await _answer(learner["session"], learner, _right(learner), {"option_index": 0})
    await _submit(learner["session"], learner)

    detail = await _get(client, f"{EXAMS}/attempts/{learner['attempt_id']}")
    assert detail["status"] == "submitted"
    assert detail["exam_title"] == exam["title"]
    assert detail["student_name"] == learner["name"]
    assert detail["score"] == 2.0 and detail["max_score"] == 4.0
    assert detail["monitor_tab_switch"] is False, "a paper that never watched the tab explained a switch"
    assert detail["tab_switch_action"] is None and detail["tab_switch_limit"] is None
    assert "switch_policy" not in detail, "the server sends codes and the screen writes the sentence"
    assert detail["passing_score"] is None, "no pass mark was set, so no verdict is missing"
    assert len(detail["answers"]) == 2
    first = next(row for row in detail["answers"] if row["exam_item_id"] == _right(learner))
    assert first["correct"] is True and first["graded"] is True and first["answered"] is True
    assert first["max_score"] == 2.0 and first["version"] == 1
    assert first["needs_manual_review"] is False


async def test_a_note_from_the_teacher_reaches_the_learner_s_result_and_nowhere_else(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"), result_visibility="immediate")
    learner = await _assigned(client, session_factory, exam["id"], "noted")
    await _submit(learner["session"], learner)

    written = await _post(
        client,
        f"{GRADING}/attempts/{learner['attempt_id']}/feedback",
        {"body": "Well done on the tenses. Read the third question again."},
    )
    assert written["attempt_id"] == learner["attempt_id"]
    assert written["student_id"] == learner["id"]

    listed = await _get(client, f"{GRADING}/students/{learner['id']}/feedback")
    assert [row["body"] for row in listed["items"]] == ["Well done on the tenses. Read the third question again."]

    result = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/result")
    assert [row["body"] for row in result["feedback"]] == [written["body"]]

    other = await _assigned(client, session_factory, exam["id"], "noteless")
    await _submit(other["session"], other)
    theirs = await _get(other["session"], f"{ATTEMPTS}/{other['token']}/result")
    assert theirs["feedback"] == []

    gone = await client.delete(f"{GRADING}/feedback/{written['id']}")
    assert gone.status_code == 200, gone.text
    assert gone.json()["removed"] is True
    assert (await _get(client, f"{GRADING}/students/{learner['id']}/feedback"))["items"] == []


# --------------------------------------------------------------------------- #
# Two facts the invariants rest on
# --------------------------------------------------------------------------- #


async def test_two_tabs_that_open_at_the_same_moment_leave_one_sitting(
    client, session_factory
) -> None:
    """`uq_attempt_number` is the last word: a double click must not cost the learner an attempt."""
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    person = await _person(client, session_factory, "clicker")
    await _assign(client, exam["id"], person)

    first, second = await asyncio.gather(
        person["session"].post(f"{STUDENT_EXAMS}/{exam['id']}/start", json={}),
        (await _second_session(session_factory, person)).post(f"{STUDENT_EXAMS}/{exam['id']}/start", json={}),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert await _attempt_count(exam["id"]) == 1
    assert first.json()["token"] == second.json()["token"]
    assert second.json()["resumed"] is True


async def test_the_activity_log_records_the_opening_and_the_closing_of_a_sitting(
    client, session_factory
) -> None:
    exam = await _paper(client, choice_body("Which word means 'kite'?"))
    learner = await _assigned(client, session_factory, exam["id"], "logged")
    await _switch(learner["session"], learner)
    await _submit(learner["session"], learner)

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ActivityEvent)
                .where(
                    ActivityEvent.student_id == uuid.UUID(learner["id"]),
                    ActivityEvent.category == "assessment",
                )
                .order_by(ActivityEvent.occurred_at)
            )
        ).scalars().all()

    assert [row.event_type for row in rows] == ["exam_attempt_start", "exam_attempt_submitted"]
    assert rows[0].context_type == "exam" and str(rows[0].context_id) == exam["id"]
    assert rows[0].payload["attempt_number"] == 1
    assert rows[1].payload["status"] == "submitted"
    assert rows[1].payload["score"] == 0.0


async def test_a_question_binned_mid_week_does_not_take_the_paper_away_from_somebody_sitting_it(
    client, session_factory
) -> None:
    """The pinned version is the learner's protection: cleaning the bank after a sitting has opened
    cannot empty the paper they are working on - and a new sitting says so instead of opening a
    paper with nothing on it."""
    question = await _post(client, QUESTIONS, choice_body("Which word means 'kite'?"))
    exam = await _draft(client, title="Binned mid week")
    await _post(
        client, f"{EXAMS}/{exam['id']}/items", {"items": [{"kind": "question", "ref_id": question["id"]}]}
    )
    exam = await _status(client, exam["id"], "active")
    learner = await _assigned(client, session_factory, exam["id"], "sheltered")

    await client.delete(f"{QUESTIONS}/{question['id']}")

    steps = await _get(learner["session"], f"{ATTEMPTS}/{learner['token']}/steps")
    assert steps["steps"][0]["view"]["prompt"] == "Which word means 'kite'?"
    assert steps["status"] == "in_progress"

    stranger = await _person(client, session_factory, "latecomer")
    await _post(client, f"{EXAMS}/{exam['id']}/assignments", {"student_ids": [stranger["id"]], "group_ids": []})
    err = await _refused(stranger["session"], "post", f"{STUDENT_EXAMS}/{exam['id']}/start", {}, 422)
    assert err["message"] == "this exam has no questions ready to serve yet"
    assert await _attempt_count(exam["id"]) == 1
