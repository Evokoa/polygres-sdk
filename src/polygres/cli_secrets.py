from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SECRET_FIELDS = {"access_token", "refresh_token", "password", "raw_key", "secret", "api_key"}
API_KEY_RE = re.compile(r"poly_live_[0-9a-f]{32}")
AUTH_RE = re.compile(r"Authorization:\s*Bearer\s+[^,\s]+", re.IGNORECASE)
URL_RE = re.compile(r"\b(?:postgresql?|https?)://[^\s\"']+")


def redact(value: Any, *, allow_key_secret: bool = False) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in SECRET_FIELDS and not (allow_key_secret and key == "secret"):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact(item, allow_key_secret=allow_key_secret)
        return redacted
    if isinstance(value, list):
        return [redact(item, allow_key_secret=allow_key_secret) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, allow_key_secret=allow_key_secret) for item in value)
    if isinstance(value, str):
        return redact_string(value)
    return value


def redact_string(value: str) -> str:
    value = AUTH_RE.sub("Authorization: Bearer [REDACTED]", value)
    value = API_KEY_RE.sub("[REDACTED]", value)
    return URL_RE.sub(lambda match: _redact_url(match.group(0)), value)


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.password:
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    auth = parts.username or ""
    if auth:
        auth = f"{auth}:[REDACTED]@"
    return urlunsplit((parts.scheme, f"{auth}{host}", parts.path, parts.query, parts.fragment))
