"""Offline tests for the attempt engine's rules (Phase 7).

An exam sitting is the one place in this platform where a wrong number lands on a learner's
record, and the rules it turns on are arithmetic and policy rather than rows: what a deadline
means when the browser's clock disagrees, which copy of a question an answer is marked against,
what a feedback rule hides, and which words a screen is allowed to branch on. All of that is
decidable without a database, so it is decided here. The sittings themselves - opening, resuming,
closing, marking, the races behind them - are in `tests/integration/test_attempt_db.py`.
"""
from __future__ import annotations

import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core import enums
from app.schemas import attempt as a
from app.services import attempt_service as at
from app.services import exam_service, question_engine

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Fakes for the pure functions
# --------------------------------------------------------------------------- #


def blueprint(
    rules: dict | None = None, items: list[dict] | None = None, contexts: dict | None = None
) -> dict:
    """A sitting's own copy of its paper, in the shape the freeze writes."""
    return {"schema": 1, "rules": rules or {}, "items": items or [], "contexts": contexts or {}}


def attempt_row(**over) -> SimpleNamespace:
    """An `ExamAttempt` as the pure functions see it: fields and a blueprint, no session."""
    base = {
        "id": uuid.uuid4(),
        "exam_id": uuid.uuid4(),
        "student_id": uuid.uuid4(),
        "session_id": "ab" * 16,
        "status": enums.AttemptStatus.IN_PROGRESS,
        "attempt_number": 1,
        "started_at": datetime(2026, 10, 8, 9, tzinfo=UTC),
        "expires_at": datetime(2026, 10, 8, 9, 30, tzinfo=UTC),
        "submitted_at": None,
        "server_seconds_used": 0,
        "tab_switch_count": 0,
        "score": None,
        "max_score": None,
        "passed": None,
        "blueprint": blueprint(),
    }
    base.update(over)
    return SimpleNamespace(**base)


def sitting(**rules) -> SimpleNamespace:
    return attempt_row(blueprint=blueprint(rules=rules))


def line(item_id: uuid.UUID | None = None, **over) -> dict:
    """One blueprint row: a question this sitting was dealt, with its own mark and version."""
    base = {
        "exam_item_id": str(item_id or uuid.uuid4()),
        "kind": "question",
        "ref_id": str(uuid.uuid4()),
        "question_version_id": str(uuid.uuid4()),
        "version": 2,
        "section_id": None,
        "points": 1.0,
        "authored_position": 0,
        "type": "multiple_choice",
        "title": "Pick one",
        "context_id": None,
    }
    base.update(over)
    return base


def authored(q_type: str, config: dict) -> dict:
    """A question's config as the bank actually stores it: run through the engine's own
    validator, so a fixture can never be a shape no real version has ever held."""
    return question_engine.validate_config(q_type, config)


def question_snapshot(**over) -> dict:
    """The pinned version as `snapshot_for_version` returns it."""
    question = {
        "id": str(uuid.uuid4()),
        "type": "multiple_choice",
        "prompt": "Pick one",
        "score": 1.0,
        "config": authored(
            "multiple_choice",
            {
                "options": [
                    {"text": "alpha", "correct": True},
                    {"text": "beta"},
                    {"text": "gamma"},
                ]
            },
        ),
        "explanation": "alpha is the one.",
        "partial_scoring": {},
        "negative_scoring": {},
    }
    question.update(over)
    return {"question": question}


def timed(**rules) -> tuple[SimpleNamespace, uuid.UUID]:
    """A sitting of one multi_select line worth 2 marks, and the id of that line."""
    item_id = uuid.uuid4()
    return (
        attempt_row(
            blueprint=blueprint(
                rules=rules, items=[line(item_id, points=2.0, type="multi_select")]
            )
        ),
        item_id,
    )


# --------------------------------------------------------------------------- #
# The clock
# --------------------------------------------------------------------------- #


def test_a_paper_with_no_time_limit_has_no_countdown() -> None:
    """`None` is the teacher's "no limit", and it must not reach the runner as `0`, which is
    the number that means the paper is over."""
    assert at.remaining_seconds(attempt_row(expires_at=None)) is None


def test_the_countdown_is_counted_from_the_server_s_clock() -> None:
    moment = datetime(2026, 10, 8, 9, tzinfo=UTC)
    row = attempt_row(expires_at=moment + timedelta(minutes=30))
    assert at.remaining_seconds(row, at=moment) == 1800
    assert at.remaining_seconds(row, at=moment + timedelta(seconds=1)) == 1799


def test_a_deadline_that_has_passed_shows_zero_and_never_a_negative_number() -> None:
    moment = datetime(2026, 10, 8, 9, tzinfo=UTC)
    row = attempt_row(expires_at=moment - timedelta(hours=3))
    assert at.remaining_seconds(row, at=moment) == 0


