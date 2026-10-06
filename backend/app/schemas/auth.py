from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import Message  # noqa: F401  - re-exported for admin endpoints


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class StudentLoginRequest(BaseModel):
    # Single "Access Key" field: username@LONG_RANDOM_SECRET
    access_key: str = Field(min_length=3, max_length=200)


class RecoveryCodeLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=4, max_length=64)


class SessionInfo(BaseModel):
    subject_type: str  # admin | student
    csrf_token: str  # also delivered as readable cookie for double-submit


class StudentSessionInfo(BaseModel):
    session_id: str
    ip: str | None
    user_agent: str | None
    last_seen_at: str | None
    current_location: str | None


class RecoveryCodesResponse(BaseModel):
    codes: list[str]  # plaintext, shown ONCE


class PasswordChangeRequest(BaseModel):
    current_password: str | None = None
    new_password: str = Field(min_length=8, max_length=200)


class UsernameChangeRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
