"""Unified error envelope: every error shape must serialise to
{"error": {"code", "message", ...}} so the frontend client can rely on it."""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.exceptions import (
    APIError,
    Conflict,
    Forbidden,
    NotFound,
    Unauthorized,
    ValidationFailed,
    register_exception_handlers,
)


class _Payload(BaseModel):
    name: str


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    r = APIRouter()

    @r.get("/boom/{exc}")
    async def boom(exc: str):
        raise {
            "unauthorized": Unauthorized("nope"),
            "forbidden": Forbidden("no"),
            "notfound": NotFound("gone"),
            "conflict": Conflict("username_taken", "taken"),
            "validation": ValidationFailed("bad field"),
            "generic": APIError("weird", "uh oh", 418),
        }[exc]

    @r.post("/needs-body")
    async def needs_body(p: _Payload):
        return p

    @app.get("/plain-404")
    async def _not_here():
        return {}

    app.include_router(r)
    return app


def test_api_error_envelopes():
    c = TestClient(_app())
    cases = {
        "unauthorized": (401, "unauthorized"),
        "forbidden": (403, "forbidden"),
        "notfound": (404, "not_found"),
        "conflict": (409, "username_taken"),
        "validation": (422, "validation_failed"),
        "generic": (418, "weird"),
    }
    for exc, (status, code) in cases.items():
        r = c.get(f"/boom/{exc}")
        assert r.status_code == status, exc
        assert r.json()["error"]["code"] == code
        assert "message" in r.json()["error"]


def test_request_validation_error_envelope():
    c = TestClient(_app())
    r = c.post("/needs-body", json={})  # missing required 'name'
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    assert isinstance(err["fields"], list) and err["fields"]


def test_starlette_http_404_envelope():
    c = TestClient(_app())
    # 405: POST to a GET-only route goes through the StarletteHTTPException handler.
    r = c.post("/plain-404")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"