def test_the_countdown_is_whole_seconds_truncated_down() -> None:
    """A learner shown "9 seconds" who then presses submit at 9.9 should not be told they were
    late. Rounding up would hand out time the paper does not have."""
    moment = datetime(2026, 10, 8, 9, tzinfo=UTC)
    row = attempt_row(expires_at=moment + timedelta(seconds=10, milliseconds=900))
    assert at.remaining_seconds(row, at=moment) == 10


def test_a_deadline_stored_without_a_timezone_is_read_as_utc() -> None:
    """The column is `timestamptz`, but a blueprint built by hand in a test or a repair script
    can carry a naive value, and comparing the two must not raise at a learner's request."""
    naive = datetime(2026, 10, 8, 9, 30)
    row = attempt_row(expires_at=naive)
    assert at.remaining_seconds(row, at=datetime(2026, 10, 8, 9, tzinfo=UTC)) == 1800


@pytest.mark.parametrize(
    "status",
    [
        enums.AttemptStatus.SUBMITTED,
        enums.AttemptStatus.AUTO_SUBMITTED,
        enums.AttemptStatus.EXPIRED,
    ],
)
def test_a_paper_that_is_already_in_a_book_cannot_expire_again(status: enums.AttemptStatus) -> None:
    """The deadline passed; so what. A submitted paper closed at its own moment, and calling it
    expired now would rewrite a result the learner already has - and the sweep would keep
    finding it."""
    row = attempt_row(
        status=status, expires_at=at._now() - timedelta(days=1), submitted_at=at._now()
    )
    assert at.is_expired(row) is False


def test_an_open_sitting_past_its_deadline_is_expired() -> None:
    assert at.is_expired(attempt_row(expires_at=at._now() - timedelta(seconds=1))) is True


def test_the_deadline_itself_is_already_too_late() -> None:
    """`expires_at` is the last instant the paper is open, so a request that lands on it is not
    inside the sitting. A learner who arrives exactly on the minute gets the same answer as one
    who arrives a second later."""
    assert at.is_expired(attempt_row(expires_at=at._now())) is True


def test_an_open_sitting_inside_its_deadline_is_not_expired() -> None:
    assert at.is_expired(attempt_row(expires_at=at._now() + timedelta(minutes=1))) is False


def test_an_open_sitting_with_no_deadline_is_never_expired() -> None:
    assert at.is_expired(attempt_row(expires_at=None)) is False


@pytest.mark.parametrize(
    ("status", "closed"),
    [
        (enums.AttemptStatus.IN_PROGRESS, False),
        (enums.AttemptStatus.SUBMITTED, True),
        (enums.AttemptStatus.AUTO_SUBMITTED, True),
        (enums.AttemptStatus.EXPIRED, True),
    ],
)
def test_which_words_mean_the_sitting_is_over(status, closed: bool) -> None:
    assert at.is_closed(attempt_row(status=status)) is closed


# --------------------------------------------------------------------------- #
# The sitting's own copy of its paper
# --------------------------------------------------------------------------- #


def test_a_blueprint_that_was_never_written_reads_as_empty_rather_than_raising() -> None:
    """A row whose freeze failed halfway is a fact to report, not a crash on a learner's read."""
    row = attempt_row(blueprint=None)
    assert at.blueprint_of(row) == {}
    assert at.rules_of(row) == {}
    assert at.entries_of(row) == []


def test_a_line_that_is_not_on_the_paper_is_refused_by_name() -> None:
    item = line()
    row = attempt_row(blueprint=blueprint(items=[item]))
    with pytest.raises(at.AttemptError, match="not on your paper"):
        at._entry(row, uuid.uuid4())


def test_the_answer_is_keyed_to_the_paper_s_line_and_not_to_the_question() -> None:
    """One paper can ask the same question twice through two pinned versions. A lookup by
    `ref_id` would mark both answers against whichever line came first."""
    shared_ref = str(uuid.uuid4())
    first, second = uuid.uuid4(), uuid.uuid4()
    row = attempt_row(
        blueprint=blueprint(
            items=[
                line(first, ref_id=shared_ref, points=1.0),
                line(second, ref_id=shared_ref, points=4.0),
            ]
        )
    )
    assert at._entry(row, second)["points"] == 4.0
    assert at._entry(row, first)["points"] == 1.0


def test_a_read_path_reports_a_missing_line_instead_of_refusing_the_screen() -> None:
    """The teacher's detail view shows every answer the sitting holds, including one whose line
    has left the blueprint. A result page that raises would hide the learner's own work."""
    item = line()
    row = attempt_row(blueprint=blueprint(items=[item]))
    assert at._blueprint_entry(row, None) == {}
    assert at._blueprint_entry(row, uuid.uuid4()) == {}
    assert at._blueprint_entry(row, uuid.UUID(item["exam_item_id"])) is item


# --------------------------------------------------------------------------- #
# Serving one line to a learner
# --------------------------------------------------------------------------- #


