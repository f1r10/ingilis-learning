"""Offline tests for the vocabulary bank rules (Phase 4).

These cover the decisions that do not need a database: what a status word may be, how
a list the teacher typed gets cleaned, which language codes are allowed to meet each
other, and - most importantly - which fields a learner payload is allowed to contain.
The last group is the leak guard: teacher notes and import provenance are columns on
the same row as the study card, so a projection that grows a field by accident has to
fail here rather than in front of a class.

The database half (duplicate races, filters, restore collisions, learner visibility)
is in `tests/integration/test_vocabulary_db.py`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import enums
from app.schemas import vocabulary as v
from app.services import settings_service
from app.services import vocabulary_service as vs


def ui_config(**overrides):
    """The shape `settings_service.get_ui_config` returns, with defaults from config."""

    def _fake(_db):
        async def _coro():
            return {
                "enabled_ui_languages": ["az", "en", "ru", "tr"],
                "default_ui_language": "az",
                "learning_languages": ["en"],
                "translation_languages": ["az", "ru", "tr"],
                **overrides,
            }

        return _coro()

    return _fake


@pytest.fixture
def configured(monkeypatch):
    """Pin the language lists so a rule test never depends on a local `.env`."""

    def _apply(**overrides):
        monkeypatch.setattr(settings_service, "get_ui_config", ui_config(**overrides))

    return _apply


def translation(language: str, value: str = "meaning") -> SimpleNamespace:
    return SimpleNamespace(language=language, value=value)


def example(sentence: str, language: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(sentence=sentence, language=language, translation=None)


# --------------------------------------------------------------------------- #
# Status words
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["draft", "ready", "archived"])
def test_a_teacher_may_set_exactly_the_three_lifecycle_states(raw: str) -> None:
    assert vs.status_enum(raw) == enums.ContentStatus(raw)


def test_trash_is_a_deletion_and_never_a_status() -> None:
    """One source of truth for the trash: `deleted_at`.

    Accepting "trash" as a status would let a row be ready and trashed at the same
    time, and then nothing in the product could say which one a learner sees.
    """
    with pytest.raises(vs.VocabularyError, match="deletion, not a status"):
        vs.status_enum("trash")


def test_an_unknown_status_is_refused_with_the_list_the_teacher_needs() -> None:
    with pytest.raises(vs.VocabularyError) as exc:
        vs.status_enum("published")
    assert "draft" in str(exc.value) and "ready" in str(exc.value)


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


def test_synonym_lists_are_trimmed_blanked_out_and_deduplicated_in_order() -> None:
    body = v.VocabularyCreate(
        word="run",
        learning_language="en",
        synonyms=["  run ", "Run", "", "   ", "sprint", "dash", "DASH"],
    )
    assert body.synonyms == ["run", "sprint", "dash"], "typed order survives, repeats do not"


def test_a_learning_language_code_is_normalised_before_it_is_compared() -> None:
    """`AZ`, ` az ` and `az` are one language to a teacher and must be one to the bank."""
    assert v.VocabularyCreate(word="sual", learning_language=" AZ ").learning_language == "az"


def test_translation_and_example_languages_are_normalised_too() -> None:
    row = v.TranslationInput(language=" RU ", value="значення")
    card = v.ExampleInput(sentence="I run daily.", language=" AZ ")
    assert row.language == "ru"
    assert card.language == "az"


def test_a_misnamed_child_field_is_a_rejected_request_not_an_ignored_one() -> None:
    """`extra="forbid"`: a typo like `{"meaning": ...}` must fail loudly.

    Silently dropping it would store a translation with no value, and the teacher
    would only find out from a learner's blank card.
    """
    with pytest.raises(ValueError):
        v.TranslationInput.model_validate({"language": "az", "meaning": "yaxşı"})
    with pytest.raises(ValueError):
        v.ExampleInput.model_validate({"text": "I run daily."})


def test_the_patch_body_cannot_touch_status() -> None:
    assert "status" not in v.VocabularyUpdate.model_fields, (
        "lifecycle changes belong to the status endpoint, which can refuse trash"
    )


def test_a_bulk_request_names_an_action_the_service_actually_implements() -> None:
    with pytest.raises(ValueError):
        v.VocabularyBulkRequest(entry_ids=["2f2f2f2f-2f2f-2f2f-2f2f-2f2f2f2f2f2f"], action="delete_forever")


def test_a_bulk_request_needs_at_least_one_entry() -> None:
    with pytest.raises(ValueError):
        v.VocabularyBulkRequest(entry_ids=[], action="trash")


def test_a_bulk_action_reports_its_own_shape() -> None:
    body = v.VocabularyBulkRequest(
        entry_ids=["2f2f2f2f-2f2f-2f2f-2f2f-2f2f2f2f2f2f"], action="status", status="draft"
    )
    assert body.action == "status" and body.status == "draft"


# --------------------------------------------------------------------------- #
# Language rules
# --------------------------------------------------------------------------- #


async def test_the_learning_language_must_be_one_the_platform_teaches(configured) -> None:
    configured()
    with pytest.raises(vs.VocabularyError, match="not an enabled learning language"):
        await vs._check_language(None, learning_language="de", translations=[], examples=[])


async def test_a_translation_language_must_be_one_the_platform_offers(configured) -> None:
    configured()
    with pytest.raises(vs.VocabularyError, match="not an enabled translation language"):
        await vs._check_language(
            None, learning_language="en", translations=[translation("de")], examples=[]
        )


async def test_one_entry_keeps_one_meaning_per_language(configured) -> None:
    """Two Azerbaijani meanings on one card read as a broken bank, not as nuance."""
    configured()
    with pytest.raises(vs.VocabularyError, match="one meaning per language"):
        await vs._check_language(
            None,
            learning_language="en",
            translations=[translation("az", "bir"), translation("az", "iki")],
            examples=[],
        )


async def test_a_word_is_never_translated_into_its_own_language(configured) -> None:
    configured()
    with pytest.raises(vs.VocabularyError, match="cannot be translated into"):
        await vs._check_language(
            None, learning_language="en", translations=[translation("en", "self")], examples=[]
        )

    # Even when the admin list offers the learning language as a target: the entry is
    # still English-to-English, and that is the mistake worth naming.
    configured(translation_languages=["az", "en"])
    with pytest.raises(vs.VocabularyError, match="cannot be translated into"):
        await vs._check_language(
            None, learning_language="en", translations=[translation("en", "self")], examples=[]
        )


async def test_an_example_is_written_in_a_language_the_class_has(configured) -> None:
    configured()
    with pytest.raises(vs.VocabularyError, match="neither a learning nor a translation language"):
        await vs._check_language(
            None, learning_language="en", translations=[], examples=[example("Ich laufe.", "de")]
        )


async def test_an_example_may_be_unlabelled_or_in_either_configured_set(configured) -> None:
    configured()
    await vs._check_language(
        None,
        learning_language="en",
        translations=[translation("az"), translation("ru")],
        examples=[example("I run every morning.", None), example("Mən hər gün qaçıram.", "az"),
                  example("Rusca cümlə.", "ru")],
    )


async def test_the_configured_lists_come_from_the_database_not_a_client(configured) -> None:
    """A teacher who enables Turkish translation in Admin Settings changes the editor's
    options on the next request; nothing about the language set is baked into the app."""
    configured(translation_languages=["az", "tr"])
    options = await vs.language_options(None)
    assert options["translation_languages"] == ["az", "tr"]
    assert options["example_languages"] == ["az", "en", "tr"], "examples may use either set"
    assert options["levels"] == vs.LEVELS
    with pytest.raises(vs.VocabularyError, match="not an enabled translation language"):
        await vs._check_language(None, learning_language="en", translations=[translation("ru")], examples=[])


# --------------------------------------------------------------------------- #
# Learner projection - the leak guard
# --------------------------------------------------------------------------- #


def test_a_study_card_carries_no_teacher_notes_and_no_provenance() -> None:
    private = {"notes", "source_file_id", "audio_asset_id", "deleted_at", "status"}
    fields = set(v.VocabularyLearnerRead.model_fields)
    assert not (fields & private), f"learner payload exposes teacher/admin-only fields: {fields & private}"


def test_a_list_row_carries_no_teacher_notes() -> None:
    assert "notes" not in v.VocabularySummary.model_fields


def test_the_editable_fields_a_word_needs_are_all_present() -> None:
    """The read payload has to hand the editor everything it can write back, or the
    first patch would drop what the form never saw."""
    readable = set(v.VocabularyRead.model_fields)
    writable = set(v.VocabularyCreate.model_fields)
    # `tag_ids` is a write-time reference; a read hands back the resolved `tags`.
    assert writable - readable == {"tag_ids"}
    assert {"tags", "translations", "examples", "synonyms", "antonyms"} <= readable
    assert {f for f in writable if f != "tag_ids"} <= set(v.VocabularyUpdate.model_fields) | {"status"}


def test_the_learner_visibility_state_is_ready_and_alive() -> None:
    ready = SimpleNamespace(status=enums.ContentStatus.READY, deleted_at=None)
    draft = SimpleNamespace(status=enums.ContentStatus.DRAFT, deleted_at=None)
    trashed = SimpleNamespace(status=enums.ContentStatus.READY, deleted_at="yesterday")
    archived = SimpleNamespace(status=enums.ContentStatus.ARCHIVED, deleted_at=None)
    assert vs.is_learner_visible(ready)
    assert not any(vs.is_learner_visible(item) for item in (draft, trashed, archived))
