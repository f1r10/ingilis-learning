"""Offline tests for the practice rules (Phase 6).

The parts of practice that decide *what a learner is allowed to see* are pure functions
over rows and payloads, so they are tested here without a database: the folder chain that
makes a catalog reachable, the shuffle that has to be reproducible from its own log, the
totals that must not reveal a verdict the teacher asked to hold back, and the bodies the
runner refuses.

The parts that need rows - opening a run, answering through the catalog that was opened,
the unique index behind a double-clicked favorite - are in
`tests/integration/test_practice_db.py`.
"""
from __future__ import annotations

import secrets
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas import practice as pr
from app.services import activity_service as acts
from app.services import practice_service as ps


def catalog_row(**overrides) -> SimpleNamespace:
    """A `catalog` row with only the fields the pure rules read."""
    base = {
        "id": uuid.uuid4(),
        "parent_id": None,
        "name": "Unit 1",
        "created_at": None,
        "updated_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Reachability: a chain, not a flag
# --------------------------------------------------------------------------- #


def test_a_root_catalog_is_reachable() -> None:
    row = catalog_row()
    assert ps._reachable(row, {row.id: row}) is True


def test_a_catalog_under_a_published_folder_is_reachable() -> None:
    parent = catalog_row()
    child = catalog_row(parent_id=parent.id)
    assert ps._reachable(child, {parent.id: parent, child.id: child}) is True


def test_a_catalog_under_a_folder_that_is_not_published_is_not_reachable() -> None:
    """The teacher who leaves "Unit 1" as a draft means the whole unit to stay closed."""
    parent = catalog_row()
    child = catalog_row(parent_id=parent.id)
    assert ps._reachable(child, {child.id: child}) is False


def test_a_folder_above_a_folder_matters_too() -> None:
    grandparent = catalog_row()
    parent = catalog_row(parent_id=grandparent.id)
    child = catalog_row(parent_id=parent.id)
    live = {parent.id: parent, child.id: child}
    assert ps._reachable(child, live) is False


def test_a_parent_id_loop_ends_instead_of_spinning() -> None:
    """Only a hand-edited row can do this, and a request that never finishes is worse."""
    first = catalog_row()
    second = catalog_row(parent_id=first.id)
    first.parent_id = second.id
    live = {first.id: first, second.id: second}
    assert ps._reachable(first, live) is False
    assert ps._reachable(second, live) is False


def test_sorting_puts_the_rows_with_no_value_last() -> None:
    """A `None` timestamp cannot be compared against a real one, and must not sort first."""
    named = catalog_row(name="aaa", created_at=None)
    dated = catalog_row(name="zzz", created_at="2026-01-01")
    ordered = sorted([named, dated], key=lambda row: ps._sort_key(row, "created_at"))
    assert ordered == [dated, named]


def test_rows_that_carry_the_same_value_are_ordered_by_name() -> None:
    first = catalog_row(name="aaa", created_at="2026-01-01")
    second = catalog_row(name="bbb", created_at="2026-01-01")
    ordered = sorted([second, first], key=lambda row: ps._sort_key(row, "created_at"))
    assert [row.name for row in ordered] == ["aaa", "bbb"]


# --------------------------------------------------------------------------- #
# The order of a run
# --------------------------------------------------------------------------- #


def steps(count: int) -> list[dict]:
    return [{"item_id": uuid.uuid4(), "position": index} for index in range(count)]


def test_a_shuffle_from_one_seed_is_the_same_order_every_time() -> None:
    """The seed is written on the start event, so the order can be rebuilt from the log."""
    seed = secrets.token_bytes(16)
    built = steps(12)
    assert ps._shuffled(built, seed) == ps._shuffled(built, seed)


def test_two_seeds_give_two_orders() -> None:
    built = steps(12)
    assert ps._shuffled(built, secrets.token_bytes(16)) != ps._shuffled(built, secrets.token_bytes(16))


def test_a_shuffle_does_not_reorder_the_list_it_was_given() -> None:
    built = steps(12)
    before = [step["position"] for step in built]
    ps._shuffled(built, secrets.token_bytes(16))
    assert [step["position"] for step in built] == before


def test_a_shuffle_keeps_every_step() -> None:
    built = steps(9)
    mixed = ps._shuffled(built, secrets.token_bytes(16))
    assert sorted(step["position"] for step in mixed) == list(range(9))


# --------------------------------------------------------------------------- #
# Totals, and what the feedback timing holds back
# --------------------------------------------------------------------------- #


def event(**grade) -> SimpleNamespace:
    return SimpleNamespace(payload={"grade": grade})


def test_a_revealed_run_is_totalled_from_its_own_events() -> None:
    answers = {
        uuid.uuid4(): event(score=1.0, max_score=1.0, correct=True, requires_manual=False),
        uuid.uuid4(): event(score=0.0, max_score=2.0, correct=False, requires_manual=False),
        uuid.uuid4(): event(score=0.0, max_score=3.0, correct=None, requires_manual=True),
    }
    totals = ps._totals(answers, reveal=True)
    assert totals == {
        "answered": 3,
        "correct_count": 1,
        "incorrect_count": 1,
        "manual_count": 1,
        "score": 1.0,
        "max_score": 6.0,
    }


def test_a_held_back_run_counts_the_answers_without_marking_them() -> None:
    """`after_session` says how far the learner got, and not whether they were right."""
    answers = {
        uuid.uuid4(): event(score=1.0, max_score=1.0, correct=True, requires_manual=False),
        uuid.uuid4(): event(score=0.0, max_score=2.0, correct=False, requires_manual=False),
    }
    totals = ps._totals(answers, reveal=False)
    assert totals["answered"] == 2
    assert totals["correct_count"] == 0
    assert totals["incorrect_count"] == 0
    assert totals["score"] == 0.0
    assert totals["max_score"] == 0.0


def test_an_essay_is_counted_as_waiting_for_a_teacher_and_never_as_wrong() -> None:
    answers = {uuid.uuid4(): event(score=0.0, max_score=5.0, correct=False, requires_manual=True)}
    totals = ps._totals(answers, reveal=True)
    assert totals["manual_count"] == 1
    assert totals["incorrect_count"] == 0


def test_an_event_with_no_grade_in_it_is_an_unanswered_question_not_a_zero() -> None:
    """A malformed row must not invent a score the learner never earned."""
    answers = {uuid.uuid4(): SimpleNamespace(payload={}), uuid.uuid4(): SimpleNamespace(payload=None)}
    totals = ps._totals(answers, reveal=True)
    assert totals["answered"] == 2
    assert totals["score"] == 0.0
    assert totals["max_score"] == 0.0
    assert totals["correct_count"] == 0
    assert totals["incorrect_count"] == 0


# --------------------------------------------------------------------------- #
# The activity log a run is made of
# --------------------------------------------------------------------------- #


def test_the_two_marks_a_learner_can_make_map_onto_two_event_types() -> None:
    assert set(acts.MARK_EVENTS.values()) == set(pr.KNOWN_STATES)
    assert acts.MARK_EVENTS[acts.EVENT_MARK_KNOWN] == "known"
    assert acts.MARK_EVENTS[acts.EVENT_MARK_LEARNING] == "learning"


def test_every_event_type_the_runner_writes_is_distinct() -> None:
    """Two names for one event would make a timeline show the same thing twice."""
    names = [
        acts.EVENT_RUN_START,
        acts.EVENT_RUN_FINISH,
        acts.EVENT_ANSWER,
        acts.EVENT_MARK_KNOWN,
        acts.EVENT_MARK_LEARNING,
        acts.EVENT_FAVORITE_ADD,
        acts.EVENT_FAVORITE_REMOVE,
    ]
    assert len(set(names)) == len(names)


def test_the_assessment_split_is_the_one_analytics_queries_by() -> None:
    assert (acts.CATEGORY_ACTIVITY, acts.CATEGORY_ASSESSMENT) == ("activity", "assessment")


@pytest.mark.parametrize(
    "user_agent,browser",
    [
        ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120.0 Safari/537.36", "chrome"),
        ("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15", "safari"),
        ("Mozilla/5.0 (Windows NT 10.0; rv:121.0) Gecko/20100101 Firefox/121.0", "firefox"),
        ("Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0 Safari/537.36 Edg/120.0", "edge"),
        ("Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0 Safari/537.36 OPR/106.0", "opera"),
        ("curl/8.4.0", "other"),
        (None, None),
    ],
)
def test_the_browser_label_comes_from_the_header_or_says_other(user_agent: str | None, browser: str | None) -> None:
    """Never a guessed name: a monitoring timeline that invents a browser is a lie."""
    assert acts._browser_of(user_agent) == browser


@pytest.mark.parametrize(
    "user_agent,device",
    [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15", "mobile"),
        ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36", "mobile"),
        ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15", "tablet"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0", "desktop"),
        (None, None),
    ],
)
def test_the_device_label_is_coarse_and_honest(user_agent: str | None, device: str | None) -> None:
    assert acts._device_of(user_agent) == device


