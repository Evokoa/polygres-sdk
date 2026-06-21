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

## Publishing

Publishing uses GitHub Actions Trusted Publishing. Do not add PyPI API tokens
or package index passwords to GitHub secrets.

Release rehearsal happens first on TestPyPI:

1. Run the `Publish Python SDK` workflow from the public repository.
2. Select `publish_target = testpypi`.
3. Verify installation from TestPyPI in a clean environment:

   ```bash
   python3 -m venv /tmp/polygres-sdk-testpypi
   /tmp/polygres-sdk-testpypi/bin/python -m pip install --upgrade pip
   /tmp/polygres-sdk-testpypi/bin/python -m pip install \
     --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ \
     polygres==0.1.0
   /tmp/polygres-sdk-testpypi/bin/python - <<'PY'
   import importlib.metadata
   from polygres import Polygres

   print(importlib.metadata.version("polygres"))
   print(Polygres)
   PY
   ```

Publish to PyPI only after the same commit has passed TestPyPI validation. Push
a release tag from the public repository:

```bash
git tag python-sdk-v0.1.0
git push origin python-sdk-v0.1.0
```

The `pypi` GitHub environment must be approved before the upload proceeds.
Project URLs in `pyproject.toml` must use valid HTTPS URLs accepted by PyPI.

## Scope

This repository contains the public Python SDK. It should stay focused on
client-side API access, models, documentation, and tests.

## Security Issues

Do not report vulnerabilities in public issues. Follow the process in
`SECURITY.md`.
