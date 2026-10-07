"""Schemas shared by the reading and listening passages (Phase 5).

A passage is a text or a recording plus the question sets that hang off it. Reading and
listening differ in their body fields - a passage has a body, a recording has audio, a
transcript and playback rules - but the *grouping* of questions under a set is one
mechanism with one set of rules, so the shapes below are declared once and `passage_service`
implements them once.

Two things are absent from these bodies on purpose:

* `status`. Lifecycle moves go through a dedicated endpoint that can refuse to treat the
  trash as a status, the same way the vocabulary bank does.
* `position` on a created set. Order is decided by `reorder` and by appending, not by a
  client inventing an index that collides with the rows already there.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: A passage is a page of text, not a book. The cap keeps one editor request inside one
#: ordinary transaction; a longer document belongs to the Phase 8 importer.
MAX_BODY_CHARACTERS = 20_000
#: Question sets per passage and questions per set are small numbers by construction -
#: a reading block in an English lesson has a handful of sets and a handful of
#: questions under each. The caps exist so a bulk assignment cannot become a page of
#: ids that outlives the request that sent it.
MAX_SETS_PER_PASSAGE = 50
MAX_QUESTIONS_PER_SET = 200

#: How many times a learner may hear a recording before the player stops. `null` means
#: no limit, and the cap exists so "unlimited" cannot be typed as a huge number that
#: outlives the lesson it was meant for.
MAX_REPLAY_LIMIT = 50

#: What a teacher may do to a whole selection of passages. `set_level` is included
#: because re-filing a unit of texts by CEFR band is a real, reversible bulk action;
#: editing a body or a transcript in bulk is not, and has no entry point here.
BULK_ACTIONS = ("status", "trash", "restore", "set_level")


def normalize_language(value: str | None) -> str | None:
    """`EN`, ` en ` and `en` are one language, because the configured lists are lowercase.

    A capitalised pick that does not match the list reads back as "not an enabled
    language" to a teacher who typed the right letters.
    """
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None



def _unique_ids(values: list[UUID]) -> list[UUID]:
    """Refuse a repeat rather than silently folding it.

    The same question named twice in one assignment is a client bug, and answering 200
    while the set holds it once would tell the teacher their list had been saved.
    """
    if len(set(values)) != len(values):
        raise ValueError("the same id cannot appear twice in one request")
    return values


class QuestionSetCreate(BaseModel):
    """A new set under a passage."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    instructions: str | None = Field(default=None, max_length=4_000)
    config: dict = Field(default_factory=dict)

    @field_validator("title", "instructions")
    @classmethod
    def _trimmed(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @field_validator("title")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("a set needs a title")
        return v


class QuestionSetUpdate(BaseModel):
    """PATCH body for a set: only the fields present are touched."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    instructions: str | None = Field(default=None, max_length=4_000)
    config: dict | None = None

    @field_validator("title", "instructions")
    @classmethod
    def _trimmed(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class ListeningSetCreate(QuestionSetCreate):
    """A listening block, optionally a slice of the recording.

    The interval is a *question* boundary, not a media edit: the stored file is untouched
    and the player is told which seconds to play. `end_seconds` is the listening's own
    length or less once a browser has measured it, but it is not checked against the
    asset here, because this backend has no codec library and would be guessing.
    """

    start_seconds: float | None = Field(default=None, ge=0, le=86_400)
    end_seconds: float | None = Field(default=None, ge=0, le=86_400)

    @model_validator(mode="after")
    def _ordered(self) -> "ListeningSetCreate":
        if self.start_seconds is not None and self.end_seconds is not None:
            if self.end_seconds <= self.start_seconds:
                raise ValueError("the block must end after it starts")
        return self


class ListeningSetUpdate(QuestionSetUpdate):
    start_seconds: float | None = Field(default=None, ge=0, le=86_400)
    end_seconds: float | None = Field(default=None, ge=0, le=86_400)

    @model_validator(mode="after")
    def _ordered(self) -> "ListeningSetUpdate":
        if self.start_seconds is not None and self.end_seconds is not None:
            if self.end_seconds <= self.start_seconds:
                raise ValueError("the block must end after it starts")
        return self


class SetQuestionAssignment(BaseModel):
    """The ordered list of questions that belong under one set.

    This is a replacement, not an append: a set that arrives with three ids holds
    exactly those three questions in that order, and anything filed there before that is
    not in the list is unfiled - never deleted. A question already under a sibling
    set moves here, which is what dragging a question between blocks means.
    """

    model_config = ConfigDict(extra="forbid")

    question_ids: list[UUID] = Field(default_factory=list, max_length=MAX_QUESTIONS_PER_SET)

    @field_validator("question_ids")
    @classmethod
    def _once_each(cls, v: list[UUID]) -> list[UUID]:
        return _unique_ids(v)


class SetReorderRequest(BaseModel):
    """Every set of the passage, in the order the teacher wants them."""

    model_config = ConfigDict(extra="forbid")

    set_ids: list[UUID] = Field(min_length=1, max_length=MAX_SETS_PER_PASSAGE)

    @field_validator("set_ids")
    @classmethod
    def _once_each(cls, v: list[UUID]) -> list[UUID]:
        return _unique_ids(v)


class StatusUpdate(BaseModel):
    """One lifecycle word: draft, ready or archived. Never trash."""

    model_config = ConfigDict(extra="forbid")

    status: str


class PassageBulkRequest(BaseModel):
    """The same four operations, applied to a whole selection.

    Both passage screens offer exactly these, so the worker in later phases and the
    browser in front of a teacher send one shape. There is no bulk *edit* here: a body
    or a transcript is content, and content that changes in bulk belongs to the Phase 8
    importer, where each row is reviewed before it is published.
    """

    model_config = ConfigDict(extra="forbid")

    passage_ids: list[UUID] = Field(min_length=1, max_length=500)
    action: str
    status: str | None = None
    level: str | None = Field(default=None, max_length=32)

    @field_validator("passage_ids")
    @classmethod
    def _once_each(cls, v: list[UUID]) -> list[UUID]:
        return _unique_ids(v)

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        if v not in BULK_ACTIONS:
            raise ValueError("action must be one of: " + ", ".join(sorted(BULK_ACTIONS)))
        return v

    @field_validator("level")
    @classmethod
    def _trimmed(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None


class QuestionStubRead(BaseModel):
    """Enough of a question to recognise it in a set list - never the answer key."""

    id: UUID
    type: str
    prompt: str | None
    status: str
    score: float


class SetQuestionRead(QuestionStubRead):
    position: int


class QuestionSetRead(BaseModel):
    id: UUID
    title: str | None
    instructions: str | None
    position: int
    config: dict = Field(default_factory=dict)
    question_count: int = 0
    questions: list[SetQuestionRead] = Field(default_factory=list)


class ListeningSetRead(QuestionSetRead):
    start_seconds: float | None = None
    end_seconds: float | None = None


class LearnerSetRead(BaseModel):
    """A set as a learner sees it: where the block sits and what it asks.

    The set's own `config` is absent on purpose. It is the teacher's authoring data, and
    nothing on a learner's screen reads from it - so a key nobody uses is not shipped, and
    a note the teacher wrote for themselves stays theirs.
    """

    id: UUID
    title: str | None
    instructions: str | None
    position: int
    question_count: int = 0
    questions: list[dict] = Field(default_factory=list)


class ListeningLearnerSetRead(LearnerSetRead):
    """A listening block, with the slice of the recording it covers.

    The interval is learner-facing: it is what the player is told to play, and without it
    a block that asks about seconds 40 to 70 would be answered against the whole file.
    """

    start_seconds: float | None = None
    end_seconds: float | None = None


class AudioRead(BaseModel):
    """The recording attached to a listening item, as the teacher's screen shows it.

    `state` is the part the player cannot work out for itself: an asset can be live, in
    the trash, or gone from storage while the row still points at it. A teacher who sees
    "no file" and a title has one thing to check, and it is not the listening.
    """

    id: UUID
    kind: str
    mime_type: str | None
    label: str | None
    duration_seconds: float | None
    state: str
    content_url: str


def check_timestamps(rows: list) -> list:
    """Validate a cue list without pretending to own its format.

    A transcript cue is `{start: seconds, end: seconds?, text: string}`. The list is
    stored exactly as it arrives, because Phase 12's speech adapter and the Phase 8
    importer both write this shape, and re-serialising it here would lose a field neither
    of them told this module about. What is refused is the nonsense: a bare string, a
    number where a second should be, a negative offset.
    """
    if len(rows) > 5_000:
        raise ValueError("too many transcript lines for one request")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each transcript line must be an object with a time and a text")
        for field in ("start", "end"):
            value = row.get(field)
            if value is not None and not isinstance(value, (int, float)):
                raise ValueError(f"transcript {field} must be a number of seconds")
            if isinstance(value, (int, float)) and value < 0:
                raise ValueError(f"transcript {field} cannot be negative")
        text = row.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError("transcript text must be a string")
    return rows
