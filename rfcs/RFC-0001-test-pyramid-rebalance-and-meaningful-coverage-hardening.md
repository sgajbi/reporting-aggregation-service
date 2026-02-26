# RFC-0001: Test Pyramid Rebalance and Meaningful Coverage Hardening

## Status

Proposed

## Date

2026-02-24

## Problem Statement

`lotus-report` had low baseline coverage and an imbalanced test pyramid:

- Coverage: `83%`
- Unit: `46.67%`
- Integration: `53.33%`
- E2E: `0%`

This reduced confidence in domain logic and did not match platform testing standards.

## Decision

Harden tests to:

- Reach meaningful line coverage `>=99%`.
- Rebalance test pyramid into target ranges:
  - Unit `70-85%`
  - Integration `15-25%`
  - E2E `5-10%`

## Implementation Summary

- Added deep unit tests for:
  - PA/PAS clients (payload parsing, contract payloads, propagation headers)
  - Observability utilities (correlation/request/trace id handling, structured logs)
  - Aggregation service branch logic and fallback behavior
  - Reporting read service validation and section behavior
  - Router branch and dependency-factory behavior
- Added e2e workflow tests for:
  - Summary flow
  - Review flow
  - Aggregation non-live flow
  - Observability headers on live request path
- Extended integration tests for:
  - Non-PDF report behavior
  - Error propagation for summary/review endpoints

## Result

- Coverage: `99.76%`
- Test count: `69`
- Bucket split:
  - Unit: `54` (`78.26%`)
  - Integration: `11` (`15.94%`)
  - E2E: `4` (`5.80%`)

## Risks and Trade-offs

- Additional test maintenance cost, offset by stronger regression protection.
- Some tests still produce framework deprecation warnings; they do not block behavior but should be cleaned in follow-up.


