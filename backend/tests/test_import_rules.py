"""Offline tests for the document import rules (Phase 8).

The importer's whole promise is that it never invents meaning, and almost every part of
that promise is a decision about a dictionary of text: what still has to be supplied before
a candidate may become content, which fields a kind owns, whose words a row is showing, and
which sentence a teacher gets when a row is refused. None of it needs a database, and all of
it is worth pinning here because the same words appear in the review screen's pickers.

The half that needs rows - a paper uploaded and read, approval writing real content through
the bank's services, one source per identical file, a batch over a page, the worker running
a job - is in `tests/integration/test_import_db.py`.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints import imports as imports_api
from app.core import enums
from app.core.exceptions import RuleBroken, as_conflict, as_invalid, as_missing
from app.models.content import ImportItem
from app.schemas import imports as i_schemas
from app.services import import_candidates as ic
from app.services import import_service as isv
from app.workers import jobs

# --------------------------------------------------------------------------- #
# Candidates, held in memory the way the review screen reads them
# --------------------------------------------------------------------------- #


def candidate(
    kind: str = "question",
    *,
    question_type: str | None = None,
    extracted: dict | None = None,
    corrected: dict | None = None,
    filing: dict | None = None,
    decision: enums.ImportItemDecision = enums.ImportItemDecision.PENDING,
    confidence: float | None = 0.95,
) -> ImportItem:
    """One row of a queue, before it has ever met a database.

    `filing` starts as the service starts it, because a candidate created by a parse always
    carries the empty filing: a gap in it is a teacher's decision not yet made, not a bug.
    """
    return ImportItem(
        job_id=uuid.uuid4(),
        detected_kind=kind,
        detected_type=question_type,
        extracted=extracted if extracted is not None else {},
        corrected=corrected,
        filing={**isv._empty_filing(), **(filing or {})},
        decision=decision,
        confidence=confidence,
    )


def choice(text: str = "Which is a cat?", *options: str, correct: str | None = None) -> dict:
    return {
        "prompt": text,
        "options": [
            {"text": option, "correct": option == correct}
            for option in options
            or ("Felis", "Canis", "Equus", "Rattus")
        ],
    }


# --------------------------------------------------------------------------- #
# What a candidate still needs
# --------------------------------------------------------------------------- #


def test_options_with_no_answer_key_are_not_approved_with_a_guess() -> None:
    """The rule Phase 8 exists to keep: a paper's option list is not an answer."""
    item = candidate("question", question_type="multiple_choice", extracted=choice("Which is a cat?"))
    assert isv.pending_fields(item) == ["answer"]
    assert not isv.is_approvable(item)


def test_a_question_with_no_text_names_both_gaps() -> None:
    item = candidate("question", question_type="multiple_choice", extracted={})
    assert isv.pending_fields(item) == ["prompt", "options"]


def test_a_complete_choice_question_needs_nothing() -> None:
    item = candidate(
        "question",
        question_type="multiple_choice",
        extracted=choice(correct="Felis"),
    )
    assert isv.pending_fields(item) == []
    assert isv.is_approvable(item)


def test_a_type_the_importer_cannot_build_is_a_gap_not_a_substitution() -> None:
    """`true_false` on the paper stays `true_false` in the row until a person changes it."""
    item = candidate("question", question_type="true_false", extracted=choice(correct="Felis"))
    assert "type" in isv.pending_fields(item)
    assert item.detected_type == "true_false"


def test_a_multi_select_needs_one_ticked_option_and_one_still_wrong() -> None:
    """The engine selects against the wrong options, so a key that ticks everything is a gap."""
    item = candidate(
        "question",
        question_type="multi_select",
        extracted=choice(correct="Felis"),
    )
    assert isv.pending_fields(item) == []
    all_ticked = {
        "prompt": "Which are animals?",
        "options": [{"text": "Felis", "correct": True}, {"text": "Canis", "correct": True}],
    }
    assert isv.pending_fields(candidate("question", question_type="multi_select", extracted=all_ticked)) == ["answer"]


def test_a_single_choice_with_two_ticked_options_is_not_an_answer() -> None:
    """The bank takes exactly one right option for `multiple_choice`, so a paper that marks
    two still needs a person to settle which one it asked for."""
    text = {
        "prompt": "Which are felines?",
        "options": [
            {"text": "Felis", "correct": True},
            {"text": "Canis", "correct": True},
        ],
    }
    item = candidate("question", question_type="multiple_choice", extracted=text)
    assert isv.pending_fields(item) == ["answer"]


