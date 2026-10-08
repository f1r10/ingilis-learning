"""Exams: an assessed run with its own rules, frozen content and assigned audience.

A catalog is practice material and an exam is an assessment; the two share the *shape* of
their content (both hold references into the central banks and never a copy) and differ in
everything else, which is why they are separate tables and separate screens:

* an `ExamItem` pins a `QuestionVersion`, so a mark awarded last term is still reproducible
  after the question has been edited a dozen times;
* an exam carries timing, attempt, navigation, feedback and scoring rules that a practice
  list has no use for, and the server - not the learner's browser - decides when its clock
  runs out;
* an exam is *assigned*. A catalog is opened by whoever can see it.

The rule fields are validated rather than trusted, because each one changes what a learner's
screen does: `duration_minutes` and `available_from/to` decide whether a run can start and
when it ends, `result_visibility` and `feedback_timing` decide what they are told and when,
`max_attempts` decides how many times they may try, and `tab_switch_action` decides what an
interruption costs them.

`grading_mode` accepts `automatic` and `manual` here and nowhere else. `ai_assisted` is a
real value of the stored enum, but the assistant that would fill it in belongs to the
local-AI phase; an exam published in that mode today would promise a teacher a queue of
suggestions that never arrives.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.passage import normalize_language

#: References in one exam. An exam is a sitting, not an archive: past a couple of hundred
#: items the teacher means several papers, and a paper the learner cannot finish is not a
#: measurement of anything.
MAX_ITEMS_PER_EXAM = 200
MAX_TITLE_CHARACTERS = 300
MAX_DESCRIPTION_CHARACTERS = 4_000
#: Minutes. Six hundred is a full day's sitting in most rooms; a longer number is almost
#: always a teacher meaning "no time limit", which is what leaving this empty does.
MAX_DURATION_MINUTES = 600
MAX_ATTEMPTS = 20
#: Tab switches the teacher is prepared to tolerate before something happens.
MAX_TAB_SWITCH_LIMIT = 100

#: Lifecycle states an exam may be moved to by the teacher. `TRASH` is never a status:
#: binning sets `deleted_at`, exactly as it does for every other content type.
SETTABLE_STATUSES = ("draft", "scheduled", "active", "finished", "archived")

#: What may be added to a paper. `question` is the only thing a learner actually answers,
#: so `reading` and `listening` are authoring shortcuts: naming a text or a recording adds
#: the questions filed under it (or under one block of it), each pinned to its own version.
#: Vocabulary is missing on purpose: a word card has no answer to grade.
ITEM_KINDS = ("question", "reading", "listening")

#: The kind an `exam_item` row is stored as. Every answer in an attempt is keyed by one item,
#: so an item is exactly one answerable question - a passage reaches the paper through the
#: questions that belong to it, and the text itself is served with them as context.
STORED_ITEM_KIND = "question"

#: When a learner is told whether an answer was right, *while they are sitting*.
FEEDBACK_TIMINGS = ("instant", "after_session")

#: When the total is revealed after the paper is handed in. `after_approval` waits for the
#: teacher to finish the review queue, `hidden` never shows it to the learner.
RESULT_VISIBILITIES = ("immediate", "after_close", "after_approval", "hidden")

#: Grading modes an exam may be authored and published in. See the module docstring for why
#: the stored `ai_assisted` value is not one of them yet.
GRADING_MODES = ("automatic", "manual")

#: What the server does when a learner leaves the exam tab and comes back.
TAB_SWITCH_ACTIONS = ("warn", "flag", "auto_submit")

VIEWS = ("bank", "trash", "all")

#: What may be done to a whole selection of exams. There is no bulk edit: the rules on a
#: paper decide what its learners experience, and rules that change in bulk belong to the
#: editor, not to a toolbar.
BULK_ACTIONS = ("status", "trash", "restore")

#: Section configuration an exam accepts. Anything else is a field nobody owns.
SECTION_CONFIG_KEYS = ("instructions",)


def _trimmed(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


def _as_utc(value: datetime | None) -> datetime | None:
    """Availability windows are compared against the server clock, so they are stored aware.

    A naive timestamp would be read as UTC by the driver and shown to a teacher as a
    different hour than the one they typed; the platform's own clock is UTC, so a naive
    value is taken to mean that and stamped with it.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class ExamRules(BaseModel):
    """The settings a paper runs on, shared by creation and update.

    Declaring them once means a rule cannot be added to one of the two screens and missed by
    the other, which is how a timer ends up running by one clock in the editor and another in
    the runner.
    """

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARACTERS)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARACTERS)
    learning_language: str | None = Field(default=None, max_length=16)
    level: str | None = Field(default=None, max_length=32)

    available_from: datetime | None = None
    available_to: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=MAX_DURATION_MINUTES)
    must_finish_before_close: bool = False
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS)
    #: A percentage of the paper's own total, because that is how a teacher says it.
    passing_score: float | None = Field(default=None, ge=0, le=100)

    shuffle_questions: bool = False
    shuffle_options: bool = False

    resume_after_disconnect: bool = True
    restrict_copy_paste: bool = False
    monitor_tab_switch: bool = False
    tab_switch_limit: int | None = Field(default=None, ge=1, le=MAX_TAB_SWITCH_LIMIT)
    tab_switch_action: str | None = None

    allow_previous: bool = True

    feedback_timing: str = "after_session"
    show_correct_answers: bool = False
    show_explanations: bool = False
    result_visibility: str = "after_close"

    partial_scoring_enabled: bool = True
    negative_marking_enabled: bool = False
    grading_mode: str = "automatic"
    auto_submit_on_expiry: bool = True

    @field_validator("title", "description", "level")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return _trimmed(v)

    @field_validator("learning_language")
    @classmethod
    def _language(cls, v: str | None) -> str | None:
        return normalize_language(v)

    @field_validator("available_from", "available_to")
    @classmethod
    def _stamp(cls, v: datetime | None) -> datetime | None:
        return _as_utc(v)

    @field_validator("feedback_timing")
    @classmethod
    def _timing(cls, v: str) -> str:
        if v not in FEEDBACK_TIMINGS:
            raise ValueError("feedback_timing must be one of: " + ", ".join(FEEDBACK_TIMINGS))
        return v

    @field_validator("result_visibility")
    @classmethod
    def _visibility(cls, v: str) -> str:
        if v not in RESULT_VISIBILITIES:
            raise ValueError("result_visibility must be one of: " + ", ".join(RESULT_VISIBILITIES))
        return v

    @field_validator("grading_mode")
    @classmethod
    def _grading(cls, v: str) -> str:
        if v not in GRADING_MODES:
            raise ValueError("grading_mode must be one of: " + ", ".join(GRADING_MODES))
        return v

    @field_validator("tab_switch_action")
    @classmethod
    def _switch_action(cls, v: str | None) -> str | None:
        if v is None or not v:
            return None
        if v not in TAB_SWITCH_ACTIONS:
            raise ValueError("tab_switch_action must be one of: " + ", ".join(TAB_SWITCH_ACTIONS))
        return v

    @model_validator(mode="after")
    def _window(self) -> "ExamRules":
        if self.available_from and self.available_to and self.available_to <= self.available_from:
            raise ValueError("the closing time has to be after the opening time")
        if self.monitor_tab_switch and self.tab_switch_action is None:
            # Watching without deciding what happens is a rule that does nothing, and a
            # teacher would have to guess which of the two fields they had left empty.
            raise ValueError("switch monitoring needs an action to take")
        if self.tab_switch_limit is not None and not self.monitor_tab_switch:
            raise ValueError("a switch limit only means something while monitoring is on")
        return self


