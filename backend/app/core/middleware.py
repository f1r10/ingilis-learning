"""ASGI middleware: request context capture and CSRF double-submit enforcement."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core import constants, security


@dataclass(frozen=True)
class RequestContext:
    ip: str | None
    user_agent: str | None
    origin: str | None


request_context: ContextVar[RequestContext] = ContextVar("request_context", default=RequestContext(None, None, None))

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# CSRF applies only to state-changing requests that carry a session cookie.
_EXEMPT_PATHS = {
    "/api/v1/auth/admin/login",
    "/api/v1/auth/admin/recovery/login",
    "/api/v1/auth/student/login",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        xff = request.headers.get("x-forwarded-for")
        ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)
        token = request_context.set(
            RequestContext(ip=ip, user_agent=request.headers.get("user-agent"), origin=request.headers.get("origin"))
        )
        try:
            return await call_next(request)
        finally:
            request_context.reset(token)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF check for cookie-authenticated, state-changing requests."""

    async def dispatch(self, request: Request, call_next):
        if request.method in _SAFE_METHODS or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        session_cookie = request.cookies.get(constants.SESSION_COOKIE)
        if not session_cookie:
            # No session -> public or Bearer-style; nothing to protect against CSRF.
            return await call_next(request)

        payload = security.read_session_token(
            session_cookie, max_age_seconds=3600 * 24 * 90
        )
        if not payload:
            return await call_next(request)

        csrf_header = request.headers.get(constants.CSRF_HEADER)
        if not csrf_header or not security.verify_csrf_token(csrf_header, payload["sid"]):
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "csrf_failed", "message": "Invalid or missing CSRF token"}},
            )
        return await call_next(request)