def test_completeness_predicts_the_engine_rather_than_arguing_with_it() -> None:
    """A candidate the screen calls ready is one the question service will not refuse a
    moment later, and a candidate it calls incomplete is one the engine would refuse."""
    cases = [
        ("multiple_choice", choice(correct="Felis"), []),
        ("multiple_choice", choice("x", "a", "b", correct=None), ["answer"]),
        ("multi_select", choice(correct="Felis"), []),
        ("multi_select", choice(), ["answer"]),
    ]
    for question_type, text, expected in cases:
        item = candidate("question", question_type=question_type, extracted=text)
        assert isv.pending_fields(item) == expected, (question_type, text)


def test_a_short_answer_with_no_accepted_text_needs_an_answer() -> None:
    item = candidate("question", question_type="short_answer", extracted={"prompt": "Spell 'cat'."})
    assert isv.pending_fields(item) == ["answer"]


def test_a_word_needs_the_language_it_belongs_to() -> None:
    """A vocabulary entry without a learning language cannot be filed, so it cannot be approved."""
    item = candidate("vocabulary", extracted={"word": "cat", "definition": "a small felid"})
    assert isv.pending_fields(item) == ["language"]
    filled = candidate(
        "vocabulary",
        extracted={"word": "cat", "definition": "a small felid"},
        filing={"language": "en"},
    )
    assert isv.pending_fields(filled) == []


def test_a_passage_needs_its_title_and_its_body() -> None:
    item = candidate("reading", extracted={"title": "Cats"})
    assert isv.pending_fields(item) == ["body"]


def test_an_unclassified_note_needs_a_person_to_say_what_it_is() -> None:
    item = candidate("note", extracted={"text": "Bring a dictionary on Friday."})
    assert isv.pending_fields(item) == ["kind"]
    assert not isv.is_approvable(item)


def test_the_teacher_s_words_win_over_the_file_s_without_replacing_them() -> None:
    """`corrected` is shown and used; `extracted` stays the evidence of what the paper said."""
    item = candidate(
        "question",
        question_type="multiple_choice",
        extracted=choice("Which is a cat?"),
        corrected={"prompt": "Which is a cat?", "options": [{"text": "Felis", "correct": True}, {"text": "Canis"}]},
    )
    assert isv.effective_text(item) == item.corrected
    assert isv.pending_fields(item) == []
    assert item.extracted["options"][0]["correct"] is False


def test_every_gap_the_service_can_name_is_explained() -> None:
    """A code the screen has no words for is a sentence a teacher cannot act on."""
    items = [
        candidate("question", question_type="true_false", extracted={}),
        candidate("question", question_type="multiple_choice", extracted=choice()),
        candidate("question", question_type="multiple_choice", extracted={}),
        candidate("question", question_type="short_answer", extracted={"prompt": "x"}),
        candidate("vocabulary", extracted={}),
        candidate("reading", extracted={}),
        candidate("note", extracted={"text": "x"}),
    ]
    named = {gap for item in items for gap in isv.pending_fields(item)}
    assert named <= set(isv._MISSING_EXPLANATIONS), sorted(named - set(isv._MISSING_EXPLANATIONS))


def test_a_row_is_only_ever_missing_a_field_the_screen_already_knows() -> None:
    """The import queue's completeness rules and its vocabulary are one list, not two."""
    assert set(isv._MISSING_EXPLANATIONS) >= {
        "prompt", "options", "accepted", "answer", "type", "word", "definition", "language",
        "title", "body", "kind",
    }


# --------------------------------------------------------------------------- #
# Attention, editability and what the queue reports
# --------------------------------------------------------------------------- #


def test_doubt_about_a_row_is_attention_even_when_the_row_is_complete() -> None:
    low = candidate("question", question_type="multiple_choice", extracted=choice(correct="Felis"), confidence=0.2)
    assert isv.pending_fields(low) == []
    assert isv.needs_attention(low)


def test_a_missing_confidence_is_doubt_not_certainty() -> None:
    """A parse that recorded no confidence cannot claim the teacher need not look."""
    item = candidate("question", question_type="multiple_choice", extracted=choice(correct="Felis"), confidence=None)
    assert isv.needs_attention(item)


