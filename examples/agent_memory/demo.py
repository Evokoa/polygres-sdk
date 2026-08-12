#!/usr/bin/env python3
"""Retrieve durable agent memory through the Polygres Context API.

Companion to the pgContext SQL example in Evokoa/pgContext:
  examples/sql/06_agent_memory.sql

Requires:
  - POLYGRES_API_KEY
  - POLYGRES_RUNTIME_URL
  - A Context collection named agent_memory_decisions (see README.md)
"""

from __future__ import annotations

import os
import sys
from typing import Any

from polygres import Polygres

COLLECTION = os.environ.get("POLYGRES_AGENT_MEMORY_COLLECTION", "agent_memory_decisions")
QUERY_EMBEDDING = [0.95, 0.05, 0.0, 0.0]
HYBRID_QUERY = "billing refund"
TENANT_ID = "acme"
USER_ID = "u-alice"
CATEGORY = "billing"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(2)
    return value


def _source_key(result: Any) -> str:
    source = getattr(result, "source", None)
    if source is not None and getattr(source, "id", None):
        return str(source.id)
    properties = getattr(result, "properties", {}) or {}
    for key in ("id", "source_key"):
        if key in properties:
            return str(properties[key])
    return str(getattr(result, "point_id", "unknown"))


def _build_context_pack(results: list[Any], *, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
    pack: list[dict[str, Any]] = []
    for result in results:
        properties = dict(getattr(result, "properties", {}) or {})
        if properties.get("tenant_id") not in (None, tenant_id):
            continue
        if properties.get("user_id") not in (None, user_id):
            continue
        pack.append(
            {
                "rank": getattr(result, "rank", len(pack) + 1),
                "score": getattr(result, "score", None),
                "decision_id": _source_key(result),
                "summary": properties.get("summary"),
                "body": properties.get("body"),
                "category": properties.get("category"),
                "decided_at": properties.get("decided_at"),
                "session_id": properties.get("session_id"),
            }
        )
    return pack


def main() -> None:
    api_key = _require_env("POLYGRES_API_KEY")
    runtime_url = _require_env("POLYGRES_RUNTIME_URL")

    client = Polygres(api_key=api_key, runtime_url=runtime_url)
    context = client.project().context

    capabilities = context.get_capabilities()
    print("Context capabilities:")
    print(f"  setup={capabilities.setup}")
    print(f"  dense_search={capabilities.dense_search}")
    print(f"  text_hybrid={capabilities.text_hybrid}")

    if not capabilities.dense_search:
        blocker = capabilities.dense_search_blocker or "unknown"
        message = capabilities.dense_search_blocker_message or ""
        print(f"Dense search unavailable ({blocker}): {message}", file=sys.stderr)
        sys.exit(1)

    memory_filter = {
        "must": [
            {"key": "tenant_id", "match": TENANT_ID},
            {"key": "user_id", "match": USER_ID},
            {"key": "category", "match": CATEGORY},
        ]
    }

    print("\nFiltered dense memory search:")
    dense = context.search(
        COLLECTION,
        QUERY_EMBEDDING,
        filter=memory_filter,
        limit=5,
    )
    for result in dense.results:
        print(f"  {_source_key(result):<18} score={result.score:.6f}")

    hybrid_results = dense.results
    if capabilities.text_hybrid:
        print("\nHybrid memory search (dense + full-text):")
        hybrid = context.text_hybrid(
            COLLECTION,
            QUERY_EMBEDDING,
            query=HYBRID_QUERY,
            limit=5,
        )
        hybrid_results = hybrid.results
        for result in hybrid.results:
            print(f"  {_source_key(result):<18} score={result.score:.6f}")
    else:
        blocker = capabilities.text_hybrid_blocker or "unknown"
        print(f"\nSkipping hybrid search ({blocker}).")

    print("\nContext pack for downstream LLM prompt assembly:")
    context_pack = _build_context_pack(
        list(hybrid_results),
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )
    if not context_pack:
        print("  No scoped memory rows returned.", file=sys.stderr)
        sys.exit(1)

    for item in context_pack:
        print(
            f"  #{item['rank']} {item['decision_id']} "
            f"({item['category']}) score={item['score']:.6f}"
        )
        print(f"     summary: {item['summary']}")
        print(f"     body: {item['body']}")


if __name__ == "__main__":
    main()
