"""Offline tests for the exam authoring rules (Phase 7).

An exam is the part of this platform where a wrong number reaches a real learner's result, so
the rules worth pinning without a database are the ones a teacher meets before any row exists:
which lifecycle words there are and which moves they permit, what may be added to a paper and
what may not, what a mark is made of when the bank has moved on, and how a sitting's own copy
of the paper is dealt.

The half that needs rows - pinning a real version, the composition lock, assignment races,
publish refusals - is in `tests/integration/test_exam_db.py`, and the sitting itself is in
`test_attempt_db.py`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import deps
from app.api.v1.endpoints import exams, student_exams
from app.core import enums
from app.main import create_app
from app.schemas import exam as e
from app.services import exam_service as es

# --------------------------------------------------------------------------- #
# Fakes for the pure functions
# --------------------------------------------------------------------------- #


def exam_row(**over) -> SimpleNamespace:
    """An `Exam` as the pure rule functions see it: fields only, no session involved."""
    base = {
        "id": uuid.uuid4(),
        "title": "October test",
        "description": None,
        "status": enums.ExamStatus.DRAFT,
        "learning_language": "en",
        "level": "B1",
        "available_from": None,
        "available_to": None,
        "duration_minutes": 30,
        "must_finish_before_close": False,
        "max_attempts": None,
        "passing_score": None,
        "shuffle_questions": False,
        "shuffle_options": False,
        "resume_after_disconnect": True,
        "restrict_copy_paste": False,
        "monitor_tab_switch": False,
        "tab_switch_limit": None,
        "tab_switch_action": None,
        "allow_previous": True,
        "feedback_timing": enums.FeedbackTiming.AFTER_SESSION,
        "show_correct_answers": False,
        "show_explanations": False,
        "result_visibility": "after_close",
        "partial_scoring_enabled": True,
        "negative_marking_enabled": False,
        "grading_mode": enums.GradingMode.AUTOMATIC,
        "auto_submit_on_expiry": True,
        "deleted_at": None,
        "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }
    base.update(over)
    return SimpleNamespace(**base)


def item_row(**over) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "exam_id": uuid.uuid4(),
        "section_id": None,
        "kind": enums.ContentKind.QUESTION,
        "ref_id": uuid.uuid4(),
        "question_version_id": uuid.uuid4(),
        "position": 0,
        "points": None,
        "config": {},
    }
    base.update(over)
    return SimpleNamespace(**base)


def question_row(**over) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "status": enums.ContentStatus.READY,
        "deleted_at": None,
        "type": "multiple_choice",
        "score": 1.0,
        "reading_id": None,
        "listening_id": None,
        "current_version": 3,
    }
    base.update(over)
    return SimpleNamespace(**base)


def passage_row(**over) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "title": "A text",
        "status": enums.ContentStatus.READY,
        "deleted_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def passages(readings: dict | None = None, listenings: dict | None = None) -> dict:
    return {"reading": readings or {}, "listening": listenings or {}}


def entry(item_id: uuid.UUID, *, position: int, section_id: str | None = None) -> dict:
    return {
        "exam_item_id": str(item_id),
        "authored_position": position,
        "section_id": section_id,
        "points": 1.0,
    }


# --------------------------------------------------------------------------- #
# The lifecycle words, and the moves between them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", list(e.SETTABLE_STATUSES))
def test_the_lifecycle_words_are_the_ones_a_teacher_may_set(raw: str) -> None:
    assert es.status_enum(raw) == enums.ExamStatus(raw)


def test_trash_is_a_deletion_and_not_an_exam_state() -> None:
    """`deleted_at` is the bin. A state called "trash" would be a second copy of the same fact."""
    with pytest.raises(es.ExamError):
        es.status_enum("trash")


def test_an_unknown_state_is_refused_with_the_choices_in_the_sentence() -> None:
    with pytest.raises(es.ExamError) as excinfo:
        es.status_enum("published")
    assert "draft, scheduled, active, finished, archived" in str(excinfo.value)


def test_no_lifecycle_move_goes_to_or_from_the_bin() -> None:
    """The trash is not a state a paper is walked through; it is a timestamp on it."""
    assert "trash" not in es.TRANSITIONS
    for targets in es.TRANSITIONS.values():
        assert "trash" not in targets


def test_a_paper_learners_have_handed_in_cannot_go_back_to_draft() -> None:
    """`finished` is the state a sat paper reaches; returning it to draft would re-open a paper
    whose sittings are already graded against a composition that no longer applies."""
    assert "draft" not in es.TRANSITIONS["finished"]
    assert "draft" not in es.TRANSITIONS["active"]


def test_archived_only_leaves_by_way_of_draft() -> None:
    """Nothing skips from the shelf straight to a learner's screen."""
    assert es.TRANSITIONS["archived"] == {"draft"}


