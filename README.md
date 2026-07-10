# Polygres Python SDK and CLI (Deprecated)

> [!WARNING]
> The `polygres` distribution is deprecated. New SDK users should install
> `polygres-sdk`; command-line users should install `polygres-cli`. This legacy
> package receives critical fixes only.

The Polygres SDK is a retrieval client for a per-project Runtime API. It uses a
Polygres API key and Runtime API URL; it does not open direct Postgres
connections or expose database passwords.

Links:

- Documentation: https://docs.evokoa.com/polygres
- Changelog: https://docs.evokoa.com/polygres/changelog
- Polygres: https://polygres.com
- Evokoa: https://evokoa.com
- X: https://x.com/evokoa_ai
- Discord: https://discord.gg/GnHR8ezuwG
- Product Hunt: https://www.producthunt.com/@evokoa

## Install

```bash
pip install polygres==0.2.1
```

For an isolated, globally available `polygres` terminal command, use `pipx`:

```bash
pipx install polygres==0.2.1
polygres --version
polygres login
```

Version `0.2.1` becomes installable from the default package index only after
the release is published to PyPI. Maintainers validate the same wheel through
TestPyPI first, following `docs/44-python-sdk-release-runbook.md`.

## Migrate To The Current Packages

The legacy and current SDK distributions both expose the `polygres` Python
import namespace. Remove the legacy distribution before installing the current
SDK:

```bash
pip uninstall polygres
pip install polygres-sdk
```

Install the standalone CLI separately when needed:

```bash
pip install polygres-cli
```

Python imports remain `from polygres import Polygres` after migration.

## CLI Quick Start

Use the CLI for control-plane and project setup tasks. Use the Python SDK below
for application retrieval queries against a project's Runtime API.

```bash
polygres login
polygres whoami
polygres projects list
polygres projects use <project-id-or-exact-name>
polygres env
polygres ready
```

`polygres login` opens the Polygres dashboard for approval and also prints a
device code for headless terminals. Credentials are saved under
`~/.config/polygres/config.json` with owner-only permissions on POSIX systems.
Run `polygres logout` to revoke the refresh token and remove local credentials.

Common setup commands include:

```bash
polygres import csv ./documents.csv --table documents --wait
polygres migrations apply --file ./001_create_documents.sql
polygres graph discover --json > graph.json
polygres graph config apply --file graph.json
polygres vector configs list
polygres text configs list
```

Run `polygres --help` for the complete supported command surface. Exit codes
distinguish validation (`2`), authentication (`3`), permission (`4`), not found
(`5`), conflict (`6`), rate limiting (`7`), remote availability (`8`), and
missing local tools such as `psql` (`9`). Native database passwords are never
stored or printed by the CLI.

## Quick Start

Create a Polygres API key from your project Settings page. Find the Runtime API
URL on your project Connect page. Use the Runtime API URL with the SDK, not the
direct or pooled Postgres connection string.

```python
from polygres import Polygres

client = Polygres(
    api_key="POLYGRES_API_KEY",
    runtime_url="POLYGRES_RUNTIME_URL",
)
project = client.project()

readiness = project.readiness()
print(readiness.graph, readiness.vector, readiness.hybrid)

connection = project.connection_info()
print(connection.direct_url_without_password)
print(connection.pooled_url_without_password)
```

`connection_info()` returns passwordless direct and pooled connection strings.
The SDK never returns the database password.

## Query Chaining

Graph and hybrid calls need real row IDs from graph-registered tables. Do not
guess IDs such as `doc_1` or `cus_123` unless those rows actually exist in your
database. A safe pattern is to start with vector or text search, then use the
returned result as the graph start node.

```python
embedding = [0.1] * 8  # Must match the configured vector dimensions.

vector_page = project.vector.search(
    embedding,
    config="documents_embedding",
    limit=5,
)

top_doc = vector_page.results[0]
start = {
    "schema": top_doc.schema,
    "table": top_doc.table,
    "id": top_doc.id,
}

graph_page = project.graph.expand(
    start,
    max_depth=2,
    limit=10,
)

similar_page = project.vector.similar_to(
    top_doc.id,
    config="documents_embedding",
    limit=5,
)

hybrid_page = project.hybrid.graph_first(
    start,
    embedding=embedding,
    config="documents_embedding",
    limit=10,
)

for result in hybrid_page.results:
    print(result.id, result.score, result.vector_score, result.graph_score)
```

If a graph call returns `Node not found`, check that:

- `schema`, `table`, and `id` refer to a real row.
- The table is registered in graph configuration.
- The graph has been rebuilt after adding or changing that row.

## Readiness And Connection Info

