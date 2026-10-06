"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.middleware import CSRFMiddleware, RequestContextMiddleware
from app.core.redis_client import close_redis

settings = get_settings()


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    yield
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Language Learning Platform API",
        version="0.1.0",
        default_response_class=ORJSONResponse,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # Middleware order (Starlette: the LAST added is the OUTERMOST and runs first).
    # Add CSRF innermost, then request-context capture, then CORS outermost so it
    # wraps everything (including CSRF 403s and error responses) and every browser
    # response carries the CORS headers. Credentials are required because auth uses
    # HttpOnly session cookies.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "env": settings.app_env}

    return app
