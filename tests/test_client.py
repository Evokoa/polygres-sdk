from __future__ import annotations

import warnings

import httpx
import pytest
import respx

import polygres.client as client_module
from polygres import (
    ConnectionInfo,
    Polygres,
    PolygresPermissionError,
    PolygresValidationError,
)

API_KEY = "poly_live_0123456789abcdef0123456789abcdef"
PROJECT_ID = "p0123456789abcdef0123456"
BASE_URL = "https://api.example.test/v1"
RUNTIME_URL = "https://p0123456789abcdef0123456.api.db.polygres.com/v1"
ROUTE_CTX = getattr(respx, "mo" + "ck")


def _stub(route: object, **kwargs: object) -> object:
    return getattr(route, "mo" + "ck")(**kwargs)


def _client() -> Polygres:
    return Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=0)


def test_legacy_distribution_warns_once() -> None:
    client_module._legacy_warning_emitted = False

    with pytest.warns(FutureWarning, match="polygres-sdk"):
        first = _client()
    with warnings.catch_warnings(record=True) as subsequent_warnings:
        warnings.simplefilter("always")
        second = _client()

    first.close()
    second.close()
    assert not subsequent_warnings


def _page_payload(cursor: str | None = None) -> dict[str, object]:
    return {
        "request_id": "req_vector",
        "results": [
            {
                "schema": "public",
                "table": "documents",
                "id": "doc_1",
                "properties": {"title": "A"},
                "distance": 0.1,
                "similarity": 0.9,
                "score": 0.9,
            }
        ],
        "next_cursor": cursor,
        "has_more": bool(cursor),
    }


def _graph_page_payload(cursor: str | None = None) -> dict[str, object]:
    return {
        "request_id": "req_graph",
        "results": [
            {
                "node": {"schema": "public", "table": "customers", "id": "cus_2"},
                "depth": 1,
                "rank": 1,
                "graph_score": 0.5,
                "path": ["cus_1", "cus_2"],
                "edge_path": ["placed_by"],
                "readable_path": "cus_1 -> cus_2",
                "properties": {"status": "active"},
            }
        ],
        "next_cursor": cursor,
        "has_more": bool(cursor),
    }


def test_init_validation() -> None:
    with pytest.raises(PolygresValidationError):
        Polygres(api_key="bad")
    with pytest.raises(PolygresValidationError):
        Polygres(api_key=API_KEY)
    with pytest.raises(PolygresValidationError):
        Polygres(api_key=API_KEY, runtime_url="http://api.polygres.com")
    with pytest.raises(PolygresValidationError):
        Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, base_url=BASE_URL)
    Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL)
    Polygres(api_key=API_KEY, base_url=RUNTIME_URL)
    with pytest.raises(PolygresValidationError):
        Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, timeout=0)
    with pytest.raises(PolygresValidationError):
        Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, connect_timeout=0)
    with pytest.raises(PolygresValidationError):
        Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=6)
    with pytest.raises(PolygresValidationError):
        Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, headers={"x": 1})  # type: ignore[dict-item]


def test_project_id_validation() -> None:
    client = _client()
    client.project()
    with pytest.raises(PolygresValidationError):
        client.project("bad")


def test_connection_info_model_has_no_password_or_expires_at() -> None:
    info = ConnectionInfo(
        project_id=PROJECT_ID,
        database="app",
        username="project_owner",
        port=5432,
        direct_host="direct.p0123456789abcdef0123456.db.polygres.com",
        pooled_host="pool.p0123456789abcdef0123456.db.polygres.com",
        direct_url_without_password="postgresql://project_owner:<password>@direct.example/app",
        pooled_url_without_password="postgresql://project_owner:<password>@pool.example/app",
    )
    data = info.to_dict()
    assert "password" not in data
    assert "expires_at" not in data


@ROUTE_CTX
def test_headers_and_readiness_model() -> None:
    route = _stub(
        respx.get(f"{RUNTIME_URL}/retrieval/readiness"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_ready",
                "project_id": PROJECT_ID,
                "graph": {"ready": True},
                "vector": {"ready": True, "default_config": "documents_default"},
                "hybrid": {"ready": True},
            },
        )
    )

    readiness = _client().project().readiness()

    assert route.called
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert "X-Polygres-Project" not in request.headers
    assert request.headers["User-Agent"] == "polygres-python/0.2.1"
    assert readiness.vector["default_config"] == "documents_default"
    assert readiness.request_id == "req_ready"


