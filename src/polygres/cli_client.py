from __future__ import annotations

import json
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx

from polygres.cli_errors import AUTH, UNAVAILABLE, CliError, api_error_from_response
from polygres.cli_secrets import redact_string

VERSION = "0.2.0"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
HEAVY_REQUEST_TIMEOUT = 120.0


class CliControlPlaneClient:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        on_token_refresh: Callable[[dict[str, Any]], None] | None = None,
        on_refresh_auth_failure: Callable[[], None] | None = None,
        verbose: bool = False,
        trace: Callable[[str], None] | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._on_token_refresh = on_token_refresh
        self._on_refresh_auth_failure = on_refresh_auth_failure
        self._refresh_attempted = False
        self._verbose = verbose
        self._trace = trace
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CliControlPlaneClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def start_login(self, client: dict[str, Any]) -> dict[str, Any]:
        return self._post("/cli/auth/start", {"client": client}, auth=False)

    def poll_login(
        self, login_session_id: str, device_code: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return self._post(
            "/cli/auth/poll",
            {"login_session_id": login_session_id, "device_code": device_code},
            auth=False,
            retry=True,
            deadline=deadline,
        )

    def refresh_login(self, refresh_token: str) -> dict[str, Any]:
        return self._post("/cli/auth/refresh", {"refresh_token": refresh_token}, auth=False)

    def revoke_login(self, refresh_token: str) -> dict[str, Any]:
        return self._post("/cli/auth/revoke", {"refresh_token": refresh_token}, auth=False)

    def me(self) -> dict[str, Any]:
        return self._get("/me")

    def list_projects(self) -> dict[str, Any]:
        return self._get("/projects")

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}")

    def create_project(
        self,
        name: str,
        *,
        request_timeout: float | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/projects", {"name": name}, timeout=request_timeout, deadline=deadline
        )

    def get_project_status(
        self, project_id: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/status", deadline=deadline)

    def connection_info(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/connection-info")

    def list_api_keys(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/api-keys")

    def create_api_key(self, project_id: str, name: str) -> dict[str, Any]:
        return self._post(f"/projects/{project_id}/api-keys", {"name": name})

    def revoke_api_key(self, project_id: str, key_id: str) -> dict[str, Any]:
        return self._delete(f"/projects/{project_id}/api-keys/{key_id}")

    def csv_preview(self, project_id: str, file: Path, fields: dict[str, str]) -> dict[str, Any]:
        with file.open("rb") as handle:
            return self._multipart(
                "POST",
                f"/projects/{project_id}/imports/csv/preview",
                handle,
                file.name,
                fields,
            )

    def start_csv_import(
        self, project_id: str, file: Path, fields: dict[str, str]
    ) -> dict[str, Any]:
        with file.open("rb") as handle:
            return self._multipart(
                "POST",
                f"/projects/{project_id}/imports/csv",
                handle,
                file.name,
                fields,
            )

    def list_imports(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/imports")

    def get_import(
        self, project_id: str, job_id: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/imports/{job_id}", deadline=deadline)

    def migrations_list(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/migrations")

    def migrations_create(self, project_id: str, name: str, sql_body: str) -> dict[str, Any]:
        return self._post(
            f"/projects/{project_id}/migrations",
            {"name": name, "sql_body": sql_body},
        )

    def migrations_apply(self, project_id: str, migration_id: str) -> dict[str, Any]:
        return self._post(
            f"/projects/{project_id}/migrations/{migration_id}/apply",
            {},
            timeout=HEAVY_REQUEST_TIMEOUT,
        )

    def graph_discover(self, project_id: str) -> dict[str, Any]:
        return self._post(f"/projects/{project_id}/graph/discover", {})

    def get_graph_configuration(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/graph/configuration")

    def put_graph_configuration(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._put(
            f"/projects/{project_id}/graph/configuration",
            payload,
            timeout=HEAVY_REQUEST_TIMEOUT,
        )

    def graph_build(self, project_id: str) -> dict[str, Any]:
        return self._post(
            f"/projects/{project_id}/graph/build", {}, timeout=HEAVY_REQUEST_TIMEOUT
        )

    def graph_status(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/graph/status")

    def list_vector_configurations(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/vector/configurations")

    def create_vector_configuration(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._post(
            f"/projects/{project_id}/vector/configurations",
            payload,
            timeout=HEAVY_REQUEST_TIMEOUT,
        )

    def delete_vector_configuration(self, project_id: str, config_id: str) -> dict[str, Any]:
        return self._delete(f"/projects/{project_id}/vector/configurations/{config_id}")

    def reindex_vector_configuration(self, project_id: str, config_id: str) -> dict[str, Any]:
        return self._post(
            f"/projects/{project_id}/vector/configurations/{config_id}/reindex",
            {},
            timeout=HEAVY_REQUEST_TIMEOUT,
        )

    def list_text_configurations(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/text/configurations")

    def create_text_configuration(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            f"/projects/{project_id}/text/configurations",
            payload,
            timeout=HEAVY_REQUEST_TIMEOUT,
        )

    def delete_text_configuration(self, project_id: str, config_id: str) -> dict[str, Any]:
        return self._delete(f"/projects/{project_id}/text/configurations/{config_id}")

    def retrieval_readiness(self, project_id: str) -> dict[str, Any]:
        return self._get(f"/projects/{project_id}/retrieval/readiness")

    def _get(self, path: str, *, deadline: float | None = None) -> dict[str, Any]:
        return self._request("GET", path, retry=True, deadline=deadline)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        auth: bool = True,
        retry: bool = False,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            path,
            json=payload,
            auth=auth,
            retry=retry,
            timeout=timeout,
            deadline=deadline,
        )

    def _put(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        return self._request("PUT", path, json=payload, retry=False, timeout=timeout)

    def _delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", path, retry=False)

    def _multipart(
        self,
        method: str,
        path: str,
        file: BinaryIO,
        filename: str,
        fields: dict[str, str],
    ) -> dict[str, Any]:
        return self._request(
            method,
            path,
            data=fields,
            files={"file": (filename, file, "text/csv")},
            retry=False,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        auth: bool = True,
        retry: bool = False,
        allow_refresh: bool = True,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        if auth and not self._access_token:
            raise CliError("AUTH_REQUIRED", "Run `polygres login` to continue.", exit_code=3)
        headers = {"User-Agent": f"polygres-cli/{VERSION}"}
        if auth and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        url = f"{self._base_url}{path}"
        retry_budget = self._max_retries if retry else 0
        started = time.monotonic()
        response: httpx.Response | None = None
        for attempt in range(retry_budget + 1):
            remaining = _remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                raise CliError("TIMEOUT", "Command deadline expired.", exit_code=UNAVAILABLE)
            try:
                request_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "json": json,
                    "data": data,
                    "files": files,
                }
                request_timeout = timeout
                if remaining is not None:
                    request_timeout = min(request_timeout or self._timeout, remaining)
                if request_timeout is not None:
                    request_kwargs["timeout"] = request_timeout
                response = self._client.request(method, url, **request_kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < retry_budget:
                    _sleep_before_retry(attempt, None, deadline=deadline)
                    continue
                raise CliError(
                    "SERVICE_UNAVAILABLE",
                    "Polygres API is unavailable.",
                    exit_code=UNAVAILABLE,
                ) from exc
            if response.status_code in RETRY_STATUSES and attempt < retry_budget:
                _sleep_before_retry(
                    attempt, response.headers.get("Retry-After"), deadline=deadline
                )
                continue
            break
        assert response is not None
        elapsed_ms = int((time.monotonic() - started) * 1000)
        payload = _json_payload(response)
        if self._verbose:
            request_id = payload.get("request_id") if isinstance(payload, dict) else None
            self._emit_trace(method, path, response.status_code, elapsed_ms, request_id)
        if response.is_error:
            if (
                auth
                and allow_refresh
                and response.status_code == 401
                and self._refresh_token
                and self._refresh_access_token()
            ):
                return self._request(
                    method,
                    path,
                    json=json,
                    data=data,
                    files=files,
                    auth=auth,
                    retry=retry,
                    allow_refresh=False,
                    timeout=timeout,
                    deadline=deadline,
                )
            raise api_error_from_response(response.status_code, payload)
        return payload

    def _refresh_access_token(self) -> bool:
        if self._refresh_attempted or not self._refresh_token:
            return False
        self._refresh_attempted = True
        try:
            payload = self.refresh_login(self._refresh_token)
        except CliError as exc:
            if exc.exit_code == AUTH and self._on_refresh_auth_failure is not None:
                self._on_refresh_auth_failure()
            raise
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            if self._on_refresh_auth_failure is not None:
                self._on_refresh_auth_failure()
            request_id = payload.get("request_id")
            raise CliError(
                "AUTH_REFRESH_INVALID",
                "Refresh response did not include replacement tokens.",
                exit_code=AUTH,
                request_id=request_id if isinstance(request_id, str) else None,
            )
        self._access_token = access_token
        self._refresh_token = refresh_token
        if self._on_token_refresh is not None:
            self._on_token_refresh(payload)
        return True

    def _emit_trace(
        self, method: str, path: str, status: int, elapsed_ms: int, request_id: object
    ) -> None:
        if not self._trace:
            return
        parsed = urlsplit(path)
        rendered_path = parsed.path or path
        parts = [f"{method} {rendered_path} -> {status}", f"{elapsed_ms}ms"]
        if request_id:
            parts.append(f"request_id={request_id}")
        self._trace(redact_string(" ".join(parts)))


def _json_payload(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sleep_before_retry(
    attempt: int, retry_after: str | None, *, deadline: float | None = None
) -> None:
    delay = _retry_after_seconds(retry_after)
    if delay is None:
        delay = min(2**attempt, 5)
    remaining = _remaining_seconds(deadline)
    if remaining is not None:
        delay = min(delay, max(remaining, 0.0))
    if delay > 0:
        time.sleep(delay)


def _remaining_seconds(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(parsed.timestamp() - time.time(), 0.0)
