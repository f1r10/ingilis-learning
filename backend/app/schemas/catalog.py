"""Catalogs: an ordered list of references into the central banks (Phase 6).

A catalog is the teacher's own re-use of content they have already written. It holds
*references* - `catalog_item` names a kind and an id - and never a copy, so a word edited
in the vocabulary bank is the corrected word in every catalog that names it, and a catalog
cannot drift into being a second version of the same exercise. That is also why
`Catalog != Exam`: an exam freezes a `QuestionVersion` per item because a historical score
has to stay reproducible, and a catalog deliberately does neither - it is practice
material, always current, with nothing pinned.

Two fields carry the whole difference from the passage editors:

* `parent_id` makes a catalog a folder as well as a list, so a unit can be organised
  before it is published. Nesting is capped and cycle-checked on the way in, because a
  folder that contains itself is not a tree a learner's list can be built from.
* `meta.feedback_timing` says when a learner is told whether they were right. It is the
  only practice rule stored here that a runner obeys, and it is validated rather than
  trusted: an unknown word in that column would leave a screen deciding for itself.

`config` on an item is deliberately narrow. A reading or listening reference may name one
of its blocks, which is a real lesson move - "do block two of this recording again" - and
every other key is refused, because a JSON blob a client can fill in is a field nobody
owns.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.passage import StatusUpdate, normalize_language

__all__ = ["StatusUpdate", "normalize_language"]

#: Steps in one catalog. A practice collection is a lesson, not an archive: past a few
#: hundred the teacher means several catalogs, and the cap keeps one `to_read` response
#: small enough to render on a screen a learner is working through.
MAX_ITEMS_PER_CATALOG = 300
#: Folder levels below the root. Four is "unit > part > block" with room for one more, and
#: a deeper tree is a list the teacher cannot see on one screen.
MAX_DEPTH = 4
MAX_NAME_CHARACTERS = 300
MAX_DESCRIPTION_CHARACTERS = 4_000

#: The lifecycle words a teacher may set. TRASH is `deleted_at`, never a status.
SETTABLE_STATUSES = ("draft", "ready", "archived")

VIEWS = ("bank", "trash", "all")

#: What may be done to a whole selection of catalogs. There is no bulk *edit*: a name, a
#: description or the set of references inside a catalog is content, and content that
#: changes in bulk belongs to the review screens, not to a toolbar.
BULK_ACTIONS = ("status", "trash", "restore", "set_level")

#: The four banks a catalog can point at. `enums.ContentKind` is the stored truth; this
#: tuple is the same list in the order a picker shows its tabs.
ITEM_KINDS = ("question", "vocabulary", "reading", "listening")

#: When a learner is told the result of an answer. `instant` grades on submit;
#: `after_session` records the answer and withholds the verdict until the run is finished.
FEEDBACK_TIMINGS = ("instant", "after_session")

#: What a catalog row can be, from the reference's point of view. `missing` is a reference
#: to a row that no longer exists at all; `broken_block` is a passage whose referenced set
#: has been deleted. Both are shown rather than hidden, because a teacher needs to know
#: which catalog now has a hole in it.
ITEM_STATES = ("ready", "draft", "archived", "trashed", "missing", "broken_block")


def _trimmed(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


class CatalogCreate(BaseModel):
    """A new catalog, or a new folder."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARACTERS)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARACTERS)
    parent_id: UUID | None = None
    learning_language: str | None = Field(default=None, max_length=16)
    level: str | None = Field(default=None, max_length=32)
    shuffle_default: bool = True
    known_states_enabled: bool = False
    feedback_timing: str = "instant"
    #: The state a catalog is *born* in. Restricted to the states one can enter; the
    #: lifecycle endpoint is the only door from one to another.
    status: str = "draft"

    @field_validator("name", "description", "level")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return _trimmed(v)

    @field_validator("name")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v:
            raise ValueError("a catalog needs a name")
        return v

    @field_validator("status")
    @classmethod
    def _born_state(cls, v: str) -> str:
        if v not in SETTABLE_STATUSES:
            raise ValueError("status must be one of: " + ", ".join(SETTABLE_STATUSES))
        return v

    @field_validator("learning_language")
    @classmethod
    def _language(cls, v: str | None) -> str | None:
        return normalize_language(v)

    @field_validator("feedback_timing")
    @classmethod
    def _timing(cls, v: str) -> str:
        if v not in FEEDBACK_TIMINGS:
            raise ValueError("feedback_timing must be one of: " + ", ".join(FEEDBACK_TIMINGS))
        return v


