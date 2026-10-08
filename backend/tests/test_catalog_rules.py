"""Offline tests for the catalog rules (Phase 6).

A catalog is a list of references, so almost everything that can go wrong with one is a
sentence a teacher has to be able to act on: which statuses may be set, what a reference
is called in a list, when a block reference has gone stale, and which bodies the API
refuses at the door. None of that needs a database, and all of it is worth pinning here
because the same words appear in the browser's pickers.

The half that does need rows - duplicate races against the unique index, the folder
chain, publishability, reordering - is in `tests/integration/test_catalog_db.py`.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import deps
from app.api.v1.endpoints import catalogs, practice
from app.core import enums
from app.main import create_app
from app.schemas import catalog as c
from app.services import catalog_service as cs


def item(
    kind: str = "question", *, config: dict | None = None, position: int = 0, ref_id: uuid.UUID | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind=enums.ContentKind(kind),
        ref_id=ref_id or uuid.uuid4(),
        position=position,
        config=config if config is not None else {},
    )


# --------------------------------------------------------------------------- #
# The words a teacher may set
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["draft", "ready", "archived"])
def test_the_three_lifecycle_words_are_the_ones_a_teacher_may_set(raw: str) -> None:
    assert cs.status_enum(raw) == enums.ContentStatus(raw)


def test_trash_is_a_deletion_and_not_a_status() -> None:
    """`deleted_at` is the trash. A status called "trash" would be a second copy of it."""
    with pytest.raises(cs.CatalogError, match="trash is a deletion"):
        cs.status_enum("trash")


def test_an_unknown_status_is_refused_with_the_choices_in_the_sentence() -> None:
    with pytest.raises(cs.CatalogError) as excinfo:
        cs.status_enum("published")
    assert "draft, ready, archived" in str(excinfo.value)


def test_feedback_timing_defaults_to_the_one_a_teacher_never_has_to_choose() -> None:
    assert cs.timing_of(None) == "instant"
    assert cs.timing_of("after_session") == "after_session"


def test_an_unknown_feedback_timing_is_refused() -> None:
    """A word in that column is obeyed by the runner, so it cannot be a free string."""
    with pytest.raises(cs.CatalogError, match="instant, after_session"):
        cs.timing_of("at_the_end")


def test_a_catalog_may_point_at_exactly_the_four_banks() -> None:
    assert {cs.kind_of(kind).kind for kind in c.ITEM_KINDS} == set(c.ITEM_KINDS)
    with pytest.raises(cs.CatalogError, match="kind must be one of"):
        cs.kind_of("exam")


def test_every_bank_has_a_name_a_teacher_recognises() -> None:
    """The labels are what the picker shows, so none of them may be the column name."""
    assert cs.KIND_LABELS == {
        "question": "question",
        "vocabulary": "word",
        "reading": "reading text",
        "listening": "recording",
    }


def test_the_level_list_is_the_cefr_one() -> None:
    assert cs._check_level("b2") == "B2"
    assert cs._check_level("  ") is None
    with pytest.raises(cs.CatalogError, match="A1, A2, B1, B2, C1, C2"):
        cs._check_level("B3")


# --------------------------------------------------------------------------- #
# One reference, described
# --------------------------------------------------------------------------- #


def test_a_reference_to_a_row_that_is_gone_says_missing() -> None:
    """Not "draft", and not silently dropped: the teacher has to see the hole."""
    assert cs._state_of(None) == "missing"


def test_a_trashed_row_is_named_as_trashed_whatever_its_status_was() -> None:
    row = SimpleNamespace(deleted_at="2026-01-01", status=enums.ContentStatus.READY)
    assert cs._state_of(row) == "trashed"


def test_a_live_row_is_named_by_its_status() -> None:
    row = SimpleNamespace(deleted_at=None, status=enums.ContentStatus.DRAFT)
    assert cs._state_of(row) == "draft"


def test_every_state_a_reference_can_be_in_is_one_the_ui_knows() -> None:
    produced = {"missing", "trashed", "draft", "ready", "archived", "broken_block"}
    assert produced == set(c.ITEM_STATES)


def test_a_question_is_described_by_its_type_and_its_mark() -> None:
    row = SimpleNamespace(type="multi_select", score=2.0, prompt="Choose two", status=enums.ContentStatus.READY)
    read = cs._item_read(cs.kind_of("question"), item("question"), row, None)
    assert read["title"] == "Choose two"
    assert read["detail"] == "multi_select · 2"
    assert read["available_to_learner"] is True


def test_a_question_with_no_mark_is_not_described_as_worth_zero() -> None:
    row = SimpleNamespace(type="essay", score=None, prompt="Explain", status=enums.ContentStatus.READY)
    assert cs._item_read(cs.kind_of("question"), item("question"), row, None)["detail"] == "essay"


def test_a_word_is_described_by_its_part_of_speech_and_falls_back_to_its_level() -> None:
    named = SimpleNamespace(word="apple", part_of_speech="noun", level="A1", status=enums.ContentStatus.READY)
    assert cs._item_read(cs.kind_of("vocabulary"), item("vocabulary"), named, None)["detail"] == "noun"
    bare = SimpleNamespace(word="apple", part_of_speech=None, level="A1", status=enums.ContentStatus.READY)
    assert cs._item_read(cs.kind_of("vocabulary"), item("vocabulary"), bare, None)["detail"] == "A1"


def test_a_reference_to_a_deleted_block_says_so_instead_of_serving_the_whole_passage() -> None:
    row = SimpleNamespace(title="A long text", level="B2", status=enums.ContentStatus.READY)
    built = item("reading", config={"set_id": str(uuid.uuid4())})
    read = cs._item_read(cs.kind_of("reading"), built, row, None)
    assert read["state"] == "broken_block"
    assert read["available_to_learner"] is False


def test_a_block_of_another_passage_is_broken_too() -> None:
    """The stale case that is easy to miss: the block exists, it is just not this text's."""
    passage_id = uuid.uuid4()
    row = SimpleNamespace(id=passage_id, title="A long text", level="B2", status=enums.ContentStatus.READY)
    block = SimpleNamespace(
        id=uuid.uuid4(), title="Part one", position=0, reading_id=uuid.uuid4()
    )
    built = item("reading", config={"set_id": str(block.id)}, ref_id=passage_id)
    read = cs._item_read(cs.kind_of("reading"), built, row, block)
    assert read["state"] == "broken_block"
    assert read["available_to_learner"] is False