def options() -> dict:
    return {
        "kind": "options",
        "options": [
            {"index": 0, "text": "alpha"},
            {"index": 1, "text": "beta"},
            {"index": 2, "text": "gamma"},
        ],
    }


def test_options_are_laid_out_in_the_dealt_order_without_losing_their_authored_index() -> None:
    """Display order is a shuffle and meaning is not: the answer names the index it was written
    with, so a learner who picks "gamma" is right or wrong on the same facts whichever line it
    appeared on."""
    shown = at._public_config(options(), [2, 0, 1])
    assert [row["text"] for row in shown["options"]] == ["gamma", "alpha", "beta"]
    assert [row["index"] for row in shown["options"]] == [2, 0, 1]


def test_a_line_with_no_option_order_is_shown_as_the_teacher_wrote_it() -> None:
    assert at._public_config(options(), None) == options()
    assert at._public_config(options(), []) == options()


@pytest.mark.parametrize("order", [[0, 1], [0, 1, 2, 3], [0, 0, 1]])
def test_an_option_list_that_does_not_match_its_layout_is_left_alone(order: list[int]) -> None:
    """A layout for a different number of options means the two came from different copies of
    the question. Reordering anyway would drop or repeat a line the learner has to answer."""
    assert at._public_config(options(), order) == options()


def test_a_block_that_holds_no_option_list_is_passed_through_untouched() -> None:
    """Gap-fills, orderings and essays have no options, and their config must not be touched by
    a shuffle that only makes sense for a choice."""
    text = {"kind": "essay", "min_words": 50}
    assert at._public_config(text, [0, 1]) is text


def test_a_reading_line_carries_the_copy_the_sitting_was_dealt_with() -> None:
    """The learner reads the text as it was when the paper opened. A teacher editing the document
    afterwards must not change what an open attempt is being asked about."""
    body = {"kind": "reading", "title": "Kite weather", "body": "A kite needs wind."}
    row = attempt_row(blueprint=blueprint(items=[line(context_id="c1")], contexts={"c1": dict(body)}))
    frozen = at.blueprint_of(row)["contexts"]["c1"]
    shown = at._context_of(row, at.entries_of(row)[0])
    assert shown == {"kind": "reading", "reading": body}
    assert shown["reading"] is not frozen, "the page was handed the blueprint's own dict"


def test_a_recording_line_is_asked_for_a_file_without_being_given_a_stored_link() -> None:
    """Only the asset id is frozen, because a signed URL outlives its signature. Everything the
    runner needs to play the file comes from the storage layer on the request."""
    body = {
        "kind": "listening",
        "title": "At the market",
        "transcript": "Two kilos of tomatoes.",
        "media_asset_id": str(uuid.uuid4()),
        "show_transcript": False,
    }
    row = attempt_row(blueprint=blueprint(items=[line(context_id="c2")], contexts={"c2": body}))
    shown = at._context_of(row, at.entries_of(row)[0])
    assert "media_asset_id" not in shown["listening"]
    assert shown["listening"]["transcript"] == body["transcript"]
    assert at.blueprint_of(row)["contexts"]["c2"]["media_asset_id"] == body["media_asset_id"]


def test_a_line_with_no_context_is_asked_on_its_own() -> None:
    row = attempt_row(blueprint=blueprint(items=[line()]))
    assert at._context_of(row, at.entries_of(row)[0]) is None


def test_a_context_id_with_nothing_behind_it_is_not_invented() -> None:
    """A blueprint that names a text it does not hold is a data gap; a made-up passage on an
    exam paper would be worse than the gap."""
    row = attempt_row(blueprint=blueprint(items=[line(context_id=str(uuid.uuid4()))]))
    assert at._context_of(row, at.entries_of(row)[0]) is None


def test_the_rules_a_runner_obeys_have_words_when_the_blueprint_has_none() -> None:
    """A sitting opened before a rule existed still has to be served. The defaults are the
    conservative ones: no copy-paste, no monitoring, no verdict until the paper is in."""
    read = at._rules_read({})
    assert read["allow_previous"] is True
    assert read["restrict_copy_paste"] is False
    assert read["monitor_tab_switch"] is False
    assert read["tab_switch_limit"] is None
    assert read["tab_switch_action"] is None
    assert read["resume_after_disconnect"] is True
    assert read["feedback_timing"] == "after_session"
    assert read["auto_submit_on_expiry"] is True


def test_the_runner_hears_about_no_rule_the_sitting_does_not_carry() -> None:
    """The runner's switches are exactly this body's fields: one key the page invented, or one
    the blueprint holds that the page never gets, and the rules a learner is under stop being
    the rules the server enforced."""
    assert set(at._rules_read({})) == set(a.AttemptRulesRead.model_fields)


