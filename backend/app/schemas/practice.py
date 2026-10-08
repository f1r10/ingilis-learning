"""Practice: the shapes a learner's run is built from and answered with (Phase 6).

Nothing here stores an answer. A run is a `session_id` the server issues, the answers are
`activity_event` rows carrying it, and every response below is projected from those rows on
request - which is why a run that was interrupted and picked up later shows the same
verdicts it would have shown the first time.

Two rules are visible in these bodies and are the reason they look the way they do:

* **A learner names the session, not the catalog.** `AnswerRequest` carries `session_id`
  and `question_id`; the catalog is read back from the start event the server wrote. A
  body that let the caller name the catalog would let one run's answers be filed against
  another catalog, and the history a teacher reads would be wrong.
* **`feedback_timing` decides what an answer returns.** With `after_session` the answer
  response says only that it was recorded, and the verdicts arrive with the run summary.
  The field is the teacher's, validated against the two words the runner understands.
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The run token is 32 hex characters issued by this backend. The pattern is checked on
#: the way in so a token of somebody else's shape cannot reach a query at all.
SESSION_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: What a learner may save for later. The two kinds the favorites table was built for:
#: an exercise to try again and a word to review. A whole reading or recording is a lesson,
#: and "saving" one is what a catalog is for.
FAVORITE_KINDS = ("question", "vocabulary")

#: The two marks a learner may put on a card. `activity_service` maps each to an event
#: type; the newest event for a card is the state the screens read back.
KNOWN_STATES = ("known", "learning")


class RunRequest(BaseModel):
    """Opening a run.

    `shuffle` overrides the catalog's default for this run only. The order is decided here
    rather than in the browser because the events are written against the steps served, and
    two clients shuffling differently would produce two histories that disagree about what
    came first.
    """

    model_config = ConfigDict(extra="forbid")

    shuffle: bool | None = None


class AnswerRequest(BaseModel):
    """One answer to one question of the run named by `session_id`.

    `response` is deliberately untyped: its shape belongs to the question's type and is
    validated by the grader, which is the only code that knows what a correct `matching`
    body looks like. Re-declaring that here would be a second definition of the engine.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(max_length=64)
    question_id: UUID
    response: Any = None
    #: Seconds the learner's own screen measured on this question. Reported, not inferred:
    #: a duration this backend cannot vouch for is stored as absent rather than estimated.
    time_spent_seconds: int | None = Field(default=None, ge=0, le=86_400)

    @field_validator("session_id")
    @classmethod
    def _token(cls, v: str) -> str:
        if not SESSION_TOKEN_PATTERN.match(v):
            raise ValueError("that is not a practice session")
        return v


class KnownStateRequest(BaseModel):
    """A learner's own mark on a word card: `known` or `learning`.

    Last mark wins, because the log keeps both rows and reads the newest.
    """

    model_config = ConfigDict(extra="forbid")

    ref_id: UUID
    state: str

    @field_validator("state")
    @classmethod
    def _known_word(cls, v: str) -> str:
        if v not in KNOWN_STATES:
            raise ValueError("state must be one of: " + ", ".join(KNOWN_STATES))
        return v


class FavoriteRequest(BaseModel):
    """Save something to the learner's own review list, or take it off again."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    ref_id: UUID

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in FAVORITE_KINDS:
            raise ValueError("kind must be one of: " + ", ".join(FAVORITE_KINDS))
        return v


class StepRead(BaseModel):
    """One thing to do in a run.

    `view` is the bank's own learner projection - a question with its key removed, a word
    card, a reading, a recording - because that projection is where the answer-key rules
    live and a second copy of them here would be a second place to get them wrong.
    """

    item_id: UUID
    kind: str
    ref_id: UUID
    position: int
    view: dict = Field(default_factory=dict)


class RunRead(BaseModel):
    session_id: str
    catalog_id: UUID
    catalog_name: str
    feedback_timing: str
    shuffle: bool
    known_states_enabled: bool
    #: How many references the catalog holds that could not be served right now. A learner
    #: is told the number, not the reason: "3 items are not available" is a fact about the
    #: lesson, while the state of somebody's draft is not theirs to read.
    skipped_count: int = 0
    steps: list[StepRead] = Field(default_factory=list)
    started_at: str


class AnswerRead(BaseModel):
    """What one answer returns, under the catalog's feedback timing."""

    recorded: bool = True
    session_id: str
    question_id: UUID
    #: `true` when the verdict is being held back until the run is finished. The learner is
    #: told that explicitly: a blank response where a mark was expected reads as a bug.
    withheld: bool = False
    correct: bool | None = None
    score: float | None = None
    max_score: float | None = None
    requires_manual: bool = False
    explanation: str | None = None


class ResultRead(BaseModel):
    question_id: UUID
    prompt: str | None = None
    correct: bool | None = None
    score: float = 0.0
    max_score: float = 0.0
    requires_manual: bool = False
    explanation: str | None = None
    answered_at: str | None = None


class RunSummaryRead(BaseModel):
    """The run, rebuilt from its own events."""

    session_id: str
    catalog_id: UUID
    catalog_name: str
    answered: int = 0
    correct_count: int = 0
    #: Lines that took part of their mark - not right, and not worth calling wrong either.
    partial_count: int = 0
    incorrect_count: int = 0
    #: Essays and anything else a teacher marks by hand: counted, never scored as wrong.
    manual_count: int = 0
    score: float = 0.0
    max_score: float = 0.0
    finished: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    results: list[ResultRead] = Field(default_factory=list)


class CatalogRowRead(BaseModel):
    """One catalog in a learner's practice list.

    No lifecycle state and no `parent_name`: a learner sees what they can open and what is
    inside it, and the folder structure is rendered from `parent_id` by their own screen.
    """

    id: UUID
    name: str
    description: str | None = None
    parent_id: UUID | None = None
    learning_language: str | None = None
    level: str | None = None
    item_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    child_count: int = 0
    shuffle_default: bool = True
    known_states_enabled: bool = False
    feedback_timing: str = "instant"
    #: How many runs this learner has already opened here. No score on the list row: the
    #: number that would go there lives in the run summary, and a list of twenty catalogs
    #: is not the place to grade anything.
    runs: int = 0


class KnownStateRead(BaseModel):
    ref_id: UUID
    state: str


class FavoriteRead(BaseModel):
    id: UUID
    kind: str
    ref_id: UUID
    #: What the saved thing is called, read from its own row: a review list of ids is a
    #: list the learner cannot use.
    title: str | None = None
    detail: str | None = None
    #: `false` when the content has since been binned or deleted, so the row can be shown
    #: as unavailable instead of opening into a 404.
    available: bool = True
    created_at: str