def test_a_live_block_adds_its_title_to_the_description() -> None:
    passage_id = uuid.uuid4()
    row = SimpleNamespace(id=passage_id, title="A long text", level="B2", status=enums.ContentStatus.READY)
    block = SimpleNamespace(id=uuid.uuid4(), title="Part two", position=1, reading_id=passage_id)
    built = item("reading", config={"set_id": str(block.id)}, ref_id=passage_id)
    read = cs._item_read(cs.kind_of("reading"), built, row, block)
    assert read["detail"] == "B2 · Part two"
    assert read["state"] == "ready"


def test_an_untitled_block_leaves_the_numbering_to_the_screen() -> None:
    """A block the teacher never named is described by nothing here, not by English words.

    `detail` reaches a teacher whose interface may be in Azerbaijani, Russian or Turkish, so
    a sentence built in Python would be the wrong language on three of four screens. The
    reference keeps naming the block by id, which is what lets the screen count it out of the
    text instead.
    """
    passage_id = uuid.uuid4()
    row = SimpleNamespace(id=passage_id, title=None, level=None, status=enums.ContentStatus.READY)
    block = SimpleNamespace(id=uuid.uuid4(), title=None, position=2, reading_id=passage_id)
    built = item("reading", config={"set_id": str(block.id)}, ref_id=passage_id)
    read = cs._item_read(cs.kind_of("reading"), built, row, block)
    assert read["detail"] is None
    assert read["title"] is None
    assert read["state"] == "ready"
    assert read["config"]["set_id"] == str(block.id)


