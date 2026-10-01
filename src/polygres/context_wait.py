from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

from polygres._vendor.polygres_lib.context import ContextOperation
from polygres.errors import (
    PolygresAPIError,
    PolygresAuthError,
    PolygresNotFoundError,
    PolygresPermissionError,
    PolygresRateLimitError,
    PolygresRuntimeError,
    PolygresValidationError,
    canonical_error_details,
)

OperationFetcher = Callable[[float], tuple[ContextOperation, str | None]]


def context_poll_interval(elapsed_stage_seconds: float) -> float:
    if elapsed_stage_seconds <= 10:
        return 2.0
    if elapsed_stage_seconds <= 60:
        return 5.0
    if elapsed_stage_seconds <= 300:
        return 15.0
    return 30.0


def wait_for_context_operation(
    operation_id: str,
    *,
    initial: ContextOperation | None,
    fetch: OperationFetcher,
    timeout: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ContextOperation:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
        or not math.isfinite(float(timeout))
    ):
        raise PolygresValidationError("timeout must be positive")
    started = monotonic()
    deadline = started + timeout
    operation = initial
    retry_after: str | None = None
    observed_stage: str | None = None
    stage_started = started

    while True:
        if operation is None:
            try:
                operation, retry_after = fetch(deadline)
            except PolygresRuntimeError as exc:
                if monotonic() >= deadline:
                    raise _timeout_error(operation_id, None) from exc
                raise

        status = _wire_value(operation.status)
        if status == "succeeded":
            return operation
        if status == "failed":
            raise _failed_operation_error(operation_id, operation)
        if status == "cancelled":
            raise PolygresAPIError(
                "Context operation was cancelled.",
                status_code=409,
                request_id=_request_id(operation),
                code="CONTEXT_OPERATION_CANCELLED",
                details={
                    "operation_id": operation_id,
                    "operation_status": "cancelled",
                },
            )

        now = monotonic()
        stage = operation.stage
        if stage != observed_stage:
            observed_stage = stage
            stage_started = now
        delay = _retry_after_seconds(retry_after)
        if delay is None:
            delay = context_poll_interval(max(now - stage_started, 0.0))
        remaining = deadline - now
        if remaining <= 0:
            raise _timeout_error(operation_id, operation)
        if delay >= remaining:
            sleep(remaining)
            raise _timeout_error(operation_id, operation)
        sleep(delay)
        operation = None
        retry_after = None


def _wire_value(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(seconds, 0.0)


def _failed_operation_error(
    operation_id: str,
    operation: ContextOperation,
) -> Exception:
    failure = operation.error
    if failure is None:
        return PolygresRuntimeError(
            "Context operation failed.",
            status_code=500,
            request_id=_request_id(operation),
            code="CONTEXT_OPERATION_FAILED",
            details={
                "operation_id": operation_id,
                "operation_status": "failed",
            },
        )
    details = dict(failure.details)
    canonical = canonical_error_details(failure.code, failure.variant, details)
    if canonical is None:
        canonical = canonical_error_details("CONTEXT_OPERATION_FAILED", None, {})
    assert canonical is not None
    details = canonical.details
    details.setdefault("operation_id", operation_id)
    details["operation_status"] = "failed"
    kwargs = {
        "status_code": canonical.status_code,
        "request_id": _request_id(operation),
        "code": canonical.code,
        "details": _redact_secret_value(details),
    }
    exception_type: type[Exception]
    if canonical.status_code == 400:
        exception_type = PolygresValidationError
    elif canonical.status_code == 401:
        exception_type = PolygresAuthError
    elif canonical.status_code == 403:
        exception_type = PolygresPermissionError
    elif canonical.status_code == 404:
        exception_type = PolygresNotFoundError
    elif canonical.status_code == 429:
        exception_type = PolygresRateLimitError
    elif canonical.status_code in {408, 500, 502, 503, 504}:
        exception_type = PolygresRuntimeError
    else:
        exception_type = PolygresAPIError
    return exception_type(canonical.message, **kwargs)


def _timeout_error(
    operation_id: str,
    operation: ContextOperation | None,
) -> PolygresRuntimeError:
    return PolygresRuntimeError(
        f"Timed out waiting for Context operation {operation_id}; it is still running.",
        status_code=None,
        request_id=_request_id(operation) if operation is not None else None,
        code="CONTEXT_OPERATION_TIMEOUT",
        details={"operation_id": operation_id},
    )


def _request_id(operation: ContextOperation) -> str | None:
    value = (operation.model_extra or {}).get("request_id")
    return _redact_secret(str(value)) if value else None


def _redact_secret(value: str) -> str:
    return re.sub(r"poly_live_[0-9a-fA-F]{32}", "[REDACTED]", value)


def _redact_secret_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_secret(value)
    if isinstance(value, list):
        return [_redact_secret_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_secret_value(item) for key, item in value.items()}
    return value