def test_a_filed_row_is_not_editable_and_not_approvable_again() -> None:
    item = candidate(
        "question",
        question_type="multiple_choice",
        extracted=choice(correct="Felis"),
        decision=enums.ImportItemDecision.APPROVED,
    )
    assert not isv.is_approvable(item)
    assert not isv.item_read(item)["editable"]


def test_a_refused_row_stays_open_because_a_changed_mind_is_a_new_decision() -> None:
    """Refusing a candidate destroys nothing: the row can be corrected and approved later.

    Only a row that has produced content is closed to the review screen, because the bank
    holds that text now and it is edited there.
    """
    refused = candidate("note", extracted={"text": "x"}, decision=enums.ImportItemDecision.REJECTED)
    assert isv.item_read(refused)["editable"]
    filed = candidate("note", extracted={"text": "x"}, decision=enums.ImportItemDecision.APPROVED)
    assert not isv.item_read(filed)["editable"]


def test_a_row_with_no_gaps_is_still_not_approvable_when_no_content_takes_it() -> None:
    """`missing == []` is not an invitation to approve: a note has no bank to file into.

    The screen reads both answers, and if Approve ever lit up for a kind the importer cannot
    write, the teacher would click something that can only ever refuse them.
    """
    note = candidate("note", extracted={"text": "A sentence with nothing missing."})
    assert isv.pending_fields(note) == ["kind"]
    assert not isv.is_approvable(note)
    unkinded = candidate(None, extracted={"text": "A sentence."})
    assert not isv.is_approvable(unkinded)


def test_the_queue_says_what_a_candidate_is_made_of_and_who_it_came_from() -> None:
    item = candidate("vocabulary", extracted={"word": "cat"}, confidence=0.4)
    row = isv.item_read(item)
    assert row["extracted"] == {"word": "cat"}
    assert row["corrected"] is None
    assert row["has_correction"] is False
    assert row["missing"] == ["definition", "language"]
    assert row["attention"] is True
    assert row["result"] is None
    assert row["filing"] == isv._empty_filing()


def test_a_filed_row_reports_the_content_it_became() -> None:
    content_id = uuid.uuid4()
    item = candidate("vocabulary", extracted={"word": "cat", "definition": "felid"}, filing={"language": "en"})
    item.decision = enums.ImportItemDecision.APPROVED
    item.result_ref_type = "vocabulary"
    item.result_ref_id = content_id
    assert isv.item_read(item)["result"] == {"kind": "vocabulary", "id": str(content_id)}


# --------------------------------------------------------------------------- #
# The text a teacher sends, cleaned before it is stored
# --------------------------------------------------------------------------- #


def test_a_field_the_kind_does_not_have_is_refused_not_stored() -> None:
    """`extracted`/`corrected` are the evidence of what the paper said; a browser may not
    invent a slot in it."""
    with pytest.raises(isv.ImportProblem) as excinfo:
        isv._clean_text("vocabulary", {"word": "cat", "audio_asset_id": "anything"})
    assert excinfo.value.code == "unknown_field"
    assert excinfo.value.params == {"fields": ["audio_asset_id"]}


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(isv.ImportProblem) as excinfo:
        isv._clean_text("listening", {"title": "x"})
    assert excinfo.value.code == "kind_invalid"


def test_a_number_where_text_belongs_is_refused() -> None:
    with pytest.raises(isv.ImportProblem) as excinfo:
        isv._clean_text("vocabulary", {"word": 42})
    assert excinfo.value.code == "field_type"


@pytest.mark.parametrize(
    ("kind", "field"),
    [("question", "options"), ("question", "accepted"), ("vocabulary", "examples")],
)
def test_a_field_that_takes_a_list_refuses_a_value_that_is_not_one(kind: str, field: str) -> None:
    """The two refusals are different sentences, so they are different codes.

    "has to be written as text" said to a teacher who typed a list is a dead end; the screen
    needs to name the field and what shape it takes.
    """
    with pytest.raises(isv.ImportProblem) as excinfo:
        isv._clean_text(kind, {field: "one value, not a list"})
    assert excinfo.value.code == "field_list"
    assert excinfo.value.params == {"field": field}


def test_an_absent_field_is_left_out_rather_than_emptied() -> None:
    """A patch that does not mention a meaning must not delete it from the candidate."""
    assert isv._clean_text("vocabulary", {"word": "  cat  "}) == {"word": "cat"}