def test_a_deleted_or_misfiled_block_is_described_by_state_not_prose() -> None:
    passage_id = uuid.uuid4()
    row = SimpleNamespace(id=passage_id, title="A long text", level="B1", status=enums.ContentStatus.READY)
    built = item("reading", config={"set_id": str(uuid.uuid4())}, ref_id=passage_id)
    read = cs._item_read(cs.kind_of("reading"), built, row, None)
    assert read["state"] == "broken_block"
    assert read["detail"] == "B1"


def test_the_block_a_reference_names_is_read_out_of_its_config_as_a_uuid() -> None:
    set_id = uuid.uuid4()
    assert cs._set_id_of(item("reading", config={"set_id": str(set_id)})) == set_id
    assert cs._set_id_of(item("reading")) is None
    assert cs._set_id_of(item("reading", config={})) is None


# --------------------------------------------------------------------------- #
# The feedback timing lives in `meta`, and agrees with the exam column
# --------------------------------------------------------------------------- #


def test_a_catalog_with_no_meta_falls_back_to_instant() -> None:
    assert cs.feedback_timing_of(SimpleNamespace(meta={})) == "instant"
    assert cs.feedback_timing_of(SimpleNamespace(meta=None)) == "instant"


def test_the_timing_a_catalog_stores_is_the_one_an_exam_will_use() -> None:
    """Phase 7 gives `exam.feedback_timing` its own column of `enums.FeedbackTiming`.

    A catalog keeps the same two words in `meta`, because the frozen schema has no column
    for it. If the two lists ever disagree, one runner would obey a setting the other
    refuses - so the pair is checked here rather than left to two hardcoded tuples.
    """
    assert c.FEEDBACK_TIMINGS == tuple(timing.value for timing in enums.FeedbackTiming)


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


def test_a_catalog_may_be_born_as_a_draft_or_ready_but_never_as_trash() -> None:
    assert c.CatalogCreate(name="Unit 1").status == "draft"
    assert c.CatalogCreate(name="Unit 1", status="ready").status == "ready"
    with pytest.raises(ValidationError, match="status must be one of"):
        c.CatalogCreate(name="Unit 1", status="trash")


def test_a_name_of_only_spaces_is_no_name() -> None:
    with pytest.raises(ValidationError, match="a catalog needs a name"):
        c.CatalogCreate(name="   ")


def test_names_and_descriptions_are_trimmed_before_they_are_stored() -> None:
    built = c.CatalogCreate(name="  Unit 1  ", description="  For the spring class  ", level=" b2 ")
    assert (built.name, built.description, built.level) == ("Unit 1", "For the spring class", "b2")


def test_an_unknown_feedback_timing_never_reaches_the_database() -> None:
    with pytest.raises(ValidationError, match="feedback_timing must be one of"):
        c.CatalogCreate(name="Unit 1", feedback_timing="never")


def test_a_patch_body_cannot_change_the_status() -> None:
    """Publishing is its own endpoint, so a rename cannot quietly be a release."""
    assert "status" not in c.CatalogUpdate.model_fields
    with pytest.raises(ValidationError):
        c.CatalogUpdate.model_validate({"status": "ready"})


def test_item_config_accepts_only_a_block() -> None:
    """A JSONB column a client can fill in is a field nobody owns."""
    assert c.ItemConfig().stored() == {}
    set_id = uuid.uuid4()
    assert c.ItemConfig(set_id=set_id).stored() == {"set_id": str(set_id)}
    with pytest.raises(ValidationError):
        c.ItemConfig.model_validate({"set_id": str(set_id), "notes": "hello"})


def test_one_body_cannot_add_the_same_content_twice() -> None:
    ref_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="cannot be added twice"):
        c.ItemAddRequest.model_validate(
            {
                "items": [
                    {"kind": "question", "ref_id": str(ref_id)},
                    {"kind": "question", "ref_id": str(ref_id)},
                ]
            }
        )