class ExamCreate(ExamRules):
    """A new paper. It is born a draft: an exam with no items and no rules checked cannot be
    assigned, and the lifecycle endpoint is the only door to a state that learners see."""

    model_config = ConfigDict(extra="forbid")

    status: str = "draft"

    @field_validator("status")
    @classmethod
    def _born_state(cls, v: str) -> str:
        if v not in ("draft", "scheduled"):
            raise ValueError("an exam starts as a draft or a scheduled paper")
        return v

    @field_validator("title")
    @classmethod
    def _titled(cls, v: str | None) -> str:
        if not v:
            raise ValueError("an exam needs a title")
        return v


class ExamUpdate(ExamRules):
    """Rules changed on an existing paper.

    Only the fields the teacher actually sent are applied, and `title` is optional here: a
    rename is one call and a timer change is another.
    """

    model_config = ConfigDict(extra="forbid")


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str

    @field_validator("status")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in SETTABLE_STATUSES:
            raise ValueError("status must be one of: " + ", ".join(SETTABLE_STATUSES))
        return v


class SectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    instructions: str | None = Field(default=None, max_length=2_000)
    position: int | None = Field(default=None, ge=0)
    shuffle_items: bool = False

    @field_validator("title", "instructions")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return _trimmed(v)


class SectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    instructions: str | None = Field(default=None, max_length=2_000)
    shuffle_items: bool | None = None

    @field_validator("title", "instructions")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return _trimmed(v)


