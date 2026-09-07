from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path = [path for path in sys.path if path != str(SRC)]
sys.path.insert(0, str(SRC))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.reporting_persistence.schema import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    LEGACY_STATUS_EVENT_BASELINE,
    ReportSchemaCompatibilityError,
    apply_report_schema_migrations,
    validate_supported_report_schema,
)

LEGACY_FIXTURE = ROOT / "scripts" / "fixtures" / "report_status_event_pre_contract_v0.sql"
LEGACY_EVENT_ID = "event-pre-contract-v0"
EXPECTED_CONTRACT_COLUMNS = {
    "event_schema_version": ("text", "NO"),
    "event_family": ("text", "NO"),
    "event_payload_json": ("jsonb", "NO"),
    "event_idempotency_key": ("text", "YES"),
}
EXPECTED_INDEXES = {
    "idx_report_status_event_family_created",
    "idx_report_status_event_idempotency_key",
}
NULLABILITY_MISMATCHES = {
    "event_schema_version": ("YES", "NO"),
    "event_family": ("YES", "NO"),
    "event_payload_json": ("YES", "NO"),
    "event_idempotency_key": ("NO", "YES"),
}
#: The columns migration 024 chose a type for, rather than transcribed from
#: SQLite. Asserted by type because a table created with TEXT everywhere would
#: satisfy a presence check while delivering neither JSONB shape validation nor
#: instant-ordered timestamps -- the two reasons for the move (report#326).
EXPECTED_INTAKE_COLUMNS = {
    "idempotency_key": ("text", "NO"),
    "intake_id": ("text", "NO"),
    "payload_fingerprint": ("text", "NO"),
    "response_json": ("jsonb", "NO"),
    "caller_context_json": ("jsonb", "NO"),
    "tenant_id": ("text", "NO"),
    "accepted_at_utc": ("timestamp with time zone", "NO"),
    "created_at_utc": ("timestamp with time zone", "NO"),
    "correlation_id": ("text", "YES"),
    "trace_id": ("text", "YES"),
}
EXPECTED_INTAKE_INDEXES = {
    # report#344 moved the admitted tenant into the intake identity, and the
    # names changed with it rather than the definitions changing underneath the
    # old names. `apply_report_schema_migrations` keeps no ledger and re-runs
    # every file on every call, so a rename is what makes both statements
    # no-ops on re-execution: DROP IF EXISTS on the old name fires once,
    # CREATE IF NOT EXISTS on the new name fires once. Reusing a name would
    # drop and rebuild the index at every startup.
    #
    # `idea_evidence_intake_pkey` is gone deliberately: uniqueness is now a
    # unique index on (tenant_id, idempotency_key), because
    # ADD CONSTRAINT ... PRIMARY KEY cannot be re-executed. Migration 025
    # states that trade-off.
    "idea_evidence_intake_tenant_identity",
    "idx_idea_evidence_intake_tenant_source",
    "idx_idea_evidence_intake_created",
}


def run_upgrade_check(database_url: str) -> None:
    schema_name = f"report_upgrade_{uuid4().hex[:12]}"
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        _execute_fixture(connection)

        detected_before = validate_supported_report_schema(connection)
        if detected_before != LEGACY_STATUS_EVENT_BASELINE:
            raise RuntimeError(
                "legacy_upgrade_fixture_mismatch:"
                f"expected={LEGACY_STATUS_EVENT_BASELINE}:actual={detected_before}"
            )

        first_run = apply_report_schema_migrations(connection)
        second_run = apply_report_schema_migrations(connection)

        detected_after = validate_supported_report_schema(connection)
        if detected_after != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "legacy_upgrade_target_mismatch:"
                f"expected={CURRENT_SCHEMA_VERSION}:actual={detected_after}"
            )
        if first_run != second_run:
            raise RuntimeError("legacy_upgrade_migration_order_not_deterministic")

        _verify_contract_columns(connection)
        _verify_legacy_row(connection)
        _verify_indexes(connection)
        _verify_intake_ledger_schema(connection)

        connection.execute("RESET search_path")
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))

        _verify_unsupported_nullability(connection)


def _execute_fixture(connection: psycopg.Connection[dict[str, object]]) -> None:
    fixture = LEGACY_FIXTURE.read_text(encoding="utf-8")
    for statement in fixture.split(";"):
        if statement.strip():
            connection.execute(statement)


