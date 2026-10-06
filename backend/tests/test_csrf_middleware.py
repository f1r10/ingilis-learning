"""CSRF double-submit middleware, tested in isolation with the real middleware
but dummy routes (so no DB/Redis is ever touched)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import constants, security
from app.core.exceptions import register_exception_handlers
from app.core.middleware import CSRFMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    register_exception_handlers(app)

    @app.get("/safe")
    async def safe():
        return {"ok": True}

    @app.post("/stateful")
    async def stateful():
        return {"ok": True}

    # A pre-auth login path (in the exempt list) that must skip CSRF even with
    # a stale session cookie present.
    @app.post("/api/v1/auth/admin/login")
    async def login():
        return {"ok": True}

    return app


def _sid() -> str:
    return "11111111-1111-1111-1111-111111111111"


def test_get_is_never_blocked():
    c = TestClient(_app())
    assert c.get("/safe").status_code == 200


def test_stateful_without_session_cookie_passes():
    # No session -> nothing to protect; request reaches handler.
    c = TestClient(_app())
    assert c.post("/stateful").status_code == 200


def test_stateful_with_session_but_no_csrf_header_is_blocked():
    c = TestClient(_app())
    sid = _sid()
    token = security.create_session_token(security.SubjectType.ADMIN, sid, epoch=0)
    r = c.post("/stateful", cookies={constants.SESSION_COOKIE: token})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


def test_stateful_with_wrong_csrf_is_blocked():
    c = TestClient(_app())
    sid = _sid()
    token = security.create_session_token(security.SubjectType.ADMIN, sid, epoch=0)
    bad = security.create_csrf_token("22222222-2222-2222-2222-222222222222")
    r = c.post("/stateful", cookies={constants.SESSION_COOKIE: token}, headers={constants.CSRF_HEADER: bad})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


def test_stateful_with_matching_csrf_passes():
    c = TestClient(_app())
    sid = _sid()
    token = security.create_session_token(security.SubjectType.ADMIN, sid, epoch=0)
    csrf = security.create_csrf_token(sid)
    r = c.post("/stateful", cookies={constants.SESSION_COOKIE: token}, headers={constants.CSRF_HEADER: csrf})
    assert r.status_code == 200


def test_exempt_login_skips_csrf_even_with_session_cookie():
    c = TestClient(_app())
    sid = _sid()
    token = security.create_session_token(security.SubjectType.ADMIN, sid, epoch=0)
    r = c.post("/api/v1/auth/admin/login", cookies={constants.SESSION_COOKIE: token})
    assert r.status_code == 200


def test_malformed_session_cookie_is_ignored_by_csrf():
    # A garbage session token yields no payload; CSRF middleware lets the request
    # through and the auth dependency (not exercised here) would reject it.
    c = TestClient(_app())
    r = c.post("/stateful", cookies={constants.SESSION_COOKIE: "not-a-signed-token"})
    assert r.status_code == 200