@pytest.mark.parametrize(
    "raw",
    [
        ["Felis", "Canis"],
        [{"text": "Felis"}, {"text": "Canis", "correct": True}],
        [{"text": "  Felis  ", "correct": False}, {"text": ""}, {"nonsense": 1}],
    ],
)
def test_both_option_shapes_the_review_screen_writes_are_understood(raw: list) -> None:
    cleaned = isv._clean_options(raw)
    assert all(set(option) == {"text", "correct"} for option in cleaned)
    assert all(option["text"] for option in cleaned)


def test_a_plain_option_list_arrives_unanswered() -> None:
    """The parsers hand over the paper's list; nobody ticked anything, so nothing is correct."""
    assert isv._clean_options(["Felis", "Canis"]) == [
        {"text": "Felis", "correct": False},
        {"text": "Canis", "correct": False},
    ]


def test_examples_keep_the_sentence_and_drop_the_rest() -> None:
    assert isv._clean_examples(["The cat sat.", {"sentence": " A cat.", "translation": "ignored"}]) == [
        {"sentence": "The cat sat."},
        {"sentence": "A cat."},
    ]


def test_a_filing_keeps_what_the_patch_did_not_choose() -> None:
    """A level decision and a language decision arrive separately and must not erase each other."""
    current = {**isv._empty_filing(), "level": "B1", "language": "en"}
    payload = i_schemas.ImportFiling(status="ready")
    merged = isv._filing_of(payload, current)
    assert merged == {**isv._empty_filing(), "level": "B1", "language": "en", "status": "ready"}


def test_an_unknown_filing_status_is_refused_with_the_words_that_are_allowed() -> None:
    with pytest.raises(isv.ImportProblem) as excinfo:
        isv._filing_of(i_schemas.ImportFiling(status="published"), None)
    assert excinfo.value.code == "status_invalid"
    assert ", ".join(isv.FILING_STATUSES) in str(excinfo.value)


@pytest.mark.parametrize(
    "written,expected",
    [
        ("Multiple Choice", "multiple_choice"),
        ("MCQ", "multiple_choice"),
        ("short-answer", "short_answer"),
        ("true_false", "true_false"),
        ("  Cloze   ", "cloze"),
    ],
)
def test_a_type_is_read_as_the_registry_spells_it(written: str, expected: str) -> None:
    assert isv._normalise_type(written) == expected


# --------------------------------------------------------------------------- #
# The request bodies the API refuses at the door
# --------------------------------------------------------------------------- #


def test_a_candidate_edit_cannot_name_a_field_the_server_does_not_have() -> None:
    with pytest.raises(ValidationError):
        i_schemas.ImportItemEdit.model_validate({"extracted": {"word": "cat"}, "admin_id": "x"})


def test_a_candidate_edit_refuses_a_kind_that_is_not_one() -> None:
    with pytest.raises(ValidationError, match="kind must be one of"):
        i_schemas.ImportItemEdit(kind="flashcard")


def test_a_bulk_action_refuses_an_unknown_verb_and_an_empty_selection() -> None:
    with pytest.raises(ValidationError, match="action must be one of"):
        i_schemas.ImportBulkRequest(item_ids=[uuid.uuid4()], action="publish")
    with pytest.raises(ValidationError):
        i_schemas.ImportBulkRequest(item_ids=[], action="approve")


def test_a_bulk_page_is_capped_at_the_number_the_meta_reports() -> None:
    with pytest.raises(ValidationError):
        i_schemas.ImportBulkRequest(
            item_ids=[uuid.uuid4() for _ in range(i_schemas.MAX_BULK_ITEMS + 1)], action="approve"
        )


def test_a_filing_cannot_smuggle_a_provenance_field() -> None:
    with pytest.raises(ValidationError):
        i_schemas.ImportFiling.model_validate({"status": "draft", "source_file_id": str(uuid.uuid4())})


# --------------------------------------------------------------------------- #
# What the service says about itself, and what the screen is built from
# --------------------------------------------------------------------------- #


