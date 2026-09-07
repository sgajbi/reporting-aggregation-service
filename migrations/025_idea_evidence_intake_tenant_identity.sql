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
--     FROM idea_evidence_intake WHERE tenant_id IS NULL,
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

-- Replace the key. The old constraint made one caller's chosen string a
-- global resource, the new one scopes it to the caller that chose it, so two
-- tenants may legitimately use the same idempotency key.
ALTER TABLE idea_evidence_intake DROP CONSTRAINT IF EXISTS idea_evidence_intake_pkey;
ALTER TABLE idea_evidence_intake
    ADD CONSTRAINT idea_evidence_intake_pkey PRIMARY KEY (tenant_id, idempotency_key);

-- Reads are tenant-scoped, and the source lookup carried from 024 is scoped
-- with them: an unscoped index invites an unscoped query.
DROP INDEX IF EXISTS idx_idea_evidence_intake_source;
CREATE INDEX IF NOT EXISTS idx_idea_evidence_intake_source
    ON idea_evidence_intake (tenant_id, report_evidence_pack_id, evidence_packet_id);