def test_a_stored_rule_is_handed_over_as_it_was_written() -> None:
    read = at._rules_read(
        {"monitor_tab_switch": True, "tab_switch_limit": 3, "tab_switch_action": "warn",
         "feedback_timing": "instant", "allow_previous": False}
    )
    assert read["monitor_tab_switch"] is True
    assert read["tab_switch_limit"] == 3
    assert read["feedback_timing"] == "instant"
    assert read["allow_previous"] is False


# --------------------------------------------------------------------------- #
# Marking one answer
# --------------------------------------------------------------------------- #


def a_multi_select() -> dict:
    return question_snapshot(
        type="multi_select",
        config=authored(
            "multi_select",
            {
                "options": [
                    {"text": "alpha", "correct": True},
                    {"text": "beta", "correct": True},
                    {"text": "gamma"},
                ]
            },
        ),
    )


def test_a_paper_the_teacher_marks_by_hand_asks_the_teacher_for_every_answer() -> None:
    """`grading_mode=manual` is a promise about the whole paper, so the engine must not put a
    number on any of it - not even the ones it could decide."""
    row, item = timed(grading_mode="manual")
    result = at._grade(row, at._entry(row, item), a_multi_select(), {"option_indexes": [0]})
    assert result.requires_manual is True
    assert result.correct is None
    assert result.score == 0.0
    assert result.detail["reason"] == "teacher_marks_everything"


def test_an_answer_with_nothing_pinned_behind_it_cannot_be_decided_by_a_machine() -> None:
    row, item = timed(grading_mode="automatic")
    result = at._grade(row, at._entry(row, item), None, {"option_index": 0})
    assert result.requires_manual is True
    assert result.detail["reason"] == "no_pinned_version"


def test_a_type_the_engine_has_never_heard_of_is_passed_to_a_person() -> None:
    """A version written on another install can name a type this build does not carry. The
    answer is kept and a teacher marks it; the alternative is a silent zero."""
    row, item = timed(grading_mode="automatic")
    result = at._grade(
        row, at._entry(row, item), question_snapshot(type="drag_and_drop"), {"pairs": []}
    )
    assert result.requires_manual is True
    assert result.detail["reason"] == "unknown_question_type"


def test_an_essay_is_never_given_a_number_the_machine_made_up() -> None:
    row, item = timed(grading_mode="automatic")
    result = at._grade(
        row, at._entry(row, item), question_snapshot(type="essay"), {"text": "a paragraph"}
    )
    assert result.requires_manual is True
    assert result.detail["reason"] == "type_needs_a_teacher"


def test_an_answer_is_worth_the_mark_the_paper_gave_it() -> None:
    """The line carries the teacher's override; the pinned version's own score does not get a
    vote once the paper has a number on it."""
    row, item = timed(grading_mode="automatic")
    result = at._grade(
        row, at._entry(row, item), question_snapshot(score=1.0), {"option_index": 0}
    )
    assert (result.score, result.max_score) == (2.0, 2.0)


def test_a_wrong_tick_cancels_a_right_one_only_when_the_exam_allows_it() -> None:
    """`select all that apply` with every box ticked is worth full marks under pure positive
    marking, which is a teacher's choice. The engine's own default cancels one tick per wrong
    one, so the exam's switch has to be what decides."""
    all_three = {"option_indexes": [0, 1, 2]}
    row, item = timed(grading_mode="automatic", negative_marking_enabled=False)
    lenient = at._grade(row, at._entry(row, item), a_multi_select(), all_three)
    assert lenient.score == 2.0

    row, item = timed(grading_mode="automatic", negative_marking_enabled=True)
    strict = at._grade(row, at._entry(row, item), a_multi_select(), all_three)
    assert strict.score == 0.0
    assert strict.correct is False


def test_a_paper_with_partial_marking_off_wants_every_part_right() -> None:
    row, item = timed(grading_mode="automatic", partial_scoring_enabled=False)
    half = at._grade(row, at._entry(row, item), a_multi_select(), {"option_indexes": [0]})
    assert half.score == 0.0
    assert half.correct is False


def test_partial_marking_is_on_unless_the_teacher_turned_it_off() -> None:
    """An absent key is not "off": a paper written before the switch existed still credits the
    parts a learner got right."""
    row, item = timed(grading_mode="automatic")
    half = at._grade(row, at._entry(row, item), a_multi_select(), {"option_indexes": [0]})
    assert half.score == 1.0
    assert half.correct is False


def test_the_question_s_own_partial_mode_is_respected_when_the_exam_allows_partial() -> None:
    """A teacher can pin one question to all-or-nothing without rewriting the exam's switch."""
    row, item = timed(grading_mode="automatic", partial_scoring_enabled=True)
    pinned = a_multi_select()
    pinned["question"]["partial_scoring"] = {"mode": "all_or_nothing"}
    half = at._grade(row, at._entry(row, item), pinned, {"option_indexes": [0]})
    assert half.score == 0.0