def test_the_meta_offers_only_what_the_importer_will_accept() -> None:
    options = isv.import_options()
    assert options["formats"], "an importer that accepts nothing has no upload screen"
    assert options["max_document_bytes"] == isv.upload_limit_bytes()
    assert options["max_document_mb"] == isv.upload_limit_bytes() // (1024 * 1024)
    assert set(options["editable_fields"]) == set(i_schemas.KINDS)
    assert options["approvable_kinds"] == list(isv.APPROVABLE_KINDS)
    assert set(options["missing_fields"]) >= set(isv._MISSING_EXPLANATIONS)
    assert options["max_bulk_items"] == i_schemas.MAX_BULK_ITEMS


def test_the_remarks_a_card_can_carry_are_offered_to_the_screen() -> None:
    """A card's remark is a code, and `/imports/meta` is where the screen learns the vocabulary.

    The reader leaves one kind of remark and auto mode leaves a refusal's code; a code that
    reaches `import_item.note` but is absent here shows up on a teacher's screen as the code
    itself, in every language. `bank_refused` is the one the reader never writes, because it
    comes back from the bank.
    """
    table = isv.note_codes()
    assert isv.import_options()["note_codes"] == table
    assert set(table) >= set(ic.NOTE_CODES) | {"bank_refused"}
    for code, sentence in table.items():
        assert code.islower() and " " not in code and code == code.strip(), f"not a code: {code!r}"
        assert sentence.strip().endswith("."), f"{code}: a remark read in a log should be a sentence"
    assert "bank_refused" in table
    assert set(ic.NOTE_CODES) <= set(table) and table["bank_refused"] not in ic.NOTE_CODES.values()
    for code, sentence in table.items():
        assert code == code.strip(), f"{code!r} is padded"
        assert sentence.strip().endswith("."), f"{code}: a remark shown in a log should read as a sentence"


def test_the_kinds_a_candidate_may_hold_are_the_ones_its_editors_cover() -> None:
    """A kind in the schema with no editable-field list would accept any body."""
    for kind in i_schemas.KINDS:
        assert i_schemas.EDITABLE_FIELDS[kind]


def test_only_the_kinds_a_screen_can_review_may_be_filed() -> None:
    assert set(isv.APPROVABLE_KINDS) <= set(i_schemas.KINDS)
    assert "note" not in isv.APPROVABLE_KINDS


def test_every_approvable_kind_has_exactly_one_target_in_the_bank() -> None:
    """Approval is one path: each kind names a model, a reference word and a payload builder."""
    assert set(isv._TARGETS) == set(isv.APPROVABLE_KINDS)
    assert set(isv._BUILDERS) == set(isv._TARGETS)
    for kind, (ref_type, model) in isv._TARGETS.items():
        assert ref_type == kind
        assert model.__name__


def test_sortable_columns_are_what_the_meta_advertises() -> None:
    options = isv.import_options()
    assert set(options["sortable_jobs"]) == set(isv.JOB_SORTABLE)
    assert set(options["sortable_items"]) == set(isv.ITEM_SORTABLE)


@pytest.mark.parametrize(
    "page,page_size",
    [(1, 50), (3, 10), (1, 200)],
)
def test_a_page_is_a_window_not_a_limit_break(page: int, page_size: int) -> None:
    offset, limit = isv._page_clause(page, page_size)
    assert offset == (page - 1) * page_size
    assert limit == page_size


@pytest.mark.parametrize("page", [0, -1])
def test_a_page_below_one_is_refused(page: int) -> None:
    with pytest.raises(isv.ImportProblem) as excinfo:
        isv._page_clause(page, 50)
    assert excinfo.value.code == "page_invalid"


def test_a_page_of_two_hundred_rows_is_the_biggest_one_asked_for() -> None:
    with pytest.raises(isv.ImportProblem):
        isv._page_clause(1, 201)
    with pytest.raises(isv.ImportProblem):
        isv._page_clause(1, 0)


# --------------------------------------------------------------------------- #
# The payload a candidate becomes: the bank's own, with nothing weaker
# --------------------------------------------------------------------------- #


def test_an_imported_question_is_built_as_the_bank_would_build_it() -> None:
    item = candidate("question", question_type="multiple_choice", extracted=choice(correct="Felis"))
    payload = isv._question_payload(item, isv.effective_text(item), item.filing)
    assert payload.type == "multiple_choice"
    assert payload.prompt == "Which is a cat?"
    assert [option["correct"] for option in payload.config["options"]] == [True, False, False, False]
    assert payload.status == "draft"
    assert payload.change_note == "Created by document import"


