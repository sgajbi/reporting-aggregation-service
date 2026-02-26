# Data Model Ownership

- Service: `lotus-report`
- Ownership status: **no persisted domain entities** in current phase.
- Domain responsibility: reporting orchestration and aggregation payload shaping.

## Service Boundaries

- Core portfolio data comes from PAS APIs.
- Advanced analytics comes from PA APIs.
- This service owns only reporting contracts and aggregation composition logic.

## Naming and Vocabulary Rules

- Follow platform glossary terms from `lotus-platform/Domain Vocabulary Glossary.md`.
- Do not introduce service-local synonyms for canonical portfolio, position, transaction, valuation, or performance terms.