def test_publishing_a_second_time_is_not_a_move_at_all() -> None:
    """`active -> active` is refused by the transition table, which is why `set_status` returns
    the paper unchanged when the teacher asks for the state it already has."""
    assert "active" not in es.TRANSITIONS["active"]


def test_an_exam_can_be_a_vocabulary_reference_and_a_catalog_cannot_be_an_exam_item() -> None:
    """A word card has no answer to grade, so it belongs to practice and never to a paper."""
    assert "vocabulary" not in e.ITEM_KINDS
    assert e.STORED_ITEM_KIND == "question"
    assert es.kind_of("reading") == "reading"
    with pytest.raises(es.ExamError, match="kind must be one of"):
        es.kind_of("vocabulary")


# --------------------------------------------------------------------------- #
# What the API refuses before a row exists
# --------------------------------------------------------------------------- #


def test_a_paper_is_born_a_draft_or_a_plan_and_neither_one_reachable() -> None:
    assert e.ExamCreate(title="T").status == "draft"
    assert e.ExamCreate(title="T", status="scheduled").status == "scheduled"
    with pytest.raises(ValidationError, match="draft or a scheduled"):
        e.ExamCreate(title="T", status="active")


def test_a_paper_needs_a_name_and_a_blank_one_is_not_it() -> None:
    with pytest.raises(ValidationError, match="needs a title"):
        e.ExamCreate(title="   ")
    assert e.ExamCreate(title="  October test  ").title == "October test"


def test_a_rule_body_carries_no_status_because_the_lifecycle_endpoint_is_the_only_door() -> None:
    """`PATCH /exams/{id}` edits rules. If it could also set a status, a paper could be handed
    out by a body that never checked whether it has any questions on it."""
    with pytest.raises(ValidationError):
        e.ExamUpdate(status="active")


def test_the_closing_time_has_to_be_after_the_opening_one() -> None:
    opens = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    assert e.ExamCreate(title="T", available_from=opens, available_to=opens + timedelta(hours=1))
    with pytest.raises(ValidationError, match="after the opening time"):
        e.ExamCreate(title="T", available_from=opens, available_to=opens - timedelta(hours=1))


def test_a_naive_window_is_read_as_the_platform_clock_and_stamped() -> None:
    """The server clock is UTC. A teacher who typed `09:00` means 09:00 on that clock, and a
    naive timestamp left naive would be compared against an aware one and crash the request."""
    body = e.ExamCreate(title="T", available_from=datetime(2026, 10, 10, 9))
    assert body.available_from.tzinfo is timezone.utc
    assert body.available_from.hour == 9


def test_watching_the_tabs_without_deciding_what_it_costs_is_refused() -> None:
    with pytest.raises(ValidationError, match="needs an action"):
        e.ExamCreate(title="T", monitor_tab_switch=True)
    assert e.ExamCreate(title="T", monitor_tab_switch=True, tab_switch_action="warn")


def test_a_switch_limit_says_nothing_while_the_watching_is_off() -> None:
    with pytest.raises(ValidationError, match="while monitoring is on"):
        e.ExamCreate(title="T", tab_switch_limit=3)


def test_the_english_of_a_rule_that_does_not_exist_is_refused_not_interpreted() -> None:
    for over in (
        {"feedback_timing": "always"},
        {"result_visibility": "public"},
        {"tab_switch_action": "block"},
        {"grading_mode": "auto"},
    ):
        with pytest.raises(ValidationError):
            e.ExamCreate(title="T", **over)


def test_ai_assisted_grading_is_not_a_mode_a_paper_can_be_published_in_yet() -> None:
    """The stored enum has the value, and the lifecycle refuses it: an exam in that mode today
    would promise the teacher a queue of suggestions that no configured assistant produces."""
    assert enums.GradingMode.AI_ASSISTED.value == "ai_assisted"
    with pytest.raises(ValidationError, match="automatic, manual"):
        e.ExamCreate(title="T", grading_mode="ai_assisted")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_minutes", 0),
        ("duration_minutes", e.MAX_DURATION_MINUTES + 1),
        ("max_attempts", 0),
        ("max_attempts", e.MAX_ATTEMPTS + 1),
        ("passing_score", -1),
        ("passing_score", 100.5),
        ("tab_switch_limit", 0),
        ("tab_switch_limit", e.MAX_TAB_SWITCH_LIMIT + 1),
    ],
)
def test_a_number_outside_the_screen_bounds_never_reaches_a_paper(field: str, value: int) -> None:
    """`duration_minutes` above the cap is almost always a teacher meaning "no limit", which is
    what leaving it empty does; `passing_score` is a percentage and nothing else."""
    with pytest.raises(ValidationError):
        e.ExamCreate(title="T", **{field: value})


def test_an_empty_string_switch_action_is_the_same_as_not_setting_one() -> None:
    """A form that clears a select sends `""`, and an empty string is not one of the three words."""
    assert e.ExamCreate(title="T", tab_switch_action="").tab_switch_action is None