def test_an_imported_short_answer_carries_its_accepted_text() -> None:
    item = candidate(
        "question", question_type="short_answer", extracted={"prompt": "Spell 'cat'.", "accepted": [" cat "]}
    )
    payload = isv._question_payload(item, isv.effective_text(item), item.filing)
    assert payload.config["accepted"] == ["cat"]


def test_a_word_is_filed_in_the_language_the_teacher_chose_not_the_paper_claimed() -> None:
    """A level written on the paper may be carried; the language comes from the filing."""
    item = candidate(
        "vocabulary",
        extracted={"word": "cat", "definition": "a small felid", "level": "A1"},
        filing={"language": "az"},
    )
    payload = isv._vocabulary_payload(item, isv.effective_text(item), item.filing)
    assert (payload.word, payload.learning_language, payload.level) == ("cat", "az", "A1")


def test_a_passage_is_filed_as_one_text_with_the_teacher_s_level() -> None:
    item = candidate(
        "reading",
        extracted={"title": "Cats", "body": "Felis is a genus.", "level": "B1"},
        filing={"language": "en", "level": "B2"},
    )
    payload = isv._reading_payload(item, isv.effective_text(item), item.filing)
    assert (payload.title, payload.language, payload.level) == ("Cats", "en", "B2")


def test_a_filing_of_tag_ids_reaches_the_bank_as_ids() -> None:
    tag = uuid.uuid4()
    item = candidate(
        "question",
        question_type="multiple_choice",
        extracted=choice(correct="Felis"),
        filing={"tag_ids": [str(tag)]},
    )
    payload = isv._question_payload(item, isv.effective_text(item), item.filing)
    assert payload.tag_ids == [tag]


# --------------------------------------------------------------------------- #
# Every refusal reaches a teacher as a status that means the right thing
# --------------------------------------------------------------------------- #


def _service_refusals() -> list[type]:
    return [
        obj
        for obj in vars(isv).values()
        if isinstance(obj, type) and issubclass(obj, RuleBroken) and obj.__module__ == isv.__name__
    ]


def test_the_import_service_declares_the_refusals_the_router_maps() -> None:
    """A new refusal with no mapping in the router would answer a teacher with a 500."""
    classes = _service_refusals()
    assert len(classes) >= 6, "the service raises almost nothing at all?"
    for cls in classes:
        handled = (
            issubclass(cls, imports_api._INVALID)
            or issubclass(cls, imports_api._CLASH)
            or cls is isv.JobNotFound
            or cls is isv.QueueDown
        )
        assert handled, f"{cls.__name__} has no status in the import router"


def test_no_refusal_is_mapped_twice_with_two_different_statuses() -> None:
    """422 and 409 for the same class would make a screen's copy disagree with itself."""
    both = set(imports_api._INVALID) & set(imports_api._CLASH)
    assert both == set()


@pytest.mark.parametrize(
    "exception,expected_status",
    [
        (isv.ImportProblem("x", code="import_rule"), 422),
        (isv.IncompleteCandidate("x", code="candidate_incomplete"), 422),
        (isv.JobNotFound("x", code="import_not_found"), 404),
        (isv.AlreadyFiled("x", code="candidate_filed"), 409),
        (isv.AlreadyInBank("x", code="content_exists"), 409),
    ],
)
def test_a_refusal_answers_with_the_status_it_promised(exception: Exception, expected_status: int) -> None:
    if expected_status == 422:
        mapped = as_invalid(exception)
    elif expected_status == 404:
        mapped = as_missing(exception)
    else:
        mapped = as_conflict(exception)
    assert mapped.http_status == expected_status


def test_an_unreachable_worker_is_a_503_and_not_a_refusal_of_the_paper() -> None:
    """422 would tell the teacher their document is bad; the document is fine, the queue is not."""
    exc = isv.QueueDown("the worker could not be reached", code="queue_unavailable")
    response = imports_api._down(exc)
    assert response.http_status == 503
    assert response.code == "queue_unavailable"


def test_a_refusal_keeps_the_numbers_a_translated_sentence_needs() -> None:
    exc = isv.IncompleteCandidate(
        "This candidate still needs: an accepted answer.",
        code="candidate_incomplete",
        params={"fields": ["answer"]},
    )
    assert as_invalid(exc).params == {"fields": ["answer"]}


