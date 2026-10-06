"""Operations: export jobs, backups and the audit log."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core import enums
from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class ExportJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Async export of banks/results/analytics/logs (CSV/XLSX/JSON/PDF)."""

    __tablename__ = "export_job"

    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # question_bank|vocab|results|...
    fmt: Mapped[str] = mapped_column(String(16), nullable=False)  # csv|xlsx|json|pdf
    status: Mapped[enums.JobStatus] = mapped_column(
        SAEnum(enums.JobStatus, name="job_status", native_enum=False),
        default=enums.JobStatus.QUEUED,
        nullable=False,
    )
    filters: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    row_count: Mapped[int | None] = mapped_column(Integer)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_user.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)


class Backup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Manual + scheduled DB/media backups and restore workflow tracking."""

    __tablename__ = "backup"

    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # database|media|full
    trigger: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)  # manual|scheduled
    status: Mapped[enums.JobStatus] = mapped_column(
        SAEnum(enums.JobStatus, name="job_status", native_enum=False),
        default=enums.JobStatus.QUEUED,
        nullable=False,
    )
    storage_key: Mapped[str | None] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Security-relevant, auditable actions. Kept separate from student activity."""

    __tablename__ = "audit_log"

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)  # admin|student|system
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    action: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_audit_actor_time", "actor_type", "created_at"),)
