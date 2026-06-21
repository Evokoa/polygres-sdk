from __future__ import annotations

from typing import Any


class PolygresError(Exception):
    """Base Polygres SDK exception."""


class PolygresValidationError(PolygresError, ValueError):
    """Raised before a request is sent when local validation fails."""


class PolygresAPIError(PolygresError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.code = code
        self.details = details or {}


class PolygresAuthError(PolygresAPIError):
    """Raised for authentication failures."""


class PolygresPermissionError(PolygresAPIError):
    """Raised for authorization failures."""


class PolygresNotFoundError(PolygresAPIError):
    """Raised when a requested resource is not found."""


class PolygresRateLimitError(PolygresAPIError):
    """Raised when the API rate limits a request."""


class PolygresRuntimeError(PolygresAPIError):
    """Raised for transient API/runtime failures."""
