"""Application configuration loaded from environment variables.

All runtime configuration is environment-based (see `.env.example`). Secrets are
NEVER read into the frontend. Branding/labels are stored in the database and are
editable via Admin Settings, so they are intentionally NOT hard-coded here.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- identity / http ---
    app_env: Literal["development", "staging", "production"] = "development"
    frontend_origin: str = "http://localhost:5173"
    public_api_base_url: str = "http://localhost:8000/api/v1"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # --- secrets ---
    session_secret: str = Field(min_length=16)
    csrf_secret: str = Field(min_length=16)

    # --- cookies ---
    cookie_secure: bool = False
    cookie_domain: str | None = None
    admin_session_ttl_hours: int = 12
    student_session_ttl_hours: int = 72

    # --- datastores ---
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    redis_url: str = "redis://localhost:6379/0"

    # --- object storage ---
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_region: str = "us-east-1"
    object_storage_access_key: str = "minioadmin"
    object_storage_secret_key: str = "minioadmin"
    object_storage_bucket: str = "platform-media"
    object_storage_secure: bool = False
    #: Per-kind upload ceilings, in megabytes. Three numbers rather than one, because
    #: the three kinds fail differently: a 40 MB photograph is a mistake, a 300 MB
    #: classroom recording is Tuesday. `/media/meta` publishes them so the browser can
    #: say "too large" before sending anything, instead of after.
    max_image_upload_mb: int = 15
    max_audio_upload_mb: int = 200
    max_video_upload_mb: int = 700
    max_file_upload_mb: int = 700

    # --- bootstrap admin ---
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""

    # --- languages ---
    enabled_ui_languages: str = "az,en,ru,tr"
    default_ui_language: str = "en"
    learning_languages: str = "en"
    translation_languages: str = "az,ru,tr"

    # --- AI / OCR / STT / Translation (local-first, all optional) ---
    ai_provider: Literal["none", "local", "gemini"] = "none"
    local_ai_base_url: str = ""
    local_ai_model: str = "llama3.1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    ocr_provider: Literal["none", "paddleocr", "tesseract"] = "none"
    stt_provider: Literal["none", "whisper"] = "none"
    translation_provider: Literal["none", "libretranslate"] = "none"
    translation_base_url: str = ""
    dictionary_provider: Literal["none", "kaikki"] = "none"

    # --- import defaults ---
    import_auto_mode_default: bool = False
    import_low_confidence_threshold: float = 0.80

    # --- retention & backups ---
    trash_retention_days: int = 30
    activity_retention_days: int = 0
    backup_enabled: bool = False
    backup_cron: str = "03:00"

    @field_validator("cookie_domain", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return v or None

    @property
    def enabled_ui_language_list(self) -> list[str]:
        return [x.strip() for x in self.enabled_ui_languages.split(",") if x.strip()]

    @property
    def learning_language_list(self) -> list[str]:
        return [x.strip() for x in self.learning_languages.split(",") if x.strip()]

    @property
    def translation_language_list(self) -> list[str]:
        return [x.strip() for x in self.translation_languages.split(",") if x.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
