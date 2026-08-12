# Polygres Python SDK

Build Python applications with Polygres graph, vector, text, and hybrid retrieval.

The SDK connects to one project's Runtime API using a Polygres API key. It does not open PostgreSQL connections or expose database passwords.

- [Documentation](https://docs.polygres.com)
- [Polygres](https://polygres.com)
- [Discord](https://discord.gg/GnHR8ezuwG)

## Install

The SDK requires Python 3.10 or newer.

```bash
pip install polygres-sdk
```

The SDK is a Python library and does not install the `polygres` terminal command. Install `polygres-cli` separately for project setup, imports, migrations, and retrieval configuration.

## Quick start

Create a Project API Key in **Settings** and copy the Runtime API URL from the project's **Connect** page. Store both values in your application's secret configuration.

```python
import os

from polygres import Polygres

client = Polygres(
    api_key=os.environ["POLYGRES_API_KEY"],
    runtime_url=os.environ["POLYGRES_RUNTIME_URL"],
)
project = client.project()

readiness = project.readiness()
print(readiness.graph, readiness.vector, readiness.hybrid)
```

Use the Runtime API URL with the SDK. Do not use a direct or pooled PostgreSQL connection string.

## Agent memory example

For a runnable end-to-end pattern over the Context API, see
[examples/agent_memory/README.md](examples/agent_memory/README.md). It mirrors
the pgContext SQL walkthrough for durable decision memory, hybrid retrieval,
and LLM context-pack assembly.

## Choose a retrieval method

| Need | Method |
| --- | --- |
| Search by semantic similarity | `project.vector.search()` |
| Find rows similar to an existing row | `project.vector.similar_to()` |
| Search text with PostgreSQL full-text search | `project.text.tsvector()` |
| Tolerate misspellings in short text | `project.text.fuzzy()` |
| Traverse relationships | `project.graph.expand()` or `project.graph.related()` |
| Combine graph and vector relevance | `project.hybrid.*` |

The corresponding graph, vector, or text configuration must be ready before the application sends retrieval requests.
New vector setup uses `project.context.create_collection()` with a native
`pgcontext.vector` column. Existing `project.vector` retrieval methods remain available
for applications using previously registered vector configurations.

## Vector retrieval

Generate the query embedding with the same model and dimensions used by the saved vector configuration.

```python
query_embedding = [0.1] * 768

page = project.vector.search(
    query_embedding,
    config="documents_embedding",
    filters={"status": "published"},
    min_similarity=0.75,
    limit=10,
)

for result in page.results:
    print(result.id, result.score, result.properties)
```

Find rows similar to an existing row without generating another embedding:

```python
page = project.vector.similar_to(
    row_id="doc_123",
    config="documents_embedding",
    limit=10,
)
```

## Text retrieval

Full-text search:

```python
page = project.text.tsvector(
    "refund policy",
    config="documents_body_tsv",
    filters={"status": "published"},
    limit=10,
)
```

Fuzzy text search:

```python
page = project.text.fuzzy(
    "acme corpration",
    config="customer_name_fuzzy",
    limit=10,
)
```

## Graph retrieval

Graph methods start from real rows in graph-registered tables. Use an ID from trusted application data or a previous retrieval result.

```python
start = {
    "schema": "public",
    "table": "documents",
    "id": "doc_123",
}

page = project.graph.expand(
    start,
    max_depth=2,
    direction="any",
    limit=20,
)

for result in page.results:
    print(result.node.id, result.depth, result.readable_path)
```

Other graph methods include:

```python
neighbors = project.graph.neighborhood(start, radius=2, limit=20)
related = project.graph.related(start, limit=20)

target = {"schema": "public", "table": "documents", "id": "doc_456"}
paths = project.graph.path(start, target, max_depth=3)
connections = project.graph.connection([start, target], max_depth=3)
```

If a graph method returns `Node not found`, confirm that the row exists, its table is registered, and the graph was rebuilt after the latest relevant changes.

## Hybrid retrieval

Graph-first retrieval starts from a known row and adds vector relevance:

```python
page = project.hybrid.graph_first(
    start,
    embedding=query_embedding,
    config="documents_embedding",
    max_depth=2,
    limit=10,
)
```

Vector-first retrieval finds semantic candidates before expanding graph context:

```python
page = project.hybrid.vector_first(
    query_embedding,
    config="documents_embedding",
    vector_limit=20,
    max_depth=1,
    limit=10,
)
```

Joint retrieval lets vector and graph rankings contribute independently:

```python
page = project.hybrid.joint(
    query_embedding,
    start,
    config="documents_embedding",
    vector_weight=0.7,
    graph_weight=0.3,
    max_depth=2,
    limit=10,
)
```

## Pagination

Retrieval methods return a `Page` with `results`, `has_more`, and `next_cursor`.

```python
page = project.vector.search(
    query_embedding,
    config="documents_embedding",
    limit=25,
)

for result in page.results:
    print(result.id)

if page.has_more:
    next_page = project.vector.search(
        query_embedding,
        config="documents_embedding",
        limit=25,
        cursor=page.next_cursor,
    )
```

Use `auto_paging_iter()` when you want the SDK to follow every page:

```python
for result in page.auto_paging_iter():
    print(result.id, result.score)
```

## Error handling

SDK exceptions include the HTTP status, stable error code, safe details, and request ID when available.

```python
from polygres import PolygresAPIError

try:
    page = project.graph.expand(start, max_depth=2)
except PolygresAPIError as exc:
    print(exc.status_code)
    print(exc.code)
    print(exc.request_id)
    print(exc.details)
```

Keep the request ID when reporting a problem. Never log or send the Project API Key.

## Connection information

`connection_info()` returns project hosts and passwordless connection strings. It never returns the database password.

```python
connection = project.connection_info()
print(connection.direct_host)
print(connection.pooled_host)
print(connection.direct_url_without_password)
```

Use a PostgreSQL driver such as psycopg or SQLAlchemy when your application needs a database connection. The Polygres SDK is an HTTP retrieval client and does not bundle a PostgreSQL driver.

## Version and support

Package version: [`0.2.0`](https://github.com/Evokoa/polygres-sdk/releases/tag/polygres-sdk-v0.2.0).

When contacting support, include the installed SDK version and the request ID.

See the [SDK 0.2.0 release notes](https://github.com/Evokoa/polygres-sdk/releases/tag/polygres-sdk-v0.2.0) for release changes.

## Optional Agent Skill

The `polygres-sdk` Agent Skill helps compatible coding agents write and review Polygres application code.

```bash
npx skills add Evokoa/polygres-skills --skill polygres-sdk
```

See the [Agent Skills repository](https://github.com/Evokoa/polygres-skills) for Codex and Claude Code installation options.
