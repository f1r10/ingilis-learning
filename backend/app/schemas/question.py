"""Question bank request/response schemas.

`config` is intentionally an open dict at the HTTP boundary: the shape is owned by
the question type registry (`app.services.question_engine`), which validates it and
returns teacher-readable errors. Duplicating that shape here would let the two
definitions drift.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaxonomyRef(BaseModel):
    id: UUID
    name: str


class QuestionCreate(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    prompt: str | None = None
    config: dict = Field(default_factory=dict)
    status: str = "ready"
    context_kind: str = "independent"
    reading_id: UUID | None = None
    listening_id: UUID | None = None
    score: float = Field(default=1.0, gt=0, le=1000)
    partial_scoring: dict = Field(default_factory=dict)
    negative_scoring: dict = Field(default_factory=dict)
    level: str | None = Field(default=None, max_length=32)
    difficulty: int | None = Field(default=None, ge=1, le=10)
    learning_language: str | None = Field(default=None, max_length=16)
    explanation: str | None = None
    teacher_notes: str | None = None
    media_asset_id: UUID | None = None
    topic_ids: list[UUID] = Field(default_factory=list)
    tag_ids: list[UUID] = Field(default_factory=list)
    change_note: str | None = Field(default=None, max_length=2000)


class QuestionUpdate(BaseModel):
    """PATCH body. Only the fields present are touched; an empty body changes nothing.

    `status` is absent on purpose: lifecycle changes go through the status endpoint,
    which can then refuse to treat trash as a status.
    """

    type: str | None = Field(default=None, min_length=1, max_length=64)
    prompt: str | None = None
    config: dict | None = None
    context_kind: str | None = None
    reading_id: UUID | None = None
    listening_id: UUID | None = None
    score: float | None = Field(default=None, gt=0, le=1000)
    partial_scoring: dict | None = None
    negative_scoring: dict | None = None
    level: str | None = Field(default=None, max_length=32)
    difficulty: int | None = Field(default=None, ge=1, le=10)
    learning_language: str | None = Field(default=None, max_length=16)
    explanation: str | None = None
    teacher_notes: str | None = None
    media_asset_id: UUID | None = None
    topic_ids: list[UUID] | None = None
    tag_ids: list[UUID] | None = None
    change_note: str | None = Field(default=None, max_length=2000)


class QuestionStatusUpdate(BaseModel):
    status: str


class TaxonomyAssignment(BaseModel):
    topic_ids: list[UUID] | None = None
    tag_ids: list[UUID] | None = None


class GradeRequest(BaseModel):
    response: dict | list | str | int | float | bool | None = None


class QuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    prompt: str | None
    status: str
    context_kind: str
    reading_id: UUID | None
    listening_id: UUID | None
    score: float
    partial_scoring: dict
    negative_scoring: dict
    level: str | None
    difficulty: int | None
    learning_language: str | None
    explanation: str | None
    teacher_notes: str | None
    media_asset_id: UUID | None
    config: dict
    current_version: int
    topics: list[TaxonomyRef] = Field(default_factory=list)
    tags: list[TaxonomyRef] = Field(default_factory=list)
    source_file_id: UUID | None = None
    source_page: int | None = None
    extraction_method: str | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class QuestionSummary(BaseModel):
    id: UUID
    type: str
    prompt: str | None
    status: str
    context_kind: str
    level: str | None
    difficulty: int | None
    learning_language: str | None
    score: float
    current_version: int
    topic_names: list[str] = Field(default_factory=list)
    tag_names: list[str] = Field(default_factory=list)
    has_media: bool = False
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class QuestionVersionRead(BaseModel):
    id: UUID
    question_id: UUID
    version: int
    change_note: str | None
    created_at: datetime
    snapshot: dict


class BulkRequest(BaseModel):
    question_ids: list[UUID] = Field(min_length=1, max_length=500)
    action: str
    status: str | None = None
    topic_id: UUID | None = None
    tag_id: UUID | None = None
    level: str | None = Field(default=None, max_length=32)
    learning_language: str | None = Field(default=None, max_length=16)

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        allowed = {
            "status",
            "trash",
            "restore",
            "add_topic",
            "remove_topic",
            "add_tag",
            "remove_tag",
            "set_level",
            "set_language",
        }
        if v not in allowed:
            raise ValueError(f"action must be one of: {', '.join(sorted(allowed))}")
        return v


class TopicCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: UUID | None = None
    language: str | None = Field(default=None, max_length=16)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: UUID | None = None
    language: str | None = Field(default=None, max_length=16)


class TopicRead(BaseModel):
    id: UUID
    name: str
    parent_id: UUID | None
    level_path: str
    language: str | None
    question_count: int = 0
    children: list["TopicRead"] = Field(default_factory=list)


TopicRead.model_rebuild()


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=32)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=32)


class TagRead(BaseModel):
    id: UUID
    name: str
    color: str | None
    question_count: int = 0
