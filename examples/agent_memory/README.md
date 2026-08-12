# Agent Memory Example (Polygres SDK)

This example shows how an application retrieves **durable agent memory** through
the Polygres Runtime **Context API** (`project.context`). It mirrors the SQL
walkthrough in [Evokoa/pgContext](https://github.com/Evokoa/pgContext):
`examples/sql/06_agent_memory.sql`.

## What it demonstrates

- Scoped memory retrieval for one tenant and user
- Filtered dense search over a Context collection
- Hybrid dense + full-text retrieval with `text_hybrid`
- Application-side context-pack assembly for LLM prompts

The demo uses fixture 4-dimensional embeddings so you can run it without calling
an external embedding model. Replace the vectors in your application with outputs
from your production embedding model.

## Prerequisites

- Python 3.10+
- A [Polygres](https://polygres.com) project with Context enabled
- [polygres-cli](https://github.com/Evokoa/polygres-cli) for one-time setup

```bash
pip install polygres-cli polygres-sdk
polygres login
polygres projects use <your-project>
```

Create a Project API key from the project **Connect** page and export:

```bash
export POLYGRES_API_KEY=poly_live_...
export POLYGRES_RUNTIME_URL=https://...
```

## One-time project setup

### 1. Load schema and seed data

From this repository root:

```bash
polygres db psql < examples/agent_memory/schema.sql
```

This creates `agent_users`, `agent_sessions`, `agent_messages`, and
`agent_decisions` with the same fixture rows as the pgContext SQL example.

### 2. Preflight and create the Context collection

Inspect capabilities:

```bash
polygres --json context capabilities
```

Preflight the collection definition:

```bash
polygres --json context sources preflight --file examples/agent_memory/collection.json
```

Create the collection (requires explicit approval in agent-driven workflows):

```bash
polygres context collections create agent_memory_decisions \
  --source existing \
  --schema public \
  --table agent_decisions \
  --source-key-column id \
  --vector-column embedding \
  --dimensions 4 \
  --metric cosine \
  --text-column body \
  --result-column summary \
  --result-column body \
  --result-column category \
  --result-column decided_at \
  --result-column user_id \
  --result-column tenant_id \
  --result-column session_id \
  --filter-column tenant_id \
  --filter-column user_id \
  --filter-column category
```

Synchronize catalog points after loading seed rows. Use the collection UUID
returned by the create command (not the collection name):

```bash
COLLECTION_ID=<uuid-from-create-output>

polygres context points upsert "$COLLECTION_ID" \
  d-billing-refund d-arch-postgres d-onboarding-delay d-other-billing
```

Or reconcile every row in the source table:

```bash
polygres context points reconcile "$COLLECTION_ID"
```

Exact CLI flag names may vary slightly by `polygres-cli` version. Use
`polygres context collections create --help` if a flag changed.

```bash
python examples/agent_memory/dry_run.py
```

This validates `collection.json` against the Context request schema and exercises
`demo.py` against mocked Runtime responses (no API key required).

## Run the demo

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r examples/agent_memory/requirements.txt
pip install -e .

python examples/agent_memory/demo.py
```

Optional override:

```bash
export POLYGRES_AGENT_MEMORY_COLLECTION=agent_memory_decisions
```

## Expected output

When setup succeeded, the script prints:

1. Context capability flags (`dense_search`, `text_hybrid`)
2. Filtered dense search returning `d-billing-refund` for tenant `acme` and user `u-alice`
3. Hybrid search ranking the billing decision highest for `billing refund`
4. A scoped context pack with summary and body fields

## Review checklist

- [ ] `schema.sql` loads without errors on the project database
- [ ] Collection creation completes and verification passes
- [ ] `demo.py` returns billing memory for the scoped user
- [ ] No API keys committed to git

## Related material

- pgContext SQL example: `Evokoa/pgContext/examples/sql/06_agent_memory.sql`
- SDK Context reference: `docs/reference-v1.md`
- Agent skill guidance: `Evokoa/polygres-skills` → `polygres-sdk/references/context.md`

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Dense search unavailable` | Context collection missing or not verified |
| Empty results | Points not upserted after seed load |
| Dimension mismatch | Collection expects 4-D cosine vectors |
| `401` / auth errors | Invalid or expired API key |
| Hybrid skipped | `text_hybrid` capability blocked on project |

Never commit `POLYGRES_API_KEY` or paste it into logs.