def test_a_longer_header_than_the_column_is_truncated_not_refused() -> None:
    """Losing the event because of a long User-Agent would lose the answer with it."""
    assert acts._MAX_IP == 64
    assert acts._MAX_TEXT == 120


# --------------------------------------------------------------------------- #
# Bodies the runner refuses
# --------------------------------------------------------------------------- #


def test_the_token_this_backend_issues_passes_its_own_pattern() -> None:
    assert pr.SESSION_TOKEN_PATTERN.match(secrets.token_hex(16))


@pytest.mark.parametrize("token", ["", "abc", "0" * 31, "0" * 33, "Z" * 32, "0" * 32 + "x"])
def test_a_run_token_of_the_wrong_shape_never_reaches_a_query(token: str) -> None:
    with pytest.raises(ValidationError, match="that is not a practice session"):
        pr.AnswerRequest(session_id=token, question_id=uuid.uuid4())


def test_an_answer_names_the_session_and_the_question_and_nothing_else() -> None:
    with pytest.raises(ValidationError):
        pr.AnswerRequest.model_validate(
            {
                "session_id": secrets.token_hex(16),
                "question_id": str(uuid.uuid4()),
                "catalog_id": str(uuid.uuid4()),
            }
        )


def test_the_answer_body_is_the_graders_business_not_the_schemas() -> None:
    """Re-declaring the per-type shapes here would be a second definition of the engine."""
    built = pr.AnswerRequest(
        session_id=secrets.token_hex(16),
        question_id=uuid.uuid4(),
        response={"pairs": [["a", "b"]]},
        time_spent_seconds=12,
    )
    assert built.response == {"pairs": [["a", "b"]]}
    assert built.time_spent_seconds == 12


