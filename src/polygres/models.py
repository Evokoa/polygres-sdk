from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


def _to_dict(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_dict(item) for key, item in value.items()}
    return value


@dataclass
class Page(Generic[T]):
    results: list[T]
    next_cursor: str | None
    has_more: bool
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _fetch_next: Callable[[str], Page[T]] | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def from_api(
        cls,
        payload: dict[str, Any],
        parser: Callable[[dict[str, Any]], T],
        fetch_next: Callable[[str], Page[T]] | None = None,
    ) -> Page[T]:
        return cls(
            results=[parser(item) for item in payload.get("results", [])],
            next_cursor=payload.get("next_cursor"),
            has_more=bool(payload.get("has_more", False)),
            request_id=payload.get("request_id"),
            metadata={
                key: value
                for key, value in payload.items()
                if key not in {"results", "next_cursor", "has_more", "request_id"}
            },
            _fetch_next=fetch_next,
        )

    def auto_paging_iter(self) -> Iterator[T]:
        page: Page[T] = self
        while True:
            yield from page.results
            if not page.has_more or not page.next_cursor or page._fetch_next is None:
                return
            page = page._fetch_next(page.next_cursor)

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [_to_dict(item) for item in self.results],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "request_id": self.request_id,
            "metadata": _to_dict(self.metadata),
        }


@dataclass
class ConnectionInfo:
    project_id: str
    database: str
    username: str
    port: int
    direct_host: str
    pooled_host: str
    direct_url_without_password: str
    pooled_url_without_password: str
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> ConnectionInfo:
        direct = payload.get("direct", {})
        pooled = payload.get("pooled", {})
        return cls(
            project_id=payload["project_id"],
            database=payload["database"],
            username=payload["username"],
            port=int(payload["port"]),
            direct_host=direct["host"],
            pooled_host=pooled["host"],
            direct_url_without_password=direct["connection_string_without_password"],
            pooled_url_without_password=pooled["connection_string_without_password"],
            request_id=payload.get("request_id"),
            metadata=dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetrievalReadiness:
    project_id: str
    graph: dict[str, Any]
    vector: dict[str, Any]
    hybrid: dict[str, Any]
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> RetrievalReadiness:
        return cls(
            project_id=payload["project_id"],
            graph=dict(payload.get("graph", {})),
            vector=dict(payload.get("vector", {})),
            hybrid=dict(payload.get("hybrid", {})),
            request_id=payload.get("request_id"),
            metadata=dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphNode:
    schema: str
    table: str
    id: str
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GraphNode:
        return cls(
            schema=payload["schema"],
            table=payload["table"],
            id=str(payload["id"]),
            properties=dict(payload.get("properties", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphPathStep:
    step: int
    node: GraphNode
    edge_label: str | None = None
    readable_path: str | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GraphPathStep:
        node = payload.get("node", payload)
        return cls(
            step=int(payload.get("step", 0)),
            node=GraphNode.from_api(node),
            edge_label=payload.get("edge_label"),
            readable_path=payload.get("readable_path"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "node": self.node.to_dict(),
            "edge_label": self.edge_label,
            "readable_path": self.readable_path,
        }


@dataclass
class GraphResult:
    node: GraphNode
    depth: int
    rank: int | None
    graph_score: float | None
    path: list[Any] | None
    edge_path: list[Any] | None
    readable_path: str | None
    relationships: list[Any] = field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GraphResult:
        node_payload = dict(payload["node"])
        if "properties" not in node_payload:
            node_payload["properties"] = payload.get("properties", {})
        return cls(
            node=GraphNode.from_api(node_payload),
            depth=int(payload.get("depth", 0)),
            rank=payload.get("rank"),
            graph_score=payload.get("graph_score"),
            path=payload.get("path"),
            edge_path=payload.get("edge_path"),
            readable_path=payload.get("readable_path"),
            relationships=list(payload.get("relationships", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node.to_dict(),
            "depth": self.depth,
            "rank": self.rank,
            "graph_score": self.graph_score,
            "path": self.path,
            "edge_path": self.edge_path,
            "readable_path": self.readable_path,
            "relationships": self.relationships,
        }


@dataclass
class VectorResult:
    schema: str
    table: str
    id: str
    properties: dict[str, Any]
    distance: float
    similarity: float | None
    score: float

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> VectorResult:
        return cls(
            schema=payload["schema"],
            table=payload["table"],
            id=str(payload["id"]),
            properties=dict(payload.get("properties", {})),
            distance=float(payload["distance"]),
            similarity=payload.get("similarity"),
            score=float(payload["score"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TextResult:
    schema: str
    table: str
    id: str
    properties: dict[str, Any]
    score: float
    similarity: float | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> TextResult:
        return cls(
            schema=payload["schema"],
            table=payload["table"],
            id=str(payload["id"]),
            properties=dict(payload.get("properties", {})),
            score=float(payload["score"]),
            similarity=payload.get("similarity"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HybridResult:
    schema: str
    table: str
    id: str
    properties: dict[str, Any]
    score: float
    vector_score: float | None
    graph_score: float | None
    distance: float | None
    similarity: float | None
    relationships: list[Any]

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> HybridResult:
        node = payload.get("node", payload)
        score = payload.get("score", payload.get("rrf_score", 0.0))
        return cls(
            schema=node["schema"],
            table=node["table"],
            id=str(node["id"]),
            properties=dict(payload.get("properties", node.get("properties", {}))),
            score=float(score),
            vector_score=payload.get("vector_score"),
            graph_score=payload.get("graph_score"),
            distance=payload.get("distance"),
            similarity=payload.get("similarity"),
            relationships=list(payload.get("relationships", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphPathResponse:
    paths: list[dict[str, Any]]
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GraphPathResponse:
        return cls(
            paths=list(payload.get("paths", [])),
            request_id=payload.get("request_id"),
            metadata={
                key: value for key, value in payload.items() if key not in {"paths", "request_id"}
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphConnectionResponse:
    connections: list[dict[str, Any]]
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GraphConnectionResponse:
        return cls(
            connections=list(payload.get("connections", [])),
            request_id=payload.get("request_id"),
            metadata={
                key: value
                for key, value in payload.items()
                if key not in {"connections", "request_id"}
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