def test_an_answer_is_marked_against_the_pinned_version_and_not_the_live_question() -> None:
    """Invariant 3 doing the work it exists for: the answer key is whatever the version the paper
    pinned said, so editing the question mid-exam-week reaches next term's paper only."""
    row, item = timed(grading_mode="automatic")
    pinned = question_snapshot(
        config=authored(
            "multiple_choice",
            {"options": [{"text": "alpha"}, {"text": "beta", "correct": True}]},
        )
    )
    assert at._grade(row, at._entry(row, item), pinned, {"option_index": 1}).correct is True


# --------------------------------------------------------------------------- #
# What a result is, and who may see it
# --------------------------------------------------------------------------- #


def test_a_pass_mark_is_a_percentage_of_this_paper() -> None:
    rules = {"passing_score": 50.0}
    assert at._passed(5.0, 10.0, rules, pending=0) is True
    assert at._passed(4.9, 10.0, rules, pending=0) is False


def test_the_threshold_itself_is_a_pass() -> None:
    """A teacher who writes "50 to pass" means exactly fifty counts."""
    assert at._passed(5.0, 10.0, {"passing_score": 50}, pending=0) is True


def test_a_paper_with_no_pass_mark_gives_no_verdict() -> None:
    assert at._passed(9.0, 10.0, {}, pending=0) is None


def test_an_unmarked_essay_means_no_pass_or_fail_yet() -> None:
    """Guessing a verdict from the marks that exist would tell a learner they failed before the
    teacher has read half the paper."""
    assert at._passed(9.0, 10.0, {"passing_score": 50}, pending=1) is None


def test_a_paper_worth_nothing_cannot_be_passed_or_failed() -> None:
    assert at._passed(0.0, 0.0, {"passing_score": 50}, pending=0) is None


def exam_row(**over) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "status": enums.ExamStatus.ACTIVE,
        "available_to": datetime(2026, 10, 20, tzinfo=UTC),
        "result_visibility": "after_close",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_a_sitting_in_progress_has_no_result_at_all() -> None:
    state, visible = at._result_state(attempt_row(), exam_row(), pending=0)
    assert (state, visible) == ("closed", False)


@pytest.mark.parametrize(
    "status",
    [enums.AttemptStatus.SUBMITTED, enums.AttemptStatus.AUTO_SUBMITTED, enums.AttemptStatus.EXPIRED],
)
def test_the_result_rule_is_the_same_however_the_paper_was_closed(status) -> None:
    state, visible = at._result_state(
        attempt_row(status=status, blueprint=blueprint(rules={"result_visibility": "immediate"})),
        exam_row(),
        pending=0,
    )
    assert (state, visible) == ("shown", True)


def test_a_result_the_teacher_chose_to_keep_back_is_never_shown() -> None:
    """`hidden` is not "not yet" - it is "not at all", so the learner's screen needs its own word
    rather than a countdown that will never finish."""
    row = sitting(result_visibility="hidden")
    row.status = enums.AttemptStatus.SUBMITTED
    assert at._result_state(row, exam_row(), pending=0) == ("hidden", False)


def test_a_result_awaiting_a_teacher_is_announced_as_that() -> None:
    """Waiting for marks and waiting for the paper to close are different news, and a learner
    who is told the wrong one stops asking the right person."""
    row = sitting(result_visibility="immediate")
    row.status = enums.AttemptStatus.SUBMITTED
    assert at._result_state(row, exam_row(), pending=2) == ("awaiting_teacher", False)


def test_after_close_holds_the_result_while_the_paper_is_still_open() -> None:
    """The default. A learner who finishes on Monday is not handed the marks while the rest of
    the class still has the paper."""
    row = attempt_row(
        status=enums.AttemptStatus.SUBMITTED, blueprint=blueprint(rules={"result_visibility": "after_close"})
    )
    assert at._result_state(row, exam_row(), pending=0) == ("closed", False)


def test_after_close_shows_the_result_once_the_window_has_run_out() -> None:
    row = attempt_row(
        status=enums.AttemptStatus.SUBMITTED, blueprint=blueprint(rules={"result_visibility": "after_close"})
    )
    past = exam_row(available_to=datetime(2026, 10, 1, tzinfo=UTC))
    assert at._result_state(row, past, pending=0) == ("shown", True)


def test_a_paper_the_teacher_finished_early_shows_its_results() -> None:
    """`after_close` means the window is over, and closing the paper by hand is the teacher
    saying it is over. Waiting for a calendar date they have cancelled would be a bug."""
    row = attempt_row(
        status=enums.AttemptStatus.SUBMITTED, blueprint=blueprint(rules={"result_visibility": "after_close"})
    )
    finished = exam_row(
        status=enums.ExamStatus.FINISHED, available_to=datetime(2026, 10, 20, tzinfo=UTC)
    )
    assert at._result_state(row, finished, pending=0) == ("shown", True)