class ItemAdd(BaseModel):
    """One thing to add to a paper, with the version it is pinned to.

    `version` names a `QuestionVersion` of that question. Left out, the paper pins whatever is
    current at the moment the item is added - and after that the exam never follows the bank,
    which is the whole point of the difference between an exam and a catalog.

    For `reading` and `listening` the reference is a passage, and `set_id` narrows it to one
    block of it; the questions that belong to what was named are what actually land on the
    paper, each pinned to its own current version. `version` and `points` do not apply to a
    passage, because a passage is not worth one mark.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    ref_id: UUID
    section_id: UUID | None = None
    version: int | None = Field(default=None, ge=1)
    #: The block of a reading or recording to take the questions from. Left out, every ready
    #: question of the whole text is added.
    set_id: UUID | None = None
    points: float | None = Field(default=None, gt=0, le=1000)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in ITEM_KINDS:
            raise ValueError("kind must be one of: " + ", ".join(ITEM_KINDS))
        return v

    @model_validator(mode="after")
    def _one_mark_one_question(self) -> "ItemAdd":
        if self.kind != "question" and (self.version is not None or self.points is not None):
            raise ValueError(
                "a reading or recording is added as its questions: mark and version belong to each of them"
            )
        return self


class ItemsAdd(BaseModel):
    """Several references at once, so a teacher can select a block of questions and add them."""

    model_config = ConfigDict(extra="forbid")

    items: list[ItemAdd] = Field(min_length=1, max_length=100)


class ItemUpdate(BaseModel):
    """An item's mark, or which section it sits in.

    The reference itself and the version it was pinned to never change: an answer already
    graded against version 3 has to stay graded against version 3, whatever the bank does next.
    """

    model_config = ConfigDict(extra="forbid")

    points: float | None = Field(default=None, gt=0, le=1000)
    section_id: UUID | None = None

    @model_validator(mode="after")
    def _something(self) -> "ItemUpdate":
        # `section_id: null` is a change - it moves the line out of its part and back to the top
        # of the paper - so the test is whether the caller named the field, not what they set.
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        return self


class ReorderRequest(BaseModel):
    """The complete new order of a paper's items, or of one section's items."""

    model_config = ConfigDict(extra="forbid")

    item_ids: list[UUID] = Field(min_length=1)
    section_id: UUID | None = None


class SectionReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_ids: list[UUID] = Field(min_length=1)


class AssignmentCreate(BaseModel):
    """Who this paper is for. Both lists may arrive empty; the exam service is what refuses it.

    A student and a group can both be named; the learner's own list shows one row per exam
    whatever number of routes reach them.

    The empty hand-out is deliberately not refused here. A pydantic complaint reaches the screen
    as the structural `validation_failed` message it was written in - English - while
    `exam_no_audience` is a code the learner-facing rule already carries, so the teacher who
    posts nothing gets told which rule they met, in their own language.
    """

    model_config = ConfigDict(extra="forbid")

    student_ids: list[UUID] = Field(default_factory=list)
    group_ids: list[UUID] = Field(default_factory=list)


class ExamBulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exam_ids: list[UUID] = Field(min_length=1, max_length=500)
    action: str
    status: str | None = None

    @field_validator("action")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in BULK_ACTIONS:
            raise ValueError("action must be one of: " + ", ".join(BULK_ACTIONS))
        return v

    @field_validator("status")
    @classmethod
    def _state(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in SETTABLE_STATUSES:
            raise ValueError("status must be one of: " + ", ".join(SETTABLE_STATUSES))
        return v

    @model_validator(mode="after")
    def _status_needed(self) -> "ExamBulkRequest":
        if self.action == "status" and self.status is None:
            raise ValueError("a status action needs the status to set")
        return self


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


class ExamItemRead(BaseModel):
    id: UUID
    kind: str
    ref_id: UUID
    position: int
    points: float | None = None
    #: The mark the question carries at the version this paper pinned, and the mark the
    #: teacher overrode, if they did. `effective_points` is what a total is made of.
    question_score: float | None = None
    effective_points: float = 0.0
    #: The pinned version, and whether the bank has moved on since the paper was made. A
    #: teacher needs to see both: "this paper asks version 3 of that question" is the fact,
    #: and "the bank is now on version 7" is the reason they may want a new paper.
    version: int | None = None
    current_version: int | None = None
    section_id: UUID | None = None
    title: str | None = None
    detail: str | None = None
    #: The text or recording this question travels with, when it has one. `context_kind` is
    #: the question's own relationship (`reading_bound` / `listening_bound` / `independent`),
    #: and the passage it names is served to the learner as context, never as a separate item.
    context_kind: str | None = None
    context_id: UUID | None = None
    context_title: str | None = None
    state: str = "ready"
    #: False when the paper cannot be published or served because this reference points at
    #: content that is draft, trashed or gone - or a bound question whose own text is not
    #: open to learners.
    serveable: bool = True


class ExamSectionRead(BaseModel):
    id: UUID
    title: str | None = None
    position: int
    shuffle_items: bool
    instructions: str | None = None
    item_count: int = 0
    points: float = 0.0


class ExamRead(BaseModel):
    id: UUID
    title: str
    description: str | None = None
    status: str
    learning_language: str | None = None
    level: str | None = None
    available_from: str | None = None
    available_to: str | None = None
    duration_minutes: int | None = None
    must_finish_before_close: bool
    max_attempts: int | None = None
    passing_score: float | None = None
    shuffle_questions: bool
    shuffle_options: bool
    resume_after_disconnect: bool
    restrict_copy_paste: bool
    monitor_tab_switch: bool
    tab_switch_limit: int | None = None
    tab_switch_action: str | None = None
    allow_previous: bool
    feedback_timing: str
    show_correct_answers: bool
    show_explanations: bool
    result_visibility: str
    partial_scoring_enabled: bool
    negative_marking_enabled: bool
    grading_mode: str
    auto_submit_on_expiry: bool
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    #: Derived, never stored: what the items add up to and what kind of paper this is.
    item_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    points: float = 0.0
    sections: list[ExamSectionRead] = Field(default_factory=list)
    items: list[ExamItemRead] = Field(default_factory=list)
    assignments: list["AssignmentRead"] = Field(default_factory=list)
    attempt_count: int = 0
    #: False while the paper has no attempts: the composition is still editable. The screen
    #: says why the controls are closed rather than letting the teacher discover it.
    composition_locked: bool = False
    publish_blockers: list[dict] = Field(default_factory=list)


class ExamRowRead(BaseModel):
    """One exam in the teacher's list, without its items.

    The list shows what a paper is and how far it has got; the items belong to the editor,
    and a hundred-item paper would otherwise make the list page the heaviest screen in the
    school.
    """

    id: UUID
    title: str
    status: str
    learning_language: str | None = None
    level: str | None = None
    available_from: str | None = None
    available_to: str | None = None
    duration_minutes: int | None = None
    item_count: int = 0
    points: float = 0.0
    assigned_count: int = 0
    attempt_count: int = 0
    submitted_count: int = 0
    #: Answers still waiting for a teacher: the number that makes the grading queue a queue.
    review_count: int = 0
    created_at: str
    deleted_at: str | None = None


class AssignmentRead(BaseModel):
    id: UUID
    exam_id: UUID
    student_id: UUID | None = None
    group_id: UUID | None = None
    #: Who or what was named, read from its own row: a list of ids is a list the teacher
    #: cannot check.
    name: str | None = None
    kind: str = "student"
    #: `False` when the student or group has since been binned, so the row can be shown as
    #: no longer reachable instead of quietly naming nobody.
    reachable: bool = True
    #: The learners this row reaches - one for a named student, the class for a named group - so
    #: the hand-out picker can mark somebody who already gets the paper through their group
    #: rather than offering them as somebody still to assign.
    member_ids: list[UUID] = Field(default_factory=list)
    attempts: int = 0
    submitted: int = 0
    created_at: str


class ExamMeta(BaseModel):
    """The option lists behind the editor's dropdowns, from the code that enforces them."""

    statuses: list[str] = Field(default_factory=list)
    kinds: list[dict[str, str]] = Field(default_factory=list)
    feedback_timings: list[str] = Field(default_factory=list)
    result_visibilities: list[str] = Field(default_factory=list)
    grading_modes: list[dict[str, Any]] = Field(default_factory=list)
    tab_switch_actions: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    limits: dict[str, int] = Field(default_factory=dict)
    views: list[str] = Field(default_factory=list)
    bulk_actions: list[str] = Field(default_factory=list)


