from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.models.identity import AdminUser
from app.schemas import settings as settings_schemas
from app.services import audit_service, settings_service

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/branding", response_model=settings_schemas.BrandingPublic)
async def get_branding(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await settings_service.get_branding_public(db)


@router.put("/branding", response_model=settings_schemas.BrandingPublic)
async def update_branding(
    payload: settings_schemas.SettingUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Branding edits never destroy consistency: only whitelisted keys are stored.
    allowed = {k: v for k, v in payload.values.items() if k in settings_service.PUBLIC_BRANDING_KEYS}
    await settings_service.set_category(db, "branding", allowed, is_public=True)
    await db.flush()
    await audit_service.record_audit(db, action="settings.branding.updated", actor_id=admin.id)
    return await settings_service.get_branding_public(db)


@router.get("/ui", response_model=settings_schemas.UiConfigPublic)
async def get_ui(
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await settings_service.get_ui_config(db)


@router.put("/ui", response_model=settings_schemas.UiConfigPublic)
async def update_ui(
    payload: settings_schemas.SettingUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await settings_service.set_category(db, "i18n", payload.values, is_public=True)
    await db.flush()
    await audit_service.record_audit(db, action="settings.ui.updated", actor_id=admin.id)
    return await settings_service.get_ui_config(db)


@router.get("/{category}", response_model=None)
async def get_category(
    category: str,
    _admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read any settings category (exam_defaults, ai, media, retention, ...)."""
    return {"category": category, "values": await settings_service.get_category(db, category)}


@router.put("/{category}", response_model=None)
async def put_category(
    category: str,
    payload: settings_schemas.SettingUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await settings_service.set_category(db, category, payload.values)
    await db.flush()
    await audit_service.record_audit(db, action="settings.updated", actor_id=admin.id, target_type=category)
    return {"category": category, "values": await settings_service.get_category(db, category)}