def test_a_paper_with_no_closing_time_shows_results_once_it_is_handed_in() -> None:
    """With no window there is nothing to wait for, so `after_close` cannot mean "never"."""
    row = attempt_row(
        status=enums.AttemptStatus.SUBMITTED, blueprint=blueprint(rules={"result_visibility": "after_close"})
    )
    assert at._result_state(row, exam_row(available_to=None), pending=0) == ("shown", True)


def test_the_sitting_s_own_visibility_rule_beats_the_exam_s_current_one() -> None:
    """A teacher who changes the rule on Tuesday reaches the learners who start Wednesday, not
    the paper that was handed in on Monday."""
    row = attempt_row(
        status=enums.AttemptStatus.SUBMITTED, blueprint=blueprint(rules={"result_visibility": "hidden"})
    )
    assert at._result_state(row, exam_row(result_visibility="immediate"), pending=0) == (
        "hidden",
        False,
    )


# --------------------------------------------------------------------------- #
# Opening a sitting
# --------------------------------------------------------------------------- #


def test_every_reason_a_paper_can_be_refused_has_a_sentence_for_the_learner() -> None:
    """`availability` returns a code; the start refusal is what the learner reads. A reason with
    no sentence would say "this exam cannot be opened right now" to a paper that simply has not
    opened yet, which is the one case worth waiting for."""
    for reason in at._AVAILABILITY_REASONS:
        if reason == "open":
            continue
        refusal = at._start_refusal(reason)
        assert refusal != at._start_refusal("anything-else")
        assert all(refusal)


def test_one_attempt_left_and_no_attempt_left_are_different_news() -> None:
    assert at._start_refusal("already_submitted") == (
        "exam_one_sitting_only",
        "you have used the one sitting this exam allows",
    )
    assert at._start_refusal("no_attempts_left") == (
        "exam_no_attempts_left",
        "you have used every attempt at this exam",
    )


def test_a_refusal_the_engine_does_not_know_is_still_a_refusal() -> None:
    assert at._start_refusal("invented") == (
        "exam_not_startable",
        "this exam cannot be opened right now",
    )


def test_a_token_is_the_thirty_two_hex_characters_this_backend_issues() -> None:
    token = at._new_token()
    assert a.ATTEMPT_TOKEN_PATTERN.match(token)
    assert len(token) == 32
    assert token != at._new_token()


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a" * 31,
        "a" * 33,
        "A" * 32,
        "-" * 32,
        "not a token",
        "https://example.com/" + "a" * 20,
    ],
)
def test_a_token_of_somebody_else_s_shape_never_reaches_a_query(token: str) -> None:
    """Checked before the database is touched, so a probe cannot be told apart from a mistake by
    how long it takes to fail."""
    with pytest.raises(ValidationError, match="not an exam attempt"):
        a.AttemptSubmit(token=token)


def test_the_learner_names_only_the_token_they_were_given() -> None:
    """The body that opens a sitting names the paper; everything after it names the sitting's
    token. A submit that could also name an attempt id could report work into somebody else's
    paper."""
    assert set(a.AttemptSubmit.model_fields) == {"token", "answers"}
    assert set(a.TabSwitchReport.model_fields) == {"token"}
    assert "attempt_id" not in a.AnswerSave.model_fields
    assert "exam_id" not in a.AnswerSave.model_fields


def test_an_unlimited_looking_attempt_number_is_one_sitting_not_a_thousand() -> None:
    """`max_attempts` unset means the paper is handed in once. A learner who set no number should
    not be able to run a bank until a result pleased them."""
    assert at._attempt_limit(SimpleNamespace(max_attempts=None)) == 1
    assert at._attempt_limit(SimpleNamespace(max_attempts=3)) == 3


# --------------------------------------------------------------------------- #
# The bodies a learner or a teacher may send
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seconds", [0, 60, a.MAX_REPORTED_SECONDS])
def test_a_reported_duration_within_the_bound_is_kept(seconds: int) -> None:
    body = a.AnswerSave(exam_item_id=uuid.uuid4(), response={"option_index": 0},
                        time_spent_seconds=seconds)
    assert body.time_spent_seconds == seconds


@pytest.mark.parametrize("seconds", [-1, a.MAX_REPORTED_SECONDS + 1])
def test_a_reported_duration_outside_the_bound_is_refused(seconds: int) -> None:
    """A duration is a fact about a tab, never a measurement. A number past a day is a broken
    clock, and a negative one is not a duration at all."""
    with pytest.raises(ValidationError):
        a.AnswerSave(exam_item_id=uuid.uuid4(), response={}, time_spent_seconds=seconds)


def test_a_batch_of_answers_is_optional_and_an_empty_one_is_a_plain_submit() -> None:
    """A tab with nothing buffered hands in what the server already holds; a tab that names an
    empty batch says the same thing. Neither is an error."""
    assert a.AttemptSubmit(token="ab" * 16).answers is None
    assert a.AttemptSubmit(token="ab" * 16, answers=[]).answers == []