def _verify_contract_columns(connection: psycopg.Connection[dict[str, object]]) -> None:
    rows = connection.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'report_status_event'
          AND column_name IN (
              'event_schema_version',
              'event_family',
              'event_payload_json',
              'event_idempotency_key'
          )
        """
    ).fetchall()
    observed = {
        str(row["column_name"]): (str(row["data_type"]), str(row["is_nullable"])) for row in rows
    }
    if observed != EXPECTED_CONTRACT_COLUMNS:
        raise RuntimeError(
            "legacy_upgrade_contract_columns_mismatch:"
            f"expected={EXPECTED_CONTRACT_COLUMNS}:actual={observed}"
        )


def _verify_legacy_row(connection: psycopg.Connection[dict[str, object]]) -> None:
    row = connection.execute(
        """
        SELECT event_schema_version, event_family, event_payload_json, event_idempotency_key,
               message, correlation_id, trace_id
        FROM report_status_event
        WHERE status_event_id = %s
        """,
        (LEGACY_EVENT_ID,),
    ).fetchone()
    if row is None:
        raise RuntimeError("legacy_upgrade_event_missing")
    expected = {
        "event_schema_version": "report-status-event.legacy.v0",
        "event_family": "job_lifecycle",
        "event_payload_json": {"payload_posture": "legacy_message_only"},
        "event_idempotency_key": None,
        "message": "Legacy event retained for executable upgrade proof.",
        "correlation_id": "corr-pre-contract-v0",
        "trace_id": "trace-pre-contract-v0",
    }
    if row != expected:
        raise RuntimeError(f"legacy_upgrade_event_mismatch:expected={expected}:actual={row}")


def _verify_indexes(connection: psycopg.Connection[dict[str, object]]) -> None:
    rows = connection.execute(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND indexname IN (
              'idx_report_status_event_family_created',
              'idx_report_status_event_idempotency_key'
          )
        """
    ).fetchall()
    observed = {str(row["indexname"]) for row in rows}
    if observed != EXPECTED_INDEXES:
        raise RuntimeError(
            f"legacy_upgrade_indexes_mismatch:expected={EXPECTED_INDEXES}:actual={observed}"
        )


def _verify_intake_ledger_schema(connection: psycopg.Connection[dict[str, object]]) -> None:
    """Migration 024 must create the intake ledger with its intended types.

    Not a populated-upgrade proof, and deliberately not named as one. The
    pre-migration ledger is a SQLite file, so there is no PostgreSQL predecessor
    to carry forward here and nothing this check could observe about a transfer.
    That proof is report#326 slice 3, against a real populated file.

    Closed PR #332 seeded a TEXT-typed table and asserted a row survived the
    migration -- which CREATE TABLE IF NOT EXISTS no-ops past, so it passed
    without converting anything. Asserting what the migration actually does is
    the honest replacement.
    """
    rows = connection.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'idea_evidence_intake'
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("intake_ledger_table_missing_after_migration")

    observed = {
        str(row["column_name"]): (str(row["data_type"]), str(row["is_nullable"]))
        for row in rows
        if str(row["column_name"]) in EXPECTED_INTAKE_COLUMNS
    }
    if observed != EXPECTED_INTAKE_COLUMNS:
        raise RuntimeError(
            f"intake_ledger_columns_mismatch:expected={EXPECTED_INTAKE_COLUMNS}:actual={observed}"
        )

    index_rows = connection.execute(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'idea_evidence_intake'
        """
    ).fetchall()
    observed_indexes = {str(row["indexname"]) for row in index_rows}
    if observed_indexes != EXPECTED_INTAKE_INDEXES:
        raise RuntimeError(
            "intake_ledger_indexes_mismatch:"
            f"expected={EXPECTED_INTAKE_INDEXES}:actual={observed_indexes}"
        )


def _verify_unsupported_nullability(
    connection: psycopg.Connection[dict[str, object]],
) -> None:
    for column, (actual_nullable, expected_nullable) in NULLABILITY_MISMATCHES.items():
        schema_name = f"report_nullability_{uuid4().hex[:12]}"
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        _execute_fixture(connection)
        apply_report_schema_migrations(connection)

        if actual_nullable == "YES":
            connection.execute(
                sql.SQL("ALTER TABLE report_status_event ALTER COLUMN {} DROP NOT NULL").format(
                    sql.Identifier(column)
                )
            )
        else:
            connection.execute(
                """
                UPDATE report_status_event
                SET event_idempotency_key = 'legacy-event-idempotency-key'
                WHERE event_idempotency_key IS NULL
                """
            )
            connection.execute(
                sql.SQL("ALTER TABLE report_status_event ALTER COLUMN {} SET NOT NULL").format(
                    sql.Identifier(column)
                )
            )

        expected_fragment = (
            f"{column}:nullable={actual_nullable}:expected_nullable={expected_nullable}"
        )
        try:
            apply_report_schema_migrations(connection)
        except ReportSchemaCompatibilityError as exc:
            if expected_fragment not in str(exc):
                raise RuntimeError(
                    "nullability_preflight_diagnostic_mismatch:"
                    f"expected={expected_fragment}:actual={exc}"
                ) from exc
        else:
            raise RuntimeError(f"nullability_preflight_accepted_unsupported:{column}")

        observed = connection.execute(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'report_status_event'
              AND column_name = %s
            """,
            (column,),
        ).fetchone()
        if observed is None or observed["is_nullable"] != actual_nullable:
            raise RuntimeError(
                "nullability_preflight_mutated_unsupported_schema:"
                f"column={column}:expected={actual_nullable}:actual={observed}"
            )

        connection.execute("RESET search_path")
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


def main() -> int:
    database_url = os.environ.get("REPORT_JOB_LEDGER_DATABASE_URL")
    if not database_url:
        print("REPORT_JOB_LEDGER_DATABASE_URL is required for schema upgrade smoke.")
        return 1
    try:
        run_upgrade_check(database_url)
    except Exception as exc:
        print(f"Report schema upgrade check failed: {exc}")
        return 1
    print(
        "Report schema upgrade check passed "
        f"(source={LEGACY_STATUS_EVENT_BASELINE}, target={CURRENT_SCHEMA_VERSION})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
