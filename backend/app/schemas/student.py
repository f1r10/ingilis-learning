from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StudentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    surname: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=120)
    group_ids: list[UUID] = []


class StudentUpdate(BaseModel):
    name: str | None = None
    surname: str | None = None
    username: str | None = None
    ui_language: str | None = None


class StudentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    surname: str
    username: str
    status: str
    ui_language: str | None
    created_at: datetime
    active_key_prefix: str | None = None
    group_ids: list[UUID] = []


class AccessKeyCreated(BaseModel):
    # Returned ONCE; the server keeps only the hash.
    student_id: UUID
    access_key: str


class StudentStatusUpdate(BaseModel):
    status: str  # active | disabled | archived


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    color: str | None = None


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None


class GroupMemberAdd(BaseModel):
    student_id: UUID


class GroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    color: str | None
    member_count: int = 0