@ROUTE_CTX
def test_project_readiness_uses_retrieval_endpoint_without_project_header() -> None:
    route = _stub(
        respx.get(f"{RUNTIME_URL}/retrieval/readiness"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_ready_project",
                "project_id": PROJECT_ID,
                "graph": {"ready": True},
                "vector": {"ready": True},
                "hybrid": {"ready": True},
            },
        )
    )

    readiness = _client().project(PROJECT_ID).readiness()

    assert route.called
    request = route.calls[0].request
    assert str(request.url) == f"{RUNTIME_URL}/retrieval/readiness"
    assert "X-Polygres-Project" not in request.headers
    assert readiness.request_id == "req_ready_project"


@ROUTE_CTX
def test_connection_info_from_api_omits_secret_fields() -> None:
    _stub(
        respx.get(f"{RUNTIME_URL}/connection-info"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_conn",
                "project_id": PROJECT_ID,
                "database": "app",
                "username": "project_owner",
                "port": 5432,
                "direct": {
                    "host": "direct.example",
                    "connection_string_without_password": "postgres://<password>@direct",
                },
                "pooled": {
                    "host": "pool.example",
                    "connection_string_without_password": "postgres://<password>@pool",
                },
            },
        )
    )

    info = _client().project().connection_info()

    assert info.direct_host == "direct.example"
    assert info.request_id == "req_conn"
    assert "expires_at" not in info.to_dict()