def test_a_batch_is_one_sitting_s_worth_of_answers() -> None:
    one = a.AnswerSave(exam_item_id=uuid.uuid4(), response={"option_index": 0})
    with pytest.raises(ValidationError):
        a.AttemptSubmit(token="ab" * 16, answers=[one] * (a.MAX_BATCH_ANSWERS + 1))


@pytest.mark.parametrize(
    "model",
    [
        a.AttemptStart,
        a.AnswerSave,
        a.AttemptAnswerRequest,
        a.AttemptSubmit,
        a.TabSwitchReport,
        a.GradeAnswerRequest,
        a.FeedbackRequest,
    ],
)
def test_no_request_body_can_carry_a_field_the_backend_did_not_ask_for(model) -> None:
    """`extra=forbid` is what turns a client and server that have drifted apart into a refusal
    rather than a silently ignored field - and an ignored `score` on a mark is a wrong result."""
    assert model.model_config.get("extra") == "forbid"


def test_a_teacher_s_mark_is_given_in_the_scale_the_question_was_written_in() -> None:
    """`score` is against the answer's own maximum, which the queue shows them, and negative
    marks are not a thing this platform awards."""
    with pytest.raises(ValidationError):
        a.GradeAnswerRequest(score=-0.5)
    assert a.GradeAnswerRequest(score=3.5).score == 3.5


def test_a_teacher_can_record_a_verdict_without_a_number() -> None:
    """An answer can be marked right or wrong with no score, and a note alone can be filed for
    the learner to read."""
    body = a.GradeAnswerRequest(correct=True)
    assert (body.score, body.correct) == (None, True)


def test_a_teacher_s_note_is_trimmed_and_bounded() -> None:
    assert a.GradeAnswerRequest(note="  well argued  ").note == "well argued"
    with pytest.raises(ValidationError):
        a.GradeAnswerRequest(note="x" * 4_001)


def test_feedback_needs_some_words() -> None:
    assert a.FeedbackRequest(body="  Read the second paragraph again.  ").body == (
        "Read the second paragraph again."
    )
    with pytest.raises(ValidationError):
        a.FeedbackRequest(body="")
    for blank in ["   ", "\n\t"]:
        with pytest.raises(ValidationError, match="needs some words"):
            a.FeedbackRequest(body=blank)


def test_an_answer_has_to_belong_to_the_line_being_saved() -> None:
    with pytest.raises(ValidationError):
        a.AnswerSave(exam_item_id="not-a-uuid", response={"option_index": 0})


def test_a_blank_answer_is_carried_in_as_a_blank_for_the_service_to_refuse() -> None:
    """The body allows `response: null` because an autosave that has not been typed into yet is a
    thing a runner does; it is `save_answer` that refuses to file it, so the rule is one place."""
    assert a.AnswerSave(exam_item_id=uuid.uuid4()).response is None


# --------------------------------------------------------------------------- #
# The words a screen is allowed to branch on
# --------------------------------------------------------------------------- #

_NOTICE_RE = re.compile(r"notice=([\"'])([a-z_]+)\1")
_AVAILABILITY_RE = re.compile(r"return (?:True|False), ([\"'])([a-z_]+)\1")
_STATE_RE = re.compile(r"return ([\"'])([a-z_]+)\1, (?:True|False)")
_REASON_RE = re.compile(r"reason=([\"'])([a-z_]+)\1")


def test_every_notice_the_engine_returns_is_one_the_screens_were_told_about() -> None:
    """A frontend translates a code. A notice the module emits but `meta()` never advertises is
    a word no locale has, so the learner sees an English fallback or nothing at all."""
    source = inspect.getsource(at)
    emitted = set(_NOTICE_RE.findall(source))
    assert emitted, "the notices stopped being literals - check this test before the screens"
    unadvertised = {word for _, word in emitted} - set(at.NOTICES)
    assert not unadvertised, f"{unadvertised} reach a page without being in meta()"


def test_every_availability_reason_the_engine_returns_is_advertised() -> None:
    found = {word for _, word in _AVAILABILITY_RE.findall(inspect.getsource(at.availability))}
    assert found, "the availability ladder no longer returns its reasons as words"
    assert found <= set(at._AVAILABILITY_REASONS), found - set(at._AVAILABILITY_REASONS)


def test_every_result_state_the_ladder_returns_is_advertised() -> None:
    found = {word for _, word in _STATE_RE.findall(inspect.getsource(at._result_state))}
    assert found, "the visibility ladder no longer returns its states as words"
    assert found <= set(at._RESULT_STATES), found - set(at._RESULT_STATES)


