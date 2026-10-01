"""Runtime-delivered notices about newer Polygres SDK releases."""

from __future__ import annotations

import re
import time
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Literal, cast
from urllib.parse import urlparse

import httpx

from polygres._vendor.polygres_lib.api_versions import (
    SDK_NOTICE_ID_HEADER,
    SDK_NOTICE_RECOMMENDED_VERSION_HEADER,
    SDK_NOTICE_SEVERITY_HEADER,
    SDK_NOTICE_URL_HEADER,
)

_NOTICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})"
    r"(?:-([0-9A-Za-z-]{1,16}(?:\.[0-9A-Za-z-]{1,16}){0,4}))?"
    r"(?:\+[0-9A-Za-z-]{1,16}(?:\.[0-9A-Za-z-]{1,16}){0,4})?$"
)
_SEVERITIES = frozenset({"info", "warning", "critical"})
_MAX_URL_LENGTH = 2048
_MAX_SEEN_NOTICES = 128
_CENTRAL_NOTICE_URL = "https://api.polygres.com/v1/sdk/notices"
_CENTRAL_NOTICE_TIMEOUT_SECONDS = 2.0
_CENTRAL_NOTICE_REFRESH_SECONDS = 10 * 60 * 60
_CENTRAL_NOTICE_RETRY_SECONDS = 10 * 60


class PolygresVersionWarning(UserWarning):
    """A visible warning that recommends upgrading the installed SDK."""


@dataclass(frozen=True, slots=True)
class PolygresVersionNotice:
    """A validated SDK release notice delivered by the Runtime API."""

    id: str
    current_version: str
    recommended_version: str
    severity: Literal["info", "warning", "critical"]
    url: str | None = None
    deprecation_at: datetime | None = None
    sunset_at: datetime | None = None

    @property
    def message(self) -> str:
        message = (
            f"Polygres SDK {self.recommended_version} is available; "
            f"you are using {self.current_version}."
        )
        if self.sunset_at is not None:
            message += f" Support for this API resource is expected to end {self.sunset_at.date()}."
        if self.url is not None:
            message += f" See {self.url}"
        return message


_seen_notice_ids: set[str] = set()
_seen_notice_lock = Lock()
_central_notice_lock = Lock()
_central_notice_next_check = 0.0
_central_notice_etag: str | None = None


def parse_version_notice(
    headers: Mapping[str, str], *, current_version: str
) -> PolygresVersionNotice | None:
    """Return a validated actionable notice, ignoring malformed or stale input."""

    notice_id = headers.get(SDK_NOTICE_ID_HEADER)
    recommended = headers.get(SDK_NOTICE_RECOMMENDED_VERSION_HEADER)
    severity = headers.get(SDK_NOTICE_SEVERITY_HEADER, "info")
    comparison = _compare_semver(recommended, current_version) if recommended is not None else None
    if (
        notice_id is None
        or recommended is None
        or _NOTICE_ID_RE.fullmatch(notice_id) is None
        or severity not in _SEVERITIES
        or comparison is None
        or comparison <= 0
    ):
        return None

    url = headers.get(SDK_NOTICE_URL_HEADER)
    if url is not None and not _is_safe_notice_url(url):
        return None

    return PolygresVersionNotice(
        id=notice_id,
        current_version=current_version,
        recommended_version=recommended,
        severity=cast(Literal["info", "warning", "critical"], severity),
        url=url,
        deprecation_at=_parse_deprecation(headers.get("Deprecation")),
        sunset_at=_parse_sunset(headers.get("Sunset")),
    )


def emit_version_notice(headers: Mapping[str, str], *, current_version: str) -> None:
    """Emit each valid Runtime-delivered SDK notice once per process."""

    notice = parse_version_notice(headers, current_version=current_version)
    if notice is None:
        return
    with _seen_notice_lock:
        if notice.id in _seen_notice_ids:
            return
        if len(_seen_notice_ids) >= _MAX_SEEN_NOTICES:
            return
        _seen_notice_ids.add(notice.id)
    warnings.warn(notice.message, PolygresVersionWarning, stacklevel=4)