class CatalogUpdate(BaseModel):
    """PATCH body: only the fields present are touched.

    No `status` - a lifecycle change is its own endpoint, so a body that meant to publish
    a catalog cannot quietly be a body that renamed it instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARACTERS)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARACTERS)
    parent_id: UUID | None = None
    learning_language: str | None = Field(default=None, max_length=16)
    level: str | None = Field(default=None, max_length=32)
    shuffle_default: bool | None = None
    known_states_enabled: bool | None = None
    feedback_timing: str | None = None

    @field_validator("name", "description", "level")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return _trimmed(v)

    @field_validator("learning_language")
    @classmethod
    def _language(cls, v: str | None) -> str | None:
        return normalize_language(v)

    @field_validator("feedback_timing")
    @classmethod
    def _timing(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in FEEDBACK_TIMINGS:
            raise ValueError("feedback_timing must be one of: " + ", ".join(FEEDBACK_TIMINGS))
        return v


class ItemConfig(BaseModel):
    """What one reference may say about itself.

    `set_id` selects a single block of a reading or listening. A question or a word has no
    blocks, so an item naming one of those kinds must leave it empty - and `extra="forbid"`
    means a key invented by a client is refused at the door instead of stored in JSONB
    where nothing ever reads it.
    """

    model_config = ConfigDict(extra="forbid")

    set_id: UUID | None = None

    def stored(self) -> dict:
        """The JSONB value: empty when nothing was chosen, never a `null` key."""
        return {"set_id": str(self.set_id)} if self.set_id else {}


class ItemAdd(BaseModel):
    """One reference being added to a catalog."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    ref_id: UUID
    config: ItemConfig = Field(default_factory=ItemConfig)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in ITEM_KINDS:
            raise ValueError("kind must be one of: " + ", ".join(ITEM_KINDS))
        return v


class ItemAddRequest(BaseModel):
    """Append references, in the order given, after whatever the catalog already holds."""

    model_config = ConfigDict(extra="forbid")

    items: list[ItemAdd] = Field(min_length=1, max_length=MAX_ITEMS_PER_CATALOG)

    @model_validator(mode="after")
    def _no_repeats(self) -> "ItemAddRequest":
        seen: set[tuple[str, UUID]] = set()
        for item in self.items:
            key = (item.kind, item.ref_id)
            if key in seen:
                raise ValueError("the same content cannot be added twice in one request")
            seen.add(key)
        return self


class ItemUpdate(BaseModel):
    """PATCH body for one reference: its block, or nothing."""

    model_config = ConfigDict(extra="forbid")

    config: ItemConfig


class ItemReorderRequest(BaseModel):
    """Every item of the catalog, in the order the teacher wants them."""

    model_config = ConfigDict(extra="forbid")

    item_ids: list[UUID] = Field(min_length=1, max_length=MAX_ITEMS_PER_CATALOG)

    @field_validator("item_ids")
    @classmethod
    def _once_each(cls, v: list[UUID]) -> list[UUID]:
        if len(set(v)) != len(v):
            raise ValueError("the same item cannot appear twice in one order")
        return v


class CatalogBulkRequest(BaseModel):
    """The same four operations over a selection, answered per id."""

    model_config = ConfigDict(extra="forbid")

    catalog_ids: list[UUID] = Field(min_length=1, max_length=500)
    action: str
    status: str | None = None
    level: str | None = Field(default=None, max_length=32)

    @field_validator("catalog_ids")
    @classmethod
    def _once_each(cls, v: list[UUID]) -> list[UUID]:
        if len(set(v)) != len(v):
            raise ValueError("the same catalog cannot appear twice in one request")
        return v

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        if v not in BULK_ACTIONS:
            raise ValueError("action must be one of: " + ", ".join(sorted(BULK_ACTIONS)))
        return v

    @field_validator("level")
    @classmethod
    def _trim_level(cls, v: str | None) -> str | None:
        stripped = _trimmed(v)
        return stripped or None


class CatalogItemRead(BaseModel):
    """One reference with the content it points at described, not copied.

    `title`, `detail` and `state` are read from the referenced row every time: they are
    what makes a catalog honest about itself. The exercise itself is never in here - a
    learner gets it from the bank's own learner projection, which is the only place the
    answer-key rules live.
    """

    id: UUID
    kind: str
    ref_id: UUID
    position: int
    config: dict = Field(default_factory=dict)
    title: str | None = None
    detail: str | None = None
    state: str = "missing"
    available_to_learner: bool = False


class CatalogSummary(BaseModel):
    id: UUID
    name: str
    description: str | None
    parent_id: UUID | None
    parent_name: str | None
    learning_language: str | None
    level: str | None
    shuffle_default: bool
    known_states_enabled: bool
    feedback_timing: str
    status: str
    item_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    #: How many references point at content a learner cannot currently use. The teacher's
    #: list shows this because it is the number that says "this catalog is not ready yet".
    #: `null` means the list did not count them - resolving every reference of every row on
    #: a page of fifty would be a hundred queries to avoid, and a `0` here would read as
    #: "nothing is missing" to a teacher whose catalog is full of it.
    unavailable_count: int | None = None
    child_count: int = 0
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class CatalogRead(CatalogSummary):
    #: Root-down list of the folders this catalog sits in, for a breadcrumb.
    path: list[dict] = Field(default_factory=list)
    items: list[CatalogItemRead] = Field(default_factory=list)
    #: Whether a learner can reach it right now: this catalog ready and un-trashed *and*
    #: every folder above it the same. The teacher's screen shows the lifecycle state and
    #: this, because "ready" on a folder whose parent is a draft is not the same answer.
    available_to_learner: bool = False