def test_an_unknown_item_kind_is_refused_before_the_service_sees_it() -> None:
    with pytest.raises(ValidationError, match="kind must be one of"):
        c.ItemAdd.model_validate({"kind": "exam", "ref_id": str(uuid.uuid4())})


def test_an_empty_selection_of_items_is_refused() -> None:
    with pytest.raises(ValidationError):
        c.ItemAddRequest.model_validate({"items": []})


def test_a_reorder_cannot_name_the_same_item_twice() -> None:
    item_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="cannot appear twice"):
        c.ItemReorderRequest.model_validate({"item_ids": [str(item_id), str(item_id)]})


@pytest.mark.parametrize("action", ["status", "trash", "restore", "set_level"])
def test_the_bulk_actions_are_the_four_the_toolbar_offers(action: str) -> None:
    assert c.CatalogBulkRequest.model_validate(
        {"catalog_ids": [str(uuid.uuid4())], "action": action}
    ).action == action


def test_an_unknown_bulk_action_is_refused() -> None:
    """There is deliberately no bulk edit: content changes belong to the review screens."""
    with pytest.raises(ValidationError, match="action must be one of"):
        c.CatalogBulkRequest.model_validate({"catalog_ids": [str(uuid.uuid4())], "action": "rename"})


def test_a_bulk_request_cannot_name_one_catalog_twice() -> None:
    catalog_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="cannot appear twice"):
        c.CatalogBulkRequest.model_validate(
            {"catalog_ids": [str(catalog_id), str(catalog_id)], "action": "trash"}
        )


def test_a_bulk_level_of_only_spaces_is_no_level() -> None:
    """Blank means "clear it", which the service then refuses to do by accident."""
    built = c.CatalogBulkRequest.model_validate(
        {"catalog_ids": [str(uuid.uuid4())], "action": "set_level", "level": "   "}
    )
    assert built.level is None


# --------------------------------------------------------------------------- #
# The route shape
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def paths() -> dict:
    return TestClient(create_app()).get("/openapi.json").json()["paths"]


def test_the_catalog_surface_is_the_one_the_editor_needs(paths: dict) -> None:
    catalog_paths = {path for path in paths if path.startswith("/api/v1/catalogs")}
    assert catalog_paths == {
        "/api/v1/catalogs",
        "/api/v1/catalogs/meta",
        "/api/v1/catalogs/bulk",
        "/api/v1/catalogs/items/{item_id}",
        "/api/v1/catalogs/{catalog_id}",
        "/api/v1/catalogs/{catalog_id}/items",
        "/api/v1/catalogs/{catalog_id}/items/reorder",
        "/api/v1/catalogs/{catalog_id}/preview",
        "/api/v1/catalogs/{catalog_id}/restore",
        "/api/v1/catalogs/{catalog_id}/status",
    }


def test_a_preview_is_a_read_and_writes_nothing(paths: dict) -> None:
    """The teacher's preview must not become a run in a learner's history."""
    assert set(paths["/api/v1/catalogs/{catalog_id}/preview"]) == {"get"}


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


def test_every_catalog_route_needs_a_teacher_session() -> None:
    """Checked from the routes themselves, because a missing dependency is a silent 200."""
    for route in catalogs.router.routes:
        guards = _guards(route)
        assert deps.get_current_admin in guards, f"{route.path} is reachable without a teacher"
        assert deps.get_current_student not in guards, f"{route.path} accepts a learner key"


def test_every_practice_route_needs_a_learner_session() -> None:
    for route in list(practice.router.routes) + list(practice.favorites_router.routes):
        guards = _guards(route)
        assert deps.get_current_student in guards, f"{route.path} is reachable without a learner"
        assert deps.get_current_admin not in guards, f"{route.path} accepts a teacher key"


def test_removing_a_favorite_is_a_post_and_not_a_delete(paths: dict) -> None:
    """No learner route carries a method a permission check has to reason about."""
    assert set(paths["/api/v1/student/favorites"]) == {"get", "post"}
    assert set(paths["/api/v1/student/favorites/remove"]) == {"post"}