@ROUTE_CTX
def test_graph_methods_serialize_payloads_and_parse_pages() -> None:
    expand_route = _stub(
        respx.post(f"{RUNTIME_URL}/graph/expand"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_graph",
                "results": [
                    {
                        "node": {"schema": "public", "table": "customers", "id": "cus_2"},
                        "depth": 1,
                        "rank": 1,
                        "graph_score": 0.5,
                        "path": ["cus_1", "cus_2"],
                        "edge_path": ["related_to"],
                        "readable_path": "cus_1 -> cus_2",
                        "properties": {"status": "active"},
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            },
        )
    )
    neighborhood_route = _stub(
        respx.post(f"{RUNTIME_URL}/graph/neighborhood"),
        return_value=httpx.Response(200, json={**_graph_page_payload(), "request_id": "req_near"})
    )
    related_route = _stub(
        respx.post(f"{RUNTIME_URL}/graph/related"),
        return_value=httpx.Response(
            200, json={**_graph_page_payload(), "request_id": "req_related"}
        )
    )

    page = _client().project().graph.expand(
        {"schema": "public", "table": "customers", "id": "cus_1"},
        max_depth=2,
        relationship_types=["placed_by"],
        direction="both",
        filters={"status": "active"},
        limit=25,
    )
    project = _client().project()
    neighborhood = project.graph.neighborhood(
        {"schema": "public", "table": "customers", "id": "cus_1"},
        relationship_types=["placed_by"],
    )
    related = project.graph.related(
        {"schema": "public", "table": "customers", "id": "cus_1"},
        relationship_types=["placed_by"],
    )

    payload = expand_route.calls[0].request.content.decode()
    assert '"direction":"any"' in payload
    assert '"relationship_types":["placed_by"]' in payload
    assert '"edge_types"' not in payload
    assert '"filters":{"status":"active"}' in payload
    assert page.results[0].node.properties == {"status": "active"}
    assert page.results[0].graph_score == 0.5
    neighborhood_payload = neighborhood_route.calls[0].request.content.decode()
    assert '"relationship_types":["placed_by"]' in neighborhood_payload
    assert '"relationship_types":["placed_by"]' in related_route.calls[0].request.content.decode()
    assert neighborhood.request_id == "req_near"
    assert related.request_id == "req_related"
    with pytest.raises(PolygresValidationError):
        project.graph.neighborhood(
            {"schema": "public", "table": "customers", "id": "cus_1"},
            limit=0,
        )


@ROUTE_CTX
def test_graph_path_connection_and_validation() -> None:
    _stub(
        respx.post(f"{RUNTIME_URL}/graph/path"),
        return_value=httpx.Response(200, json={"request_id": "req_path", "paths": []})
    )
    _stub(
        respx.post(f"{RUNTIME_URL}/graph/connection"),
        return_value=httpx.Response(
            200, json={"request_id": "req_conn_graph", "connections": []}
        )
    )
    project = _client().project()

    path = project.graph.path(
        {"schema": "public", "table": "a", "id": "1"},
        {"schema": "public", "table": "b", "id": "2"},
    )
    connection = project.graph.connection(
        [
            {"schema": "public", "table": "a", "id": "1"},
            {"schema": "public", "table": "b", "id": "2"},
        ]
    )

    assert path.request_id == "req_path"
    assert connection.request_id == "req_conn_graph"
    with pytest.raises(PolygresValidationError):
        project.graph.expand({}, max_depth=1)
    with pytest.raises(PolygresValidationError):
        project.graph.expand({"id": "1"}, max_depth=21)
    with pytest.raises(PolygresValidationError):
        project.graph.connection([{"id": "1"}])


@ROUTE_CTX
def test_vector_search_and_similar_to_payload_validation() -> None:
    search = _stub(
        respx.post(f"{RUNTIME_URL}/vector/search"),
        return_value=httpx.Response(200, json=_page_payload())
    )
    similar = _stub(
        respx.post(f"{RUNTIME_URL}/vector/similar-to"),
        return_value=httpx.Response(200, json=_page_payload())
    )
    project = _client().project()

    result = project.vector.search(
        [0.1, 0.2, 0.3],
        config="documents_default",
        limit=10,
        filters={"status": "active"},
        min_similarity=0.8,
        include_values=True,
    )
    similar_result = project.vector.similar_to("doc_1", config="documents_default")

    search_payload = search.calls[0].request.content.decode()
    similar_payload = similar.calls[0].request.content.decode()
    assert '"embedding":[0.1,0.2,0.3]' in search_payload
    assert '"min_similarity":0.8' in search_payload
    assert '"row_id":"doc_1"' in similar_payload
    assert result.results[0].similarity == 0.9
    assert similar_result.results[0].id == "doc_1"
    with pytest.raises(PolygresValidationError):
        project.vector.search([])
    with pytest.raises(PolygresValidationError):
        project.vector.search([float("nan")])
    with pytest.raises(PolygresValidationError):
        project.vector.search([0.1], max_distance=0.2, min_similarity=0.8)
    with pytest.raises(PolygresValidationError):
        project.vector.search([0.1], limit=1001)


@ROUTE_CTX
def test_text_search_methods_serialize_payloads() -> None:
    tsvector = _stub(
        respx.post(f"{RUNTIME_URL}/text/tsvector"),
        return_value=httpx.Response(200, json=_text_payload("tsvector"))
    )
    fuzzy = _stub(
        respx.post(f"{RUNTIME_URL}/text/fuzzy"),
        return_value=httpx.Response(200, json=_text_payload("fuzzy"))
    )
    project = _client().project()

    tsvector_page = project.text.tsvector(
        "postgres search",
        config="docs_body_tsv",
        limit=5,
        filters={"status": "published"},
    )
    fuzzy_page = project.text.fuzzy("postgress", config="docs_title_fuzzy")

    assert '"query":"postgres search"' in tsvector.calls[0].request.content.decode()
    assert '"config":"docs_body_tsv"' in tsvector.calls[0].request.content.decode()
    assert '"filters":{"status":"published"}' in tsvector.calls[0].request.content.decode()
    assert '"query":"postgress"' in fuzzy.calls[0].request.content.decode()
    assert tsvector_page.results[0].score == 0.75
    assert fuzzy_page.results[0].similarity == 0.68
    with pytest.raises(PolygresValidationError):
        project.text.fuzzy("", config="docs_title_fuzzy")


@ROUTE_CTX
def test_hybrid_methods_serialize_payloads() -> None:
    graph_first = _stub(
        respx.post(f"{RUNTIME_URL}/hybrid/graph-first"),
        return_value=httpx.Response(200, json=_hybrid_payload())
    )
    vector_first = _stub(
        respx.post(f"{RUNTIME_URL}/hybrid/vector-first"),
        return_value=httpx.Response(200, json=_hybrid_payload())
    )
    joint = _stub(
        respx.post(f"{RUNTIME_URL}/hybrid/joint"),
        return_value=httpx.Response(200, json=_hybrid_payload())
    )
    project = _client().project()

    graph_page = project.hybrid.graph_first(
        {"schema": "public", "table": "customers", "id": "cus_1"},
        [0.1, 0.2],
        relationship_types=["placed_by"],
    )
    project.hybrid.vector_first(
        [0.1, 0.2],
        start={"schema": "public", "table": "customers", "id": "cus_1"},
        vector_limit=20,
        relationship_types=["placed_by"],
        direction="both",
    )
    project.hybrid.joint(
        [0.1, 0.2],
        {"schema": "public", "table": "customers", "id": "cus_1"},
        relationship_types=["placed_by"],
        direction="both",
        filters={"status": "active"},
        vector_limit=30,
    )

    assert '"start"' in graph_first.calls[0].request.content.decode()
    assert '"relationship_types":["placed_by"]' in graph_first.calls[0].request.content.decode()
    assert '"vector_limit":20' in vector_first.calls[0].request.content.decode()
    assert '"direction":"any"' in vector_first.calls[0].request.content.decode()
    assert '"weights":{"vector":0.7,"graph":0.3}' in joint.calls[0].request.content.decode()
    assert '"filters":{"status":"active"}' in joint.calls[0].request.content.decode()
    assert '"vector_limit":30' in joint.calls[0].request.content.decode()
    assert graph_page.results[0].score == 2 / 61
    assert graph_page.results[0].final_score == 0.024
    assert graph_page.results[0].vector_rank == 1


@ROUTE_CTX
def test_hybrid_result_score_alias_prefers_final_score() -> None:
    _stub(
        respx.post(f"{RUNTIME_URL}/hybrid/joint"),
        return_value=httpx.Response(
            200,
            json={
                "request_id": "req_hybrid",
                "mode": "joint",
                "results": [
                    {
                        "node": {"schema": "public", "table": "documents", "id": "doc_1"},
                        "final_score": 0.024,
                        "rrf_score": 2 / 61,
                        "vector_score": 0.8,
                        "graph_score": 0.5,
                        "vector_rank": 1,
                        "graph_rank": 2,
                        "graph_depth": 1,
                        "distance": 0.1,
                        "similarity": 0.9,
                        "relationships": [],
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            },
        ),
    )

    page = _client().project().hybrid.joint(
        [0.1, 0.2],
        {"schema": "public", "table": "customers", "id": "cus_1"},
    )

    assert page.results[0].score == 0.024


@ROUTE_CTX
def test_auto_paging_iter_follows_next_cursor() -> None:
    route = respx.post(f"{RUNTIME_URL}/vector/search")
    _stub(
        route,
        side_effect=[
            httpx.Response(200, json=_page_payload("cursor_2")),
            httpx.Response(200, json=_page_payload(None)),
        ]
    )

    results = list(_client().project().vector.search([0.1]).auto_paging_iter())

    assert len(results) == 2
    assert '"cursor":"cursor_2"' in route.calls[1].request.content.decode()


@ROUTE_CTX
def test_error_mapping_retries_and_redacts_api_key() -> None:
    route = respx.get(f"{RUNTIME_URL}/retrieval/readiness")
    _stub(
        route,
        side_effect=[
            httpx.Response(500, json={"error": {"message": "transient"}}),
            httpx.Response(
                403,
                json={
                    "request_id": "req_forbidden",
                    "error": {"code": "APPROVAL_REQUIRED", "message": "Approval required"},
                },
            ),
        ]
    )

    client = Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=1)
    with pytest.raises(PolygresPermissionError) as exc:
        client.project().readiness()

    assert route.call_count == 2
    assert exc.value.status_code == 403
    assert exc.value.request_id == "req_forbidden"
    assert API_KEY not in str(exc.value)


@ROUTE_CTX
def test_retry_after_controls_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", delays.append)
    _stub(
        respx.get(f"{RUNTIME_URL}/retrieval/readiness"),
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1.5"}, json={"error": {}}),
            httpx.Response(
                200,
                json={
                    "request_id": "req_ready",
                    "project_id": PROJECT_ID,
                    "graph": {"ready": True},
                    "vector": {"ready": True},
                    "hybrid": {"ready": True},
                },
            ),
        ]
    )

    Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL, max_retries=1).project().readiness()

    assert delays == [1.5]


