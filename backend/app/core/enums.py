"""Shared domain enums (stored as native DB enums / strings)."""
from __future__ import annotations

import enum


class StudentStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class AccessKeyStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ContentStatus(str, enum.Enum):
    DRAFT = "draft"
    READY = "ready"
    ARCHIVED = "archived"
    TRASH = "trash"


class TranscriptSource(str, enum.Enum):
    """Where a listening's transcript came from.

    This is provenance, not lifecycle, and only the module that produced the text may
    claim to have produced it: the editor can write `MANUAL` or `ABSENT`, the document
    importer `IMPORTED`, and the speech adapter `AUTO`. A client allowed to name any of
    the four would leave a transcript with no honest origin.
    """

    MANUAL = "manual"
    IMPORTED = "imported"
    AUTO = "auto"
    ABSENT = "absent"


class ExamStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    FINISHED = "finished"
    ARCHIVED = "archived"


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    AUTO_SUBMITTED = "auto_submitted"
    EXPIRED = "expired"


class GradingMode(str, enum.Enum):
    AUTOMATIC = "automatic"
    AI_ASSISTED = "ai_assisted"
    MANUAL = "manual"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportItemDecision(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class ContentKind(str, enum.Enum):
    """Polymorphic catalog/exam item targets."""

    QUESTION = "question"
    VOCABULARY = "vocabulary"
    READING = "reading"
    LISTENING = "listening"


class QuestionContext(str, enum.Enum):
    """Explicit parent/context dependency (CRITICAL CONTENT RELATIONSHIP RULE)."""

    INDEPENDENT = "independent"
    READING = "reading_bound"
    LISTENING = "listening_bound"


class NotificationAudience(str, enum.Enum):
    TEACHER = "teacher"
    STUDENT = "student"


class FeedbackTiming(str, enum.Enum):
    INSTANT = "instant"
    AFTER_SESSION = "after_session"
