from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from polygres.errors import (
    PolygresAPIError,
    PolygresAuthError,
    PolygresNotFoundError,
    PolygresPermissionError,
    PolygresRateLimitError,
    PolygresRuntimeError,
    PolygresValidationError,
)
from polygres.models import (
    ConnectionInfo,
    GraphConnectionResponse,
    GraphPathResponse,
    GraphResult,
    HybridResult,
    Page,
    RetrievalReadiness,
    TextResult,
    VectorResult,
)

API_KEY_RE = re.compile(r"^poly_live_[0-9a-f]{32}$")
PROJECT_RE = re.compile(r"^p[a-z0-9]{23}$")
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
VERSION = "0.1.0"


class Polygres:
    def __init__(
        self,
        *,
        api_key: str,
        runtime_url: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = 30.0,
        connect_timeout: float = 10.0,
        max_retries: int = 2,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not API_KEY_RE.match(api_key):
            raise PolygresValidationError("API key must match poly_live_[32hex]")
        selected_url = _select_runtime_url(runtime_url=runtime_url, base_url=base_url)
        _validate_base_url(selected_url)
        _validate_positive_timeout(connect_timeout, "connect_timeout")
        if isinstance(timeout, (int, float)):
            _validate_positive_timeout(float(timeout), "timeout")
            timeout_config: float | httpx.Timeout = httpx.Timeout(
                float(timeout), connect=connect_timeout
            )
        elif isinstance(timeout, httpx.Timeout):
            timeout_config = timeout
        else:
            raise PolygresValidationError("timeout must be a positive number or httpx.Timeout")
        if max_retries < 0 or max_retries > 5:
            raise PolygresValidationError("max_retries must be between 0 and 5")
        if headers is not None and not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise PolygresValidationError("headers must contain string keys and values")

        self._api_key = api_key
        self._base_url = selected_url.rstrip("/")
        self._timeout = timeout_config
        self._max_retries = max_retries
        self._headers = headers or {}
        self._client = httpx.Client(timeout=timeout_config)

    def project(self, project_id: str | None = None) -> Project:
        if project_id is not None and not PROJECT_RE.match(project_id):
            raise PolygresValidationError("project id must match ^p[a-z0-9]{23}$")
        return Project(self, project_id)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Polygres:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _headers_for(self) -> dict[str, str]:
        headers = dict(self._headers)
        headers.update(
            {
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": f"polygres-python/{VERSION}",
            }
        )
        return headers

    def _get(
        self,
        path: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET", path, timeout=timeout, max_retries=max_retries
        )

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            path,
            json=payload,
            timeout=timeout,
            max_retries=max_retries,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        retry_budget = self._max_retries if max_retries is None else max_retries
        if retry_budget < 0 or retry_budget > 5:
            raise PolygresValidationError("max_retries must be between 0 and 5")
        timeout_config = self._timeout if timeout is None else timeout
        if isinstance(timeout_config, (int, float)):
            _validate_positive_timeout(float(timeout_config), "timeout")
        url = f"{self._base_url}{path}"
        for attempt in range(retry_budget + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=self._headers_for(),
                    json=json,
                    timeout=timeout_config,
                )
            except httpx.TimeoutException as exc:
                if attempt < retry_budget:
                    _sleep_before_retry(attempt, None)
                    continue
                raise PolygresRuntimeError(
                    "Polygres request timed out", status_code=None
                ) from exc
            except httpx.NetworkError as exc:
                if attempt < retry_budget:
                    _sleep_before_retry(attempt, None)
                    continue
                raise PolygresRuntimeError(
                    "Polygres network request failed", status_code=None
                ) from exc

            if response.status_code in RETRY_STATUSES and attempt < retry_budget:
                _sleep_before_retry(attempt, response.headers.get("Retry-After"))
                continue
            if response.is_error:
                raise _api_error(response)
            return response.json()
        raise PolygresRuntimeError("Polygres request failed")


@dataclass
class Project:
    _client: Polygres
    project_id: str | None
    graph: GraphNamespace = field(init=False)
    vector: VectorNamespace = field(init=False)
    text: TextNamespace = field(init=False)
    hybrid: HybridNamespace = field(init=False)

    def __post_init__(self) -> None:
        self.graph = GraphNamespace(self)
        self.vector = VectorNamespace(self)
        self.text = TextNamespace(self)
        self.hybrid = HybridNamespace(self)

    def readiness(self) -> RetrievalReadiness:
        payload = self._client._get("/retrieval/readiness")
        return RetrievalReadiness.from_api(payload)

    def connection_info(self) -> ConnectionInfo:
        payload = self._client._get("/connection-info")
        return ConnectionInfo.from_api(payload)

    def _post_page(
        self,
        path: str,
        payload: dict[str, Any],
        parser: Any,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
    ) -> Page[Any]:
        response = self._client._post(
            path,
            _compact(payload),
            timeout=timeout,
            max_retries=max_retries,
        )

        def fetch_next(cursor: str) -> Page[Any]:
            return self._post_page(
                path,
                {**payload, "cursor": cursor},
                parser,
                timeout=timeout,
                max_retries=max_retries,
            )

        return Page.from_api(response, parser, fetch_next)


@dataclass
class GraphNamespace:
    _project: Project

    def expand(
        self,
        start: dict[str, Any] | list[dict[str, Any]],
        *,
        max_depth: int = 5,
        relationship_types: list[str] | None = None,
        direction: str = "out",
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> Page[GraphResult]:
        _validate_required(start, "start")
        _validate_range(max_depth, "max_depth", 1, 20)
        _validate_range(limit, "limit", 1, 1000)
        payload = {
            "start": start,
            "max_depth": max_depth,
            "relationship_types": relationship_types,
            "direction": _sdk_direction(direction),
            "filters": filters or {},
            "limit": limit,
            "cursor": cursor,
        }
        return self._project._post_page(
            "/graph/expand",
            payload,
            GraphResult.from_api,
            timeout=timeout,
            max_retries=max_retries,
        )

    def neighborhood(
        self,
        start: dict[str, Any],
        *,
        radius: int = 2,
        relationship_types: list[str] | None = None,
        direction: str = "any",
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[GraphResult]:
        _validate_range(radius, "radius", 1, 20)
        _validate_range(limit, "limit", 1, 1000)
        return self._project._post_page(
            "/graph/neighborhood",
            {
                "start": start,
                "max_depth": radius,
                "relationship_types": relationship_types,
                "direction": _sdk_direction(direction),
                "filters": filters or {},
                "limit": limit,
                "cursor": cursor,
            },
            GraphResult.from_api,
        )

    def related(
        self,
        start: dict[str, Any],
        *,
        relationship_types: list[str] | None = None,
        direction: str = "any",
        filters: dict[str, Any] | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[GraphResult]:
        _validate_range(limit, "limit", 1, 1000)
        return self._project._post_page(
            "/graph/related",
            {
                "start": start,
                "max_depth": 1,
                "relationship_types": relationship_types,
                "direction": _sdk_direction(direction),
                "filters": filters or {},
                "limit": limit,
                "cursor": cursor,
            },
            GraphResult.from_api,
        )

    def path(
        self,
        source: dict[str, Any],
        target: dict[str, Any],
        *,
        max_depth: int = 5,
        relationship_types: list[str] | None = None,
        direction: str = "any",
    ) -> GraphPathResponse:
        _validate_required(source, "source")
        _validate_required(target, "target")
        _validate_range(max_depth, "max_depth", 1, 20)
        payload = _compact(
            {
                "source": source,
                "target": target,
                "max_depth": max_depth,
                "relationship_types": relationship_types,
                "direction": _sdk_direction(direction),
            }
        )
        response = self._project._client._post(
            "/graph/path",
            payload,
        )
        return GraphPathResponse.from_api(response)

    def connection(
        self,
        entities: list[dict[str, Any]],
        *,
        max_depth: int = 5,
        relationship_types: list[str] | None = None,
        direction: str = "any",
    ) -> GraphConnectionResponse:
        if len(entities) < 2 or len(entities) > 10:
            raise PolygresValidationError("entities must contain 2..10 items")
        _validate_range(max_depth, "max_depth", 1, 20)
        payload = _compact(
            {
                "entities": entities,
                "max_depth": max_depth,
                "relationship_types": relationship_types,
                "direction": _sdk_direction(direction),
            }
        )
        response = self._project._client._post(
            "/graph/connection",
            payload,
        )
        return GraphConnectionResponse.from_api(response)


@dataclass
class VectorNamespace:
    _project: Project

    def search(
        self,
        embedding: list[float],
        *,
        config: str | None = None,
        limit: int | None = None,
        filters: dict[str, Any] | None = None,
        max_distance: float | None = None,
        min_similarity: float | None = None,
        include_values: bool = False,
        cursor: str | None = None,
    ) -> Page[VectorResult]:
        _validate_embedding(embedding)
        _validate_vector_options(limit, max_distance, min_similarity)
        return self._project._post_page(
            "/vector/search",
            {
                "embedding": embedding,
                "config": config,
                "limit": limit,
                "filters": filters or {},
                "max_distance": max_distance,
                "min_similarity": min_similarity,
                "include_values": include_values,
                "cursor": cursor,
            },
            VectorResult.from_api,
        )

    def similar_to(
        self,
        row_id: str,
        *,
        config: str | None = None,
        limit: int | None = None,
        filters: dict[str, Any] | None = None,
        max_distance: float | None = None,
        min_similarity: float | None = None,
        include_values: bool = False,
        cursor: str | None = None,
    ) -> Page[VectorResult]:
        if not row_id:
            raise PolygresValidationError("row_id is required")
        _validate_vector_options(limit, max_distance, min_similarity)
        return self._project._post_page(
            "/vector/similar-to",
            {
                "row_id": row_id,
                "config": config,
                "limit": limit,
                "filters": filters or {},
                "max_distance": max_distance,
                "min_similarity": min_similarity,
                "include_values": include_values,
                "cursor": cursor,
            },
            VectorResult.from_api,
        )


@dataclass
class TextNamespace:
    _project: Project

    def tsvector(
        self,
        query: str,
        *,
        config: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
    ) -> Page[TextResult]:
        _validate_text_query(query)
        _validate_range(limit, "limit", 1, 1000)
        return self._text_page(
            "tsvector",
            {
                "query": query,
                "config": config,
                "limit": limit,
                "filters": filters or {},
                "cursor": cursor,
            },
        )

    def fuzzy(
        self,
        query: str,
        *,
        config: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
    ) -> Page[TextResult]:
        _validate_text_query(query)
        _validate_range(limit, "limit", 1, 1000)
        return self._text_page(
            "fuzzy",
            {
                "query": query,
                "config": config,
                "limit": limit,
                "filters": filters or {},
                "cursor": cursor,
            },
        )

    def _text_page(self, mode: str, payload: dict[str, Any]) -> Page[TextResult]:
        return self._project._post_page(
            f"/text/{mode}",
            payload,
            TextResult.from_api,
        )


@dataclass
class HybridNamespace:
    _project: Project

    def graph_first(
        self,
        start: dict[str, Any],
        embedding: list[float],
        *,
        config: str | None = None,
        max_depth: int = 2,
        relationship_types: list[str] | None = None,
        direction: str = "any",
        filters: dict[str, Any] | None = None,
        vector_weight: float = 0.7,
        graph_weight: float = 0.3,
        limit: int = 10,
        cursor: str | None = None,
    ) -> Page[HybridResult]:
        _validate_embedding(embedding)
        _validate_range(max_depth, "max_depth", 1, 20)
        _validate_range(limit, "limit", 1, 1000)
        return self._hybrid_page(
            "graph-first",
            {
                "start": start,
                "embedding": embedding,
                "config": config,
                "max_depth": max_depth,
                "relationship_types": relationship_types,
                "direction": _sdk_direction(direction),
                "filters": filters or {},
                "weights": {"vector": vector_weight, "graph": graph_weight},
                "limit": limit,
                "cursor": cursor,
            },
        )

    def vector_first(
        self,
        embedding: list[float],
        *,
        start: dict[str, Any] | None = None,
        config: str | None = None,
        vector_limit: int = 20,
        max_depth: int = 1,
        relationship_types: list[str] | None = None,
        direction: str = "any",
        filters: dict[str, Any] | None = None,
        vector_weight: float = 0.7,
        graph_weight: float = 0.3,
        limit: int = 10,
        cursor: str | None = None,
    ) -> Page[HybridResult]:
        _validate_embedding(embedding)
        _validate_range(vector_limit, "vector_limit", 1, 1000)
        _validate_range(max_depth, "max_depth", 1, 20)
        _validate_range(limit, "limit", 1, 1000)
        return self._hybrid_page(
            "vector-first",
            {
                "embedding": embedding,
                "start": start,
                "config": config,
                "vector_limit": vector_limit,
                "max_depth": max_depth,
                "relationship_types": relationship_types,
                "direction": _sdk_direction(direction),
                "filters": filters or {},
                "weights": {"vector": vector_weight, "graph": graph_weight},
                "limit": limit,
                "cursor": cursor,
            },
        )

    def joint(
        self,
        embedding: list[float],
        start: dict[str, Any],
        *,
        config: str | None = None,
        vector_weight: float = 0.7,
        graph_weight: float = 0.3,
        max_depth: int = 2,
        relationship_types: list[str] | None = None,
        direction: str = "any",
        filters: dict[str, Any] | None = None,
        vector_limit: int = 20,
        limit: int = 10,
        cursor: str | None = None,
    ) -> Page[HybridResult]:
        _validate_embedding(embedding)
        _validate_range(max_depth, "max_depth", 1, 20)
        _validate_range(vector_limit, "vector_limit", 1, 1000)
        _validate_range(limit, "limit", 1, 1000)
        return self._hybrid_page(
            "joint",
            {
                "embedding": embedding,
                "start": start,
                "config": config,
                "weights": {"vector": vector_weight, "graph": graph_weight},
                "max_depth": max_depth,
                "relationship_types": relationship_types,
                "direction": _sdk_direction(direction),
                "filters": filters or {},
                "vector_limit": vector_limit,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def _hybrid_page(self, mode: str, payload: dict[str, Any]) -> Page[HybridResult]:
        return self._project._post_page(
            f"/hybrid/{mode}",
            payload,
            HybridResult.from_api,
        )


def _select_runtime_url(*, runtime_url: str | None, base_url: str | None) -> str:
    normalized_runtime_url = runtime_url.rstrip("/") if runtime_url is not None else None
    normalized_base_url = base_url.rstrip("/") if base_url is not None else None
    if (
        normalized_runtime_url
        and normalized_base_url
        and normalized_runtime_url != normalized_base_url
    ):
        raise PolygresValidationError("runtime_url and base_url must match when both are provided")
    selected = normalized_runtime_url or normalized_base_url
    if selected is None:
        raise PolygresValidationError("runtime_url is required")
    return selected


def _validate_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}:
        return
    raise PolygresValidationError("base_url must be HTTPS except localhost development")


def _validate_positive_timeout(value: float, name: str) -> None:
    if value <= 0 or not math.isfinite(value):
        raise PolygresValidationError(f"{name} must be positive")


def _validate_required(value: Any, name: str) -> None:
    if not value:
        raise PolygresValidationError(f"{name} is required")


def _validate_range(value: int, name: str, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise PolygresValidationError(f"{name} must be between {minimum} and {maximum}")


def _validate_embedding(embedding: list[float]) -> None:
    if not embedding:
        raise PolygresValidationError("embedding must be non-empty")
    for value in embedding:
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise PolygresValidationError("embedding values must be finite numbers")


def _validate_text_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise PolygresValidationError("query must be non-empty")


def _validate_vector_options(
    limit: int | None, max_distance: float | None, min_similarity: float | None
) -> None:
    if limit is not None:
        _validate_range(limit, "limit", 1, 1000)
    if max_distance is not None and min_similarity is not None:
        raise PolygresValidationError("max_distance and min_similarity are mutually exclusive")


def _sdk_direction(direction: str) -> str:
    if direction not in {"out", "in", "any", "both"}:
        raise PolygresValidationError("direction must be out, in, any, or both")
    return "any" if direction == "both" else direction


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
    delay = 0.025 * (2**attempt) + random.uniform(0, 0.005)
    if retry_after:
        try:
            parsed_delay = float(retry_after)
            if parsed_delay >= 0:
                delay = parsed_delay
        except ValueError:
            pass
    time.sleep(delay)


def _api_error(response: httpx.Response) -> PolygresAPIError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    message = str(error.get("message") or f"Polygres API error {response.status_code}")
    request_id = body.get("request_id") if isinstance(body, dict) else None
    code = error.get("code")
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    kwargs = {
        "status_code": response.status_code,
        "request_id": request_id,
        "code": code,
        "details": details,
    }
    if response.status_code == 401:
        return PolygresAuthError(message, **kwargs)
    if response.status_code == 403:
        return PolygresPermissionError(message, **kwargs)
    if response.status_code == 404:
        return PolygresNotFoundError(message, **kwargs)
    if response.status_code == 429:
        return PolygresRateLimitError(message, **kwargs)
    if response.status_code in {408, 500, 502, 503, 504}:
        return PolygresRuntimeError(message, **kwargs)
    return PolygresAPIError(message, **kwargs)
