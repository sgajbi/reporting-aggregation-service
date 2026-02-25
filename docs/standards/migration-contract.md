# Migration Contract Standard

- Service: `reporting-aggregation-service`
- Persistence mode: **no persistent schema** (stateless reporting facade in current phase).
- Migration policy: **versioned migration contract is still mandatory** even in no-schema mode.

## Deterministic Checks

- `make migration-smoke` validates that this contract document exists and remains aligned.
- CI executes `make migration-smoke` on each PR.

## Rollback and Forward-Fix

- There is no runtime schema rollback in no-schema mode.
- Any contract issue is resolved through **forward-fix** in code/docs and re-run of CI gates.

## Future Upgrade Path

If/when persistent storage is introduced:

1. Add versioned migrations.
2. Add deterministic migration apply checks in CI.
3. Keep forward-only migration policy with explicit rollback strategy documented.