def test_the_body_of_a_question_reference_is_checked_before_the_database_sees_it() -> None:
    ref = str(uuid.uuid4())
    assert e.ItemAdd(kind="question", ref_id=ref).version is None
    with pytest.raises(ValidationError, match="kind must be one of"):
        e.ItemAdd(kind="vocabulary", ref_id=ref)
    with pytest.raises(ValidationError, match="mark and version belong to each of them"):
        e.ItemAdd(kind="reading", ref_id=ref, points=2)
    with pytest.raises(ValidationError, match="mark and version belong to each of them"):
        e.ItemAdd(kind="listening", ref_id=ref, version=3)


def test_a_passage_may_narrow_to_one_block_but_never_carries_a_mark() -> None:
    block = str(uuid.uuid4())
    body = e.ItemAdd(kind="reading", ref_id=str(uuid.uuid4()), set_id=block)
    assert body.set_id == uuid.UUID(block)
    with pytest.raises(ValidationError):
        e.ItemAdd(kind="question", ref_id=str(uuid.uuid4()), points=0)


def test_one_call_adds_a_selection_and_not_an_import() -> None:
    ref = str(uuid.uuid4())
    with pytest.raises(ValidationError):
        e.ItemsAdd(items=[])
    with pytest.raises(ValidationError):
        e.ItemsAdd(items=[e.ItemAdd(kind="question", ref_id=ref)] * (100 + 1))


def test_an_item_edit_that_changes_nothing_is_refused() -> None:
    with pytest.raises(ValidationError, match="nothing to change"):
        e.ItemUpdate()
    assert e.ItemUpdate(points=2).points == 2


def test_moving_an_item_out_of_its_section_is_naming_the_section_as_null() -> None:
    """A part is a thing a teacher can put a question back out of, so `section_id: null` has to
    reach the service as a change. The test of a change is what the caller named, not what
    value they named - otherwise the one edit that undoes a part is the one refused."""
    body = e.ItemUpdate(section_id=None)
    assert body.model_dump(exclude_unset=True) == {"section_id": None}


def test_taking_a_mark_override_off_an_item_is_naming_the_mark_as_null() -> None:
    """An item with no override is worth whatever the pinned version says, so clearing the
    number is a real edit and not an empty one."""
    body = e.ItemUpdate(points=None)
    assert body.model_dump(exclude_unset=True) == {"points": None}


def test_a_reorder_has_to_name_the_whole_paper() -> None:
    with pytest.raises(ValidationError):
        e.ReorderRequest(item_ids=[])
    with pytest.raises(ValidationError):
        e.SectionReorderRequest(section_ids=[])


async def test_an_exam_cannot_be_assigned_to_nobody() -> None:
    """A paper nobody is told about is a draft with a timer on it, and the assignment endpoint
    is the only place a learner ever comes into the picture.

    The refusal is the service's, not the payload's. A schema complaint on the same body would
    reach the screen as `validation_failed` with an English sentence and no code to look up,
    while `exam_no_audience` is the rule the teacher's own interface already names.
    """
    assert e.AssignmentCreate().student_ids == [] and e.AssignmentCreate().group_ids == []
    with pytest.raises(es.ExamError) as excinfo:
        # Nothing between this call and the refusal touches the database, so the rule can be
        # read on its own, without a server.
        await es.assign(None, exam_row(), e.AssignmentCreate(), admin_id=uuid.uuid4())
    assert excinfo.value.code == "exam_no_audience"
    both = e.AssignmentCreate(student_ids=[str(uuid.uuid4())], group_ids=[str(uuid.uuid4())])
    assert len(both.student_ids) == 1 and len(both.group_ids) == 1


def test_a_bulk_action_names_a_page_of_papers_and_no_more() -> None:
    with pytest.raises(ValidationError):
        e.ExamBulkRequest(exam_ids=[])
    with pytest.raises(ValidationError):
        e.ExamBulkRequest(exam_ids=[str(uuid.uuid4()) for _ in range(501)])


def test_a_section_holds_instructions_and_a_position_that_cannot_be_negative() -> None:
    assert e.SectionCreate(title="  Listening  ", position=0).title == "Listening"
    with pytest.raises(ValidationError):
        e.SectionCreate(title="T", position=-1)
    with pytest.raises(ValidationError):
        e.SectionCreate(title="T", instructions="x" * 2_001)


def test_a_status_body_carries_one_word_and_nothing_else() -> None:
    assert e.StatusUpdate(status="active").status == "active"
    with pytest.raises(ValidationError):
        e.StatusUpdate(status="ready")
    with pytest.raises(ValidationError):
        e.StatusUpdate(status="active", force=True)


def test_an_unknown_section_field_is_refused_rather_than_stored_as_configuration() -> None:
    """`exam_section.config` is a JSON blob, which makes it the natural home for a typo - and
    the reason the schema has to name the one key it accepts."""
    assert e.SECTION_CONFIG_KEYS == ("instructions",)
    with pytest.raises(ValidationError):
        e.SectionCreate(title="T", colour="red")


