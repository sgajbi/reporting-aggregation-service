# Reporting & Aggregation Service

Service scope:
- build aggregated read models for reporting from lotus-core and lotus-performance data contracts
- generate reporting artifacts metadata and download references
- own reporting endpoints for portfolio summary and portfolio review payloads

## Local Run

```powershell
python -m pip install -e ".[dev]"
$env:PYTHONPATH="src"
uvicorn app.main:app --reload --port 8300
```

API docs:
- http://localhost:8300/docs

Key reporting endpoints:
- `GET /integration/capabilities`
- `POST /reports/portfolios/{portfolio_id}/summary`
- `POST /reports/portfolios/{portfolio_id}/review`

Current orchestration model:
- lotus-report composes summary/review responses from lotus-core core snapshot contracts.
- lotus-report enriches review performance section from lotus-performance analytics contracts.

## Tests

```powershell
$env:PYTHONPATH="src"
python -m pytest tests -q
```

## Docker

```powershell
docker compose up -d --build
```

## Platform Foundation Commands

- `make migration-smoke`
- `make migration-apply`
- `make security-audit`

Standards documentation:

- `docs/standards/migration-contract.md`
- `docs/standards/data-model-ownership.md`


