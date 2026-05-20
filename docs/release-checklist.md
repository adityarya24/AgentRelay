# AgentRelay Release Checklist

Use this before tagging or publishing a public release.

## 1. Clean Tree

```bash
git status --short
```

Expected: no uncommitted source changes.

## 2. Local Verification

```bash
python -m pip install -e ".[dev]"
ruff check agentrelay tests
pytest -q
python -m build
```

Expected: lint passes, tests pass, and `dist/` contains a wheel plus source archive.

## 3. Container Verification

Create a local token:

```bash
mkdir -p secrets
python -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/agentrelay_token
```

Build and smoke test:

```bash
docker compose build
docker compose up -d
python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8899/', timeout=3)))"
docker compose down
```

Expected: the health endpoint returns `{"status": "ok", ...}`.

## 4. GitHub CI

Push a branch or open a pull request and confirm the CI workflow passes for:

- Python 3.11
- Python 3.12
- Python 3.13

## 5. Package Metadata

Review:

- `pyproject.toml` version
- `README.md` install and quickstart instructions
- `DEPLOYMENT.md` production warnings
- `docs/roadmap-telegram-b2b.md` release stance

## 6. Optional Clawpatch Review

Run this after provider quota is available:

```bash
clawpatch ci --state-dir .clawpatch --limit 5 --jobs 1 --provider codex --output clawpatch-report.md
```

Triage or fix any confirmed high/medium findings before publishing.

## 7. Tag

```bash
git tag v2.1.0
git push origin v2.1.0
```

## 8. Publish

```bash
python -m twine check dist/*
python -m twine upload dist/*
```

Do not publish if CI, package checks, or smoke tests are failing.
