"""Aggregate v1 API router (clean versioning)."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    auth,
    catalogs,
    groups,
    health,
    listening,
    media,
    practice,
    questions,
    reading,
    settings,
    students,
    tags,
    topics,
    vocabulary,
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
api_router.include_router(vocabulary.router)
api_router.include_router(vocabulary.student_router)
api_router.include_router(media.router)
api_router.include_router(media.student_router)
api_router.include_router(reading.router)
api_router.include_router(reading.student_router)
api_router.include_router(listening.router)
api_router.include_router(listening.student_router)
api_router.include_router(catalogs.router)
api_router.include_router(practice.router)
api_router.include_router(practice.favorites_router)