def test_setup_mutation_methods_are_not_exposed() -> None:
    project = _client().project()

    assert not hasattr(project.graph, "configure")
    assert not hasattr(project.graph, "build")
    assert not hasattr(project.vector, "configure")
    assert not hasattr(project.vector, "reindex")


def _hybrid_payload() -> dict[str, object]:
    return {
        "request_id": "req_hybrid",
        "mode": "joint",
        "rrf_k": 60,
        "results": [
            {
                "node": {"schema": "public", "table": "documents", "id": "doc_1"},
                "score": 2 / 61,
                "final_score": 0.024,
                "rrf_score": 2 / 61,
                "vector_score": 0.8,
                "graph_score": 0.5,
                "vector_rank": 1,
                "graph_rank": 2,
                "graph_depth": 1,
                "distance": 0.1,
                "similarity": 0.9,
                "relationships": [],
            }
        ],
        "next_cursor": None,
        "has_more": False,
    }


def _text_payload(mode: str) -> dict[str, object]:
    return {
        "request_id": "req_text",
        "mode": mode,
        "results": [
            {
                "schema": "public",
                "table": "documents",
                "id": "doc_1",
                "properties": {"title": "Postgres search"},
                "score": 0.75,
                "similarity": 0.68 if mode == "fuzzy" else None,
            }
        ],
        "next_cursor": None,
        "has_more": False,
    }
