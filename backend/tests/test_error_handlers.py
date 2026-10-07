"""Unified error envelope: every error shape must serialise to
{"error": {"code", "message", ...}} so the frontend client can rely on it."""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from app.core.exceptions import (
    APIError,
    Conflict,
    Forbidden,
    NotFound,
    Unauthorized,
    ValidationFailed,
    register_exception_handlers,
    validation_message,
)


class _Payload(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _needs_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a name needs text")
        return v


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


def test_a_body_refusal_says_why_in_the_message():
    """`message` is the part every client reads, so a refusal that only says "failed"
    tells a caller nothing they can act on - and the caller is not always this
    project's own browser."""
    c = TestClient(_app())
    r = c.post("/needs-body", json={"name": "   "})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    assert "a name needs text" in err["message"], err["message"]
    assert "Value error," not in err["message"], "the library's prefix is not a teacher's sentence"
    assert [field["loc"][-1] for field in err["fields"]] == ["name"]
    assert err["fields"][0]["msg"] == "Value error, a name needs text", "the raw field stays as sent"


def test_the_reason_summary_stays_a_summary():
    """Nine bad fields is a long body, not nine things to read before fixing one."""
    message = validation_message([f"reason {i}" for i in range(9)])
    assert message.startswith("Request validation failed: reason 0; reason 1; reason 2")
    assert "(+6 more)" in message
    assert "reason 3" not in message
    assert validation_message([]) == "Request validation failed"
