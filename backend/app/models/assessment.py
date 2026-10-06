"""Catalogs, exams, assignments, attempts and answers.

A catalog is NOT an exam. Published exams use immutable snapshots: an ExamItem
references a QuestionVersion, and an AttemptAnswer references the same frozen
version, so editing a question NEVER changes historical results.

Student temporary self-practice exams are NOT persisted as exam definitions -
only granular ActivityEvent rows are stored (see activity.py)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import enums
from app.core.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


# --------------------------------------------------------------------------- #
# Catalogs
# --------------------------------------------------------------------------- #
class Catalog(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Reusable learning/practice collection of mixed content. Teacher-defined
    name; optional nested folders via parent_id."""

    __tablename__ = "catalog"

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("catalog.id", ondelete="SET NULL"), index=True
    )
    learning_language: Mapped[str | None] = mapped_column(String(16), index=True)
    level: Mapped[str | None] = mapped_column(String(32))
    shuffle_default: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    known_states_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[enums.ContentStatus] = mapped_column(
        SAEnum(enums.ContentStatus, name="content_status", native_enum=False),
        default=enums.ContentStatus.READY,
        nullable=False,
    )
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    items: Mapped[list["CatalogItem"]] = relationship(
        back_populates="catalog", cascade="all, delete-orphan", order_by="CatalogItem.position"
    )


class CatalogItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Ordered, polymorphic reference into the central banks (no duplication)."""

    __tablename__ = "catalog_item"

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[enums.ContentKind] = mapped_column(
        SAEnum(enums.ContentKind, name="content_kind", native_enum=False), nullable=False
    )
    ref_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    catalog: Mapped[Catalog] = relationship(back_populates="items")

    __table_args__ = (Index("ix_catalog_item_order", "catalog_id", "position"),)


# --------------------------------------------------------------------------- #
# Exams
# --------------------------------------------------------------------------- #
class Exam(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A separately configured assessment with its own rules & snapshot items."""

    __tablename__ = "exam"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[enums.ExamStatus] = mapped_column(
        SAEnum(enums.ExamStatus, name="exam_status", native_enum=False),
        default=enums.ExamStatus.DRAFT,
        nullable=False,
    )
    learning_language: Mapped[str | None] = mapped_column(String(16), index=True)
    level: Mapped[str | None] = mapped_column(String(32))

    # availability vs duration (kept separate)
    available_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    must_finish_before_close: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_attempts: Mapped[int | None] = mapped_column(Integer)
    passing_score: Mapped[float | None] = mapped_column(Float)

    # randomisation
    shuffle_questions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shuffle_options: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # integrity / monitoring
    resume_after_disconnect: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    restrict_copy_paste: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    monitor_tab_switch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tab_switch_limit: Mapped[int | None] = mapped_column(Integer)
    tab_switch_action: Mapped[str | None] = mapped_column(String(32))  # warn|flag|auto_submit

    # navigation
    allow_previous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # feedback / results policy
    feedback_timing: Mapped[enums.FeedbackTiming] = mapped_column(
        SAEnum(enums.FeedbackTiming, name="feedback_timing", native_enum=False),
        default=enums.FeedbackTiming.AFTER_SESSION,
        nullable=False,
    )
    show_correct_answers: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    show_explanations: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result_visibility: Mapped[str] = mapped_column(
        String(32), default="after_close", nullable=False
    )  # immediate|after_close|after_approval|hidden

    # scoring
    partial_scoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    negative_marking_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    grading_mode: Mapped[enums.GradingMode] = mapped_column(
        SAEnum(enums.GradingMode, name="grading_mode", native_enum=False),
        default=enums.GradingMode.AUTOMATIC,
        nullable=False,
    )
    auto_submit_on_expiry: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    sections: Mapped[list["ExamSection"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan", order_by="ExamSection.position"
    )
    assignments: Mapped[list["ExamAssignment"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )
    attempts: Mapped[list["ExamAttempt"]] = relationship(back_populates="exam")


class ExamSection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "exam_section"

    exam_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(300))
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shuffle_items: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    exam: Mapped[Exam] = relationship(back_populates="sections")
    items: Mapped[list["ExamItem"]] = relationship(
        back_populates="section", cascade="all, delete-orphan", order_by="ExamItem.position"
    )


class ExamItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A frozen reference used by the exam: pins a specific QuestionVersion (or
    reading/listening set). This is the immutable-snapshot mechanism."""

    __tablename__ = "exam_item"

    exam_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam.id", ondelete="CASCADE"), index=True, nullable=False
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exam_section.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[enums.ContentKind] = mapped_column(
        SAEnum(enums.ContentKind, name="content_kind", native_enum=False), nullable=False
    )
    ref_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    question_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("question_version.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    points: Mapped[float | None] = mapped_column(Float)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    exam: Mapped[Exam] = relationship()
    section: Mapped[ExamSection | None] = relationship(back_populates="items")


class ExamAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Assigns an exam to a student or group."""

    __tablename__ = "exam_assignment"

    exam_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"), index=True
    )

    exam: Mapped[Exam] = relationship(back_populates="assignments")

    __table_args__ = (Index("ix_assignment_target", "exam_id", "student_id", "group_id"),)


# --------------------------------------------------------------------------- #
# Attempts & answers (frozen against QuestionVersion)
# --------------------------------------------------------------------------- #
class ExamAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "exam_attempt"

    exam_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam.id", ondelete="CASCADE"), index=True, nullable=False
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[enums.AttemptStatus] = mapped_column(
        SAEnum(enums.AttemptStatus, name="attempt_status", native_enum=False),
        default=enums.AttemptStatus.IN_PROGRESS,
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # server is authoritative on the timer
    server_seconds_used: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    max_score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    tab_switch_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # frozen composition used for this attempt (order + versions)
    blueprint: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    exam: Mapped[Exam] = relationship(back_populates="attempts")
    answers: Mapped[list["AttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class AttemptAnswer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attempt_answer"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_attempt.id", ondelete="CASCADE"), index=True, nullable=False
    )
    exam_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_item.id", ondelete="CASCADE"), index=True, nullable=False
    )
    question_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("question_version.id", ondelete="RESTRICT"), index=True
    )
    response: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    correct: Mapped[bool | None] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Float)
    graded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    time_spent_seconds: Mapped[int | None] = mapped_column(Integer)
    changed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # continuous autosave / offline cache reconciliation
    client_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempt: Mapped[ExamAttempt] = relationship(back_populates="answers")

    __table_args__ = (Index("ix_answer_attempt_item", "attempt_id", "exam_item_id", unique=True),)


class ManualReview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Student Questions Box entry for open/manual answers awaiting review."""

    __tablename__ = "manual_review"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_attempt.id", ondelete="CASCADE"), index=True, nullable=False
    )
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempt_answer.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_suggestion: Mapped[dict | None] = mapped_column(JSONB)  # verdict/score/confidence/reason
    final_score: Mapped[float | None] = mapped_column(Float)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_note: Mapped[str | None] = mapped_column(Text)

    answer: Mapped[AttemptAnswer] = relationship()


class TeacherFeedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Feedback a teacher leaves that the STUDENT may see (vs private notes)."""

    __tablename__ = "teacher_feedback"

    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exam_attempt.id", ondelete="CASCADE"), index=True
    )
    answer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attempt_answer.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True, nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
