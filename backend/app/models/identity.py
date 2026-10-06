"""Identity & access domain: admin, recovery codes, students, access keys,
sessions, groups and memberships.

Only secure hashes are ever stored for passwords, access keys and recovery codes.
Students are soft-deleted (archived) by default and can use one account across
multiple devices.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import enums
from app.core.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AdminUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single teacher/admin. Username + password both changeable in settings."""

    __tablename__ = "admin_user"

    username: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped to invalidate ALL issued admin session tokens at once (logout,
    # credential change). Admin tokens are stateless-signed and embed this value.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)

    recovery_codes: Mapped[list["AdminRecoveryCode"]] = relationship(
        back_populates="admin", cascade="all, delete-orphan"
    )


class AdminRecoveryCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One-time password-recovery codes. Server stores hashes only.

    Each code works once. Generating a new set invalidates the previous set
    (old rows are marked superseded / deleted by the service).
    """

    __tablename__ = "admin_recovery_code"

    admin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_user.id", ondelete="CASCADE"), index=True, nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    admin: Mapped[AdminUser] = relationship(back_populates="recovery_codes")


class Student(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A learner. Created with name/surname/username; group optional; multiple
    groups allowed. Never hard-deleted by default (archive / disable instead)."""

    __tablename__ = "student"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    surname: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    status: Mapped[enums.StudentStatus] = mapped_column(
        SAEnum(enums.StudentStatus, name="student_status", native_enum=False),
        default=enums.StudentStatus.ACTIVE,
        nullable=False,
    )
    ui_language: Mapped[str | None] = mapped_column(String(16))  # chosen by the student
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list["GroupMembership"]] = relationship(back_populates="student")
    access_keys: Mapped[list["StudentAccessKey"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    notes: Mapped[list["StudentNote"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_student_status_deleted", "status", "deleted_at"),)


class StudentAccessKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Hashed access key for a student. Raw key is shown once at generation."""

    __tablename__ = "student_access_key"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Short, non-secret fingerprint so the teacher can recognise a key in a list,
    # and so a login can resolve its candidate with an equality lookup.
    key_prefix: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[enums.AccessKeyStatus] = mapped_column(
        SAEnum(enums.AccessKeyStatus, name="access_key_status", native_enum=False),
        default=enums.AccessKeyStatus.ACTIVE,
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    student: Mapped[Student] = relationship(back_populates="access_keys")

    # A student has AT MOST ONE live key: enforced by the database, not just by
    # Python, so two concurrent rotations cannot both leave an ACTIVE row behind
    # (issue #1 symptom was `scalar_one_or_none()` raising on the student detail
    # endpoint once a second ACTIVE key existed).
    __table_args__ = (
        Index(
            "uq_student_access_key_active",
            "student_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class StudentSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Server-side session enabling session termination and presence monitoring.

    A student may hold multiple concurrent sessions (multiple devices)."""

    __tablename__ = "student_session"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_location: Mapped[str | None] = mapped_column(String(200))  # page/section for monitoring
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    student: Mapped[Student] = relationship()

    __table_args__ = (Index("ix_student_session_active", "student_id", "terminated_at", "expires_at"),)


class Group(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Class/group, e.g. 'IELTS 2026'. Students may belong to many."""

    __tablename__ = "group"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(String(32))

    memberships: Mapped[list["GroupMembership"]] = relationship(back_populates="group")


class GroupMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "group_membership"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )

    group: Mapped[Group] = relationship(back_populates="memberships")
    student: Mapped[Student] = relationship(back_populates="memberships")

    __table_args__ = (UniqueConstraint("group_id", "student_id", name="uq_group_student"),)


class StudentNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Private teacher notes. Students must NEVER see these."""

    __tablename__ = "student_note"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    student: Mapped[Student] = relationship(back_populates="notes")
