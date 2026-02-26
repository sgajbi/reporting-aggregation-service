# Durability and Consistency Standard (RAS)

- Standard reference: `lotus-platform/Durability and Consistency Standard.md`
- Scope: reporting and aggregation read APIs built from PAS/PA sourced data.
- Change control: RFC required for standard changes; ADR required for temporary deviations.

## Workflow Consistency Classification

- Strong consistency:
  - deterministic report payload generation for same request and `as_of_date`
  - reproducibility metadata in response contracts
- Eventual consistency:
  - upstream PAS/PA source freshness prior to request execution

## Idempotency and Write Semantics

- RAS currently exposes read-only reporting endpoints (no core write paths).
- If write endpoints are introduced, `Idempotency-Key` will be mandatory.
- Evidence:
  - `src/app/routers/reports.py`
  - `src/app/routers/aggregations.py`

## Atomicity Boundaries

- Aggregation responses are built per-request using scoped upstream reads.
- No partial persistent business writes are performed in current architecture.
- Evidence:
  - `src/app/services/reporting_read_service.py`
  - `src/app/services/aggregation_service.py`

## As-Of and Reproducibility Semantics

- `as_of_date` is a mandatory request field for reporting workflows.
- Responses include contract/policy versions where applicable.
- Evidence:
  - `src/app/models/contracts.py`
  - `src/app/services/reporting_read_service.py`
  - `tests/unit/test_reporting_read_service_additional.py`

## Concurrency and Conflict Policy

- Request processing is stateless and deterministic for equivalent inputs.
- Upstream call retries are bounded and explicit.
- Evidence:
  - `src/app/clients/http_resilience.py`
  - `tests/unit/test_http_resilience.py`

## Integrity Constraints

- Request schema validation enforces section/as-of contract integrity.
- Invalid request shapes are rejected with explicit 4xx responses.
- Evidence:
  - `src/app/models/*`
  - `tests/integration/test_api.py`

## Release-Gate Tests

- Unit: `tests/unit/*`
- Integration: `tests/integration/*`
- E2E: `tests/e2e/*`

## Deviations

- Any future durable write path introduced in RAS without explicit idempotency and atomicity controls requires ADR with expiry review date.


