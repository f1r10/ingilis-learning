"""Typed API exceptions and FastAPI exception handlers."""
from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class APIError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class Unauthorized(APIError):
    def __init__(self, message: str = "Not authenticated") -> None:
        super().__init__("unauthorized", message, status.HTTP_401_UNAUTHORIZED)


class Forbidden(APIError):
    def __init__(self, message: str = "Not permitted") -> None:
        super().__init__("forbidden", message, status.HTTP_403_FORBIDDEN)


class NotFound(APIError):
    def __init__(self, message: str = "Not found") -> None:
        super().__init__("not_found", message, status.HTTP_404_NOT_FOUND)


class Conflict(APIError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status.HTTP_409_CONFLICT)


class ValidationFailed(APIError):
    def __init__(self, message: str) -> None:
        # The numeric code, because Starlette deprecated `HTTP_422_UNPROCESSABLE_ENTITY`
        # in favour of a renamed alias that older versions do not have.
        super().__init__("validation_failed", message, 422)


#: How many distinct reasons one summary line carries. Forty bad rows in a bulk body is
#: one mistake repeated, and the first few sentences are what a caller can act on.
_MAX_SUMMARY_REASONS = 3


def _reasons(errors: list[dict]) -> list[str]:
    """The distinct sentences pydantic gave, with its `Value error, ` prefix removed.

    A custom validator's message arrives as `"Value error, a recording needs a title"`;
    the prefix names the library, not the problem, and a teacher has no use for it.
    """
    out: list[str] = []
    for err in errors:
        msg = err.get("msg")
        if not isinstance(msg, str) or not msg:
            continue
        text = msg.removeprefix("Value error, ").strip()
        if text and text not in out:
            out.append(text)
    return out


def validation_message(reasons: list[str]) -> str:
    """A body refusal has to be readable by a client that only looks at `message`.

    The per-field detail lives in `fields[].msg`, so a bare "Request validation failed"
    told a caller that did not know to go digging nothing they could act on - and the
    caller is not always this project's own browser.
    """
    if not reasons:
        return "Request validation failed"
    head = reasons[:_MAX_SUMMARY_REASONS]
    extra = f" (+{len(reasons) - len(head)} more)" if len(reasons) > len(head) else ""
    return "Request validation failed: " + "; ".join(head) + extra


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error_handler(_: Request, exc: APIError) -> ORJSONResponse:
        return ORJSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> ORJSONResponse:
        errors = list(exc.errors())
        fields = [
            {"loc": [str(x) for x in err.get("loc", [])], "msg": err.get("msg"), "type": err.get("type")}
            for err in errors
        ]
        return ORJSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": validation_message(_reasons(errors)),
                    "fields": fields,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_: Request, exc: StarletteHTTPException) -> ORJSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "error"
        )
        return ORJSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
            headers=getattr(exc, "headers", None),
        )
