"""Typed API exceptions and FastAPI exception handlers."""
from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class APIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        params: dict | None = None,
    ) -> None:
        """`code` names the rule; `message` spells it out in one language.

        A screen shows the code, so a teacher's or learner's own language comes from their
        device rather than from this service's English. `params` carries the numbers a
        translated sentence needs - "that answer is worth {{max}}" - so the screen can put
        the rule into words without quoting the backend.
        """
        self.code = code
        self.message = message
        self.http_status = http_status
        self.params = params or {}
        super().__init__(message)


class RuleBroken:
    """Names the rule a caller broke.

    The sentence is written for whoever reads the API directly, and it stays so that a
    colleague's script or a log entry still makes sense on its own. The product's own
    screens never show it: they ask after `code`, translate that into the language the
    person is reading the interface in, and take the numbers a sentence needs from
    `params` - so "two of those students" arrives as two, not as English.
    """

    code: str = "not_allowed"
    params: dict | None = None

    def __init__(self, message: str, *, code: str | None = None, params: dict | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.params = params


class Unauthorized(APIError):
    def __init__(self, message: str = "Not authenticated") -> None:
        super().__init__("unauthorized", message, status.HTTP_401_UNAUTHORIZED)


class Forbidden(APIError):
    def __init__(self, message: str = "Not permitted") -> None:
        super().__init__("forbidden", message, status.HTTP_403_FORBIDDEN)


class NotFound(APIError):
    def __init__(self, message: str = "Not found", *, code: str = "not_found") -> None:
        super().__init__(code, message, status.HTTP_404_NOT_FOUND)


class Conflict(APIError):
    def __init__(self, code: str, message: str, *, params: dict | None = None) -> None:
        super().__init__(code, message, status.HTTP_409_CONFLICT, params)


class ValidationFailed(APIError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "validation_failed",
        params: dict | None = None,
    ) -> None:
        # The numeric code, because Starlette deprecated `HTTP_422_UNPROCESSABLE_ENTITY`
        # in favour of a renamed alias that older versions do not have.
        super().__init__(code, message, 422, params)


#: How many distinct reasons one summary line carries. Forty bad rows in a bulk body is
#: one mistake repeated, and the first few sentences are what a caller can act on.
_MAX_SUMMARY_REASONS = 3


def as_invalid(exc: RuleBroken) -> ValidationFailed:
    """A rule the caller broke, answered 422 with the rule's own name."""
    return ValidationFailed(str(exc), code=exc.code, params=exc.params)


def as_missing(exc: RuleBroken) -> NotFound:
    """404. What the caller named did not resolve, or was never theirs to name."""
    return NotFound(str(exc), code=exc.code)


def as_conflict(exc: RuleBroken) -> Conflict:
    """409: the request was legal until something else in the bank said no."""
    return Conflict(exc.code, str(exc), params=exc.params)


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
        body = {"code": exc.code, "message": exc.message}
        if exc.params:
            body["params"] = exc.params
        return ORJSONResponse(
            status_code=exc.http_status,
            content={"error": body},
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
