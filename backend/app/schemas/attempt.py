"""Attempts: one learner sitting one paper, timed by the server.

Two shapes dominate everything below, and both come from the invariant that the server is
authoritative for exam timers.

* **The learner names a token, and the token names the sitting.** `session_id` is issued here
  when a run starts; `exam_id`, `attempt_number` and the frozen composition are read back from
  the row it points at. A body that let a client name the attempt it was answering would let a
  tab report an answer into somebody else's paper, and the score history would be a record of
  what the browser claimed.
* **Every read returns the clock, never a countdown the client computes.** `remaining_seconds`
  is produced from `expires_at - now()` on the way out, so a learner who changes device,
  closes a tab or moves their clock forward is still measured by the same deadline. `state`
  is also where an expired attempt is closed: the sweep that runs every fifteen seconds is a
  backstop, not the only path, and a submission that arrives after the deadline has to be
  graded by the rules that were in force when it passed.

What an answer returns is decided by `feedback_timing` (while sitting) and `result_visibility`
(afterwards), so a response carries the verdict only when the paper's own rules say the learner
may see it - and says so explicitly with `withheld`, because a blank where a mark was expected
reads as a bug.
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The attempt token is 32 hex characters issued by this backend when the sitting starts.
#: Checked on the way in so a token of somebody else's shape cannot reach a query at all.
ATTEMPT_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: Seconds a client may claim to have spent on one question. The bound is the longest a
#: single screen could plausibly be open in; a bigger number is a broken clock, and a
#: reported duration is only ever a fact about the learner's tab, not a measurement.
MAX_REPORTED_SECONDS = 86_400

#: How many answers one offline batch may carry: one sitting's worth, not an import.
MAX_BATCH_ANSWERS = 200

#: Reasons a paper can be closed, recorded on the attempt.
CLOSE_REASONS = ("submitted", "expired", "tab_limit")


def _token(value: str) -> str:
    if not ATTEMPT_TOKEN_PATTERN.match(value):
        raise ValueError("that is not an exam attempt")
    return value


class AttemptStart(BaseModel):
    """Opening a sitting of one assigned paper."""

    model_config = ConfigDict(extra="forbid")

    exam_id: UUID


class AnswerSave(BaseModel):
    """One answer, autosaved or submitted on the spot.

    `response` is deliberately untyped: its shape belongs to the question's type and is
    validated by the grader, which is the only code that knows what a correct `matching`
    body looks like. `exam_item_id` is the paper's own reference, not the question id,
    because one paper can ask the same question twice through two different versions.
    """

    model_config = ConfigDict(extra="forbid")

    exam_item_id: UUID
    response: Any = None
    time_spent_seconds: int | None = Field(default=None, ge=0, le=MAX_REPORTED_SECONDS)
    #: What the learner's clock said when they typed it. Stored for the timeline only; the
    #: deadline is always the server's.
    client_updated_at: Any = None


class AttemptAnswerRequest(AnswerSave):
    """An answer as it arrives on its own: the token plus one item."""

    token: str

    @field_validator("token")
    @classmethod
    def _shape(cls, v: str) -> str:
        return _token(v)


class AttemptSubmit(BaseModel):
    """Handing the paper in.

    `answers` lets a learner whose tab was offline send what it had buffered at the same
    moment; each one is stored under the same rules an autosave uses, and the paper is then
    graded once. Without it the attempt is closed with exactly what the server already holds.
    """

    model_config = ConfigDict(extra="forbid")

    token: str
    answers: list[AnswerSave] | None = Field(default=None, max_length=MAX_BATCH_ANSWERS)

    @field_validator("token")
    @classmethod
    def _shape(cls, v: str) -> str:
        return _token(v)


class TabSwitchReport(BaseModel):
    """The learner's tab left the paper and came back.

    A browser-reported count, recorded as such: it is the teacher's own switch limit applied
    to what the page could see, not a finding about what the learner did.
    """

    model_config = ConfigDict(extra="forbid")

    token: str

    @field_validator("token")
    @classmethod
    def _shape(cls, v: str) -> str:
        return _token(v)


class GradeAnswerRequest(BaseModel):
    """A teacher's verdict on one answer that the engine could not decide.

    `score` is against the answer's own maximum, and the maximum the teacher is shown comes
    from the pinned version - so a mark can only ever be given in the scale that question was
    written in.
    """

    model_config = ConfigDict(extra="forbid")

    score: float | None = Field(default=None, ge=0)
    correct: bool | None = None
    note: str | None = Field(default=None, max_length=4_000)

    @field_validator("note")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class FeedbackRequest(BaseModel):
    """Something the teacher wants the learner to read.

    `student_note` does not exist here on purpose: private notes belong to `student_note` and
    the student screens never project them.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=8_000)
    answer_id: UUID | None = None

    @field_validator("body")
    @classmethod
    def _written(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("feedback needs some words")
        return value


# --------------------------------------------------------------------------- #
# Reads: the learner's own screen
# --------------------------------------------------------------------------- #


class AttemptStepRead(BaseModel):
    """One thing to answer, in the order this sitting was frozen in.

    `view` is the bank's own learner projection of the *pinned version*, with the answer key
    removed, so a resumed paper shows exactly what the first one did. `saved` is the learner's
    own last answer coming back to them - never a verdict.
    """

    exam_item_id: UUID
    kind: str
    ref_id: UUID
    position: int
    section_id: UUID | None = None
    section_title: str | None = None
    points: float = 0.0
    #: The text or recording this question travels with, when it has one. The runner prints
    #: it once for each consecutive run of steps that share it, which is why the grouping key
    #: is handed over rather than dug out of the view.
    context_id: UUID | None = None
    view: dict = Field(default_factory=dict)
    saved: Any = None
    answered: bool = False


class AttemptRulesRead(BaseModel):
    """What the learner's runner has to obey, stated once by the server.

    The screen reads these rather than deciding for itself: whether they may go back and
    change an answer, whether the page should block copy and paste, whether leaving the tab
    is being watched and what it costs.
    """

    allow_previous: bool = True
    restrict_copy_paste: bool = False
    monitor_tab_switch: bool = False
    tab_switch_limit: int | None = None
    tab_switch_action: str | None = None
    resume_after_disconnect: bool = True
    feedback_timing: str = "after_session"
    auto_submit_on_expiry: bool = True


class AttemptRead(BaseModel):
    """The sitting as the server sees it. The clock in here is the only one that counts."""

    token: str
    attempt_id: UUID
    exam_id: UUID
    title: str
    status: str
    attempt_number: int
    max_attempts: int | None = None
    started_at: str
    expires_at: str | None = None
    #: Computed on every read from the server's clock and the stored deadline. `None` when the
    #: paper has no time limit; `0` when it is over.
    remaining_seconds: int | None = None
    server_seconds_used: int | None = None
    duration_minutes: int | None = None
    total_items: int = 0
    answered_items: int = 0
    points: float = 0.0
    tab_switches: int = 0
    rules: AttemptRulesRead = Field(default_factory=AttemptRulesRead)
    #: A word for what just happened to the paper, so a screen that resumed onto an expired
    #: attempt can say so instead of showing a form that will not accept answers.
    notice: str | None = None


class AttemptStepsRead(BaseModel):
    token: str
    status: str
    remaining_seconds: int | None = None
    steps: list[AttemptStepRead] = Field(default_factory=list)


class TabSwitchRead(BaseModel):
    """What one reported tab switch cost, under the paper's own rule.

    `notice` is the only judgement here: the count is the browser's, the limit and the action
    are the teacher's, and the platform never says what the learner looked at.
    """

    token: str
    tab_switches: int
    tab_switch_limit: int | None = None
    tab_switch_action: str | None = None
    status: str
    notice: str
    #: `true` only when this report is what closed the paper.
    auto_submitted: bool = False
    remaining_seconds: int | None = None


class AnswerRead(BaseModel):
    """What saving one answer returns."""

    recorded: bool = True
    token: str
    exam_item_id: UUID
    #: `true` when the verdict is being held back by the paper's feedback timing.
    withheld: bool = False
    correct: bool | None = None
    score: float | None = None
    max_score: float | None = None
    requires_manual: bool = False
    #: Seconds left, recomputed by the server on the way out.
    remaining_seconds: int | None = None
    #: A switch limit reached, or the deadline passed while the learner was typing.
    notice: str | None = None


class AttemptResultLine(BaseModel):
    exam_item_id: UUID
    prompt: str | None = None
    answered: bool = False
    correct: bool | None = None
    score: float = 0.0
    max_score: float = 0.0
    requires_manual: bool = False
    #: Only ever the question's own explanation, and only when the paper allows explanations.
    explanation: str | None = None
    #: What the teacher wrote when they gave this line its mark. A number on its own leaves the
    #: learner no wiser and gives the teacher no way to answer "why this mark".
    reviewer_note: str | None = None


class AttemptResultRead(BaseModel):
    """The learner's result screen, as far as the paper's visibility rules allow.

    `visible` is the whole answer to "can they see this yet": when it is false the totals are
    left out and `state` says which rule is holding them - waiting for the paper to close,
    waiting for the teacher, or not shown at all.
    """

    token: str
    exam_id: UUID
    title: str
    status: str
    submitted_at: str | None = None
    server_seconds_used: int | None = None
    result_visibility: str = "after_close"
    visible: bool = False
    #: `closed` | `awaiting_teacher` | `hidden` | `shown`. The learner's screen words it.
    state: str = "closed"
    #: What just happened, when this body came back from a submit rather than a plain read:
    #: `closed`, or `already_submitted` for a second press that must not look like a new result.
    notice: str | None = None
    score: float | None = None
    max_score: float | None = None
    passed: bool | None = None
    passing_score: float | None = None
    answered_items: int = 0
    total_items: int = 0
    correct_count: int = 0
    #: Lines that took part of their mark. `correct` is a two-valued flag, so a `2 of 3` answer is
    #: neither counted right nor worth calling wrong next to the mark it did get.
    partial_count: int = 0
    incorrect_count: int = 0
    manual_count: int = 0
    show_correct_answers: bool = False
    lines: list[AttemptResultLine] = Field(default_factory=list)
    feedback: list["FeedbackRead"] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Reads: the teacher's screens
# --------------------------------------------------------------------------- #


class AttemptRowRead(BaseModel):
    """One learner's sitting of one paper, as a row on the teacher's list."""

    id: UUID
    token: str | None = None
    exam_id: UUID
    student_id: UUID
    student_name: str | None = None
    status: str
    attempt_number: int
    started_at: str
    expires_at: str | None = None
    submitted_at: str | None = None
    remaining_seconds: int | None = None
    server_seconds_used: int | None = None
    answered_items: int = 0
    total_items: int = 0
    score: float | None = None
    max_score: float | None = None
    passed: bool | None = None
    tab_switches: int = 0
    needs_review: int = 0


class ManualReviewRead(BaseModel):
    """One answer waiting for a teacher.

    The queue is the worklist, so each row carries enough to work from without opening the
    attempt: whose answer it is, which paper, what they wrote, the mark it is worth and
    whether anything has been suggested yet.
    """

    id: UUID
    attempt_id: UUID
    answer_id: UUID
    exam_id: UUID
    exam_title: str
    student_id: UUID
    student_name: str | None = None
    question_id: UUID | None = None
    question_type: str | None = None
    prompt: str | None = None
    response: Any = None
    max_score: float = 0.0
    auto_score: float | None = None
    reviewed: bool = False
    final_score: float | None = None
    reviewer_note: str | None = None
    submitted_at: str | None = None
    #: A suggestion is only ever shown when some assistant actually produced one; the queue
    #: never invents a mark to look busy.
    ai_suggestion: dict | None = None


class AttemptAnswerRead(BaseModel):
    exam_item_id: UUID
    position: int
    kind: str
    prompt: str | None = None
    question_type: str | None = None
    version: int | None = None
    response: Any = None
    answered: bool = False
    correct: bool | None = None
    score: float | None = None
    max_score: float = 0.0
    graded: bool = False
    needs_manual_review: bool = False
    flagged: bool = False
    time_spent_seconds: int | None = None
    changed_count: int = 0


class AttemptDetailRead(BaseModel):
    """Everything the teacher sees about one sitting, including what the learner may not."""

    id: UUID
    token: str | None = None
    exam_id: UUID
    exam_title: str
    student_id: UUID
    student_name: str | None = None
    status: str
    attempt_number: int
    started_at: str
    expires_at: str | None = None
    submitted_at: str | None = None
    server_seconds_used: int | None = None
    score: float | None = None
    max_score: float | None = None
    passed: bool | None = None
    #: The threshold this sitting was frozen with, as a percentage of its own marks. `passed` is
    #: null while a line still waits for a teacher, and the screen has to tell that apart from a
    #: paper that never set a pass mark at all.
    passing_score: float | None = None
    tab_switches: int = 0
    #: What this paper's own rule was about leaving the tab, as data rather than as a sentence:
    #: the teacher's screen says it in their own interface language, and a count of tabs is never
    #: called evidence about what the learner opened.
    monitor_tab_switch: bool = False
    tab_switch_action: str | None = None
    tab_switch_limit: int | None = None
    answers: list[AttemptAnswerRead] = Field(default_factory=list)
    feedback: list["FeedbackRead"] = Field(default_factory=list)


class FeedbackRead(BaseModel):
    id: UUID
    attempt_id: UUID | None = None
    answer_id: UUID | None = None
    student_id: UUID
    body: str
    created_at: str


class LearnerExamRead(BaseModel):
    """One assigned paper on the learner's own list.

    No composition and no answer keys, but everything a learner needs to decide to start:
    whether it is open, how long they get, how many attempts they have left, and whether they
    have already sat it.
    """

    id: UUID
    title: str
    description: str | None = None
    level: str | None = None
    learning_language: str | None = None
    status: str
    available: bool = False
    #: `open` | `not_yet` | `closed` | `no_attempts_left` | `already_submitted`. The reason is
    #: the useful part; a list of papers a learner cannot open is not a list, it is a wall.
    availability_reason: str = "closed"
    opens_at: str | None = None
    closes_at: str | None = None
    duration_minutes: int | None = None
    item_count: int = 0
    points: float = 0.0
    max_attempts: int | None = None
    attempts_used: int = 0
    attempts_left: int | None = None
    best_score: float | None = None
    best_max_score: float | None = None
    latest_status: str | None = None
    #: `true` when a sitting is still open on this paper, so the list can offer "continue"
    #: rather than "start" and never ask the learner to begin a second one.
    resume_token: str | None = None
    result_visible: bool = False
    feedback_waiting: bool = False


class StudentExamBriefRead(BaseModel):
    """The page before the paper: the rules, stated plainly, and the start button."""

    id: UUID
    title: str
    description: str | None = None
    level: str | None = None
    item_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    points: float = 0.0
    duration_minutes: int | None = None
    opens_at: str | None = None
    closes_at: str | None = None
    must_finish_before_close: bool = False
    max_attempts: int | None = None
    attempts_used: int = 0
    attempts_left: int | None = None
    passing_score: float | None = None
    rules: AttemptRulesRead = Field(default_factory=AttemptRulesRead)
    available: bool = False
    availability_reason: str = "closed"
    resume_token: str | None = None


class StartRead(BaseModel):
    """A sitting opened, or picked up again."""

    token: str
    attempt_id: UUID
    exam_id: UUID
    title: str
    attempt_number: int
    max_attempts: int | None = None
    started_at: str
    expires_at: str | None = None
    remaining_seconds: int | None = None
    total_items: int = 0
    points: float = 0.0
    rules: AttemptRulesRead = Field(default_factory=AttemptRulesRead)
    #: `false` when this response opened a new sitting, `true` when the learner's existing one
    #: was handed back instead - which is what a second tab or a reconnect looks like.
    resumed: bool = False
    steps: list[AttemptStepRead] = Field(default_factory=list)


class AttemptMeta(BaseModel):
    """The words the attempt screens branch on, from the code that produces them."""

    statuses: list[str] = Field(default_factory=list)
    #: The learner's list also carries the *paper's* lifecycle word on every row, so the locale
    #: needs both sets: `active` is a paper, `in_progress` is a sitting of one.
    exam_statuses: list[str] = Field(default_factory=list)
    availability_reasons: list[str] = Field(default_factory=list)
    result_states: list[str] = Field(default_factory=list)
    close_reasons: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)


class LearnerAttemptsRead(BaseModel):
    """The learner's own history of one paper."""

    exam_id: UUID
    title: str
    attempts: list[AttemptRowRead] = Field(default_factory=list)


class GradingSummaryRead(BaseModel):
    """The teacher's grading queue as a whole: what is waiting, and on which paper."""

    pending: int = 0
    reviewed_today: int = 0
    exams: list[dict[str, Any]] = Field(default_factory=list)
    students: list[dict[str, Any]] = Field(default_factory=list)


# The two reads above name `FeedbackRead` before it exists; rebuilding resolves those forward
# references now that the whole file is defined.
AttemptResultRead.model_rebuild()
AttemptDetailRead.model_rebuild()
