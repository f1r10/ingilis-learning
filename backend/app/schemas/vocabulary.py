"""Vocabulary bank request/response schemas (Phase 4).

A word entry is one learning-language word plus everything a teacher decides belongs
to it: meaning, pronunciation, the languages it is translated into, and the example
sentences that show it in use. Translations and examples are child rows, so the
teacher edits them as part of the entry and the API replaces the whole set on a patch
that mentions them - a list that arrives without one of its translations would
otherwise silently keep a stale one.

Nothing here invents content: `enrichment` fields an AI provider may fill later
(Phase 12) are plain columns, and a null stays null.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_LIST_ITEMS = 50


def _language_code(value: str | None) -> str | None:
    """Language codes are compared against the configured lists, so `AZ`, ` az ` and
    `az` must mean one thing - otherwise a capitalised pick looks like a duplicate."""
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _clean_list(values: list[str]) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively, keep the typed order.

    `RUN, run` in a synonym list is one entry the teacher will never use twice, and
    a picker that shows both reads as a broken bank.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = (raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


class TranslationInput(BaseModel):
    """One meaning in one target language."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=1, max_length=16)
    value: str = Field(min_length=1)

    @field_validator("language")
    @classmethod
    def _code(cls, v: str) -> str:
        return _language_code(v) or ""


class ExampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentence: str = Field(min_length=1)
    language: str | None = Field(default=None, max_length=16)
    translation: str | None = None

    @field_validator("language")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        return _language_code(v)


class VocabularyCreate(BaseModel):
    word: str = Field(min_length=1, max_length=300)
    learning_language: str = Field(min_length=1, max_length=16)
    definition: str | None = None
    ipa: str | None = Field(default=None, max_length=200)
    part_of_speech: str | None = Field(default=None, max_length=64)
    level: str | None = Field(default=None, max_length=32)
    status: str = "ready"
    synonyms: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    antonyms: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    notes: str | None = None
    audio_asset_id: UUID | None = None
    translations: list[TranslationInput] = Field(default_factory=list, max_length=40)
    examples: list[ExampleInput] = Field(default_factory=list, max_length=40)
    tag_ids: list[UUID] = Field(default_factory=list)

    @field_validator("synonyms", "antonyms")
    @classmethod
    def _words(cls, v: list[str]) -> list[str]:
        return _clean_list(v)

    @field_validator("learning_language")
    @classmethod
    def _learning_code(cls, v: str) -> str:
        return _language_code(v) or ""


class VocabularyUpdate(BaseModel):
    """PATCH body. Only the fields present are touched.

    `status` is absent on purpose: lifecycle changes go through the status endpoint,
    which can then refuse to treat trash as a status. `learning_language` cannot be
    cleared, because a word with no language is a word that cannot be filed.

    Unknown keys are refused rather than dropped: a patch that sent `status` and got
    200 back would tell the teacher the word was published when it never was.
    """

    model_config = ConfigDict(extra="forbid")

    word: str | None = Field(default=None, min_length=1, max_length=300)
    learning_language: str | None = Field(default=None, min_length=1, max_length=16)
    definition: str | None = None
    ipa: str | None = Field(default=None, max_length=200)
    part_of_speech: str | None = Field(default=None, max_length=64)
    level: str | None = Field(default=None, max_length=32)
    synonyms: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    antonyms: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    notes: str | None = None
    audio_asset_id: UUID | None = None
    translations: list[TranslationInput] | None = Field(default=None, max_length=40)
    examples: list[ExampleInput] | None = Field(default=None, max_length=40)
    tag_ids: list[UUID] | None = None

    @field_validator("synonyms", "antonyms")
    @classmethod
    def _words(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_list(v)

    @field_validator("learning_language")
    @classmethod
    def _learning_code(cls, v: str | None) -> str | None:
        return _language_code(v)


class VocabularyStatusUpdate(BaseModel):
    status: str


class VocabularyTaxonomyAssignment(BaseModel):
    tag_ids: list[UUID]


class VocabularyBulkRequest(BaseModel):
    entry_ids: list[UUID] = Field(min_length=1, max_length=500)
    action: str
    status: str | None = None
    tag_id: UUID | None = None
    level: str | None = Field(default=None, max_length=32)

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        allowed = {"status", "trash", "restore", "add_tag", "remove_tag", "set_level"}
        if v not in allowed:
            raise ValueError(f"action must be one of: {', '.join(sorted(allowed))}")
        return v


class TranslationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    language: str
    value: str


class ExampleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sentence: str
    language: str | None
    translation: str | None


class TaxonomyRef(BaseModel):
    id: UUID
    name: str


class VocabularyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    word: str
    learning_language: str | None
    definition: str | None
    ipa: str | None
    part_of_speech: str | None
    level: str | None
    status: str
    synonyms: list[str]
    antonyms: list[str]
    notes: str | None
    audio_asset_id: UUID | None
    #: The address the pronunciation is served from, written by the server so a payload
    #: cannot invent its own media path (see `constants.media_content_url`).
    audio_url: str | None = None
    source_file_id: UUID | None
    translations: list[TranslationRead] = Field(default_factory=list)
    examples: list[ExampleRead] = Field(default_factory=list)
    tags: list[TaxonomyRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class VocabularySummary(BaseModel):
    id: UUID
    word: str
    learning_language: str | None
    definition: str | None
    ipa: str | None
    part_of_speech: str | None
    level: str | None
    status: str
    synonyms: list[str]
    translation_languages: list[str] = Field(default_factory=list)
    example_count: int = 0
    tag_names: list[str] = Field(default_factory=list)
    has_audio: bool = False
    has_source: bool = False
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class VocabularyLearnerRead(BaseModel):
    """What a learner is allowed to see: the entry without teacher-only notes."""

    id: UUID
    word: str
    learning_language: str | None
    level: str | None
    part_of_speech: str | None
    ipa: str | None
    definition: str | None
    synonyms: list[str] = Field(default_factory=list)
    antonyms: list[str] = Field(default_factory=list)
    translations: list[TranslationRead] = Field(default_factory=list)
    examples: list[ExampleRead] = Field(default_factory=list)
    has_audio: bool = False
    #: The learner's own address for the pronunciation: their session authorises every
    #: byte request to it, so a study card never carries the library's path.
    audio_url: str | None = None
    tags: list[TaxonomyRef] = Field(default_factory=list)
