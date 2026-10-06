"""Shared assertions for the integration suite."""
from __future__ import annotations

from httpx import Response


def error_of(resp: Response) -> dict:
    """Assert the single documented error envelope and hand back the error body.

    Every failure path must answer `{"error": {"code", "message", ...}}`; a test
    that read `.json()["detail"]` would silently pass on an inconsistent API.
    """
    body = resp.json()
    assert "error" in body, f"missing error envelope in {resp.status_code} body: {body}"
    err = body["error"]
    assert isinstance(err, dict), f"error is not an object: {err}"
    assert err.get("code"), f"error has no code: {err}"
    assert isinstance(err.get("message"), str) and err["message"], f"error has no message: {err}"
    return err
