"""Activity monitoring, favorites, question reports and notifications.

ActivityEvent is the granular, append-only source of truth behind analytics. It
is rendered into human-readable timelines in the API layer (teachers never read
raw JSON). Student self-practice is recorded here but the generated practice exam
definition itself is never persisted."""
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
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core import enums
from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class ActivityEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "activity_event"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # category separates Student Activity vs Assessment logs in queries
    category: Mapped[str] = mapped_column(String(32), index=True, nullable=False)  # activity|assessment
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    human_summary: Mapped[str | None] = mapped_column(Text)  # "Answered 'apple' correctly"
    context_type: Mapped[str | None] = mapped_column(String(32))  # question|vocab|reading|listening|exam|catalog|page
    context_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    ref_question_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("question.id", ondelete="SET NULL"))
    correct: Mapped[bool | None] = mapped_column(Boolean)
    time_spent_seconds: Mapped[int | None] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(String(120))
    browser: Mapped[str | None] = mapped_column(String(120))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_activity_student_time", "student_id", "occurred_at"),
        Index("ix_activity_category", "category", "occurred_at"),
    )


class Favorite(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Student-saved questions / vocabulary for a personal review area."""

    __tablename__ = "favorite"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[enums.ContentKind] = mapped_column(
        SAEnum(enums.ContentKind, name="content_kind", native_enum=False), nullable=False
    )
    ref_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)

    __table_args__ = (Index("ix_favorite_unique", "student_id", "kind", "ref_id", unique=True),)


class QuestionReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A student-reported problematic question -> admin inbox."""

    __tablename__ = "question_report"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), index=True, nullable=False
    )
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Internal notifications only. External services not required initially."""

    __tablename__ = "notification"

    audience: Mapped[enums.NotificationAudience] = mapped_column(
        SAEnum(enums.NotificationAudience, name="notification_audience", native_enum=False),
        nullable=False,
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (Index("ix_notif_unread", "audience", "read_at", "student_id"),)
