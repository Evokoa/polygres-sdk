from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUCCESS = 0
GENERAL_FAILURE = 1
USAGE = 2
AUTH = 3
PERMISSION = 4
NOT_FOUND = 5
CONFLICT = 6
RATE_LIMITED = 7
UNAVAILABLE = 8
LOCAL_DEPENDENCY = 9

HTTP_EXIT_CODES = {
    400: USAGE,
    401: AUTH,
    403: PERMISSION,
    404: NOT_FOUND,
    409: CONFLICT,
    422: USAGE,
    429: RATE_LIMITED,
    500: UNAVAILABLE,
    502: UNAVAILABLE,
    503: UNAVAILABLE,
    504: UNAVAILABLE,
}


@dataclass
class CliError(Exception):
    code: str
    message: str
    exit_code: int = GENERAL_FAILURE
    details: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None

    def __str__(self) -> str:
        return self.message


class UsageError(CliError):
    def __init__(self, message: str, *, code: str = "INVALID_USAGE") -> None:
        super().__init__(code=code, message=message, exit_code=USAGE)


def api_error_from_response(status_code: int, payload: dict[str, Any] | None) -> CliError:
    payload = payload or {}
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {}
    return CliError(
        code=str(error.get("code") or _default_code(status_code)),
        message=str(error.get("message") or _default_message(status_code)),
        details=error.get("details") if isinstance(error.get("details"), dict) else {},
        request_id=payload.get("request_id"),
        exit_code=HTTP_EXIT_CODES.get(status_code, GENERAL_FAILURE),
    )


def _default_code(status_code: int) -> str:
    if status_code == 401:
        return "AUTH_REQUIRED"
    if status_code == 403:
        return "PERMISSION_DENIED"
    if status_code == 404:
        return "NOT_FOUND"
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code in {500, 502, 503, 504}:
        return "SERVICE_UNAVAILABLE"
    return "API_ERROR"


def _default_message(status_code: int) -> str:
    if status_code == 401:
        return "Run `polygres login` to continue."
    if status_code == 403:
        return "Permission denied."
    if status_code == 404:
        return "Resource not found."
    if status_code == 429:
        return "Rate limited."
    if status_code in {500, 502, 503, 504}:
        return "Polygres API is unavailable."
    return "Polygres API request failed."