class PreviewItemRead(BaseModel):
    """One item of the composition a learner would be served, in the order they would meet it."""

    position: int
    kind: str
    ref_id: UUID
    exam_item_id: UUID
    title: str | None = None
    version: int | None = None
    points: float = 0.0
    section_title: str | None = None
    context_id: UUID | None = None
    context_title: str | None = None
    #: The authored option indexes in the order the learner would see them, when this paper
    #: shuffles options. Shown as numbers because the teacher's screen prints the words from
    #: the question itself; what matters here is that the order is decided by the server and
    #: frozen for the sitting, so a resumed attempt shows the same layout.
    shuffled_options: list[int] | None = None


class ExamPreviewRead(BaseModel):
    """What a run would look like, without starting one.

    The order and the option shuffles are drawn from the paper's own rules with a seed the
    caller can pass in, because "shuffle the questions" is a rule a teacher is entitled to
    see the effect of before a learner ever meets it. Nothing is written: no attempt, no
    event, no timer.
    """

    exam_id: UUID
    title: str
    seed: str
    item_count: int
    points: float
    duration_minutes: int | None = None
    opens_at: str | None = None
    closes_at: str | None = None
    items: list[PreviewItemRead] = Field(default_factory=list)
    #: Items the preview had to leave out, with the reason in the teacher's words - a draft
    #: question, a deleted block. A preview that silently drops them would be a paper that
    #: looks fine and then serves less.
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class ExamListRead(BaseModel):
    items: list[ExamRowRead]
    total: int
    page: int
    page_size: int


ExamRead.model_rebuild()
