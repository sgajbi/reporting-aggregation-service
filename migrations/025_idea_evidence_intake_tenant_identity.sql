-- report#344: the admitted tenant becomes half of intake identity.
--
-- Migration 024 created this table with `idempotency_key TEXT PRIMARY KEY`.
-- An idempotency key is a value the CALLER chooses to name its own retry, so
-- it is unique only within a caller. Keying on it alone meant a lookup could
-- resolve to a record belonging to a different tenant: measured, tenant B
-- presenting tenant A's key with an identical body received A's stored
-- response, and with a different body was refused for a payload it had not
-- changed.
--
-- The tenant was already stored -- inside `caller_context_json` -- but nothing
-- read it for identity. This makes it a column, backfills it from the context
-- that is already there, and moves it into the primary key.

ALTER TABLE idea_evidence_intake ADD COLUMN IF NOT EXISTS tenant_id TEXT;

-- Backfill from the caller context each row already carries. `->>` yields NULL
-- for an absent key rather than raising, so rows whose context never held a
-- tenant stay NULL and are handled explicitly below rather than silently
-- acquiring a value.
UPDATE idea_evidence_intake
SET tenant_id = NULLIF(btrim(caller_context_json ->> 'tenant_id'), '')
WHERE tenant_id IS NULL;

-- Refuse to continue if any row could not be attributed.
--
-- `SET NOT NULL` is the refusal. It fails on any row the backfill above could
-- not attribute, naming the column, and it leaves those rows in place.
--
-- The alternative -- a default tenant, or deleting the rows -- is the failure
-- this migration exists to prevent. A defaulted tenant is an invented
-- authority: it would make one tenant the owner of another's retained receipt,
-- which is worse than the defect being fixed because the result would be
-- indistinguishable from a genuine record. Deleting destroys receipts a
-- consumer may still replay against.
--
-- If this statement fails, the unattributed rows are visible and recoverable:
--
--     SELECT idempotency_key, report_evidence_pack_id, caller_context_json
--     FROM idea_evidence_intake
--     WHERE COALESCE(btrim(caller_context_json ->> 'tenant_id'), '') = '',
--
-- Predicated on the caller context, not on tenant_id. This migration runs in a
-- transaction, so a refusal rolls back the ADD COLUMN above it and a query
-- naming tenant_id would fail with column does not exist.
--
-- The operator attributes them deliberately from the surrounding evidence and
-- re-runs. Nothing here guesses.
--
-- Written without a dollar-quoted block on purpose. The migration runner in
-- reporting_persistence/schema.py splits each file on the statement separator,
-- so a dollar-quoted body containing separators is torn apart before it reaches
-- the server. That split is naive enough to cut inside comments too, so this
-- file avoids the character entirely outside real statement ends.
ALTER TABLE idea_evidence_intake ALTER COLUMN tenant_id SET NOT NULL;

-- Replace the key with a form that is a no-op on re-execution.
--
-- The migration runner keeps no ledger of applied migrations: it re-executes
-- every file on every call, and ensure_runtime_schema() calls it more than once
-- per process. So each statement here must be idempotent AND cheap. A
-- DROP CONSTRAINT / ADD CONSTRAINT PRIMARY KEY pair is neither -- it takes an
-- exclusive lock and rebuilds the unique index on every startup, which on a
-- growing append-only ledger can block running instances or exceed the
-- statement timeout and stop the API and workers from starting at all.
--
-- The old single-column key is dropped by name, which is a no-op once it is
-- gone, and uniqueness is then carried by a unique index created IF NOT EXISTS.
-- On a NOT NULL column pair that is the same guarantee a composite primary key
-- gives, and it is what ON CONFLICT (tenant_id, idempotency_key) in the
-- transfer path infers. The trade-off, stated rather than hidden: the table
-- carries no formal PRIMARY KEY designation afterwards. Attaching one with
-- ADD CONSTRAINT ... PRIMARY KEY USING INDEX would reintroduce a statement that
-- cannot be re-run, so it belongs with a migration ledger, not here.
--
-- Conditional DDL is the alternative and is not available: the runner splits
-- files on the statement separator, which tears a dollar-quoted DO block apart.
ALTER TABLE idea_evidence_intake DROP CONSTRAINT IF EXISTS idea_evidence_intake_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS idea_evidence_intake_tenant_identity
    ON idea_evidence_intake (tenant_id, idempotency_key);

-- Reads are tenant-scoped, so the source lookup is too: an unscoped index
-- invites an unscoped query. Named distinctly from the index it replaces, for
-- the same reason as the key above -- DROP IF EXISTS on the OLD name is a no-op
-- once it is gone, and CREATE IF NOT EXISTS on the NEW name is a no-op once it
-- exists. Reusing one name would drop and rebuild the index on every startup.
DROP INDEX IF EXISTS idx_idea_evidence_intake_source;
CREATE INDEX IF NOT EXISTS idx_idea_evidence_intake_tenant_source
    ON idea_evidence_intake (tenant_id, report_evidence_pack_id, evidence_packet_id);
