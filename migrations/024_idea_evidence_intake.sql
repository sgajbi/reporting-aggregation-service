-- report#326 slice 1: the PostgreSQL home for the Idea evidence intake
-- ledger, which is today the only production Report store on SQLite and
-- therefore the only one outside the migration contract.
--
-- Creation only. There is no pre-existing PostgreSQL table to alter: the
-- prior state is a SQLite FILE, so CREATE is the honest operation and
-- this migration proves nothing about carrying existing rows across.
-- That transfer, with a real populated file, is slice 3.
--
-- Types are chosen rather than transcribed. SQLite held the two payloads
-- and both timestamps as TEXT because it has no better option. Keeping
-- TEXT here would move the store without gaining anything the move is
-- for: JSONB is queryable and validates its own shape on write, and
-- TIMESTAMPTZ compares as an instant rather than as a string, which for
-- ISO-8601 sorts correctly under UTC and wrongly across offsets.
CREATE TABLE IF NOT EXISTS idea_evidence_intake (
    idempotency_key TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    response_json JSONB NOT NULL,
    caller_context_json JSONB NOT NULL,
    report_evidence_pack_id TEXT NOT NULL,
    conversion_intent_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    evidence_packet_id TEXT NOT NULL,
    evidence_content_fingerprint TEXT NOT NULL,
    producer TEXT NOT NULL,
    supportability_status TEXT NOT NULL,
    accepted_at_utc TIMESTAMPTZ NOT NULL,
    created_at_utc TIMESTAMPTZ NOT NULL,
    correlation_id TEXT,
    trace_id TEXT
);

-- The idempotency key is the primary key, which is what makes a replay
-- recognisable at all. Named separately from the SQLite original only in
-- that PostgreSQL enforces it as a real constraint rather than a
-- convention the writer maintains.

-- The source lookup index is created by migration 025, tenant-leading.
--
-- It was created here first, unscoped, and 025 dropped and replaced it. That
-- is a correct end state and a wasteful path: the runner keeps no ledger of
-- applied migrations, so every startup rebuilt the unscoped index here and
-- dropped it there -- twice per process, since ensure_runtime_schema() runs
-- the migrations through two readiness checks. On a populated ledger that is
-- real I/O and locking for an index nothing ever reads.
--
-- Removing the creation rather than the drop, because the end state is
-- identical either way and this is the statement with no lasting effect.

-- Creation-ordered reads for operational inspection.
CREATE INDEX IF NOT EXISTS idx_idea_evidence_intake_created
    ON idea_evidence_intake (created_at_utc);
