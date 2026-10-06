from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BrandingPublic(BaseModel):
    """Login-screen branding; safe for unauthenticated clients."""

    system_name: str = "Learning Platform"
    short_name: str = "Learn"
    logo_url: str | None = None
    favicon_url: str | None = None
    login_image_url: str | None = None
    login_title: str | None = None
    welcome_message: str | None = None
    login_instructions: str | None = None
    footer: str | None = None
    support_text: str | None = None
    accent_color: str | None = None


class UiConfigPublic(BaseModel):
    enabled_ui_languages: list[str]
    default_ui_language: str
    learning_languages: list[str]
    translation_languages: list[str]


class SessionBootstrap(BaseModel):
    branding: BrandingPublic
    ui: UiConfigPublic


class SettingUpdate(BaseModel):
    category: str
    values: dict[str, Any]


class SettingItem(BaseModel):
    category: str
    key: str
    value: Any
    is_public: bool = False
