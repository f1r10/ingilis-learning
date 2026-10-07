"""Media library request/response schemas (Phase 5).

There is deliberately no create schema with a `kind`, a `mime_type` or a `storage_key`:
an asset is created by uploading bytes, and what those bytes are is decided by reading
them (see `app.core.media_types`). A client that could name the type could name a
`text/html` body "image/png" and have the library believe it.

There is also no duration or pixel size on the read side that the server produced. This
backend has no codec library, so the only honest source for "this recording is 41
seconds" is the player that played it; `MediaMetadataPatch` is how a browser reports
that, and the rest of the library simply shows the number as unknown until it arrives.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: A day of audio is the longest thing this platform will describe with a number. Past
#: that, a value is a mistake or a junk field, not a lesson.
MAX_DURATION_SECONDS = 86_400
#: The largest dimension a browser reports. 30 000 px is already far beyond anything a
#: teacher photographs, and it stops a bogus number from reaching a player.
MAX_DIMENSION_PIXELS = 30_000
#: How many assets one bulk action may name. The list is answered per id, so the limit
#: is about keeping one request inside one request cycle, not about protecting a cap.
MAX_BULK_ASSETS = 500


class MediaMetadataPatch(BaseModel):
    """Report what the player measured, and/or rename the asset. Absent fields are
    left alone.

    `extra="forbid"`: a patch that sent `mime_type` and got a 200 back would tell the
    teacher the file was relabelled when the bytes never changed.
    """

    model_config = ConfigDict(extra="forbid")

    duration_seconds: float | None = Field(default=None, gt=0, lt=MAX_DURATION_SECONDS)
    width: int | None = Field(default=None, ge=1, le=MAX_DIMENSION_PIXELS)
    height: int | None = Field(default=None, ge=1, le=MAX_DIMENSION_PIXELS)
    label: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("label")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("a label needs text")
        return v


class MediaReferenceRead(BaseModel):
    """What live content points at this asset - the reason a trash can be refused."""

    words: int = 0
    listenings: int = 0
    questions: int = 0

    @property
    def total(self) -> int:
        return self.words + self.listenings + self.questions


class MediaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    mime_type: str | None
    format_label: str | None
    original_filename: str | None
    label: str | None
    size_bytes: int | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    source_origin: str | None
    #: "available" or "trashed": derived from `deleted_at`, never a second flag that
    #: could disagree with it.
    state: str
    content_url: str
    referenced_by: MediaReferenceRead
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class MediaSummary(BaseModel):
    """A library row. The full record is one request away; a list stays light."""

    id: UUID
    kind: str
    mime_type: str | None
    format_label: str | None
    original_filename: str | None
    label: str | None
    size_bytes: int | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    source_origin: str | None
    state: str
    content_url: str
    reference_count: int = 0
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class MediaLearnerRead(BaseModel):
    """What a learner's player is given: a URL and nothing about the library."""

    id: UUID
    kind: str
    mime_type: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    content_url: str


class MediaBulkRequest(BaseModel):
    asset_ids: list[UUID] = Field(min_length=1, max_length=MAX_BULK_ASSETS)
    action: str

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        allowed = {"trash", "restore"}
        if v not in allowed:
            raise ValueError(f"action must be one of: {', '.join(sorted(allowed))}")
        return v
