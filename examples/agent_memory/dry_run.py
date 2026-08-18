#!/usr/bin/env python3
"""Dry-run checks for the agent memory SDK example without live credentials."""

from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from polygres import Polygres
from polygres._vendor.polygres_lib.context.models import CollectionCreateRequest

EXAMPLE_DIR = Path(__file__).resolve().parent
DEMO_PATH = EXAMPLE_DIR / "demo.py"
COLLECTION_PATH = EXAMPLE_DIR / "collection.json"
FIXTURES_PATH = ROOT / "tests" / "fixtures" / "context" / "contract-fixtures.json"
API_KEY = "poly_live_0123456789abcdef0123456789abcdef"
RUNTIME_URL = "https://runtime.example.test/v1"
COLLECTION_ID = "00000000-0000-0000-0000-000000000010"

FIXTURES = json.loads(FIXTURES_PATH.read_text())


def _load_demo_module():
    spec = importlib.util.spec_from_file_location("agent_memory_demo", DEMO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _validate_collection_json() -> None:
    payload = json.loads(COLLECTION_PATH.read_text())
    request = CollectionCreateRequest.model_validate(payload)
    assert request.name == "agent_memory_decisions"
    assert request.source.table_name == "agent_decisions"
    assert request.vector.dimensions == 4
    assert request.text_column == "body"
    assert "body" in request.result_columns
    assert set(request.filter_columns) == {"tenant_id", "user_id", "category"}


def _memory_properties(*, tenant_id: str, user_id: str) -> dict[str, str]:
    return {
        "summary": "Approved partial enterprise refund",
        "body": "Approved a partial refund for the enterprise billing dispute after policy review.",
        "category": "billing",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": "s-billing-1",
        "decided_at": "2026-07-01T10:00:00Z",
    }


def _mock_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/v1")
    if path == "/context/capabilities":
        payload = deepcopy(FIXTURES["responses"]["CapabilitiesResponse"])
        payload.update(
            {
                "setup": True,
                "setup_blocker": None,
                "setup_blocker_message": None,
                "dense_search": True,
                "dense_search_blocker": None,
                "dense_search_blocker_message": None,
                "text_hybrid": True,
                "text_hybrid_blocker": None,
                "text_hybrid_blocker_message": None,
            }
        )
        return httpx.Response(200, json=payload)
    if path == "/context/search":
        payload = deepcopy(FIXTURES["responses"]["RankedResponse"])
        payload["mode"] = "dense"
        payload["results"] = [
            {
                "point_id": 1,
                "source": {
                    "schema": "public",
                    "table": "agent_decisions",
                    "id": "d-billing-refund",
                },
                "rank": 1,
                "score": 0.001382,
                "score_kind": "context_metric",
                "metric": "cosine",
                "properties": _memory_properties(tenant_id="acme", user_id="u-alice"),
            }
        ]
        return httpx.Response(200, json=payload)
    if path == "/context/hybrid/text":
        payload = deepcopy(FIXTURES["responses"]["RankedResponse"])
        payload["mode"] = "text_hybrid"
        payload["results"] = [
            {
                "point_id": 1,
                "source": {
                    "schema": "public",
                    "table": "agent_decisions",
                    "id": "d-billing-refund",
                },
                "rank": 1,
                "score": 0.032787,
                "score_kind": "rrf",
                "rrf_k": 60,
                "properties": _memory_properties(tenant_id="acme", user_id="u-alice"),
            },
            {
                "point_id": 2,
                "source": {
                    "schema": "public",
                    "table": "agent_decisions",
                    "id": "d-other-billing",
                },
                "rank": 2,
                "score": 0.032258,
                "score_kind": "rrf",
                "rrf_k": 60,
                "properties": {
                    "summary": "Denied refund for other tenant",
                    "body": "Denied refund request for a billing dispute in another tenant workspace.",
                    "category": "billing",
                    "tenant_id": "other",
                    "user_id": "u-bob",
                    "session_id": "s-other-1",
                    "decided_at": "2026-07-02T12:00:00Z",
                },
            },
        ]
        return httpx.Response(200, json=payload)
    return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": path}})


def _run_demo_dry_run() -> None:
    demo = _load_demo_module()
    client = Polygres(api_key=API_KEY, runtime_url=RUNTIME_URL)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(_mock_handler))

    context = client.project().context
    capabilities = context.get_capabilities()
    assert capabilities.dense_search
    assert capabilities.text_hybrid

    memory_filter = {
        "must": [
            {"key": "tenant_id", "match": demo.TENANT_ID},
            {"key": "user_id", "match": demo.USER_ID},
            {"key": "category", "match": demo.CATEGORY},
        ]
    }
    dense = context.search(demo.COLLECTION, demo.QUERY_EMBEDDING, filter=memory_filter, limit=5)
    assert len(dense.results) == 1
    assert demo._source_key(dense.results[0]) == "d-billing-refund"

    hybrid = context.text_hybrid(
        demo.COLLECTION,
        demo.QUERY_EMBEDDING,
        query=demo.HYBRID_QUERY,
        limit=5,
    )
    pack = demo._build_context_pack(
        list(hybrid.results),
        tenant_id=demo.TENANT_ID,
        user_id=demo.USER_ID,
    )
    assert len(pack) == 1
    assert pack[0]["decision_id"] == "d-billing-refund"
    assert pack[0]["summary"] == "Approved partial enterprise refund"
    assert "partial refund" in pack[0]["body"]


def main() -> None:
    _validate_collection_json()
    _run_demo_dry_run()
    print("agent memory example dry run passed")


if __name__ == "__main__":
    main()
