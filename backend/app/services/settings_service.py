"""Settings & branding service.

Branding/UI values live in the database (`system_setting`) so they are editable
in Admin Settings and NEVER hard-coded. Falls back to environment defaults when a
key has not been set yet.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.system import SystemSetting

_settings = get_settings()

# Defaults shown before the teacher customises anything.
BRANDING_DEFAULTS: dict[str, Any] = {
    "system_name": "Learning Platform",
    "short_name": "Learn",
    "logo_url": None,
    "favicon_url": None,
    "login_image_url": None,
    "login_title": None,
    "welcome_message": None,
    "login_instructions": None,
    "footer": None,
    "support_text": None,
    "button_login_label": None,
    "accent_color": None,
}

# Which branding keys are safe to expose on the (unauthenticated) login screen.
PUBLIC_BRANDING_KEYS = set(BRANDING_DEFAULTS.keys())


async def get_category(db: AsyncSession, category: str) -> dict[str, Any]:
    result = await db.execute(select(SystemSetting).where(SystemSetting.category == category))
    out: dict[str, Any] = {}
    for row in result.scalars():
        out[row.key] = row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value
    return out


async def set_category(
    db: AsyncSession, category: str, values: dict[str, Any], *, is_public: bool = False
) -> None:
    for key, val in values.items():
        result = await db.execute(
            select(SystemSetting).where(
                SystemSetting.category == category, SystemSetting.key == key
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            db.add(
                SystemSetting(
                    category=category, key=key, value={"v": val}, is_public=is_public
                )
            )
        else:
            row.value = {"v": val}
            row.is_public = is_public
    await db.flush()


async def get_branding_public(db: AsyncSession) -> dict[str, Any]:
    stored = await get_category(db, "branding")
    merged = {**BRANDING_DEFAULTS, **{k: v for k, v in stored.items() if k in PUBLIC_BRANDING_KEYS}}
    return merged


async def get_ui_config(db: AsyncSession) -> dict[str, Any]:
    """Enabled UI/learning/translation languages, DB-overridable."""
    stored = await get_category(db, "i18n")
    return {
        "enabled_ui_languages": stored.get("enabled_ui_languages", _settings.enabled_ui_language_list),
        "default_ui_language": stored.get("default_ui_language", _settings.default_ui_language),
        "learning_languages": stored.get("learning_languages", _settings.learning_language_list),
        "translation_languages": stored.get("translation_languages", _settings.translation_language_list),
    }