def test_only_the_recorded_close_reasons_can_end_a_sitting() -> None:
    """Every route that closes a paper names one of these, and `finalise` refuses any other - so
    a status a learner's history shows is always a thing that has a rule behind it."""
    assert set(a.CLOSE_REASONS) == {"submitted", "expired", "tab_limit"}
    found = {word for _, word in _REASON_RE.findall(inspect.getsource(at))}
    assert found <= set(a.CLOSE_REASONS), found - set(a.CLOSE_REASONS)


def test_the_meta_words_are_the_ones_the_code_produces() -> None:
    """`meta()` is what the four locales translate, so it is built from the enums and the tuples
    rather than a list someone maintains beside them."""
    words = at.meta()
    assert words["statuses"] == [status.value for status in enums.AttemptStatus]
    assert words["exam_statuses"] == list(exam_service.SETTABLE_STATUSES)
    assert words["availability_reasons"] == list(at._AVAILABILITY_REASONS)
    assert words["result_states"] == list(at._RESULT_STATES)
    assert words["close_reasons"] == list(a.CLOSE_REASONS)
    assert words["notices"] == list(at.NOTICES)


def test_only_a_issued_or_graded_paper_can_appear_on_a_learner_s_list() -> None:
    """The learner's list is the screen a class looks at, so which lifecycle words may reach it is
    a rule, not a detail: `active` and `finished` are, and the other three are not."""
    assert set(exam_service.SETTABLE_STATUSES) - set(at._HIDDEN_STATUSES) == {"active", "finished"}
    assert set(at._HIDDEN_STATUSES) <= set(exam_service.SETTABLE_STATUSES), (
        "the list hides a state the editor cannot even set"
    )


def test_the_best_sitting_of_a_paper_is_a_number_and_not_a_pending_call() -> None:
    """`_best_of` awaits nothing, and the learner's list unpacks its result in place. A coroutine
    reaching that unpack is a 500 on their exam list, so the shape is checked here as well as the
    arithmetic."""
    assert not inspect.iscoroutinefunction(at._best_of)
    rows = [
        attempt_row(score=6, max_score=10),
        attempt_row(score=9, max_score=10),
        attempt_row(score=None, max_score=None),
    ]
    assert at._best_of(rows) == (9.0, 10.0)
    assert at._best_of([attempt_row(score=0, max_score=10)]) == (0.0, 10.0)
    assert at._best_of([attempt_row()]) == (None, None), "an unmarked sitting is not a score of zero"


def test_a_sitting_can_only_be_told_it_is_closed_in_one_of_the_advertised_ways() -> None:
    """`is_closed` is what stops an answer reaching a graded paper. A status outside it is an
    open sitting, so the list has to agree with the word list the frontend was given."""
    closed = {value.value for value in enums.AttemptStatus if at.is_closed(
        attempt_row(status=value)
    )}
    assert closed == {"submitted", "auto_submitted", "expired"}
    assert "in_progress" not in closed


# --------------------------------------------------------------------------- #
# Timestamps that come out of a stored rule or a browser
# --------------------------------------------------------------------------- #


def test_a_stored_instant_is_read_back_whether_it_is_text_or_a_datetime() -> None:
    moment = datetime(2026, 10, 20, 9, tzinfo=UTC)
    assert at._from_iso(moment.isoformat()) == moment
    assert at._from_iso(moment) == moment
    assert at._from_iso("2026-10-20T09:00:00Z") == moment


def test_an_instant_that_is_not_one_is_treated_as_no_deadline() -> None:
    """A rule written by hand can carry text that is not an instant. The alternative is a
    500 on every read of the sitting, and the paper has no deadline rather than a fake one."""
    assert at._from_iso("tomorrow morning") is None
    assert at._from_iso(None) is None
    assert at._from_iso("") is None


def test_a_browser_s_timestamp_is_kept_only_when_it_is_a_timestamp() -> None:
    moment = datetime(2026, 10, 20, 9, tzinfo=UTC)
    assert at._parse_client_stamp(moment.isoformat()) == moment
    assert at._parse_client_stamp("  ") is None
    assert at._parse_client_stamp(None) is None
    assert at._parse_client_stamp("the cat sat down") is None


def test_a_browser_s_clock_is_never_made_timezone_from_the_server_s_hour() -> None:
    """A naive stamp is a local clock; assuming UTC would quietly shift the timeline by hours in
    either direction, which is exactly the record Phase 9 is asked to show."""
    naive = datetime(2026, 10, 20, 9)
    assert at._parse_client_stamp(naive) == naive.replace(tzinfo=UTC)


def test_the_small_translators_do_what_the_reads_depend_on() -> None:
    assert at._label_of(enums.AttemptStatus.SUBMITTED) == "submitted"
    assert at._label_of("submitted") == "submitted"
    assert at._iso(None) is None
    assert at._iso(datetime(2026, 10, 20, 9, tzinfo=UTC)) == "2026-10-20T09:00:00+00:00"
    assert at._aware(datetime(2026, 10, 20, 9)).tzinfo == UTC
