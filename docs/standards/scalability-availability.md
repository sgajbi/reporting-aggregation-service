# Scalability and Availability Standard Alignment

Service: RAS

This repository adopts the platform-wide standard defined in pbwm-platform-docs/Scalability and Availability Standard.md.

## Implemented Baseline

- Stateless service behavior with externalized durable state.
- Explicit timeout and bounded retry/backoff for inter-service communication where applicable.
- Health/liveness/readiness endpoints for runtime orchestration.
- Observability instrumentation for latency/error/throughput diagnostics.
- API pagination/filter guardrails for report sections via bounded `sectionLimit` query parameter.

## Database Scalability Fundamentals

- Query plan and index reviews are required for report read paths that join portfolio snapshot and analytics data.
- Data growth and retention expectations are tracked per report artifact family.
- Archival policy: historical generated report artifacts follow retention and archival guidance from platform standards.

## Caching Policy Baseline

- No hidden in-memory cache for report correctness-critical outputs.
- Any future cache introduction must define explicit TTL, invalidation ownership, and stale-read behavior.
- Cache policy changes require ADR/RFC references.

## Availability Baseline

- Internal SLO baseline: p95 report read latency < 500 ms for summary/review payload assembly.
- Recovery assumptions: RTO 30 minutes, RPO 15 minutes.
- Backup and restore readiness is validated through upstream dependency runbooks and platform restore drills.

## Required Evidence

- Compliance matrix entry in pbwm-platform-docs/output/scalability-availability-compliance.md.
- Service-specific tests covering resilience and concurrency-critical paths.

## Deviation Rule

Any deviation from this standard requires ADR/RFC with remediation timeline.
