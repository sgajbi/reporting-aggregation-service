-- report#350: report_request identity is the caller's idempotency key alone, so
-- two tenants choosing the same string collide in the job ledger that backs
-- every report job route.
--
-- Measured through the real HTTP materialization route before this change:
--   tenant-a -> 202
--   tenant-b -> 409 {"code": "idempotency_conflict",
--                    "message": "Idempotency-Key was reused with different idea evidence content."}
-- Tenant B reused nothing and sent identical content. It collided with another
-- tenant's request, and the refusal named a cause that was not true. Where the
-- bodies match, both tenants resolve to ONE stored report_request_id, which is
-- the more serious half.
--
-- `tenant_id` is already NOT NULL and populated on every row, so identity moves
-- from (idempotency_key) to (tenant_id, idempotency_key) with no backfill and
-- nothing to attribute. That is the whole reason this is safe to do in one
-- step, unlike migration 025 where the tenant had to be recovered from a JSON
-- blob and unattributable rows were refused rather than defaulted.
--
-- `report_request_id` is NOT re-derived. lotus-idea confirmed under C5-X03 that
-- owner_request_id, owner_realization_id and the rest of the materialization
-- receipt identity are immutable. Re-deriving them would hand a consumer an
-- identity it has never seen for a receipt it already holds.

ALTER TABLE report_request
    DROP CONSTRAINT IF EXISTS report_request_idempotency_key_key;

-- Named explicitly rather than left to the implicit constraint name, so a later
-- migration can find it. Partial-free and non-deferrable: two tenants holding
-- the same key must both be storable at the same instant.
CREATE UNIQUE INDEX IF NOT EXISTS report_request_tenant_idempotency_key
    ON report_request (tenant_id, idempotency_key);
