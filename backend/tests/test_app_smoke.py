"""App smoke tests against the real create_app(): route wiring, health endpoint,
and the middleware ordering guarantee that CORS headers wrap even a CSRF 403.

None of these hit Postgres/Redis: /healthz is DB-free, and CSRF short-circuits
before any get_db dependency resolves."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core import constants, security
from app.core.config import get_settings
from app.main import create_app


def test_healthz_and_openapi_routes_present():
    c = TestClient(create_app())
    assert c.get("/healthz").status_code == 200

    paths = c.get("/openapi.json").json()["paths"]
    for p in [
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/admin/login",
        "/api/v1/auth/student/login",
        "/api/v1/auth/logout",
        "/api/v1/students",
        "/api/v1/groups",
        "/api/v1/groups/{group_id}/members",
        "/api/v1/groups/{group_id}/members/{student_id}",
        "/api/v1/settings/branding",
        # Phase 3: the question bank and its taxonomy.
        "/api/v1/questions",
        "/api/v1/questions/{question_id}",
        "/api/v1/topics",
        "/api/v1/tags",
        # Phase 4: both vocabulary surfaces. A router that is written but never
        # included looks identical in a grep, so this is where it gets caught.
        "/api/v1/vocabulary",
        "/api/v1/vocabulary/{entry_id}",
        "/api/v1/vocabulary/{entry_id}/preview",
        "/api/v1/vocabulary/{entry_id}/restore",
        "/api/v1/vocabulary/{entry_id}/status",
        "/api/v1/vocabulary/{entry_id}/taxonomy",
        "/api/v1/vocabulary/bulk",
        "/api/v1/vocabulary/meta",
        "/api/v1/student/vocabulary",
        "/api/v1/student/vocabulary/meta",
        "/api/v1/student/vocabulary/{entry_id}",
    ]:
        assert p in paths, p


def test_the_student_vocabulary_surface_is_read_only():
    """A learner may browse cards and nothing else.

    The student router must not grow a write method: the word bank belongs to the
    teacher, and a route that lets a student edit it would be a permission bug
    shipped as an API.
    """
    paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    for path, operations in paths.items():
        if not path.startswith("/api/v1/student/"):
            continue
        for method in operations:
            assert method in ("get", "post"), f"{method.upper()} {path} is not a read for a student"
        if path.startswith("/api/v1/student/vocabulary"):
            assert set(operations) == {"get"}, f"{path} exposes {sorted(operations)} to a learner"


def test_cors_wraps_csrf_403():
    c = TestClient(create_app())
    sid = "33333333-3333-3333-3333-333333333333"
    token = security.create_session_token(security.SubjectType.ADMIN, sid, epoch=0)
    frontend_origin = get_settings().frontend_origin
    r = c.put(
        "/api/v1/settings/branding",
        cookies={constants.SESSION_COOKIE: token},
        headers={constants.CSRF_HEADER: "bad", "Origin": frontend_origin},
        json={"values": {"system_name": "X"}},
    )
    # CSRF blocks it (403) BEFORE the DB dependency, and CORS still decorates it.
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"
    assert r.headers.get("access-control-allow-origin") == frontend_origin
    assert r.headers.get("access-control-allow-credentials") == "true"
