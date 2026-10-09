"""Document import request bodies (Phase 8).

A candidate is the document's own text, and the review screen is the only place where a
person's words join it. So an edit sends the whole candidate back rather than a patch:
the server holds the list of fields that kind may have, refuses anything else, and can
therefore tell a teacher afterwards which parts came from the file and which were typed.
Where an answer key is missing, that is what the review says - no schema here invents one.

`filing` is kept apart from the text for the same reason. Level, lifecycle and taxonomy
are a teacher's decision about the bank, never something extracted from a paper, so they
arrive as their own object and are stored in `import_item.filing`.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: How many candidates one bulk action may cover. A whole paper is a few hundred rows,
#: and a request that walks them one at a time would be a queue of round trips a teacher
#: cannot watch finish.
MAX_BULK_ITEMS = 300

#: The kinds a candidate may hold. `note` is "the importer could not classify this", and
#: it is the only kind that may be re-filed by a teacher rather than approved.
KINDS = ("question", "vocabulary", "reading", "note")

#: Which fields of a candidate a teacher may write, per kind. Everything here is a slot
#: the parsers already fill; a key outside the list is refused rather than stored, so
#: `extracted` can never quietly gain a field the platform does not show.
EDITABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "question": ("prompt", "options", "accepted", "answer_text", "level", "explanation"),
    "vocabulary": ("word", "definition", "examples", "level"),
    "reading": ("title", "body", "level"),
    "note": ("text",),
}

BULK_ACTIONS = ("approve", "reject", "file")


class ImportFiling(BaseModel):
    """Where approved content goes in the bank.

    `language` is the learning language: a word needs one before it can be filed at all,
    and a question or passage may stay unset until the teacher picks it.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(default="draft", max_length=32)
    level: str | None = Field(default=None, max_length=32)
    language: str | None = Field(default=None, max_length=16)
    topic_ids: list[UUID] = Field(default_factory=list, max_length=50)
    tag_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @field_validator("level", "language", "status")
    @classmethod
    def _trimmed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class ImportItemEdit(BaseModel):
    """The candidate as it should read, plus where it goes once approved.

    `extracted` replaces the whole dictionary of the candidate's own text. `kind` re-files
    a note as a question, word or passage: the text stays the document's, and only its
    shape changes.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str | None = Field(default=None, max_length=64)
    type: str | None = Field(default=None, max_length=64)
    extracted: dict | None = None
    filing: ImportFiling | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str | None) -> str | None:
        if v is not None and v not in KINDS:
            raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
        return v


class ImportItemDecision(BaseModel):
    """Approve or refuse one candidate.

    `filing` may be sent with an approval so the teacher decides the text and where it
    lands in a single action; it is stored on the item before the content is created,
    which is what keeps the provenance honest about who chose what.
    """

    model_config = ConfigDict(extra="forbid")

    approve: bool
    filing: ImportFiling | None = None


class ImportBulkRequest(BaseModel):
    """One action across a page of the review queue.

    `file` changes only the filing of rows still waiting - it is the teacher's answer to
    "this whole paper belongs at B1", and it creates nothing.
    """

    model_config = ConfigDict(extra="forbid")

    item_ids: list[UUID] = Field(min_length=1, max_length=MAX_BULK_ITEMS)
    action: str
    filing: ImportFiling | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        if v not in BULK_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(BULK_ACTIONS)}")
        return v
