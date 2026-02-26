# Enterprise Readiness Baseline (lotus-report)

- Standard reference: `lotus-platform/Enterprise Readiness Standard.md`
- Scope: reporting/aggregation read and export APIs consuming lotus-core/lotus-performance sources.
- Change control: RFC for standard changes, ADR for temporary deviations.

## Security and IAM Baseline

- Write-path/privileged action audit middleware is enabled.
- Audit metadata includes actor/tenant/role/correlation with sensitive-field redaction.

Evidence:
- `src/app/enterprise_readiness.py`
- `src/app/main.py`
- `tests/unit/test_enterprise_readiness.py`

## API Governance Baseline

- OpenAPI contracts are versioned and enforced through CI conformance checks.
- Compatibility/deprecation policy follows platform RFC governance.

Evidence:
- `src/app/main.py`
- `tests/integration`

## Configuration and Feature Management Baseline

- Feature flags are centrally loaded from environment JSON.
- Tenant/role scoping is deterministic and deny-by-default for missing/invalid config.

Evidence:
- `src/app/enterprise_readiness.py`
- `tests/unit/test_enterprise_readiness.py`

## Data Quality and Reconciliation Baseline

- Reporting payload shaping includes validation and explicit failure on invalid upstream data.
- Reconciliation expectations are documented with durability standards.

Evidence:
- `src/app/services`
- `docs/standards/durability-consistency.md`

## Reliability and Operations Baseline

- Resilient upstream clients, health/readiness probes, and migration/runbook conventions are in place.

Evidence:
- `src/app/clients.py`
- `docs/standards/scalability-availability.md`
- `docs/standards/migration-contract.md`

## Privacy and Compliance Baseline

- Redaction and audit traceability applied for critical actions.

Evidence:
- `src/app/enterprise_readiness.py`
- `tests/unit/test_enterprise_readiness.py`

## Deviations

- Deviations require ADR with mitigation and expiry review date.


