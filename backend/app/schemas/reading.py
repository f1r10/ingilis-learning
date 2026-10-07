"""Reading passages (Phase 5): a text and the question sets filed under it.

A reading is one body of text with a title, a language, a CEFR band and a layout, plus
the `reading_question_set` rows `passage_service` manages. Two fields are the server's,
not the client's:

* `word_count` is a deterministic count of the text the teacher just saved. A number a
  browser typed in is a number that drifts on the next edit, and it is the figure a
  teacher uses to judge whether a text suits the lesson.
* `status` is absent from every body here. Lifecycle goes through the status endpoint,
  which can then refuse to treat the trash as a status.

Nothing is invented: a passage with no level stays without one, and a set with no
questions is reported as one, because an empty set is what the teacher left behind and
the editor has to show it.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.passage import (
    MAX_BODY_CHARACTERS,
    LearnerSetRead,
    QuestionSetRead,
    QuestionStubRead,
    normalize_language,
)


def _trimmed(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


class ReadingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=400)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARACTERS)
    language: str | None = Field(default=None, max_length=16)
    level: str | None = Field(default=None, max_length=32)
    layout: str | None = Field(default=None, max_length=32)
    status: str = "ready"

    @field_validator("title", "body")
    @classmethod
    def _text(cls, v: str) -> str:
        return _trimmed(v) or ""

    @field_validator("title", "body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("a reading needs both a title and a text")
        return v

    @field_validator("language")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        return normalize_language(v)

    @field_validator("level")
    @classmethod
    def _band(cls, v: str | None) -> str | None:
        return _trimmed(v) or None


class ReadingUpdate(BaseModel):
    """PATCH body. A field that is absent is not touched.

    `word_count` is refused rather than ignored: a client that recomputed it locally and
    sent it would be telling the server what the text says, and the two would disagree
    after the next edit.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=400)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_BODY_CHARACTERS)
    language: str | None = Field(default=None, max_length=16)
    level: str | None = Field(default=None, max_length=32)
    layout: str | None = Field(default=None, max_length=32)

    @field_validator("title", "body", "level")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return _trimmed(v)

    @field_validator("language")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        return normalize_language(v)


class ReadingSummary(BaseModel):
    """One row of the reading list. `excerpt` is the opening of the text, cut for a row."""

    id: UUID
    title: str
    excerpt: str
    language: str | None
    level: str | None
    layout: str
    status: str
    word_count: int | None
    set_count: int
    question_count: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ReadingRead(BaseModel):
    """The editor's payload: the text, its sets, and the questions not filed yet."""

    id: UUID
    title: str
    body: str
    language: str | None
    level: str | None
    layout: str
    status: str
    word_count: int | None
    source_file_id: UUID | None
    set_count: int
    question_count: int
    sets: list[QuestionSetRead] = Field(default_factory=list)
    unfiled: list[QuestionStubRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ReadingLearnerSummary(BaseModel):
    """One row of the learner's reading list: enough to choose a text, nothing else.

    `question_count` counts the questions a learner can actually answer, and there is no
    set count: a learner sees the sets on the next screen, and a number that disagrees
    with the list under it because one question is still a draft is worse than no number.
    """

    id: UUID
    title: str
    excerpt: str
    language: str | None
    level: str | None
    layout: str
    word_count: int | None
    question_count: int


class ReadingLearnerRead(BaseModel):
    """What a learner is shown: the text and the answerable questions, nothing else.

    No provenance, no timestamps and no draft questions - a learner is handed an exercise
    or nothing at all.
    """

    id: UUID
    title: str
    body: str
    language: str | None
    level: str | None
    layout: str
    sets: list[LearnerSetRead] = Field(default_factory=list)
    unfiled: list[dict] = Field(default_factory=list)
