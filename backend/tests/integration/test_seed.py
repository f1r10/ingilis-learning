"""Seed/bootstrap idempotency (spec section 4): run the seeder twice on a fresh
schema and prove it neither duplicates rows nor creates a second admin.

Skips (via db_ready) when Postgres is unavailable."""
from __future__ import annotations

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.identity import AdminUser
from app.models.system import Language, SystemSetting


async def _counts(db):
    admins = (await db.execute(select(func.count()).select_from(AdminUser))).scalar_one()
    langs = (await db.execute(select(func.count()).select_from(Language))).scalar_one()
    branding = (
        await db.execute(
            select(func.count()).select_from(SystemSetting).where(SystemSetting.category == "branding")
        )
    ).scalar_one()
    return admins, langs, branding


async def test_seed_is_idempotent(db_ready, monkeypatch):
    """Run against the schema the committed migration builds, twice in a row."""
    import scripts.seed as seedmod

    # Guarantee a bootstrap password independent of the ambient .env.
    monkeypatch.setattr(seedmod.settings, "bootstrap_admin_password", "seed-pw-12345678", raising=True)

    await seedmod.seed()
    async with SessionLocal() as db:
        first = await _counts(db)

    await seedmod.seed()
    async with SessionLocal() as db:
        second = await _counts(db)

    admins, langs, branding = first
    assert admins == 1
    assert langs == 4
    assert branding == 2  # system_name + short_name
    assert first == second
