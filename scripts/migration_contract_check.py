from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path = [path for path in sys.path if path != str(SRC)]
sys.path.insert(0, str(SRC))

import psycopg  # noqa: E402

from app.report_batch_orchestrator.postgres_ledger import PostgresReportBatchLedger  # noqa: E402
from app.reporting_jobs.postgres_ledger import PostgresReportJobLedger  # noqa: E402
from app.reporting_lineage.postgres_store import PostgresReportInputSnapshotStore  # noqa: E402

REQUIRED_DOC = Path("docs/standards/migration-contract.md")
REQUIRED_PHRASES = (
    "report job ledger schema",
    "forward-fix",
    "forward-only schema",
    "report_request",
    "report_job",
    "report_status_event",
    "report_input_snapshot",
    "report_upstream_call",
    "report_batch",
    "report_batch_item",
    # The contract must name the intake TABLE it now governs: one in the
    # mandatory set but absent from the document leaves the standard silently
    # narrower than the gate (report#326).
    #
    # Backticked on purpose. The check lowercases the document, so a bare
    # "idea_evidence_intake" is already satisfied by the environment variable
    # names IDEA_EVIDENCE_INTAKE_LEDGER_PATH and
    # REPORT_IDEA_EVIDENCE_INTAKE_LEDGER_BACKEND -- a phrase that can never go
    # missing gates nothing. The closing backtick is what separates the table
    # from those prefixes.
    "`idea_evidence_intake`",
    "archive_request_id",
    "archive_document_id",
    "archive_completed_at",
)


#: The intake table's types as migration 024 creates them. Checked against the
#: schema a deployment actually runs on, not only the throwaway schema the
#: upgrade smoke builds: CREATE TABLE IF NOT EXISTS no-ops on an existing
#: table, so a TEXT-typed predecessor would pass an existence check and then
#: fail at runtime on the JSONB bind (report#326).
DEPLOYED_INTAKE_COLUMN_TYPES = {
    "response_json": "jsonb",
    "caller_context_json": "jsonb",
    "accepted_at_utc": "timestamp with time zone",
    "created_at_utc": "timestamp with time zone",
}


