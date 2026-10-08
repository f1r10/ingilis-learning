"""Offline tests for the question engine.

The engine decides what every question type means, so these tests are the ones that
protect the answer key: they check that each type validates, that grading gets the
maths right, and - most importantly - that the student-facing projection can never hand
a learner the answer.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import question_engine as qe

ALL_TYPES = [
    "essay",
    "gap_fill",
    "matching",
    "multi_select",
    "multiple_choice",
    "ordering",
    "short_answer",
    "translation",
    "true_false",
]


def valid_config(question_type: str) -> dict[str, Any]:
    return {
        "multiple_choice": {
            "options": [
                {"text": "cat", "correct": False},
                {"text": "dog", "correct": True},
                {"text": "fish", "correct": False},
            ]
        },
        "multi_select": {
            "options": [
                {"text": "apple", "correct": True},
                {"text": "carrot", "correct": False},
                {"text": "banana", "correct": True},
                {"text": "table", "correct": False},
            ]
        },
        "true_false": {"statement": "Baku is a capital city.", "correct": True},
        "short_answer": {
            "accepted": ["colour"],
            "normalization": {},
            "max_characters": 40,
            "hint": "British spelling",
        },
        "gap_fill": {
            "text": "She ___ to the ___ every morning.",
            "blanks": [{"accepted": ["walks", "goes"], "label": "verb"}, {"accepted": ["shop"]}],
            "normalization": {},
            "word_bank": ["walks", "shop", "sleeps"],
        },
        "matching": {
            "pairs": [{"left": "cat", "right": "kedi"}, {"left": "dog", "right": "itek"}],
            "extra_rights": ["quus"],
        },
        "ordering": {"items": ["Once upon", "a time", "there was"], "separator": " ", "show_separator": True},
        "translation": {
            "source": "I would like a tea, please.",
            "accepted": ["cay istayiramen"],
            "normalization": {"ignore_punctuation": True},
            "source_language": "en",
            "target_language": "az",
            "max_words": 12,
        },
        "essay": {
            "min_words": 10,
            "max_words": 200,
            "rubric": [{"name": "Grammar", "max_score": 5.0, "guidance": "Accuracy"}],
            "guidance": "Write about your weekend.",
        },
    }[question_type]


def grade(question_type: str, response: Any, **kwargs) -> qe.GradeResult:
    return qe.grade(question_type, valid_config(question_type), response, **kwargs)


def short_config(accepted: list[str], **rules: Any) -> dict:
    return {"accepted": accepted, "normalization": rules}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_owns_the_type_vocabulary():
    assert qe.type_keys() == ALL_TYPES
    info = qe.registry_info()
    assert [item["type"] for item in info] == ALL_TYPES
    for item in info:
        assert item["group"] and item["answer_widget"]
        assert "label" not in item, "the vocabulary is a code; the screen puts it into words"
        assert item["config_schema"]["properties"], item["type"]
        assert item["supports_partial"] == (item["type"] in qe.PARTIAL_TYPES)
        assert item["gradable_automatically"] == (item["type"] not in qe.MANUAL_TYPES)


def test_descriptor_rejects_an_unknown_type_with_the_alternatives():
    with pytest.raises(ValueError) as exc:
        qe.descriptor("guess_the_word")
    message = str(exc.value)
    assert "guess_the_word" in message
    for key in ALL_TYPES:
        assert key in message


def test_is_known_and_expects_answer():
    assert qe.is_known("essay") and not qe.is_known("Essay")
    assert qe.expects_answer("multiple_choice") is True
    assert qe.expects_answer("essay") is False


@pytest.mark.parametrize("question_type", ALL_TYPES)
def test_a_valid_config_canonicalises_idempotently(question_type):
    original = valid_config(question_type)
    once = qe.validate_config(question_type, original)
    assert qe.validate_config(question_type, once) == once
    assert json.dumps(once)
    # Defaults are materialised, so a stored config always carries every key the
    # grader reads out of it.
    if question_type in ("short_answer", "gap_fill", "translation"):
        assert set(once["normalization"]) == {
            "case_insensitive",
            "trim",
            "collapse_spaces",
            "ignore_punctuation",
            "ignore_articles",
            "ignore_diacritics",
        }
    if question_type in ("multiple_choice", "multi_select"):
        assert all("correct" in option for option in once["options"])


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #

INVALID = [
    ("multiple_choice", {"options": [{"text": "a"}, {"text": "b"}]}, "exactly one correct"),
    ("multiple_choice", {"options": [{"text": "a", "correct": True}, {"text": "b", "correct": True}]}, "exactly one"),
    ("multiple_choice", {"options": [{"text": "a"}]}, "at least 2 items"),
    ("multiple_choice", {"options": []}, "at least 2 items"),
    ("multiple_choice", {"options": [{"text": "a", "correct": True}], "shuffle": True}, "Extra inputs"),
    ("multi_select", {"options": [{"text": "a", "correct": True}, {"text": "b", "correct": True}]}, "wrong option"),
    ("multi_select", {"options": [{"text": "a"}, {"text": "b"}]}, "at least one correct"),
    ("true_false", {"correct": "maybe"}, "valid boolean"),
    ("true_false", {"correct": True, "correct_answer": True}, "Extra inputs"),
    ("true_false", {}, "Field required"),
    ("short_answer", {"accepted": []}, "at least 1 item"),
    ("short_answer", {"accepted": [str(i) for i in range(41)]}, "at most 40 items"),
    ("short_answer", {"accepted": ["x"], "max_characters": 0}, "greater than or equal to 1"),
    ("gap_fill", {"text": "one ___ blank", "blanks": [{"accepted": ["a"]}, {"accepted": ["b"]}]}, "blank marker"),
    ("gap_fill", {"text": "no marker here", "blanks": [{"accepted": ["a"]}]}, "blank marker"),
    ("gap_fill", {"text": "a ___ b", "blanks": [{"accepted": ["   "]}]}, "non-empty accepted"),
    ("gap_fill", {"text": "a ___ b", "blanks": [{"accepted": ["x"]}], "word_bank": ["  "]}, "must not be blank"),
    ("matching", {"pairs": [{"left": "a", "right": "x"}, {"left": "A", "right": "y"}]}, "distinct"),
    ("matching", {"pairs": [{"left": "a", "right": "x"}]}, "at least 2 items"),
    ("matching", {"pairs": [{"left": "a", "right": "x"}, {"left": "b"}]}, "Field required"),
    ("ordering", {"items": ["only one"]}, "at least 2 items"),
    ("ordering", {"items": ["a", "b"], "separator": "way too long"}, "at most 10 characters"),
    ("translation", {"source": "hi", "accepted": []}, "at least 1 item"),
    ("translation", {"source": "", "accepted": ["x"]}, "at least 1 character"),
    ("essay", {"min_words": 50, "max_words": 10}, "below min_words"),
    ("essay", {"rubric": [{"name": "Accuracy", "max_score": 0}]}, "greater than"),
]


@pytest.mark.parametrize("question_type,config,expectation", INVALID)
def test_invalid_configs_are_refused_with_teacher_readable_text(question_type, config, expectation):
    with pytest.raises(ValueError) as exc:
        qe.validate_config(question_type, config)
    assert expectation.lower() in str(exc.value).lower()


@pytest.mark.parametrize("question_type", ALL_TYPES)
def test_a_non_object_config_is_refused(question_type):
    with pytest.raises(ValueError, match="must be a JSON object"):
        qe.validate_config(question_type, ["not", "a", "dict"])


def test_an_unknown_type_config_is_refused():
    with pytest.raises(ValueError, match="Unknown question type"):
        qe.validate_config("fill_in", {"text": "___"})


# --------------------------------------------------------------------------- #
# Scoring knobs
# --------------------------------------------------------------------------- #


def test_score_must_be_positive_and_types_and_knobs_must_agree():
    with pytest.raises(ValueError, match="greater than zero"):
        qe.validate_scoring("multiple_choice", 0, {}, {})
    with pytest.raises(ValueError, match="no partial credit"):
        qe.validate_scoring("multiple_choice", 1.0, {"mode": "partial"}, {})
    with pytest.raises(ValueError, match="must be 'all_or_nothing' or 'partial'"):
        qe.validate_scoring("multi_select", 1.0, {"mode": "half_credit"}, {})
    with pytest.raises(ValueError, match="no wrong answers to penalise"):
        qe.validate_scoring("ordering", 1.0, {}, {"penalty": 0.25})
    with pytest.raises(ValueError, match="between 0 and 1"):
        qe.validate_scoring("multi_select", 1.0, {}, {"penalty": 1.5})
    with pytest.raises(ValueError, match="unsupported scoring keys"):
        qe.validate_scoring("multi_select", 1.0, {"mode": "partial", "grace": 1}, {})
    qe.validate_scoring("multi_select", 2.0, {"mode": "partial"}, {"penalty": 0.25})
    qe.validate_scoring("multiple_choice", 2.0, {}, {})


def test_partial_knobs_are_allowed_for_every_partial_type():
    for question_type in sorted(qe.PARTIAL_TYPES):
        qe.validate_scoring(question_type, 1.0, {"mode": "all_or_nothing"}, {})


# --------------------------------------------------------------------------- #
# Grading: the maths
# --------------------------------------------------------------------------- #


def test_multiple_choice_full_and_zero_marks():
    result = grade("multiple_choice", {"option_index": 1}, score=4)
    assert (result.score, result.correct) == (4.0, True)
    assert result.detail["correct_index"] == 1
    wrong = grade("multiple_choice", {"option_index": 2}, score=4)
    assert (wrong.score, wrong.correct) == (0.0, False)
    # Out of range and absent answers score zero rather than raising.
    for response in ({"option_index": 9}, {"option_index": -1}, {"option_index": "1"}, None, "1", {}):
        assert grade("multiple_choice", response, score=4).score == 0.0


def test_multiple_choice_accepts_a_bare_option_index():
    assert grade("multiple_choice", 1, score=2).score == 2.0


def test_multi_select_partial_credit_and_penalty():
    full = grade("multi_select", {"option_indexes": [0, 2]}, score=4)
    assert (full.score, full.correct) == (4.0, True)

    half = grade("multi_select", {"option_indexes": [0]}, score=4)
    assert (half.score, half.correct) == (2.0, False)
    assert half.detail == {"hits": 1, "misses": 1, "wrong": 0}

    # By default one wrong tick cancels one right tick, so ticking both right answers
    # plus a distractor is worth half, not nearly everything.
    one_extra_pick = grade("multi_select", {"option_indexes": [0, 2, 1]}, score=4)
    assert (one_extra_pick.score, one_extra_pick.correct) == (2.0, False)
    assert one_extra_pick.detail == {"hits": 2, "misses": 0, "wrong": 1}

    # Ticking the whole list is the attack the default exists to stop.
    everything = grade("multi_select", {"option_indexes": [0, 1, 2, 3]}, score=4)
    assert (everything.score, everything.correct) == (0.0, False)

    # Positive-only marking is a teacher's choice, made explicitly.
    lenient = grade("multi_select", {"option_indexes": [0, 2, 1]}, score=4, negative_scoring={"penalty": 0})
    assert (lenient.score, lenient.correct) == (4.0, False)

    # A penalty at full strength cannot take the score below zero.
    punished = grade("multi_select", {"option_indexes": [1, 3]}, score=4, negative_scoring={"penalty": 1.0})
    assert punished.score == 0.0
    assert punished.detail == {"hits": 0, "misses": 2, "wrong": 2}


def test_multi_select_all_or_nothing():
    strict = grade("multi_select", {"option_indexes": [0, 2, 1]}, score=4, partial_scoring={"mode": "all_or_nothing"})
    assert strict.score == 0.0
    assert strict.correct is False
    perfect = grade("multi_select", {"option_indexes": [0, 2]}, score=4, partial_scoring={"mode": "all_or_nothing"})
    assert perfect.score == 4.0


def test_multi_select_refuses_repeated_and_out_of_range_indexes():
    for response in ({"option_indexes": [0, 0, 2]}, {"option_indexes": [0, 7]}, {"option_indexes": ["0", "2"]}, {"option_indexes": [{"a": 1}]}):
        result = grade("multi_select", response, score=4)
        assert (result.score, result.correct) == (0.0, False)
        assert result.detail["reason"] == "no_valid_answer"


def test_multi_select_an_empty_selection_scores_zero_without_a_reason():
    result = grade("multi_select", {"option_indexes": []}, score=4)
    assert (result.score, result.correct) == (0.0, False)
    assert result.detail == {"hits": 0, "misses": 2, "wrong": 0}


def test_true_false():
    assert grade("true_false", {"value": True}, score=1).correct is True
    wrong = grade("true_false", {"value": False}, score=1)
    assert (wrong.score, wrong.correct) == (0.0, False)
    assert wrong.detail["correct"] is True
    assert grade("true_false", {"value": "true"}, score=1).detail["reason"] == "no_valid_answer"
    assert grade("true_false", True, score=3).score == 3.0


def test_short_answer_normalization_defaults():
    assert grade("short_answer", {"text": "  COLOUR  "}, score=2).correct is True
    assert grade("short_answer", {"text": "colour!"}, score=2).correct is False
    assert grade("short_answer", {"text": "  "}, score=2).correct is False
    assert grade("short_answer", {"text": "x" * 60}, score=2).detail["reason"] == "too_long"
    assert grade("short_answer", {"text": 12}, score=2).detail["reason"] == "no_valid_answer"
    assert grade("short_answer", "colour", score=2).correct is True


def test_normalization_switches_are_honoured():
    def score_for(accepted: str, given: str, **rules: Any) -> bool | None:
        return qe.grade("short_answer", short_config([accepted], **rules), {"text": given}, score=1).correct

    assert score_for("the red apple", "red apple") is False
    assert score_for("the red apple", "red apple", ignore_articles=True) is True
    assert score_for("köpek", "kopek") is False
    assert score_for("köpek", "kopek", ignore_diacritics=True) is True
    assert score_for("not bad.", "not bad") is False
    assert score_for("not bad.", "not bad", ignore_punctuation=True) is True
    assert score_for("very good", "  very   good  ") is True
    assert score_for("very good", "  very   good  ", trim=False, collapse_spaces=False) is False
    assert score_for("DNA", "dna") is True
    assert score_for("DNA", "dna", case_insensitive=False) is False
    assert score_for("DNA", "DNA", case_insensitive=False) is True


def test_multiple_accepted_answers_are_all_owed():
    config = short_config(["colour", "color"])
    assert qe.grade("short_answer", config, {"text": "color"}, score=1).correct is True
    assert qe.grade("short_answer", config, {"text": "clour"}, score=1).correct is False


def test_gap_fill_per_blank_credit():
    result = grade("gap_fill", {"blanks": ["sleeps", "shop"]}, score=3)
    assert (result.score, result.correct) == (1.5, False)
    assert result.detail["marks"] == [False, True]
    assert result.detail["hits"] == 1

    full = grade("gap_fill", {"blanks": ["walks", "shop"]}, score=3)
    assert (full.score, full.correct) == (3.0, True)
    # An accepted alternative in the key earns the same mark.
    assert grade("gap_fill", {"blanks": ["goes", "shop"]}, score=3).correct is True

    # A short answer leaves the missing blanks ungraded rather than failing the request.
    short = grade("gap_fill", {"blanks": ["walks"]}, score=3)
    assert (short.score, short.correct) == (1.5, False)
    assert short.detail["given"] == ["walks", ""]

    strict = grade("gap_fill", {"blanks": ["walks"]}, score=3, partial_scoring={"mode": "all_or_nothing"})
    assert strict.score == 0.0
    assert grade("gap_fill", {"blanks": "walks"}, score=3).detail["reason"] == "no_valid_answer"


def test_gap_fill_uses_each_blank_own_answers():
    # "shop" is only accepted in the second blank, so the answers cannot be swapped.
    result = grade("gap_fill", {"blanks": ["shop", "walks"]}, score=2)
    assert (result.score, result.detail["hits"]) == (0.0, 0)


def published_matching(config: dict) -> tuple[list[dict], list[dict]]:
    public = qe.public_config("matching", config)
    return public["lefts"], public["rights"]


def matching_answer(config: dict, wanted: list[tuple[str, str]]) -> dict:
    """An answer built the way a learner builds one: from the published view only."""
    lefts, rights = published_matching(config)
    left_ref_of = {item["text"]: item["ref"] for item in lefts}
    right_ref_of = {item["text"]: item["ref"] for item in rights}
    return {
        "pairs": [{"left_ref": left_ref_of[left_text], "right_ref": right_ref_of[right_text]} for left_text, right_text in wanted]
    }


def test_matching_needs_one_entry_per_left_and_rejects_conflicts():
    config = valid_config("matching")
    complete = matching_answer(config, [("cat", "kedi"), ("dog", "itek")])
    result = grade("matching", complete, score=2)
    assert (result.score, result.correct) == (2.0, True)

    conflict = grade("matching", matching_answer(config, [("cat", "itek"), ("dog", "itek")]), score=2)
    assert (conflict.score, conflict.detail["reason"]) == (0.0, "answer_conflict")

    missing = grade("matching", matching_answer(config, [("cat", "kedi")]), score=2)
    assert missing.detail == {"reason": "answer_incomplete", "expected": 2, "given": 1}

    assert grade("matching", {"pairs": [{"left_ref": "nope", "right_ref": "nope"} for _ in range(2)]}, score=2).detail["reason"] == "no_valid_answer"
    # An unhashable reference must not crash the grader.
    assert grade("matching", {"pairs": [{"left_ref": [], "right_ref": {}} for _ in range(2)]}, score=2).detail["reason"] == "no_valid_answer"


def test_matching_partial_and_penalty():
    config = valid_config("matching")
    half = grade("matching", matching_answer(config, [("cat", "kedi"), ("dog", "quus")]), score=3)
    assert (half.score, half.correct) == (1.5, False)
    assert half.detail == {"hits": 1, "wrong": 1}

    punished = grade(
        "matching", matching_answer(config, [("cat", "kedi"), ("dog", "quus")]), score=3, negative_scoring={"penalty": 1.0}
    )
    assert punished.score == 0.0

    strict = grade(
        "matching", matching_answer(config, [("cat", "kedi"), ("dog", "quus")]), score=3, partial_scoring={"mode": "all_or_nothing"}
    )
    assert strict.score == 0.0
    assert strict.correct is False


def test_ordering_counts_positions_held():
    config = valid_config("ordering")
    tokens = qe.public_config("ordering", config)["tokens"]
    ref_of = {token["text"]: token["ref"] for token in tokens}
    ordered_refs = [ref_of[item] for item in config["items"]]

    perfect = grade("ordering", {"order": ordered_refs}, score=4)
    assert (perfect.score, perfect.correct) == (4.0, True)

    # Only the tokens sitting in their own slot keep their mark (6 dp, as the engine
    # rounds, so a score is never a repeating decimal).
    swapped_last_two = grade("ordering", {"order": [ordered_refs[0], ordered_refs[2], ordered_refs[1]]}, score=4)
    assert (swapped_last_two.score, swapped_last_two.detail["hits"]) == (1.333333, 1)

    for bad in (
        [ordered_refs[0], ordered_refs[0], ordered_refs[1]],
        [ordered_refs[0], ordered_refs[1]],
        ["unknown" + "x" * 20] * 3,
        [1, 2, 3],
    ):
        result = grade("ordering", {"order": bad}, score=4)
        assert (result.score, result.correct) == (0.0, False)

    assert grade("ordering", "not a list", score=4).detail["reason"] == "no_valid_answer"
    strict = grade(
        "ordering", {"order": [ordered_refs[1], ordered_refs[0], ordered_refs[2]]}, score=4, partial_scoring={"mode": "all_or_nothing"}
    )
    assert strict.score == 0.0


def test_translation_word_limit_and_accepted_forms():
    ok = grade("translation", {"text": "  cay  istayiramen.  "}, score=2)
    assert (ok.score, ok.correct) == (2.0, True)
    assert grade("translation", {"text": "cay istayiramen"}, score=2).correct is True
    # A missing or extra word is a different sentence, not a spelling variant.
    assert grade("translation", {"text": "cay istayiramen men"}, score=2).correct is False
    too_long = grade("translation", {"text": " ".join(["word"] * 30)}, score=2)
    assert too_long.detail == {"reason": "too_many_words", "limit": 12}
    assert grade("translation", {"text": ""}, score=2).correct is False
    assert grade("translation", 5, score=2).detail["reason"] == "no_valid_answer"


def test_essay_is_always_handed_to_a_teacher():
    result = grade("essay", {"text": " ".join(["word"] * 20)}, score=10)
    assert (result.score, result.correct, result.requires_manual) == (0.0, None, True)
    assert result.detail["word_count"] == 20
    assert "flagged" not in result.detail

    assert grade("essay", {"text": "   "}, score=10).detail["flagged"] == "empty"
    assert grade("essay", {"text": "one two three"}, score=10).detail["flagged"] == "below_minimum"
    long_essay = grade("essay", {"text": " ".join(["w"] * 201)}, score=10)
    assert (long_essay.detail["flagged"], long_essay.detail["word_count"]) == ("above_maximum", 201)


# --------------------------------------------------------------------------- #
# The answer key must stay private
# --------------------------------------------------------------------------- #


def _values(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        out: list[Any] = list(payload.values())
        for value in payload.values():
            out.extend(_values(value))
        return out
    if isinstance(payload, list):
        out = []
        for value in payload:
            out.extend(_values(value))
        return out
    return [payload]


def _keys(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        found = set(payload)
        for value in payload.values():
            found |= _keys(value)
        return found
    if isinstance(payload, list):
        return set().union(*[_keys(value) for value in payload]) if payload else set()
    return set()


@pytest.mark.parametrize("question_type", ALL_TYPES)
def test_public_view_carries_no_answer_key_fields(question_type):
    public = qe.public_config(question_type, valid_config(question_type))
    keys = _keys(public)
    assert "correct" not in keys
    assert "accepted" not in keys
    assert "blanks" not in keys or question_type == "gap_fill"
    assert json.dumps(public)


def test_public_choice_view_keeps_indexes_but_not_correctness():
    public = qe.public_config("multiple_choice", valid_config("multiple_choice"))
    assert [option["index"] for option in public["options"]] == [0, 1, 2]
    assert [option["text"] for option in public["options"]] == ["cat", "dog", "fish"]
    assert _keys(public) == {"kind", "options", "index", "text"}
    multi = qe.public_config("multi_select", valid_config("multi_select"))
    assert multi["multi"] is True


def test_public_true_false_hides_the_answer():
    assert qe.public_config("true_false", valid_config("true_false")) == {
        "kind": "boolean",
        "statement": "Baku is a capital city.",
    }


def test_public_text_views_hide_accepted_answers():
    short = qe.public_config("short_answer", valid_config("short_answer"))
    assert short == {"kind": "text", "max_characters": 40, "hint": "British spelling", "accepted_count": 1}

    translation = qe.public_config("translation", valid_config("translation"))
    assert "cay istayiramen" not in _values(translation)
    assert translation["accepted_count"] == 1
    assert translation["source"] == "I would like a tea, please."

    gap = qe.public_config("gap_fill", valid_config("gap_fill"))
    assert [blank["index"] for blank in gap["blanks"]] == [0, 1]
    assert _keys(gap["blanks"][0]) == {"index", "label"}
    assert gap["blanks"][0]["label"] == "verb"
    assert gap["text"] == "She ___ to the ___ every morning."
    # A word bank is deliberate scaffolding, so it is published; the per-blank answers
    # are not, even where a word is not in the bank.
    assert gap["word_bank"] == ["walks", "shop", "sleeps"]
    assert "goes" not in _values(gap)


def test_public_matching_publishes_only_opaque_refs():
    config = valid_config("matching")
    public = qe.public_config("matching", config)
    assert _keys(public) == {"kind", "lefts", "rights", "ref", "text"}
    refs = [item["ref"] for item in public["lefts"] + public["rights"]]
    assert len(refs) == len(set(refs)) == 5
    for ref in refs:
        assert len(ref) == 12 and not ref.isdigit()
    left_map, right_map = qe._matching_maps(config)
    # Every authored element is published exactly once, and the distractor is published
    # like any other right: marking it would hand the learner the process of elimination.
    assert sorted(right_map[item["ref"]] for item in public["rights"]) == [0, 1, 2]
    assert {item["text"] for item in public["rights"]} == {"kedi", "itek", "quus"}
    # Pairing the two columns as they stand - first with first, second with second -
    # must not be the key.
    straight = sum(
        1
        for left, right in zip(public["lefts"], public["rights"], strict=False)
        if left_map[left["ref"]] == right_map[right["ref"]]
    )
    assert straight == 0, "answering straight down must score nothing"
    assert [item["text"] for item in public["lefts"]] != [pair["left"] for pair in config["pairs"]]


def test_public_ordering_does_not_leak_the_sequence():
    config = valid_config("ordering")
    public = qe.public_config("ordering", config)
    assert [token["text"] for token in public["tokens"]] != config["items"]
    assert _keys(public) == {"kind", "separator", "show_separator", "tokens", "ref", "text"}
    assert public["show_separator"] is True
    # Reading the tokens in the published order must not reproduce the story.
    item_map = qe._ordering_map(config)
    aligned = sum(
        1 for position, token in enumerate(public["tokens"]) if item_map[token["ref"]] == position
    )
    assert aligned < len(public["tokens"])


def test_the_published_order_is_deterministic():
    """Same config, same display order: a re-opened question cannot reshuffle itself."""
    for question_type in ("matching", "ordering"):
        config = valid_config(question_type)
        first = qe.public_config(question_type, config)
        assert qe.public_config(question_type, config) == first
        assert qe.public_config(question_type, dict(config)) == first


def test_refs_are_stable_and_content_derived():
    config = valid_config("ordering")
    first = [token["ref"] for token in qe.public_config("ordering", config)["tokens"]]
    second = [token["ref"] for token in qe.public_config("ordering", dict(config))["tokens"]]
    assert first == second
    changed = dict(config, items=["Once upon", "a time", "and they lived"])
    assert [token["ref"] for token in qe.public_config("ordering", changed)["tokens"]] != first


def test_identical_texts_in_one_question_keep_distinct_refs():
    config = qe.validate_config("matching", {"pairs": [{"left": "a", "right": "same"}, {"left": "b", "right": "same"}]})
    public = qe.public_config("matching", config)
    rights = [item["ref"] for item in public["rights"]]
    assert len(rights) == len(set(rights)) == 2
    lefts = {item["text"]: item["ref"] for item in public["lefts"]}
    # The two identical rights are different elements, so tying both lefts to the one
    # the learner clicked is a conflict, not a pair of correct answers.
    both_on_one = qe.grade(
        "matching",
        config,
        {"pairs": [{"left_ref": lefts["a"], "right_ref": rights[0]}, {"left_ref": lefts["b"], "right_ref": rights[0]}]},
        score=2,
    )
    assert both_on_one.detail["reason"] == "answer_conflict"
    # Which published ref was authored first is what decides correctness, never the
    # text: the two ways to hand out the two identical rights score differently.
    right_ref_of = {index: ref for ref, index in qe._matching_maps(config)[1].items()}
    keyed = qe.grade(
        "matching",
        config,
        {
            "pairs": [
                {"left_ref": lefts["a"], "right_ref": right_ref_of[0]},
                {"left_ref": lefts["b"], "right_ref": right_ref_of[1]},
            ]
        },
        score=2,
    )
    assert (keyed.score, keyed.correct, keyed.detail["hits"]) == (2.0, True, 2)
    swapped = qe.grade(
        "matching",
        config,
        {
            "pairs": [
                {"left_ref": lefts["a"], "right_ref": right_ref_of[1]},
                {"left_ref": lefts["b"], "right_ref": right_ref_of[0]},
            ]
        },
        score=2,
    )
    assert (swapped.score, swapped.correct, swapped.detail["hits"]) == (0.0, False, 0)


# --------------------------------------------------------------------------- #
# Snapshot grading (the path Phase 7 will use)
# --------------------------------------------------------------------------- #


def snapshot(question_type: str, *, score: float = 2.0) -> dict:
    return {
        "snapshot_schema": 1,
        "question": {
            "type": question_type,
            "config": valid_config(question_type),
            "score": score,
            "partial_scoring": {},
            "negative_scoring": {},
        },
    }


def test_grading_a_snapshot_needs_no_database():
    result = qe.grade_snapshot(snapshot("multiple_choice"), {"option_index": 1})
    assert (result.score, result.max_score, result.correct) == (2.0, 2.0, True)


def test_a_frozen_snapshot_keeps_scoring_the_way_it_did():
    old = snapshot("multiple_choice")
    new = snapshot("multiple_choice")
    new["question"]["config"]["options"][0]["correct"] = True
    new["question"]["config"]["options"][1]["correct"] = False

    assert qe.grade_snapshot(old, {"option_index": 1}).correct is True
    assert qe.grade_snapshot(new, {"option_index": 1}).correct is False
    assert qe.grade_snapshot(old, {"option_index": 1}).score == 2.0


def test_grade_snapshot_accepts_a_bare_question_payload():
    assert qe.grade_snapshot(snapshot("true_false")["question"], True).correct is True


def test_grade_snapshot_carries_the_scoring_knobs_from_the_row():
    frozen = snapshot("multi_select", score=4.0)
    frozen["question"]["partial_scoring"] = {"mode": "all_or_nothing"}
    assert qe.grade_snapshot(frozen, {"option_indexes": [0, 2, 1]}).score == 0.0
    frozen["question"]["partial_scoring"] = {}
    frozen["question"]["negative_scoring"] = {"penalty": 0.5}
    assert qe.grade_snapshot(frozen, {"option_indexes": [0, 2, 1]}).score == 3.0


def test_grade_snapshot_grades_matching_through_the_published_refs():
    frozen = snapshot("matching", score=2.0)
    answer = matching_answer(frozen["question"]["config"], [("cat", "kedi"), ("dog", "itek")])
    assert qe.grade_snapshot(frozen, answer).correct is True


@pytest.mark.parametrize("question_type", ALL_TYPES)
def test_the_engine_never_raises_on_any_response_shape(question_type):
    hostile: list[Any] = [
        None,
        {},
        [],
        "text",
        0,
        1,
        True,
        {"value": [[1]]},
        {"option_index": {"nested": True}},
        {"option_indexes": [{"a": 1}]},
        {"blanks": [{"a": 1}]},
        {"order": [[], {}]},
        {"pairs": ["not a dict"]},
        {"pairs": [{"left_ref": "a" * 12, "right_ref": None}]},
        {"text": 1},
        {"value": {"deep": {"nested": [1, 2]}}},
    ]
    for response in hostile:
        result = grade(question_type, response, score=1)
        assert 0.0 <= result.score <= 1.0
        assert set(result.as_dict()) == {"score", "max_score", "correct", "requires_manual", "detail"}
        assert json.dumps(result.as_dict())