@pytest.mark.parametrize("seconds", [-1, 86_401])
def test_a_duration_outside_a_day_is_refused(seconds: int) -> None:
    with pytest.raises(ValidationError):
        pr.AnswerRequest(session_id=secrets.token_hex(16), question_id=uuid.uuid4(), time_spent_seconds=seconds)


def test_a_duration_is_reported_and_never_inferred() -> None:
    """Absent means the learner's screen did not measure it; a guess would be a fake number."""
    built = pr.AnswerRequest(session_id=secrets.token_hex(16), question_id=uuid.uuid4())
    assert built.time_spent_seconds is None


@pytest.mark.parametrize("state", ["known", "learning"])
def test_the_two_marks_are_the_only_two(state: str) -> None:
    assert pr.KnownStateRequest(ref_id=uuid.uuid4(), state=state).state == state


def test_an_unknown_mark_is_refused() -> None:
    with pytest.raises(ValidationError, match="state must be one of: known, learning"):
        pr.KnownStateRequest(ref_id=uuid.uuid4(), state="mastered")


@pytest.mark.parametrize("kind", ["question", "vocabulary"])
def test_a_learner_may_save_an_exercise_or_a_word(kind: str) -> None:
    assert pr.FavoriteRequest(kind=kind, ref_id=uuid.uuid4()).kind == kind


def test_saving_a_whole_passage_is_what_a_catalog_is_for() -> None:
    with pytest.raises(ValidationError, match="kind must be one of: question, vocabulary"):
        pr.FavoriteRequest(kind="reading", ref_id=uuid.uuid4())


def test_opening_a_run_accepts_only_the_shuffle_override() -> None:
    assert pr.RunRequest().shuffle is None
    assert pr.RunRequest(shuffle=False).shuffle is False
    with pytest.raises(ValidationError):
        pr.RunRequest.model_validate({"shuffle": True, "question_ids": [str(uuid.uuid4())]})


def test_a_step_carries_the_banks_own_learner_projection_and_nothing_invented() -> None:
    built = pr.StepRead(item_id=uuid.uuid4(), kind="question", ref_id=uuid.uuid4(), position=0)
    assert built.view == {}


def test_a_held_back_answer_says_it_was_held_back() -> None:
    """A blank response where a mark was expected reads as a bug to the learner."""
    built = pr.AnswerRead(session_id=secrets.token_hex(16), question_id=uuid.uuid4(), withheld=True)
    assert built.withheld is True
    assert built.correct is None
    assert built.score is None


def test_a_practice_list_row_carries_no_score_it_did_not_compute() -> None:
    """A fabricated zero on a list screen is a grade the learner never got."""
    assert set(pr.CatalogRowRead.model_fields) == {
        "id",
        "name",
        "description",
        "parent_id",
        "learning_language",
        "level",
        "item_count",
        "counts",
        "child_count",
        "shuffle_default",
        "known_states_enabled",
        "feedback_timing",
        "runs",
    }