def test_every_refusal_class_carries_a_code_a_screen_can_translate() -> None:
    """The browser asks after `code` and puts it into the reader's own language; two refusals
    sharing one name could not be told apart there."""
    classes = _service_refusals()
    codes = [cls.code for cls in classes]
    assert len(set(codes)) == len(codes), f"two refusals share a code: {codes}"
    for cls in classes:
        assert cls.code and cls.code == cls.code.strip().lower(), cls.__name__


class _NoQueueFound:
    """A database that answers every read with nothing, so only the refusal is under test."""

    def scalars(self) -> "_NoQueueFound":
        return self

    def first(self) -> None:
        return None

    async def execute(self, *_args, **_kwargs) -> "_NoQueueFound":
        return self


async def test_a_duplicate_document_with_no_queue_says_so_instead_of_answering_with_nothing() -> None:
    """The duplicate answer promises a queue; a library row with none must not be silent.

    Reaching this means the two rows were separated by something this module does not do -
    so the teacher gets a sentence naming what to do, and not an empty payload the screen
    would render as a queue with no name and no candidates.
    """
    source = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(isv.JobNotFound) as caught:
        await isv._duplicate_payload(_NoQueueFound(), source)
    assert caught.value.code == "queue_missing"
    assert as_missing(caught.value).http_status == 404


# --------------------------------------------------------------------------- #
# Handing the work to the worker
# --------------------------------------------------------------------------- #


def test_the_task_name_and_the_worker_function_are_one_name() -> None:
    """A message sent to a task the worker does not run leaves a queue waiting forever."""
    assert isv.tasks.IMPORT_JOB_TASK == jobs.process_import_job.__name__


async def test_the_job_is_committed_before_the_worker_is_told(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker handed an id whose rows are not committed finds no job at all."""
    events: list[str] = []
    job = SimpleNamespace(id=uuid.uuid4())

    class _Db:
        async def commit(self) -> None:
            events.append("commit")

    async def fake_enqueue(task: str, *args: object) -> None:
        events.append(f"enqueue:{task}:{args[0]}")

    monkeypatch.setattr(isv.tasks, "enqueue", fake_enqueue)
    await isv.dispatch(_Db(), job)  # type: ignore[arg-type]
    assert events == ["commit", f"enqueue:{isv.tasks.IMPORT_JOB_TASK}:{job.id}"]


async def test_a_down_queue_leaves_the_stored_document_and_a_retryable_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must not undo the upload: the rows stay, and the screen's retry is enough."""
    committed: list[int] = []
    job = SimpleNamespace(id=uuid.uuid4())

    class _Db:
        async def commit(self) -> None:
            committed.append(1)

    async def unreachable(task: str, *args: object) -> None:
        raise isv.tasks.QueueUnavailable("redis is down")

    monkeypatch.setattr(isv.tasks, "enqueue", unreachable)
    with pytest.raises(isv.QueueDown):
        await isv.dispatch(_Db(), job)  # type: ignore[arg-type]
    assert committed == [1]


# --------------------------------------------------------------------------- #
# The worker's own half
# --------------------------------------------------------------------------- #


async def test_a_message_with_no_job_id_does_not_open_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id that names no job is a fault in the message; no row may be touched by it."""

    def never() -> object:
        raise AssertionError("the worker opened a database for an id that is not one")

    monkeypatch.setattr(jobs, "SessionLocal", never)
    assert await jobs.process_import_job({}, "not-a-uuid") == "bad_id"


async def test_a_worker_that_crashes_reports_the_job_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue must never wait on a reading that already stopped."""
    job = SimpleNamespace(
        id=uuid.uuid4(),
        status=enums.JobStatus.PROCESSING,
        error=None,
        finished_at=None,
        progress={"stage": "processing"},
    )
    calls = {"commit": 0, "rollback": 0}

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, model: type, pk: uuid.UUID) -> object:
            return job

        async def rollback(self) -> None:
            calls["rollback"] += 1

        async def commit(self) -> None:
            calls["commit"] += 1

    async def crash(db: object, job_id: uuid.UUID) -> str:
        raise RuntimeError("the parser fell over")

    monkeypatch.setattr(jobs, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(isv, "run_job", crash)
    assert await jobs.process_import_job({}, str(job.id)) == "failed"
    assert job.status is enums.JobStatus.FAILED
    assert calls == {"commit": 1, "rollback": 1}
    assert job.error, "a failed import with no sentence is a teacher with nothing to read"
    assert job.progress["outcome"] == "worker_stopped"