# --------------------------------------------------------------------------- #
# What a mark is made of
# --------------------------------------------------------------------------- #


def test_a_teachers_own_mark_beats_the_pinned_versions_score() -> None:
    item = item_row(points=3)
    assert es.effective_points(item, {"question": {"score": 1}}) == 3.0


def test_the_default_mark_is_the_version_the_paper_pinned_not_the_live_question() -> None:
    """The whole point of pinning: a paper is worth what its own versions were worth. Reading
    the live row here would move a historical total every time a question was edited."""
    item = item_row(points=None)
    assert es.effective_points(item, {"question": {"score": 2}}) == 2.0


def test_an_item_that_resolves_to_nothing_carries_no_mark_rather_than_a_guessed_one() -> None:
    """No pinned version and no override means there is nothing honest to say, and 0.0 is that
    statement. The publish check is what refuses the paper, not an invented number."""
    assert es.effective_points(item_row(points=None), None) == 0.0


def test_a_snapshot_without_a_question_body_is_the_same_gap() -> None:
    assert es.effective_points(item_row(points=None), {"question": {}}) == 0.0
    assert es.effective_points(item_row(points=None), {}) == 0.0


# --------------------------------------------------------------------------- #
# Whether a reference can actually be served
# --------------------------------------------------------------------------- #


def test_a_question_that_is_not_there_cannot_be_served() -> None:
    assert es._question_state(None, passages()) == "missing"


