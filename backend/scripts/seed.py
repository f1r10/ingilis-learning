"""Minimal, idempotent development seed.

Creates the bootstrap admin, the supported UI/learning/translation languages and
default branding. Seeds ONLY minimal development data (per the spec). Run after
`alembic upgrade head`:

    python -m scripts.seed
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core import security
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.identity import AdminUser
from app.models.system import Language
from app.services import settings_service

settings = get_settings()

SUPPORTED_UI_LANGUAGES = {
    "az": ("Azerbaijani", "Azərbaycan"),
    "en": ("English", "English"),
    "ru": ("Russian", "Русский"),
    "tr": ("Turkish", "Türkçe"),
}


async def seed() -> None:
    async with SessionLocal() as db:
        # Bootstrap admin
        existing = (
            await db.execute(select(AdminUser).where(AdminUser.username == settings.bootstrap_admin_username))
        ).scalar_one_or_none()
        if not existing:
            if not settings.bootstrap_admin_password:
                raise SystemExit("BOOTSTRAP_ADMIN_PASSWORD must be set to seed the admin")
            db.add(
                AdminUser(
                    username=settings.bootstrap_admin_username,
                    password_hash=security.hash_secret(settings.bootstrap_admin_password),
                )
            )
            print(f"created admin: {settings.bootstrap_admin_username}")

        # Languages
        enabled_ui = set(settings.enabled_ui_language_list)
        for code, (name_en, native) in SUPPORTED_UI_LANGUAGES.items():
            row = (await db.execute(select(Language).where(Language.code == code))).scalar_one_or_none()
            if not row:
                row = Language(code=code, name_en=name_en, native_name=native)
                db.add(row)
            row.is_ui = code in enabled_ui
            row.is_learning = code in set(settings.learning_language_list)
            row.is_translation = code in set(settings.translation_language_list)
            row.enabled = code in enabled_ui or row.is_learning or row.is_translation

        # Default branding + i18n only if absent
        from app.models.system import SystemSetting

        has_branding = (
            await db.execute(select(SystemSetting).where(SystemSetting.category == "branding").limit(1))
        ).scalar_one_or_none()
        if not has_branding:
            await settings_service.set_category(
                db, "branding", {"system_name": "Learning Platform", "short_name": "Learn"}, is_public=True
            )
        has_i18n = (
            await db.execute(select(SystemSetting).where(SystemSetting.category == "i18n").limit(1))
        ).scalar_one_or_none()
        if not has_i18n:
            await settings_service.set_category(
                db,
                "i18n",
                {
                    "enabled_ui_languages": settings.enabled_ui_language_list,
                    "default_ui_language": settings.default_ui_language,
                    "learning_languages": settings.learning_language_list,
                    "translation_languages": settings.translation_language_list,
                },
                is_public=True,
            )

        await db.commit()
    print("seed complete")


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
