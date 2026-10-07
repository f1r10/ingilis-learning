"""Listening items (Phase 5): a recording, its transcript, and the sets filed under it.

A listening is the audio-side twin of a reading: the recording itself lives in the media
library as a `MediaAsset` and is referenced, never copied, so one file can serve several
lessons and the library can still answer "is anything using this?".

Three fields are the server's:

* `transcript_source` says where the words came from - typed here, read in by the Phase 8
  importer, dictated by the Phase 12 speech adapter, or absent. It is derived from what
  this endpoint was actually sent, because a client that claims "imported" would leave a
  transcript with no honest provenance.
* `duration_seconds` on the *asset* is measured by a player, not guessed here.
* `status` is absent from these bodies; lifecycle has its own endpoint.

The playback rules (`replay_limit`, `allow_pause`, `allow_seek`, `show_transcript`) are
stored on the listening and served to the learner as delivered: they are the teacher's
control over the exercise, and a player that decided them itself would quietly change
the conditions of a lesson.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.media import MediaLearnerRead
from app.schemas.passage import (
    MAX_BODY_CHARACTERS,
    MAX_REPLAY_LIMIT,
    AudioRead,
    ListeningLearnerSetRead,
    ListeningSetRead,
    QuestionStubRead,
    normalize_language,
)


def _trimmed(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


class ListeningCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=400)
    media_asset_id: UUID | None = None
    language: str | None = Field(default=None, max_length=16)
    level: str | None = Field(default=None, max_length=32)
    transcript: str | None = Field(default=None, max_length=MAX_BODY_CHARACTERS)
    transcript_timestamps: list = Field(default_factory=list)
    replay_limit: int | None = Field(default=None, ge=0, le=MAX_REPLAY_LIMIT)
    allow_pause: bool = True
    allow_seek: bool = True
    show_transcript: bool = False
    status: str = "ready"

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        value = _trimmed(v) or ""
        if not value:
            raise ValueError("a recording needs a title")
        return value

    @field_validator("transcript")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return _trimmed(v)

    @field_validator("language")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        return normalize_language(v)

    @field_validator("level")
    @classmethod
    def _band(cls, v: str | None) -> str | None:
        return _trimmed(v) or None


class ListeningUpdate(BaseModel):
    """PATCH body. Only the fields present are touched.

    `media_asset_id` may be sent as `null` to detach a recording - the asset itself is
    untouched in the library, and the listening keeps its transcript. `transcript: null`
    clears the words and, with them, the provenance that claimed there were any.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=400)
    media_asset_id: UUID | None = None
    language: str | None = Field(default=None, max_length=16)
    level: str | None = Field(default=None, max_length=32)
    transcript: str | None = Field(default=None, max_length=MAX_BODY_CHARACTERS)
    transcript_timestamps: list | None = None
    replay_limit: int | None = Field(default=None, ge=0, le=MAX_REPLAY_LIMIT)
    allow_pause: bool | None = None
    allow_seek: bool | None = None
    show_transcript: bool | None = None

    @field_validator("title", "transcript", "level")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return _trimmed(v)

    @field_validator("language")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        return normalize_language(v)


class ListeningSummary(BaseModel):
    """One row of the recording list.

    `audio_state` is on the row because it is the one thing a teacher cannot tell from a
    title: a listening whose file has been thrown out of the library looks identical to
    one that is ready to play until somebody presses play.
    """

    id: UUID
    title: str
    language: str | None
    level: str | None
    status: str
    has_audio: bool
    audio_state: str | None
    duration_seconds: float | None
    has_transcript: bool
    transcript_source: str
    show_transcript: bool
    set_count: int
    question_count: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ListeningRead(BaseModel):
    """The editor's payload: playback rules, the file behind it, and the sets."""

    id: UUID
    title: str
    language: str | None
    level: str | None
    status: str
    audio: AudioRead | None
    transcript: str | None
    transcript_source: str
    transcript_timestamps: list = Field(default_factory=list)
    replay_limit: int | None
    allow_pause: bool
    allow_seek: bool
    show_transcript: bool
    source_file_id: UUID | None
    set_count: int
    question_count: int
    sets: list[ListeningSetRead] = Field(default_factory=list)
    unfiled: list[QuestionStubRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ListeningLearnerSummary(BaseModel):
    """One row of the learner's recording list.

    `question_count` counts answerable questions only, and there is no set count: the
    sets are on the next screen, and a number that disagrees with them because a question
    is still a draft is worse than no number.
    """

    id: UUID
    title: str
    language: str | None
    level: str | None
    duration_seconds: float | None
    has_audio: bool
    replay_limit: int | None
    show_transcript: bool
    question_count: int


class ListeningLearnerRead(BaseModel):
    """What a learner is shown: the player, the words if allowed, the questions.

    `audio` is the student-path projection of the asset, so the file is authorised by the
    learner's own session on every byte request. The transcript is `null` unless the
    teacher allowed it - a listening exercise whose words are on the screen is a reading
    exercise, and the teacher decided which one this is.
    """

    id: UUID
    title: str
    language: str | None
    level: str | None
    audio: MediaLearnerRead | None
    duration_seconds: float | None
    replay_limit: int | None
    allow_pause: bool
    allow_seek: bool
    show_transcript: bool
    transcript: str | None
    transcript_timestamps: list = Field(default_factory=list)
    sets: list[ListeningLearnerSetRead] = Field(default_factory=list)
    unfiled: list[dict] = Field(default_factory=list)