def test_a_trashed_question_keeps_its_own_word_rather_than_becoming_a_status() -> None:
    row = question_row(deleted_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert es._question_state(row, passages()) == "trashed"


def test_a_ready_question_under_a_draft_text_is_not_ready() -> None:
    """Invariant 5 in force: a bound question answers one block of one passage, so the passage's
    own state travels with it. Otherwise a paper would carry an unpublished text into a learner's
    screen inside its own context."""
    text = passage_row(status=enums.ContentStatus.DRAFT)
    row = question_row(reading_id=text.id)
    assert es._question_state(row, passages(readings={text.id: text})) == "draft"


def test_a_ready_question_whose_recording_was_thrown_away_is_missing() -> None:
    row = question_row(listening_id=uuid.uuid4())
    assert es._question_state(row, passages()) == "missing"


def test_a_ready_question_under_a_ready_text_is_served() -> None:
    text = passage_row()
    row = question_row(listening_id=text.id)
    assert es._question_state(row, passages(listenings={text.id: text})) == "ready"


def test_an_independent_question_needs_no_passage_to_be_served() -> None:
    assert es._question_state(question_row(), passages()) == "ready"


# --------------------------------------------------------------------------- #
# Why a paper could not be handed out
# --------------------------------------------------------------------------- #


def served(title: str = "Q") -> dict:
    return {"serveable": True, "title": title}


def test_the_reasons_are_the_ones_the_publish_check_uses() -> None:
    """The editor names these rules before a teacher presses Publish, as the codes the lifecycle
    check refuses with. A blocker that said one thing and a refusal that said another would make
    the note above the form a promise the endpoint does not keep."""
    with_items = [served()]
    assert es._publish_blockers(exam_row(), [], assignments=2) == [{"code": "exam_has_no_questions"}]
    assert es._publish_blockers(
        exam_row(), [{"serveable": False, "title": "Old"}], assignments=1
    ) == [{"code": "exam_items_not_ready", "params": {"n": 1, "example": "Old"}}]
    assert es._publish_blockers(
        exam_row(status=enums.ExamStatus.SCHEDULED), with_items, assignments=1
    ) == [{"code": "exam_schedule_needs_opening_time"}]
    assert es._publish_blockers(exam_row(status=enums.ExamStatus.ACTIVE), with_items, assignments=0) == [
        {"code": "exam_no_audience"}
    ]
    assert es._publish_blockers(exam_row(status=enums.ExamStatus.DRAFT), with_items, assignments=0) == []


def test_a_draft_needs_nobody_assigned_to_be_editable() -> None:
    """`exam_no_audience` is a refusal about handing a paper out, and a draft is not being handed
    out. Reporting it while the teacher is still writing would be noise."""
    assert es._publish_blockers(exam_row(), [served()], assignments=0) == []


def test_a_trashed_paper_reports_its_own_blockers_without_inventing_a_state() -> None:
    blockers = es._publish_blockers(
        exam_row(deleted_at=datetime(2026, 10, 1, tzinfo=timezone.utc)), [], assignments=0
    )
    assert blockers == [{"code": "exam_has_no_questions"}]


# --------------------------------------------------------------------------- #
# The sitting's own copy of the rules
# --------------------------------------------------------------------------- #


#: The two rules whose effect is spent the moment the paper is dealt: they decide the order the
#: learner meets the questions in and the order of each option list, and both are written into
#: the blueprint itself. A sitting never reads them back, so they are the only rule fields the
#: blueprint carries as a done thing rather than as a word to obey later.
DEAL_TIME_RULES = {"shuffle_questions", "shuffle_options"}


def test_every_field_a_sitting_experiences_is_in_the_blueprint() -> None:
    """A learner mid-paper keeps the paper they were given. Any rule the runner or the result
    screen reads has to be copied, or editing the exam would reach the sitting in progress."""
    rules = es.rules_payload(exam_row())
    missing = set(es.RULE_FIELDS) - set(rules)
    assert missing == DEAL_TIME_RULES, f"{missing} would be read live from the exam"


def test_the_blueprint_carries_no_rule_the_exam_does_not_have() -> None:
    """The other direction: a blueprint field with no rule behind it would be a value the
    teacher can never set and the runner still has to obey."""
    rules = es.rules_payload(exam_row())
    assert set(rules) - DEAL_TIME_RULES == set(es.RULE_FIELDS) - DEAL_TIME_RULES


def test_the_window_in_the_blueprint_is_a_string_a_later_reader_can_parse() -> None:
    opens = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    rules = es.rules_payload(exam_row(available_from=opens, available_to=None))
    assert rules["available_from"] == opens.isoformat()
    assert rules["available_to"] is None


def test_the_enums_in_the_blueprint_are_written_as_their_words() -> None:
    """The blueprint is JSON: a Python enum object would serialise as an unnamed string and the
    runner's comparisons would stop matching."""
    rules = es.rules_payload(exam_row())
    for key in ("feedback_timing", "grading_mode"):
        assert isinstance(rules[key], str)
    assert rules["feedback_timing"] == "after_session"
    assert rules["grading_mode"] == "automatic"


def test_the_lifecycle_check_and_the_editor_agree_about_the_window() -> None:
    """`_check_window` runs on a row whose timestamps were saved as they arrived; the schema
    validator runs on the body. Both refuse the same three mistakes."""
    with pytest.raises(es.ExamError, match="after the opening time"):
        es._check_window(exam_row(available_from=datetime(2026, 10, 2), available_to=datetime(2026, 10, 1)))
    with pytest.raises(es.ExamError, match="needs an action"):
        es._check_window(exam_row(monitor_tab_switch=True, tab_switch_action=None))
    with pytest.raises(es.ExamError, match="while monitoring is on"):
        es._check_window(exam_row(monitor_tab_switch=False, tab_switch_limit=2))
    es._check_window(exam_row(monitor_tab_switch=True, tab_switch_action="flag", tab_switch_limit=2))


def test_the_blueprint_carries_a_schema_stamp_the_reader_can_check() -> None:
    """An old sitting's blueprint is read through the stamp it was written with, so a later
    change to what a blueprint holds cannot be mistaken for the shape that sitting used."""
    assert es.BLUEPRINT_SCHEMA == 1


# --------------------------------------------------------------------------- #
# How one sitting is dealt
# --------------------------------------------------------------------------- #


def test_the_seed_of_a_preview_is_checked_before_it_is_used() -> None:
    good = "ab" * 16
    assert es._seed_from(good) == bytes.fromhex(good)
    assert es._seed_from(None) is None
    assert es._seed_from("") is None
    with pytest.raises(es.ExamError, match="32 hexadecimal"):
        es._seed_from("zz" * 16)
    with pytest.raises(es.ExamError, match="32 hexadecimal"):
        es._seed_from("ab" * 8)


def test_an_options_layout_is_stable_for_one_seed_and_differs_between_items() -> None:
    """A resumed sitting has to show the same page the learner was answering: the order comes
    from the attempt's own seed and the item, not from anything the client sends back.

    The item ids are fixed rather than random because two layouts genuinely can agree in every
    size at once - the sizes read the same first draws - and a test that bets on that not
    happening would fail now and then for a reason that says nothing about the derivation.
    """
    seed = bytes.fromhex("cd" * 16)
    items = [uuid.uuid5(uuid.NAMESPACE_OID, f"item {n}") for n in range(40)]
    assert es._option_order(seed, items[0], 4) == es._option_order(seed, items[0], 4)
    assert sorted(es._option_order(seed, items[0], 4)) == [0, 1, 2, 3]
    for count in (2, 3, 4, 5):
        layouts = {tuple(es._option_order(seed, item, count)) for item in items}
        assert len(layouts) > 1, f"every item shows the same layout for {count} options"
    # A different attempt seed deals the same item differently, which is what stops a learner
    # reading one paper from predicting the layout of the next one.
    assert es._option_order(seed, items[0], 4) != es._option_order(
        bytes.fromhex("ee" * 16), items[0], 4
    )


def sections(*specs: tuple[str, bool]) -> dict[uuid.UUID, SimpleNamespace]:
    """Parts in the order a teacher wrote them, keyed by their own ids.

    The ids are stable rather than random because `_deal` shuffles a part with
    `seed + str(section.id)`: a test that asks whether a shuffling part was ever dealt in a
    different order would answer that question afresh on every run if the ids moved, and an
    assertion that holds only most days is not a guard.
    """
    out: dict[uuid.UUID, SimpleNamespace] = {}
    for position, (title, shuffles) in enumerate(specs):
        key = uuid.UUID(int=position + 1)
        out[key] = SimpleNamespace(id=key, title=title, position=position, shuffle_items=shuffles)
    return out


def parts(dealt: list[dict]) -> list[list[dict]]:
    """The dealt paper split into its blocks of consecutive same-part questions."""
    out: list[list[dict]] = []
    for row in dealt:
        if out and out[-1][0]["section_id"] == row["section_id"]:
            out[-1].append(row)
        else:
            out.append([row])
    return out


def test_a_part_of_a_paper_is_never_interleaved_with_another() -> None:
    """`shuffle_questions` reorders inside each part. A learner who is told "Part 1: reading,
    Part 2: grammar" is answering a paper in that order, and no shuffle may break it."""
    one = sections(("Reading", True), ("Grammar", True))
    ids = list(one)
    entries = [
        entry(item_row(section_id=ids[0]).id, position=0, section_id=str(ids[0])),
        entry(item_row(section_id=ids[1]).id, position=1, section_id=str(ids[1])),
        entry(item_row(section_id=ids[0]).id, position=2, section_id=str(ids[0])),
        entry(item_row(section_id=ids[1]).id, position=3, section_id=str(ids[1])),
    ]
    for seed_hex in ["11" * 16, "22" * 16, "33" * 16, "44" * 16]:
        dealt = es._deal(entries, one, shuffle_all=True, seed=bytes.fromhex(seed_hex))
        blocks = parts(dealt)
        assert [block[0]["section_id"] for block in blocks] == [str(ids[0]), str(ids[1])], (
            f"the parts were not dealt in part order: {[r['section_id'] for r in dealt]}"
        )
        assert [len(row) for row in blocks] == [2, 2], "a part lost or borrowed a question"
        assert sorted(row["authored_position"] for row in dealt) == [0, 1, 2, 3]


def test_questions_outside_every_part_are_dealt_as_one_block_at_the_top() -> None:
    """A paper with parts and loose questions is not a coin toss: the unnamed questions are one
    block, ahead of the named ones, each block in the order the teacher wrote."""
    one = sections(("Reading", False))
    only = list(one)[0]
    entries = [
        entry(item_row().id, position=0, section_id=None),
        entry(item_row(section_id=only).id, position=1, section_id=str(only)),
        entry(item_row().id, position=2, section_id=None),
    ]
    dealt = es._deal(entries, one, shuffle_all=False, seed=bytes.fromhex("11" * 16))
    assert [row["authored_position"] for row in dealt] == [0, 2, 1]
    assert [len(block) for block in parts(dealt)] == [2, 1]


def test_one_part_may_shuffle_on_its_own_while_the_rest_keeps_the_authored_order() -> None:
    """`shuffle_questions` and a section's `shuffle_items` answer different questions a teacher
    can ask, so they are two switches and not one."""
    one = sections(("Reading", True), ("Grammar", False))
    reading, grammar = list(one)
    entries = [
        entry(item_row().id, position=0, section_id=None),
        entry(item_row(section_id=reading).id, position=1, section_id=str(reading)),
        entry(item_row(section_id=reading).id, position=2, section_id=str(reading)),
        entry(item_row(section_id=reading).id, position=3, section_id=str(reading)),
        entry(item_row(section_id=grammar).id, position=4, section_id=str(grammar)),
        entry(item_row(section_id=grammar).id, position=5, section_id=str(grammar)),
    ]
    moved = 0
    for seed_hex in ["aa" * 16, "bb" * 16, "cc" * 16, "dd" * 16]:
        dealt = es._deal(entries, one, shuffle_all=False, seed=bytes.fromhex(seed_hex))
        blocks = parts(dealt)
        assert [block[0]["section_id"] for block in blocks] == [None, str(reading), str(grammar)]
        assert blocks[0][0]["authored_position"] == 0, "the loose question moved with no shuffle on"
        inside = [row["authored_position"] for row in blocks[1]]
        assert sorted(inside) == [1, 2, 3], "the shuffling part lost or invented a question"
        assert [row["authored_position"] for row in blocks[2]] == [4, 5], (
            "a part that did not ask to shuffle was reordered anyway"
        )
        moved += inside != [1, 2, 3]
    assert moved, "a part that asked to shuffle was never dealt in a different order"


def test_the_same_seed_always_deals_the_same_paper() -> None:
    one = sections(("Reading", True))
    only = list(one)[0]
    entries = [entry(item_row(section_id=only).id, position=n, section_id=str(only)) for n in range(6)]
    seed = bytes.fromhex("99" * 16)
    first = es._deal(entries, one, shuffle_all=True, seed=seed)
    second = es._deal(entries, one, shuffle_all=True, seed=seed)
    assert [row["exam_item_id"] for row in first] == [row["exam_item_id"] for row in second]


def test_a_deal_without_sections_is_one_part_and_shuffles_the_whole_paper() -> None:
    entries = [entry(item_row().id, position=n) for n in range(8)]
    dealt = es._deal(entries, {}, shuffle_all=True, seed=bytes.fromhex("55" * 16))
    assert sorted(row["authored_position"] for row in dealt) == list(range(8))
    assert [row["authored_position"] for row in dealt] != list(range(8))


def test_a_single_question_part_has_nothing_to_shuffle() -> None:
    one = sections(("Only", True))
    only = list(one)[0]
    entries = [entry(item_row(section_id=only).id, position=0, section_id=str(only))]
    assert es._deal(entries, one, shuffle_all=True, seed=bytes.fromhex("55" * 16)) == entries


# --------------------------------------------------------------------------- #
# The context that travels with a question
# --------------------------------------------------------------------------- #


def test_a_reading_is_copied_as_the_learner_will_read_it() -> None:
    text = SimpleNamespace(
        id=uuid.uuid4(),
        title="Kite weather",
        body="A kite needs wind, not luck.",
        layout="two_columns",
        language="en",
        level="A2",
    )
    frozen = es._frozen_reading(text)
    assert frozen["kind"] == "reading"
    assert frozen["body"] == text.body
    assert frozen["layout"] == "two_columns"


def test_a_recordings_transcript_is_only_copied_when_the_teacher_opened_it() -> None:
    """A transcript is the answer key of a listening in all but name. Freezing it into the
    blueprint while `show_transcript` is off would put it on the learner's paper for the whole
    rest of that sitting, whatever the player does."""
    recording = SimpleNamespace(
        id=uuid.uuid4(),
        title="At the shop",
        media_asset_id=uuid.uuid4(),
        replay_limit=1,
        allow_pause=False,
        allow_seek=False,
        show_transcript=False,
        transcript="W: How much are the apples? S: Two fifty.",
        language="en",
        level="A1",
    )
    frozen = es._frozen_listening(recording, None)
    assert frozen["transcript"] is None
    assert frozen["show_transcript"] is False
    recording.show_transcript = True
    assert es._frozen_listening(recording, None)["transcript"] == recording.transcript


def test_a_frozen_recording_names_the_file_and_never_a_link_to_it() -> None:
    """A stored URL would outlive its signature; the asset id is re-resolved per request through
    the learner's own media path."""
    recording = SimpleNamespace(
        id=uuid.uuid4(),
        title="At the shop",
        media_asset_id=uuid.uuid4(),
        replay_limit=2,
        allow_pause=True,
        allow_seek=True,
        show_transcript=False,
        transcript=None,
        language="en",
        level="A1",
    )
    frozen = es._frozen_listening(recording, None)
    assert frozen["media_asset_id"] == str(recording.media_asset_id)
    assert not any("http" in str(value) for value in frozen.values())


def test_a_block_of_a_recording_travels_with_its_slice() -> None:
    recording = SimpleNamespace(
        id=uuid.uuid4(),
        title="At the shop",
        media_asset_id=None,
        replay_limit=0,
        allow_pause=True,
        allow_seek=False,
        show_transcript=False,
        transcript=None,
        language="en",
        level="A1",
    )
    block = SimpleNamespace(title="Second listen", instructions="Answer 3 to 5.", start_seconds=30, end_seconds=75)
    frozen = es._frozen_listening(recording, block)
    assert (frozen["start_seconds"], frozen["end_seconds"]) == (30, 75)
    assert frozen["block_title"] == "Second listen"
    whole = es._frozen_listening(recording, None)
    assert whole["start_seconds"] is None and whole["block_title"] is None


# --------------------------------------------------------------------------- #
# Names, labels and the small readers
# --------------------------------------------------------------------------- #


def test_a_learner_is_named_from_their_own_row_and_never_from_a_stored_string() -> None:
    assert es.student_name(SimpleNamespace(name="Aysel", surname="Mammadova")) == "Aysel Mammadova"
    assert es.student_name(SimpleNamespace(name="Amid", surname="")) == "Amid"


def test_enums_are_read_as_their_words_and_naive_stamps_as_the_platform_clock() -> None:
    assert es._label_of(enums.ExamStatus.ACTIVE) == "active"
    assert es._label_of("active") == "active"
    assert es._aware(datetime(2026, 10, 1)).tzinfo is timezone.utc
    assert es._aware(datetime(2026, 10, 1, tzinfo=timezone.utc)).tzinfo is timezone.utc
    assert es._iso(None) is None
    assert es._iso("already text") == "already text"


def test_an_unknown_sort_or_view_is_refused_before_it_can_show_the_trash() -> None:
    """A sort name is looked up in a dict rather than handed to SQL, and an unknown `view` would
    otherwise read as "everything, including the bin"."""
    for over in ({"view": "allies"}, {"sort": "score"}, {"order": "up"}):
        with pytest.raises(es.ExamError):
            es._check_list_args(**{"view": "bank", "sort": "title", "order": "asc", **over})
    es._check_list_args(view="trash", sort="available_from", order="desc")


# --------------------------------------------------------------------------- #
# The route shape
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def paths() -> dict:
    return TestClient(create_app()).get("/openapi.json").json()["paths"]


def test_the_teacher_exam_surface_is_the_one_the_editor_needs(paths: dict) -> None:
    assert {path for path in paths if path.startswith("/api/v1/exams")} == {
        "/api/v1/exams",
        "/api/v1/exams/meta",
        "/api/v1/exams/bulk",
        "/api/v1/exams/sections/{section_id}",
        "/api/v1/exams/items/{item_id}",
        "/api/v1/exams/attempts/{attempt_id}",
        "/api/v1/exams/assignments/{assignment_id}",
        "/api/v1/exams/{exam_id}",
        "/api/v1/exams/{exam_id}/status",
        "/api/v1/exams/{exam_id}/restore",
        "/api/v1/exams/{exam_id}/clone",
        "/api/v1/exams/{exam_id}/preview",
        "/api/v1/exams/{exam_id}/sections",
        "/api/v1/exams/{exam_id}/sections/reorder",
        "/api/v1/exams/{exam_id}/items",
        "/api/v1/exams/{exam_id}/items/reorder",
        "/api/v1/exams/{exam_id}/assignments",
        "/api/v1/exams/{exam_id}/attempts",
    }


def test_the_grading_surface_is_separate_from_the_paper_it_marks(paths: dict) -> None:
    """The queue is the teacher's worklist across every paper, not a sub-page of one exam: an
    answer is marked for the sitting it belongs to, and naming the exam again would let one
    mark be filed against a different paper than the one that produced it."""
    assert {path for path in paths if path.startswith("/api/v1/grading")} == {
        "/api/v1/grading/meta",
        "/api/v1/grading/summary",
        "/api/v1/grading/queue",
        "/api/v1/grading/answers/{review_id}",
        "/api/v1/grading/answers/{review_id}/grade",
        "/api/v1/grading/attempts/{attempt_id}/feedback",
        "/api/v1/grading/feedback/{feedback_id}",
        "/api/v1/grading/students/{student_id}/feedback",
    }


def test_a_preview_is_a_read_and_starts_no_sitting(paths: dict) -> None:
    """A preview is the teacher deciding what the paper deals. Recording one as an attempt would
    put a sitting in a learner's history that nobody sat, and lock a composition nobody began."""
    assert set(paths["/api/v1/exams/{exam_id}/preview"]) == {"get"}


def test_a_bin_is_a_post_and_a_restore_its_own_move(paths: dict) -> None:
    assert set(paths["/api/v1/exams/{exam_id}"]) == {"get", "patch", "delete"}
    assert set(paths["/api/v1/exams/{exam_id}/restore"]) == {"post"}


def _guards(route) -> set:
    """Every dependency a route pulls in, at any depth."""
    found: set = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            found.add(dependant.call)
        stack.extend(dependant.dependencies)
    return found


def test_every_exam_and_grading_route_needs_a_teacher_session() -> None:
    """Checked from the routes themselves, because a missing dependency is a silent 200."""
    for route in list(exams.router.routes) + list(exams.grading_router.routes):
        guards = _guards(route)
        assert deps.get_current_admin in guards, f"{route.path} is reachable without a teacher"
        assert deps.get_current_student not in guards, f"{route.path} accepts a learner key"


def test_no_learner_exam_route_can_be_reached_by_a_teacher_key() -> None:
    """A teacher's session is not a learner's sitting. Every student surface is scoped by the
    session's own student id, and a route a teacher could open on a learner's token would be a
    paper answered from two identities at once."""
    for route in list(student_exams.router.routes) + list(student_exams.attempts_router.routes):
        guards = _guards(route)
        assert deps.get_current_student in guards, f"{route.path} is reachable without a learner"
        assert deps.get_current_admin not in guards, f"{route.path} accepts a teacher key"


def test_a_learner_never_names_an_exam_or_an_attempt_they_are_answering(paths: dict) -> None:
    """The body that opens a sitting names the paper; everything after it names the token the
    backend issued. A body that could name both would let one tab report an answer into a
    different paper than the one it was served."""
    submit = paths["/api/v1/student/attempts/submit"]["post"]
    ref = submit["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert "Attempt" in ref
    for path in ("/api/v1/student/attempts/{token}", "/api/v1/student/attempts/{token}/result"):
        assert set(paths[path]) == {"get"}


def test_an_assignment_is_removed_by_its_own_id_and_not_by_editing_the_paper(paths: dict) -> None:
    assert set(paths["/api/v1/exams/assignments/{assignment_id}"]) == {"delete"}
    assert set(paths["/api/v1/exams/{exam_id}/assignments"]) == {"get", "post"}