def _deployed_intake_column_mismatch(connection: object) -> str:
    """Empty when the deployed intake table has the types migration 024 chose."""
    rows = connection.execute(  # type: ignore[attr-defined]
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'idea_evidence_intake'
        """
    ).fetchall()
    observed = {
        str(row[0]): str(row[1]) for row in rows if str(row[0]) in DEPLOYED_INTAKE_COLUMN_TYPES
    }
    if observed != DEPLOYED_INTAKE_COLUMN_TYPES:
        return (
            "deployed idea_evidence_intake column types differ from migration 024: "
            f"expected={DEPLOYED_INTAKE_COLUMN_TYPES} actual={observed}"
        )
    return ""


def run_ledger_schema_checks() -> int:
    if not REQUIRED_DOC.exists():
        print(f"Missing required migration contract document: {REQUIRED_DOC}")
        return 1

    content = REQUIRED_DOC.read_text(encoding="utf-8").lower()
    missing = [phrase for phrase in REQUIRED_PHRASES if phrase not in content]
    if missing:
        print("Migration contract document is missing required phrases:")
        for phrase in missing:
            print(f"- {phrase}")
        return 1

    database_url = os.environ.get("REPORT_JOB_LEDGER_DATABASE_URL")
    if not database_url:
        print("REPORT_JOB_LEDGER_DATABASE_URL is required for PostgreSQL migration smoke.")
        return 1

    with (
        PostgresReportJobLedger(database_url) as ledger,
        PostgresReportInputSnapshotStore(database_url) as snapshot_store,
        PostgresReportBatchLedger(database_url) as batch_ledger,
    ):
        ledger.check_ready()
        snapshot_store.check_ready()
        batch_ledger.check_ready()

    with psycopg.connect(database_url) as connection:
        table_rows = connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN (
                  'report_request',
                  'report_job',
                  'report_status_event',
                  'report_batch',
                  'report_batch_item',
                  'idea_evidence_intake'
              )
            """
        ).fetchall()
        tables = {row[0] for row in table_rows}
        missing_tables = {
            "report_request",
            "report_job",
            "report_status_event",
            "report_batch",
            "report_batch_item",
            "idea_evidence_intake",
        } - tables
        if missing_tables:
            print(f"Ledger schema smoke failed: missing tables {sorted(missing_tables)}")
            return 1

        intake_mismatch = _deployed_intake_column_mismatch(connection)
        if intake_mismatch:
            print(f"Ledger schema smoke failed: {intake_mismatch}")
            return 1

        snapshot_table_rows = connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('report_input_snapshot', 'report_upstream_call')
            """
        ).fetchall()
        snapshot_tables = {row[0] for row in snapshot_table_rows}
        missing_snapshot_tables = {
            "report_input_snapshot",
            "report_upstream_call",
        } - snapshot_tables
        if missing_snapshot_tables:
            print(f"Ledger schema smoke failed: missing tables {sorted(missing_snapshot_tables)}")
            return 1

        index_rows = connection.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname IN (
                  'idx_report_request_created',
                  'idx_report_request_tenant_region_created',
                  'idx_report_request_as_of_date',
                  'idx_report_request_scope_created',
                  'idx_report_job_status_updated',
                  'idx_report_job_created',
                  'idx_report_job_completed',
                  'idx_report_job_request',
                  'idx_report_job_archive_document',
                  'idx_report_status_event_job_created',
                  'idx_report_input_snapshot_created',
                  'idx_report_input_snapshot_supportability',
                  'idx_report_input_snapshot_report_type_created',
                  'idx_report_upstream_call_snapshot',
                  'idx_report_upstream_call_service_endpoint',
                  'idx_report_upstream_call_supportability',
                  'idx_report_upstream_call_created',
                  'idx_report_batch_created',
                  'idx_report_batch_tenant_region_created',
                  'idx_report_batch_status_created',
                  'idx_report_batch_item_batch_position',
                  'idx_report_batch_item_portfolio',
                  'idx_report_batch_item_status_created',
                  'idx_report_batch_item_lease_expiry',
                  'idx_report_batch_item_report_job',
                  'idx_report_batch_item_retry',
                  'idx_report_batch_cycle_recognition',
                  'idx_report_input_snapshot_revision'
              )
            """
        ).fetchall()
        indexes = {row[0] for row in index_rows}
        missing_indexes = {
            "idx_report_request_created",
            "idx_report_request_tenant_region_created",
            "idx_report_request_as_of_date",
            "idx_report_request_scope_created",
            "idx_report_job_status_updated",
            "idx_report_job_created",
            "idx_report_job_completed",
            "idx_report_job_request",
            "idx_report_job_archive_document",
            "idx_report_status_event_job_created",
            "idx_report_input_snapshot_created",
            "idx_report_input_snapshot_supportability",
            "idx_report_input_snapshot_report_type_created",
            "idx_report_upstream_call_snapshot",
            "idx_report_upstream_call_service_endpoint",
            "idx_report_upstream_call_supportability",
            "idx_report_upstream_call_created",
            "idx_report_batch_created",
            "idx_report_batch_tenant_region_created",
            "idx_report_batch_status_created",
            "idx_report_batch_item_batch_position",
            "idx_report_batch_item_portfolio",
            "idx_report_batch_item_status_created",
            "idx_report_batch_item_lease_expiry",
            "idx_report_batch_item_report_job",
            "idx_report_batch_item_retry",
            "idx_report_batch_cycle_recognition",
            "idx_report_input_snapshot_revision",
        } - indexes
        if missing_indexes:
            print(f"Ledger schema smoke failed: missing indexes {sorted(missing_indexes)}")
            return 1

        unique_rows = connection.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'report_request'::regclass
              AND contype = 'u'
              AND conkey = ARRAY[
                  (
                      SELECT attnum
                      FROM pg_attribute
                      WHERE attrelid = 'report_request'::regclass
                        AND attname = 'tenant_id'
                  ),
                  (
                      SELECT attnum
                      FROM pg_attribute
                      WHERE attrelid = 'report_request'::regclass
                        AND attname = 'idempotency_key'
                  )
              ]::smallint[]
            """
        ).fetchall()
        if not unique_rows:
            # report#350: identity is (tenant_id, idempotency_key). Asserting the
            # old single-column uniqueness would keep demanding the exact defect
            # this gate is meant to prevent -- two tenants colliding on one key.
            print("Ledger schema smoke failed: (tenant_id, idempotency_key) uniqueness is missing.")
            return 1

        batch_unique_rows = connection.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'report_batch'::regclass
              AND contype = 'u'
              AND conkey = ARRAY[
                  (
                      SELECT attnum
                      FROM pg_attribute
                      WHERE attrelid = 'report_batch'::regclass
                        AND attname = 'idempotency_key'
                  )
              ]::smallint[]
            """
        ).fetchall()
        if not batch_unique_rows:
            print("Ledger schema smoke failed: report_batch.idempotency_key uniqueness is missing.")
            return 1

        batch_column_rows = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'report_batch'
              AND column_name IN (
                  'updated_at',
                  'started_at',
                  'completed_at',
                  'cancelled_at',
                  'failed_at'
              )
            """
        ).fetchall()
        batch_columns = {row[0] for row in batch_column_rows}
        missing_batch_columns = {
            "updated_at",
            "started_at",
            "completed_at",
            "cancelled_at",
            "failed_at",
        } - batch_columns
        if missing_batch_columns:
            print(
                "Ledger schema smoke failed: missing batch lifecycle columns "
                f"{sorted(missing_batch_columns)}"
            )
            return 1

        batch_item_column_rows = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'report_batch_item'
              AND column_name IN (
                  'report_job_id',
                  'lease_owner',
                  'lease_token',
                  'lease_acquired_at',
                  'lease_expires_at',
                  'last_heartbeat_at',
                  'dispatched_at',
                  'attempt_count',
                  'retry_eligible',
                  'next_retry_at',
                  'last_error_category',
                  'last_error_summary',
                  'started_at',
                  'completed_at',
                  'cancelled_at'
              )
            """
        ).fetchall()
        batch_item_columns = {row[0] for row in batch_item_column_rows}
        missing_batch_item_columns = {
            "report_job_id",
            "lease_owner",
            "lease_token",
            "lease_acquired_at",
            "lease_expires_at",
            "last_heartbeat_at",
            "dispatched_at",
            "attempt_count",
            "retry_eligible",
            "next_retry_at",
            "last_error_category",
            "last_error_summary",
            "started_at",
            "completed_at",
            "cancelled_at",
        } - batch_item_columns
        if missing_batch_item_columns:
            print(
                "Ledger schema smoke failed: missing batch dispatch columns "
                f"{sorted(missing_batch_item_columns)}"
            )
            return 1

        batch_status_rows = connection.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'report_batch'::regclass
              AND conname = 'report_batch_status_check'
            """
        ).fetchall()
        if not batch_status_rows:
            print("Ledger schema smoke failed: report batch status check constraint is missing.")
            return 1
        batch_status_constraint = str(batch_status_rows[0][0])
        for status in (
            "materialized",
            "running",
            "paused",
            "cancelled",
            "completed",
            "completed_with_failures",
            "failed",
        ):
            if status not in batch_status_constraint:
                print(
                    "Ledger schema smoke failed: report batch status check constraint "
                    f"is missing {status}."
                )
                return 1

        batch_item_status_rows = connection.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'report_batch_item'::regclass
              AND conname = 'report_batch_item_status_check'
            """
        ).fetchall()
        if not batch_item_status_rows:
            print("Ledger schema smoke failed: batch item status check constraint is missing.")
            return 1
        batch_item_status_constraint = str(batch_item_status_rows[0][0])
        for status in (
            "materialized",
            "leased",
            "waiting_on_report_job",
            "succeeded",
            "failed_retryable",
            "failed_terminal",
            "cancelled",
            "recovery_pending",
        ):
            if status not in batch_item_status_constraint:
                print(
                    "Ledger schema smoke failed: batch item status check constraint "
                    f"is missing {status}."
                )
                return 1

        failure_category_rows = connection.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'report_job'::regclass
              AND conname = 'report_job_failure_category_check'
            """
        ).fetchall()
        if not failure_category_rows:
            print("Ledger schema smoke failed: failure category check constraint is missing.")
            return 1
        failure_category_constraint = str(failure_category_rows[0][0])
        for category in (
            "entitlement_failed",
            "validation_failed",
            "upstream_data_failed",
            "data_incomplete",
            "timeout",
            "cancelled",
            "operator_intervention_required",
            "archive_validation_failed",
            "archive_conflict",
            "archive_storage_failed",
            "archive_execution_failed",
        ):
            if category not in failure_category_constraint:
                print(
                    "Ledger schema smoke failed: failure category check constraint "
                    f"is missing {category}."
                )
                return 1

        status_rows = connection.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'report_job'::regclass
              AND conname = 'report_job_status_check'
            """
        ).fetchall()
        if not status_rows:
            print("Ledger schema smoke failed: report job status check constraint is missing.")
            return 1
        status_constraint = str(status_rows[0][0])
        for status in ("archiving", "archived"):
            if status not in status_constraint:
                print(
                    "Ledger schema smoke failed: report job status check constraint "
                    f"is missing {status}."
                )
                return 1

        archive_column_rows = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'report_job'
              AND column_name IN (
                  'archive_request_id',
                  'archive_document_id',
                  'archive_completed_at'
              )
            """
        ).fetchall()
        archive_columns = {row[0] for row in archive_column_rows}
        missing_archive_columns = {
            "archive_request_id",
            "archive_document_id",
            "archive_completed_at",
        } - archive_columns
        if missing_archive_columns:
            print(
                "Ledger schema smoke failed: missing archive columns "
                f"{sorted(missing_archive_columns)}"
            )
            return 1

        snapshot_constraint_rows = connection.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'report_input_snapshot'::regclass
              AND contype = 'u'
            """
        ).fetchall()
        if not any("report_job_id" in str(row[0]) for row in snapshot_constraint_rows):
            print(
                "Ledger schema smoke failed: "
                "report_input_snapshot.report_job_id uniqueness is missing."
            )
            return 1

        upstream_failure_category_rows = connection.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'report_upstream_call'::regclass
              AND contype = 'c'
              AND conname LIKE '%failure_category%'
            """
        ).fetchall()
        if not upstream_failure_category_rows:
            print(
                "Ledger schema smoke failed: "
                "report_upstream_call failure category check is missing."
            )
            return 1
        upstream_failure_category_constraint = str(upstream_failure_category_rows[0][0])
        for category in (
            "none",
            "partial_data",
            "unsupported_input",
            "upstream_unavailable",
            "upstream_error",
            "timeout",
            "redacted",
        ):
            if category not in upstream_failure_category_constraint:
                print(
                    "Ledger schema smoke failed: upstream failure category check constraint "
                    f"is missing {category}."
                )
                return 1

    print("Migration contract check passed (PostgreSQL report job and batch ledger schema mode).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate migration contract requirements.")
    parser.add_argument("--mode", choices=["ledger-schema", "no-schema"], default="ledger-schema")
    args = parser.parse_args()

    if args.mode in {"ledger-schema", "no-schema"}:
        return run_ledger_schema_checks()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
