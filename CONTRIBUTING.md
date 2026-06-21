# Contributing

Thanks for helping improve the Polygres Python SDK.

## Development Setup

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

Run these before opening a pull request:

```bash
pytest
ruff check .
python -m build
```

## Scope

This repository contains the public Python SDK. It should stay focused on
client-side API access, models, documentation, and tests.

The SDK should not include Polygres service internals, deployment configuration,
private repository references, secrets, or direct database credentials.

## Security Issues

Do not report vulnerabilities in public issues. Follow the process in
`SECURITY.md`.
