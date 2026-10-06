"""Aggregate v1 API router (clean versioning)."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    auth,
    groups,
    health,
    questions,
    settings,
    students,
    tags,
    topics,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(students.router)
api_router.include_router(groups.router)
api_router.include_router(settings.router)
api_router.include_router(questions.router)
api_router.include_router(topics.router)
api_router.include_router(tags.router)
