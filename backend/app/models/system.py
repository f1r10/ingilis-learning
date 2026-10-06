"""System settings & language configuration.

Branding, defaults and provider settings are stored here (NOT hard-coded) so the
teacher can edit everything in Admin Settings. Grouped JSONB by category keeps
the model extensible without schema churn.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class SystemSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Key/value settings grouped by category. Values are JSON.

    Example categories: branding, i18n, exam_defaults, practice_defaults, ai,
    ocr, media, retention, backups, dashboard_widgets.
    """

    __tablename__ = "system_setting"

    category: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_public: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, doc="Exposable to unauthenticated login screen"
    )

    __table_args__ = (UniqueConstraint("category", "key", name="uq_setting_category_key"),)


class Language(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A language known to the platform.

    role: ui | learning | translation (a language can serve multiple roles via
    boolean flags). Codes are ISO-like (az, en, ru, tr, ...).
    """

    __tablename__ = "language"

    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    name_en: Mapped[str] = mapped_column(String(120), nullable=False)
    native_name: Mapped[str | None] = mapped_column(String(120))
    is_ui: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_learning: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_translation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)