```python
readiness = project.readiness()

if readiness.vector["ready"]:
    print("default vector config:", readiness.vector["default_config"])

connection = project.connection_info()
print(connection.direct_host)
print(connection.pooled_host)
```

## Vector Retrieval

```python
page = project.vector.search(
    [0.1] * 8,
    config="documents_embedding",
    filters={"status": "published"},
    min_similarity=0.75,
    limit=10,
)

for result in page.results:
    print(result.id, result.schema, result.table, result.score)
```

Find rows similar to an existing row:

```python
page = project.vector.similar_to(
    row_id="doc_security_01178",
    config="documents_embedding",
    limit=10,
)
```

Set `include_values=True` when you need returned embedding values:

```python
page = project.vector.search(
    [0.1] * 8,
    config="documents_embedding",
    include_values=True,
)
```

## Text Retrieval

TSVector full-text search:

```python
page = project.text.tsvector(
    "refund policy",
    config="documents_body_tsv",
    filters={"status": "published"},
    limit=10,
)
```

Fuzzy search:

```python
page = project.text.fuzzy(
    "acme corporation",
    config="customer_name_fuzzy",
    limit=10,
)
```

Text results expose `id`, `schema`, `table`, `properties`, `score`, and
`similarity`.

## Graph Retrieval

Graph start nodes must identify real rows:

```python
start = {"schema": "public", "table": "documents", "id": "doc_security_01178"}
```

Expand from a node:

```python
page = project.graph.expand(
    start,
    max_depth=2,
    direction="any",
    filters={"status": "published"},
    limit=20,
)

for result in page.results:
    print(result.node.id, result.depth, result.graph_score)
```

Neighborhood is an alias-shaped traversal with `radius`:

```python
page = project.graph.neighborhood(
    start,
    radius=2,
    direction="any",
    limit=20,
)
```

Related returns one-hop related nodes:

```python
page = project.graph.related(
    start,
    direction="any",
    limit=20,
)
```

Find paths between two nodes:

```python
target = {"schema": "public", "table": "documents", "id": "doc_security_01744"}

path_response = project.graph.path(
    start,
    target,
    max_depth=3,
)

print(path_response.paths)
```

Find connections across a chain of entities:

```python
connection_response = project.graph.connection(
    [start, target],
    max_depth=3,
)

print(connection_response.connections)
```

`GraphResult` uses `result.node.id`. `HybridResult` exposes `result.id`
directly.

## Hybrid Retrieval

Graph-first starts from a graph node, then blends graph context with vector
similarity:

```python
page = project.hybrid.graph_first(
    start,
    embedding=[0.1] * 8,
    config="documents_embedding",
    max_depth=2,
    limit=10,
)
```

Vector-first starts with vector candidates, then expands graph context:

```python
page = project.hybrid.vector_first(
    [0.1] * 8,
    config="documents_embedding",
    vector_limit=20,
    max_depth=1,
    limit=10,
)
```

Joint combines a vector query with a graph start node:

```python
page = project.hybrid.joint(
    [0.1] * 8,
    start,
    config="documents_embedding",
    vector_weight=0.7,
    graph_weight=0.3,
    max_depth=2,
    limit=10,
)
```

Hybrid results expose `id`, `schema`, `table`, `score`, `vector_score`,
`graph_score`, `distance`, `similarity`, `properties`, and `relationships`.

## Paging

Every list-style retrieval method returns a `Page`.

```python
page = project.vector.search([0.1] * 8, config="documents_embedding", limit=25)

for result in page.results:
    print(result.id)

if page.has_more:
    next_page = project.vector.search(
        [0.1] * 8,
        config="documents_embedding",
        limit=25,
        cursor=page.next_cursor,
    )
```

Use `auto_paging_iter()` to iterate through all pages:

```python
page = project.text.tsvector(
    "security incident",
    config="documents_body_tsv",
    limit=25,
)

for result in page.auto_paging_iter():
    print(result.id, result.score)
```

## Error Handling

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

Common graph failures include `Node not found` when the requested start or
target row is not present in the graph projection.

## Client Behavior

The SDK sends `Authorization` and `User-Agent` on every request. It does not
send `X-Polygres-Project`; the project identity is bound to the Runtime API URL.

The SDK supports retrieval against saved graph, vector, TSVector full-text,
fuzzy text-search, and hybrid configurations. It does not expose dashboard-only
setup mutations for graph/vector/text configuration, graph builds, or index
reindexing.

The SDK is an HTTP client. It does not bundle direct Postgres drivers such as
`asyncpg` or `psycopg`, and it does not implement SQL editor script execution
locally. Future SQL editor SDK methods must call Polygres API routes instead of
opening database connections.