def check_central_version_notices(*, current_version: str) -> None:
    """Fetch public SDK notices at most once per refresh interval."""

    global _central_notice_etag, _central_notice_next_check

    now = time.monotonic()
    if now < _central_notice_next_check or not _central_notice_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now < _central_notice_next_check:
            return
        headers = {"User-Agent": f"polygres-python/{current_version}"}
        if _central_notice_etag is not None:
            headers["If-None-Match"] = _central_notice_etag
        try:
            response = httpx.get(
                _CENTRAL_NOTICE_URL,
                params={"version": current_version, "channel": _release_channel(current_version)},
                headers=headers,
                timeout=_CENTRAL_NOTICE_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            if response.status_code == 304:
                _central_notice_next_check = now + _CENTRAL_NOTICE_REFRESH_SECONDS
                return
            response.raise_for_status()
            payload = response.json()
            notices = payload.get("notices") if isinstance(payload, dict) else None
            if not isinstance(notices, list):
                raise ValueError("invalid SDK notice response")
            for value in notices[:_MAX_SEEN_NOTICES]:
                if not isinstance(value, dict):
                    continue
                notice_headers = {
                    SDK_NOTICE_ID_HEADER: value.get("id"),
                    SDK_NOTICE_RECOMMENDED_VERSION_HEADER: value.get("recommended_version"),
                    SDK_NOTICE_SEVERITY_HEADER: value.get("severity", "info"),
                }
                if value.get("url") is not None:
                    notice_headers[SDK_NOTICE_URL_HEADER] = value.get("url")
                if all(isinstance(item, str) for item in notice_headers.values()):
                    emit_version_notice(notice_headers, current_version=current_version)
            _central_notice_etag = response.headers.get("ETag")
            _central_notice_next_check = now + _CENTRAL_NOTICE_REFRESH_SECONDS
        except (ValueError, httpx.HTTPError):
            _central_notice_next_check = now + _CENTRAL_NOTICE_RETRY_SECONDS
    finally:
        _central_notice_lock.release()


def _release_channel(version: str) -> str:
    parsed = _parse_semver(version)
    if parsed is None or not parsed[3]:
        return "stable"
    label = parsed[3][0].lower()
    for channel in ("preview", "alpha", "beta", "rc"):
        if label.startswith(channel):
            return channel
    return "preview"


def _parse_semver(value: str) -> tuple[int, int, int, tuple[str, ...]] | None:
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease


def _compare_semver(left: str, right: str) -> int | None:
    parsed_left = _parse_semver(left)
    parsed_right = _parse_semver(right)
    if parsed_left is None or parsed_right is None:
        return None
    if parsed_left[:3] != parsed_right[:3]:
        return -1 if parsed_left[:3] < parsed_right[:3] else 1
    left_pre, right_pre = parsed_left[3], parsed_right[3]
    if left_pre == right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    for left_part, right_part in zip(left_pre, right_pre, strict=False):
        if left_part == right_part:
            continue
        if left_part.isdigit() and right_part.isdigit():
            return -1 if int(left_part) < int(right_part) else 1
        if left_part.isdigit() != right_part.isdigit():
            return -1 if left_part.isdigit() else 1
        return -1 if left_part < right_part else 1
    return -1 if len(left_pre) < len(right_pre) else 1


def _is_safe_notice_url(value: str) -> bool:
    if (
        len(value) > _MAX_URL_LENGTH
        or not value.isascii()
        or any(character in value for character in '<>"\\')
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _parse_deprecation(value: str | None) -> datetime | None:
    if value is None or not value.startswith("@") or not value[1:].isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(value[1:]), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_sunset(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = ["PolygresVersionNotice", "PolygresVersionWarning"]
