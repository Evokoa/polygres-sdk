from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from polygres.cli_errors import AUTH, CliError


def clear_auth(config: dict[str, Any]) -> dict[str, Any]:
    config.pop("auth", None)
    return config


def parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CliError(
            "AUTH_RESPONSE_INVALID",
            f"Authentication response did not include a valid {field}.",
            exit_code=AUTH,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CliError(
            "AUTH_RESPONSE_INVALID",
            f"Authentication response included an invalid {field}.",
            exit_code=AUTH,
        ) from exc
    if parsed.tzinfo is None:
        raise CliError(
            "AUTH_RESPONSE_INVALID",
            f"Authentication response included an invalid {field}.",
            exit_code=AUTH,
        )
    return parsed.astimezone(timezone.utc)


def validate_start_response(payload: dict[str, Any]) -> tuple[str, str, str, datetime, int]:
    values = tuple(payload.get(key) for key in ("login_session_id", "browser_url", "device_code"))
    if not all(isinstance(value, str) and value for value in values):
        raise CliError(
            "AUTH_RESPONSE_INVALID",
            "Authentication start response is incomplete.",
            exit_code=AUTH,
        )
    interval = payload.get("poll_interval_seconds", 2)
    if isinstance(interval, bool):
        interval = 2
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = 2
    return (
        str(values[0]),
        str(values[1]),
        str(values[2]),
        parse_timestamp(payload.get("expires_at"), field="expires_at"),
        min(max(interval, 1), 30),
    )


def validated_approved_auth(payload: dict[str, Any]) -> dict[str, Any]:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    user = payload.get("user")
    if not isinstance(access_token, str) or not access_token:
        raise CliError(
            "AUTH_RESPONSE_INVALID", "Login response omitted access_token.", exit_code=AUTH
        )
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CliError(
            "AUTH_RESPONSE_INVALID", "Login response omitted refresh_token.", exit_code=AUTH
        )
    if not isinstance(user, dict):
        raise CliError("AUTH_RESPONSE_INVALID", "Login response omitted user.", exit_code=AUTH)
    expires_at = payload.get("expires_at")
    parse_timestamp(expires_at, field="expires_at")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "user": user,
    }
