"""Domain errors that map to an HTTP status and a user-facing message.

Every error response has the same envelope: {"error", "message", "request_id", ...extra}.
"""

from typing import Any

import structlog


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def to_body(self) -> dict:
        return error_body(self.code, self.message, **self.extra)


def current_request_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("request_id")


def error_body(code: str, message: str, **extra: Any) -> dict:
    return {"error": code, "message": message, "request_id": current_request_id(), **extra}


PRICE_UNAVAILABLE_MESSAGE = (
    "Gold prices are temporarily unavailable, so nothing was charged. Please try again in a few minutes."
)


def price_unavailable() -> AppError:
    return AppError(503, "price_unavailable", PRICE_UNAVAILABLE_MESSAGE)
