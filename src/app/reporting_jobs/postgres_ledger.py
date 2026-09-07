from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterator, Mapping, cast
from uuid import uuid4

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.postgres import PostgresConnectionProvider
from app.report_ordering_catalogue.template_resolution import job_template_identity
from app.reporting_jobs.event_contracts import (
    build_report_status_event_contract,
    legacy_report_status_event_contract,
)
from app.reporting_jobs.lease_telemetry import record_report_job_work_lease_event
from app.reporting_jobs.ledger import (
    IdempotencyConflictError,
    InvalidReportJobTransitionError,
    InvalidReportJobWorkTransitionError,
    MissingIdempotencyKeyError,
    PendingArchiveLineage,
    ReportJobNotFoundError,
    _request_parts,
    _transition_event_payload,
    client_identity_hash_from_record,
    compute_request_hash,
    resolve_job_accepted_contract,
    utc_now,
)
from app.reporting_jobs.lifecycle_policy import (
    is_report_job_cancellable,
    is_report_job_transition_allowed,
)
from app.reporting_jobs.models import (
    OutcomeReviewReportJobRequest,
    PortfolioReviewJobRequest,
    ProofPackReportJobRequest,
    ReportCallerContext,
    ReportJobArchiveStatusRecord,
    ReportJobLedgerRecord,
    ReportJobListFilters,
    ReportJobOwnerSnapshot,
    ReportJobRelationshipRecord,
    ReportJobRelationshipType,
    ReportJobStatus,
    ReportRerenderAttemptRecord,
    ReportStatusEvent,
    WaveReportJobRequest,
)
from app.reporting_jobs.work_queue import (
    ReportJobWorkItem,
    ReportJobWorkRetryPolicy,
    decide_report_job_work_failure,
)
from app.reporting_persistence import ManagedPostgresAdapter, apply_report_schema_migrations


class PostgresReportJobLedger(ManagedPostgresAdapter):
    """PostgreSQL-backed runtime ledger for report request/job/status lifecycle state."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        connection_provider: PostgresConnectionProvider | None = None,
    ) -> None:
        if connection_provider is None:
            if database_url is None:
                raise ValueError("report_job_ledger_database_url_required")
            connection_provider = PostgresConnectionProvider(database_url=database_url)
            self._owns_connection_provider = True
        else:
            self._owns_connection_provider = False
        self._connection_provider = connection_provider
        self.ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[Connection[Mapping[str, Any]]]:
        with self._connection_provider.connection() as connection:
            yield connection

    def close(self) -> None:
        if self._owns_connection_provider:
            self._connection_provider.close()

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            apply_report_schema_migrations(connection)

    def check_ready(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                      'report_request',
                      'report_job',
                      'report_status_event',
                      'report_job_work_item'
                  )
                """
            ).fetchall()
            present = {str(row["table_name"]) for row in rows}
            missing = {
                "report_request",
                "report_job",
                "report_status_event",
                "report_job_work_item",
            } - present
            if missing:
                raise RuntimeError(f"report_job_ledger_schema_missing:{','.join(sorted(missing))}")
            relationship_rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'report_job_relationship'
                """
            ).fetchall()
            if not relationship_rows:
                raise RuntimeError("report_job_relationship_schema_missing")

            event_column_rows = connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'report_status_event'
                  AND column_name IN (
                      'event_schema_version',
                      'event_family',
                      'event_payload_json',
                      'event_idempotency_key'
                  )
                """
            ).fetchall()
            event_columns = {str(row["column_name"]) for row in event_column_rows}
            missing_event_columns = {
                "event_schema_version",
                "event_family",
                "event_payload_json",
                "event_idempotency_key",
            } - event_columns
            if missing_event_columns:
                raise RuntimeError(
                    "report_status_event_contract_schema_missing:"
                    f"{','.join(sorted(missing_event_columns))}"
                )

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
            archive_columns = {str(row["column_name"]) for row in archive_column_rows}
            missing_archive_columns = {
                "archive_request_id",
                "archive_document_id",
                "archive_completed_at",
            } - archive_columns
            if missing_archive_columns:
                raise RuntimeError(
                    "report_job_ledger_archive_schema_missing:"
                    f"{','.join(sorted(missing_archive_columns))}"
                )
            rerender_rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'report_rerender_attempt'
                """
            ).fetchall()
            if not rerender_rows:
                raise RuntimeError("report_rerender_attempt_schema_missing")

    def create_portfolio_review_job(
        self,
        *,
        request: PortfolioReviewJobRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
    ) -> ReportJobLedgerRecord:
        return self._create_report_job(
            report_type="portfolio_review",
            accepted_message="Portfolio review report job accepted.",
            request=request,
            caller_context=caller_context,
            idempotency_key=idempotency_key,
        )

    def create_replay_derived_job(
        self,
        *,
        source_job_id: str,
        request: PortfolioReviewJobRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
        reason: str,
    ) -> ReportJobLedgerRecord:
        """Create the replay's derived job with the one-replacement guard.

        The guard runs inside the same lock/transaction as the idempotency
        resolution: an existing record for THIS key returns idempotently, but
        a novel key is refused while any prior replay of the source is in
        flight or has succeeded - a failed source must never gain a second
        replacement document.
        """

        source_job = self.get_job(source_job_id)
        return self._create_report_job(
            report_type="portfolio_review",
            accepted_message="Portfolio review report job accepted.",
            request=request,
            caller_context=caller_context,
            idempotency_key=idempotency_key,
            replay_source_job_id=source_job_id,
            replay_reason=reason,
            inherited_template=(
                source_job.render_template_id,
                source_job.render_template_version,
            ),
            inherited_contract=source_job.accepted_document_contract,
        )

    def submit_portfolio_review_job(
        self,
        *,
        request: PortfolioReviewJobRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
    ) -> ReportJobLedgerRecord:
        return self._create_report_job(
            report_type="portfolio_review",
            accepted_message="Portfolio review report job accepted.",
            request=request,
            caller_context=caller_context,
            idempotency_key=idempotency_key,
            enqueue=True,
        )

    def create_outcome_review_report_job(
        self,
        *,
        request: OutcomeReviewReportJobRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
    ) -> ReportJobLedgerRecord:
        return self._create_report_job(
            report_type="outcome_review",
            accepted_message="Outcome review report job accepted.",
            request=request,
            caller_context=caller_context,
            idempotency_key=idempotency_key,
        )

    def create_proof_pack_report_job(
        self,
        *,
        request: ProofPackReportJobRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
    ) -> ReportJobLedgerRecord:
        return self._create_report_job(
            report_type="proof_pack",
            accepted_message="Proof-pack report job accepted.",
            request=request,
            caller_context=caller_context,
            idempotency_key=idempotency_key,
        )

    def create_wave_report_job(
        self,
        *,
        request: WaveReportJobRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
    ) -> ReportJobLedgerRecord:
        return self._create_report_job(
            report_type="rebalance_wave",
            accepted_message="Rebalance wave report job accepted.",
            request=request,
            caller_context=caller_context,
            idempotency_key=idempotency_key,
        )

    def _create_report_job(
        self,
        *,
        report_type: str,
        accepted_message: str,
        request: PortfolioReviewJobRequest
        | OutcomeReviewReportJobRequest
        | ProofPackReportJobRequest
        | WaveReportJobRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
        enqueue: bool = False,
        replay_source_job_id: str | None = None,
        replay_reason: str = "Replay of failed report work.",
        inherited_template: tuple[str | None, str | None] | None = None,
        inherited_contract: dict[str, Any] | None = None,
    ) -> ReportJobLedgerRecord:
        if not idempotency_key or not idempotency_key.strip():
            raise MissingIdempotencyKeyError("missing_idempotency_key")

        portfolio_scope, as_of_date, output_formats, reporting_currency, options = _request_parts(
            report_type=report_type,
            request=request,
        )
        # Template selection is an immutable job fact, stamped at acceptance
        # for PDF-capable jobs - identical to the SQLite ledger's discipline.
        render_template_id, render_template_version = job_template_identity(
            report_type, output_formats, inherited_template
        )
        job_accepted_contract = resolve_job_accepted_contract(
            report_type=report_type,
            output_formats=output_formats,
            inherited_template=inherited_template,
            inherited_contract=inherited_contract,
        )
        normalized_key = idempotency_key.strip()
        request_hash = compute_request_hash(
            report_type=report_type,
            request=request,
            caller_context=caller_context,
        )

        try:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT report_request_id, request_hash
                    FROM report_request
                    WHERE tenant_id = %s AND idempotency_key = %s
                    """,
                    (caller_context.tenant_id, normalized_key),
                ).fetchone()
                if existing:
                    record = self._existing_or_conflict(connection, existing, request_hash)
                    if enqueue:
                        self._ensure_work_item(connection, record=record)
                    return record

                replay_source_status: str | None = None
                replay_source_failure_category: str | None = None
                if replay_source_job_id is not None:
                    root_row = connection.execute(
                        """
                        WITH RECURSIVE ancestors(job_id) AS (
                            SELECT %s
                            UNION
                            SELECT rel.source_report_job_id
                            FROM report_job_relationship rel
                            JOIN ancestors ON rel.derived_report_job_id = ancestors.job_id
                            WHERE rel.relationship_type = 'failed_work_replay'
                        )
                        SELECT a.job_id
                        FROM ancestors a
                        WHERE NOT EXISTS (
                            SELECT 1 FROM report_job_relationship rel
                            WHERE rel.derived_report_job_id = a.job_id
                              AND rel.relationship_type = 'failed_work_replay'
                        )
                        LIMIT 1
                        """,
                        (replay_source_job_id,),
                    ).fetchone()
                    lineage_root_id = root_row["job_id"] if root_row else replay_source_job_id
                    if lineage_root_id != replay_source_job_id:
                        # Serialize every creator in this lineage on the ROOT
                        # row (root first, then source - one consistent lock
                        # order), so replacements on different branches cannot
                        # race each other into duplicate documents.
                        connection.execute(
                            "SELECT 1 FROM report_job WHERE report_job_id = %s FOR UPDATE",
                            (lineage_root_id,),
                        ).fetchone()
                    # Then the source row itself: the second novel-key
                    # transaction blocks here until the first commits, then
                    # sees its relationship below.
                    source_row = connection.execute(
                        "SELECT status, failure_category, retry_eligible, "
                        "archive_document_id FROM report_job "
                        "WHERE report_job_id = %s FOR UPDATE",
                        (replay_source_job_id,),
                    ).fetchone()
                    if not source_row:
                        raise ReportJobNotFoundError("report_job_not_found")
                    if (
                        source_row["status"] != "failed"
                        or not source_row["retry_eligible"]
                        or source_row["archive_document_id"]
                    ):
                        # The service validated eligibility BEFORE this lock; a
                        # concurrent resolver may have adopted a committed
                        # document meanwhile (failed -> archived). Stale
                        # observers must not create a replacement for a source
                        # that no longer needs one.
                        raise InvalidReportJobTransitionError("report_job_cannot_be_replayed")
                    # The initial idempotency lookup ran before this lock; a
                    # concurrent SAME-key request may have committed while we
                    # waited. Re-check so same-key retries converge on that
                    # job instead of tripping the live-relationship refusal.
                    existing = connection.execute(
                        """
                        SELECT report_request_id, request_hash
                        FROM report_request
                        WHERE tenant_id = %s AND idempotency_key = %s
                        """,
                        (caller_context.tenant_id, normalized_key),
                    ).fetchone()
                    if existing:
                        record = self._existing_or_conflict(connection, existing, request_hash)
                        if enqueue:
                            self._ensure_work_item(connection, record=record)
                        return record
                    replay_source_status = source_row["status"]
                    replay_source_failure_category = source_row["failure_category"]
                    live = connection.execute(
                        """
                        WITH RECURSIVE lineage(job_id) AS (
                            SELECT %s
                            UNION
                            SELECT rel.derived_report_job_id
                            FROM report_job_relationship rel
                            JOIN lineage ON rel.source_report_job_id = lineage.job_id
                            WHERE rel.relationship_type = 'failed_work_replay'
                        )
                        SELECT 1
                        FROM lineage
                        JOIN report_job member ON member.report_job_id = lineage.job_id
                        WHERE lineage.job_id != %s
                          AND NOT (
                              member.status = 'cancelled'
                              OR (
                                  member.status = 'failed'
                                  AND (
                                      member.failure_category NOT IN (
                                          'archive_storage_failed',
                                          'archive_execution_failed',
                                          'archive_outcome_unknown'
                                      )
                                      OR EXISTS (
                                          SELECT 1 FROM report_job_relationship child
                                          WHERE child.source_report_job_id
                                                = member.report_job_id
                                            AND child.relationship_type
                                                = 'failed_work_replay'
                                      )
                                  )
                              )
                          )
                        LIMIT 1
                        """,
                        (lineage_root_id, replay_source_job_id),
                    ).fetchone()
                    if live:
                        # A replacement already exists (in flight or archived).
                        # Only its own idempotency key may converge on it - a
                        # novel key must not mint a second client document.
                        raise InvalidReportJobTransitionError("report_job_cannot_be_replayed")

                now = utc_now()
                request_id = f"rrq_{uuid4().hex}"
                job_id = f"rjob_{uuid4().hex}"

                connection.execute(
                    """
                    INSERT INTO report_request (
                        report_request_id, report_type, portfolio_scope_json,
                        requested_output_formats_json, as_of_date, reporting_currency,
                        options_json, trigger_type, triggered_by, caller_application,
                        tenant_id, region, booking_center_code, role, idempotency_key,
                        request_hash, correlation_id, trace_id, created_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        request_id,
                        report_type,
                        Jsonb(portfolio_scope),
                        Jsonb(sorted(output_formats)),
                        as_of_date,
                        reporting_currency,
                        Jsonb(options),
                        caller_context.trigger_type,
                        caller_context.triggered_by,
                        caller_context.caller_application,
                        caller_context.tenant_id,
                        caller_context.region,
                        caller_context.booking_center_code,
                        caller_context.role,
                        normalized_key,
                        request_hash,
                        caller_context.correlation_id,
                        caller_context.trace_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO report_job (
                        report_job_id, report_request_id, report_type, portfolio_scope_json,
                        status, failure_category, failure_message, current_step, retry_eligible,
                        cancel_requested, created_at, updated_at, started_at, completed_at,
                        cancelled_at, render_job_id, render_output_format, render_template_id,
                        render_template_version, render_artifact_sha256,
                        render_bounded_determinism_fingerprint, render_runtime_engine,
                        render_runtime_engine_version, render_duration_ms,
                        archive_request_id, archive_document_id, archive_completed_at,
                        accepted_document_contract_json
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        job_id,
                        request_id,
                        report_type,
                        Jsonb(portfolio_scope),
                        "accepted",
                        None,
                        None,
                        "accepted",
                        False,
                        False,
                        now,
                        now,
                        None,
                        None,
                        None,
                        None,
                        None,
                        render_template_id,
                        render_template_version,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        Jsonb(job_accepted_contract),
                    ),
                )
                self._append_status_event(
                    connection=connection,
                    job_id=job_id,
                    from_status=None,
                    to_status="accepted",
                    event_type="job_accepted",
                    message=accepted_message,
                    event_payload={"report_type": report_type},
                    event_idempotency_key=normalized_key,
                    actor=caller_context.triggered_by,
                    correlation_id=caller_context.correlation_id,
                    trace_id=caller_context.trace_id,
                    created_at=now,
                )
                record = self._load_by_request_id(connection, request_id)
                if replay_source_job_id is not None:
                    connection.execute(
                        """
                        INSERT INTO report_job_relationship (
                            relationship_id, source_report_job_id, derived_report_job_id,
                            relationship_type, source_status, derived_status,
                            source_failure_category, derived_failure_category,
                            archive_consequence, previous_archive_document_id,
                            new_archive_document_id, actor, reason, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, 'failed_work_replay', %s, %s, %s, NULL,
                                NULL, NULL, NULL, %s, %s, %s, %s)
                        ON CONFLICT (source_report_job_id, derived_report_job_id, relationship_type)
                        DO NOTHING
                        """,
                        (
                            f"rjr_{uuid4().hex}",
                            replay_source_job_id,
                            record.job_id,
                            replay_source_status,
                            record.status,
                            replay_source_failure_category,
                            caller_context.triggered_by,
                            replay_reason,
                            now,
                            now,
                        ),
                    )
                if enqueue:
                    self._ensure_work_item(connection, record=record)
                return record
        except UniqueViolation as exc:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT report_request_id, request_hash
                    FROM report_request
                    WHERE tenant_id = %s AND idempotency_key = %s
                    """,
                    (caller_context.tenant_id, normalized_key),
                ).fetchone()
                if existing:
                    record = self._existing_or_conflict(connection, existing, request_hash)
                    if enqueue:
                        self._ensure_work_item(connection, record=record)
                    return record
            raise IdempotencyConflictError("idempotency_key_unique_violation") from exc

    def _ensure_work_item(
        self,
        connection: Connection[Mapping[str, Any]],
        *,
        record: ReportJobLedgerRecord,
    ) -> None:
        if record.status in {"archived", "completed_with_warnings", "failed", "cancelled"}:
            return
        now = utc_now()
        connection.execute(
            """
            INSERT INTO report_job_work_item (
                work_item_id, report_job_id, status, attempt_count, available_at,
                lease_owner, lease_token, lease_acquired_at, lease_expires_at,
                last_error_category, last_error_summary, created_at, updated_at, completed_at
            )
            VALUES (%s, %s, 'pending', 0, %s, NULL, NULL, NULL, NULL, NULL, NULL, %s, %s, NULL)
            ON CONFLICT (report_job_id) DO NOTHING
            """,
            (f"rwork_{uuid4().hex}", record.job_id, now, now, now),
        )

    def get_work_item_for_job(self, job_id: str) -> ReportJobWorkItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM report_job_work_item WHERE report_job_id = %s",
                (job_id,),
            ).fetchone()
        return _work_item_from_row(row) if row else None

    def claim_work_items(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        retry_policy: ReportJobWorkRetryPolicy | None = None,
        now: datetime | None = None,
    ) -> list[ReportJobWorkItem]:
        if limit < 1:
            return []
        claimed_at = now or utc_now()
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            recovered_count, exhausted_count = self._recover_expired_work_items(
                connection=connection,
                recovered_at=claimed_at,
                retry_policy=retry_policy or ReportJobWorkRetryPolicy(),
            )
            rows = connection.execute(
                """
                SELECT work_item_id
                FROM report_job_work_item
                WHERE status IN ('pending', 'retry_pending') AND available_at <= %s
                ORDER BY available_at ASC, created_at ASC, work_item_id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (claimed_at, limit),
            ).fetchall()
            claimed: list[ReportJobWorkItem] = []
            for row in rows:
                lease_token = f"rlease_{uuid4().hex}"
                claimed_row = connection.execute(
                    """
                    UPDATE report_job_work_item
                    SET status = 'leased', attempt_count = attempt_count + 1,
                        lease_owner = %s, lease_token = %s, lease_acquired_at = %s,
                        lease_expires_at = %s, updated_at = %s
                    WHERE work_item_id = %s
                      AND status IN ('pending', 'retry_pending')
                    RETURNING *
                    """,
                    (
                        worker_id,
                        lease_token,
                        claimed_at,
                        lease_expires_at,
                        claimed_at,
                        row["work_item_id"],
                    ),
                ).fetchone()
                if claimed_row:
                    claimed.append(_work_item_from_row(claimed_row))
        record_report_job_work_lease_event(outcome="recovered", count=recovered_count)
        record_report_job_work_lease_event(outcome="exhausted", count=exhausted_count)
        return claimed

    def complete_work_item(
        self,
        *,
        work_item_id: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> ReportJobWorkItem:
        completed_at = now or utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE report_job_work_item
                SET status = 'completed', lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    updated_at = %s, completed_at = %s
                WHERE work_item_id = %s AND status = 'leased' AND lease_token = %s
                RETURNING *
                """,
                (completed_at, completed_at, work_item_id, lease_token),
            ).fetchone()
            if not row:
                record_report_job_work_lease_event(outcome="stale_conflict")
                raise InvalidReportJobWorkTransitionError("report_job_work_lease_not_owned")
            return _work_item_from_row(row)

    def defer_work_item(
        self,
        *,
        work_item_id: str,
        lease_token: str,
        wait_reason: str,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> ReportJobWorkItem:
        """Reschedule a lease WITHOUT burning the failure budget.

        Identical semantics to the SQLite ledger: one fenced UPDATE does
        ownership check, attempt refund, and release atomically, so a lease
        reclaimed between any check and write simply fails the fence.
        """

        deferred_at = now or utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE report_job_work_item
                SET status = 'retry_pending', lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    attempt_count = GREATEST(attempt_count - 1, 0),
                    available_at = %s, last_error_category = %s, last_error_summary = %s,
                    updated_at = %s
                WHERE work_item_id = %s AND status = 'leased' AND lease_token = %s
                RETURNING *
                """,
                (
                    deferred_at + timedelta(seconds=max(delay_seconds, 1)),
                    wait_reason[:80],
                    "Waiting on owner-side work; the failure budget is untouched.",
                    deferred_at,
                    work_item_id,
                    lease_token,
                ),
            ).fetchone()
            if not row:
                record_report_job_work_lease_event(outcome="stale_conflict")
                raise InvalidReportJobWorkTransitionError("report_job_work_lease_not_owned")
            return _work_item_from_row(row)

    def fail_work_item(
        self,
        *,
        work_item_id: str,
        lease_token: str,
        error_category: str,
        error_summary: str,
        retry_policy: ReportJobWorkRetryPolicy | None = None,
        now: datetime | None = None,
    ) -> ReportJobWorkItem:
        policy = retry_policy or ReportJobWorkRetryPolicy()
        failed_at = now or utc_now()
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT attempt_count, lease_owner FROM report_job_work_item
                WHERE work_item_id = %s AND status = 'leased' AND lease_token = %s
                FOR UPDATE
                """,
                (work_item_id, lease_token),
            ).fetchone()
            if not current:
                record_report_job_work_lease_event(outcome="stale_conflict")
                raise InvalidReportJobWorkTransitionError("report_job_work_lease_not_owned")
            attempt_count = int(current["attempt_count"])
            decision = decide_report_job_work_failure(
                attempt_count=attempt_count,
                failed_at=failed_at,
                retry_policy=policy,
            )
            row = connection.execute(
                """
                UPDATE report_job_work_item
                SET status = %s, lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    available_at = %s, last_error_category = %s, last_error_summary = %s,
                    updated_at = %s
                WHERE work_item_id = %s AND status = 'leased' AND lease_token = %s
                RETURNING *
                """,
                (
                    decision.status,
                    decision.available_at,
                    error_category[:80],
                    " ".join(error_summary.split())[:240],
                    failed_at,
                    work_item_id,
                    lease_token,
                ),
            ).fetchone()
            if not row:
                # The fenced UPDATE matched nothing: the lease expired and was
                # reclaimed between the ownership SELECT and this write.
                record_report_job_work_lease_event(outcome="stale_conflict")
                raise InvalidReportJobWorkTransitionError("report_job_work_lease_not_owned")
            if decision.status == "failed":
                self._terminalize_report_job_from_work(
                    connection=connection,
                    work_item_id=work_item_id,
                    actor=str(current["lease_owner"] or "report-job-worker"),
                    failure_category="operator_intervention_required",
                    failure_summary=error_summary,
                )
            return _work_item_from_row(row)

    def _recover_expired_work_items(
        self,
        *,
        connection: Connection[Mapping[str, Any]],
        recovered_at: datetime,
        retry_policy: ReportJobWorkRetryPolicy,
    ) -> tuple[int, int]:
        recovered_count = 0
        exhausted_count = 0
        expired_rows = connection.execute(
            """
            SELECT work_item_id, attempt_count, lease_owner, lease_expires_at
            FROM report_job_work_item
            WHERE status = 'leased' AND lease_expires_at < %s
            ORDER BY lease_expires_at ASC, work_item_id ASC
            FOR UPDATE SKIP LOCKED
            """,
            (recovered_at,),
        ).fetchall()
        for expired in expired_rows:
            decision = decide_report_job_work_failure(
                attempt_count=int(expired["attempt_count"]),
                failed_at=expired["lease_expires_at"],
                retry_policy=retry_policy,
            )
            connection.execute(
                """
                UPDATE report_job_work_item
                SET status = %s, lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    available_at = %s, last_error_category = 'expired_work_lease',
                    last_error_summary = 'Report job work lease expired before completion.',
                    updated_at = %s
                WHERE work_item_id = %s AND status = 'leased'
                """,
                (
                    decision.status,
                    decision.available_at,
                    recovered_at,
                    expired["work_item_id"],
                ),
            )
            if decision.status == "failed":
                exhausted_count += 1
                self._terminalize_report_job_from_work(
                    connection=connection,
                    work_item_id=str(expired["work_item_id"]),
                    actor=str(expired["lease_owner"] or "report-job-worker"),
                    failure_category="timeout",
                    failure_summary=(
                        "Report job work lease expired after the final permitted attempt."
                    ),
                )
            else:
                recovered_count += 1
        return recovered_count, exhausted_count

    def _terminalize_report_job_from_work(
        self,
        *,
        connection: Connection[Mapping[str, Any]],
        work_item_id: str,
        actor: str,
        failure_category: str,
        failure_summary: str,
    ) -> None:
        job_context = connection.execute(
            """
            SELECT work.report_job_id, job.status, request.correlation_id, request.trace_id
            FROM report_job_work_item AS work
            JOIN report_job AS job ON job.report_job_id = work.report_job_id
            JOIN report_request AS request
              ON request.report_request_id = job.report_request_id
            WHERE work.work_item_id = %s
            """,
            (work_item_id,),
        ).fetchone()
        if not job_context or str(job_context["status"]) in {
            "archived",
            "completed_with_warnings",
            "failed",
            "cancelled",
        }:
            return
        bounded_summary = " ".join(failure_summary.split())[:240]
        self._transition_job(
            connection=connection,
            job_id=str(job_context["report_job_id"]),
            to_status="failed",
            failure_category=failure_category,
            failure_message=(
                f"Durable report processing exhausted its permitted attempts. {bounded_summary}"
            )[:500],
            current_step="failed",
            retry_eligible=True,
            actor=actor,
            correlation_id=str(job_context["correlation_id"]),
            trace_id=str(job_context["trace_id"]),
            event_type="job_failed",
            event_message=bounded_summary,
            set_started_at=True,
            set_completed_at=True,
            render_job_id=None,
            render_output_format=None,
            render_template_id=None,
            render_template_version=None,
            render_artifact_sha256=None,
            render_bounded_determinism_fingerprint=None,
            render_runtime_engine=None,
            render_runtime_engine_version=None,
            render_duration_ms=None,
            archive_request_id=None,
            archive_document_id=None,
            archive_completed_at=None,
        )

    def get_job(self, job_id: str) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_request_id FROM report_job WHERE report_job_id = %s",
                (job_id,),
            ).fetchone()
            if not row:
                raise ReportJobNotFoundError("report_job_not_found")
            return self._load_by_request_id(connection, str(row["report_request_id"]))

    def get_archive_statuses_by_job_ids(
        self,
        job_ids: list[str],
        *,
        tenant_id: str,
    ) -> list[ReportJobArchiveStatusRecord]:
        """Project archive status for one tenant only.

        tenant_id is required rather than optional so a caller cannot accidentally read
        another tenant's job by omitting it. A job outside the tenant is not returned, so
        neither its lifecycle status nor its archive_document_id can reach the projection.
        """

        unique_job_ids = sorted({job_id for job_id in job_ids if job_id})
        if not unique_job_ids:
            return []

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    report_job.report_job_id,
                    report_job.status,
                    report_job.archive_document_id
                FROM report_job
                JOIN report_request
                  ON report_request.report_request_id = report_job.report_request_id
                WHERE report_request.tenant_id = %s
                  AND report_job.report_job_id = ANY(%s)
                ORDER BY report_job.report_job_id ASC
                """,
                (tenant_id, unique_job_ids),
            ).fetchall()
        return [
            ReportJobArchiveStatusRecord(
                report_job_id=str(row["report_job_id"]),
                status=row["status"],
                archive_document_id=row.get("archive_document_id"),
            )
            for row in rows
        ]

    def list_pending_archive_lineage(self, *, limit: int) -> list[PendingArchiveLineage]:
        """Jobs with lineage pairs still pending, oldest first, bounded.

        Pair-level: a pending event with no recorded or refused outcome for
        the same pair (matched by the idempotency-key pair suffix), so
        settled history never re-enters the reconciliation pass.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.report_job_id AS job_id,
                       MIN(p.created_at) AS oldest_created_at
                FROM report_status_event p
                WHERE p.event_type = 'job_archive_lineage_pending'
                  AND p.event_idempotency_key IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM report_status_event r
                      WHERE r.report_job_id = p.report_job_id
                        AND r.event_type IN (
                            'job_archive_lineage_recorded',
                            'job_archive_lineage_refused'
                        )
                        AND substr(r.event_idempotency_key, length(r.event_type) + 2)
                            = substr(p.event_idempotency_key, length(p.event_type) + 2)
                  )
                GROUP BY p.report_job_id
                ORDER BY oldest_created_at
                LIMIT %s
                """,
                (max(0, limit),),
            ).fetchall()
        return [
            PendingArchiveLineage(
                job_id=str(row["job_id"]),
                oldest_created_at=row["oldest_created_at"],
            )
            for row in rows
        ]

    def list_status_events(self, job_id: str) -> list[ReportStatusEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM report_status_event
                WHERE report_job_id = %s
                ORDER BY created_at ASC, status_event_id ASC
                """,
                (job_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def upsert_job_relationship(
        self,
        *,
        source_job: ReportJobLedgerRecord,
        derived_job: ReportJobLedgerRecord,
        relationship_type: ReportJobRelationshipType,
        actor: str,
        reason: str,
        archive_consequence: str | None = None,
        previous_archive_document_id: str | None = None,
        new_archive_document_id: str | None = None,
    ) -> ReportJobRelationshipRecord:
        bounded_reason = _bounded_relationship_reason(reason)
        now = utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT relationship_id, created_at
                FROM report_job_relationship
                WHERE source_report_job_id = %s
                  AND derived_report_job_id = %s
                  AND relationship_type = %s
                """,
                (source_job.job_id, derived_job.job_id, relationship_type),
            ).fetchone()
            relationship_id = str(existing["relationship_id"]) if existing else f"rjr_{uuid4().hex}"
            created_at = _dt_from_value(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO report_job_relationship (
                    relationship_id, source_report_job_id, derived_report_job_id,
                    relationship_type, source_status, derived_status,
                    source_failure_category, derived_failure_category,
                    archive_consequence, previous_archive_document_id,
                    new_archive_document_id, actor, reason, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT(source_report_job_id, derived_report_job_id, relationship_type)
                DO UPDATE SET
                    source_status = EXCLUDED.source_status,
                    derived_status = EXCLUDED.derived_status,
                    source_failure_category = EXCLUDED.source_failure_category,
                    derived_failure_category = EXCLUDED.derived_failure_category,
                    archive_consequence = EXCLUDED.archive_consequence,
                    previous_archive_document_id = EXCLUDED.previous_archive_document_id,
                    new_archive_document_id = EXCLUDED.new_archive_document_id,
                    actor = EXCLUDED.actor,
                    reason = EXCLUDED.reason,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    relationship_id,
                    source_job.job_id,
                    derived_job.job_id,
                    relationship_type,
                    source_job.status,
                    derived_job.status,
                    source_job.failure_category,
                    derived_job.failure_category,
                    archive_consequence,
                    previous_archive_document_id,
                    new_archive_document_id,
                    actor,
                    bounded_reason,
                    created_at or now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM report_job_relationship WHERE relationship_id = %s",
                (relationship_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("report_job_relationship_write_failed")
            return _relationship_from_row(row)

    def list_job_relationships(self, job_id: str) -> list[ReportJobRelationshipRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM report_job_relationship
                WHERE source_report_job_id = %s OR derived_report_job_id = %s
                ORDER BY created_at ASC, relationship_id ASC
                """,
                (job_id, job_id),
            ).fetchall()
        return [_relationship_from_row(row) for row in rows]

    def list_rerender_attempts(
        self,
        job_id: str,
        *,
        limit: int = 25,
    ) -> list[ReportRerenderAttemptRecord]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM report_rerender_attempt
                WHERE report_job_id = %s
                ORDER BY updated_at DESC, created_at DESC, rerender_attempt_id DESC
                LIMIT %s
                """,
                (job_id, bounded_limit),
            ).fetchall()
        return [_rerender_attempt_from_row(row) for row in rows]

    def append_job_event(
        self,
        *,
        job_id: str,
        event_type: str,
        message: str,
        event_payload: dict[str, Any] | None = None,
        event_idempotency_key: str | None = None,
        actor: str,
        correlation_id: str,
        trace_id: str,
        skip_if_idempotency_key_exists: bool = False,
    ) -> bool:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT status FROM report_job WHERE report_job_id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if not existing:
                raise ReportJobNotFoundError("report_job_not_found")
            if (
                skip_if_idempotency_key_exists
                and event_idempotency_key
                and connection.execute(
                    "SELECT 1 FROM report_status_event "
                    "WHERE report_job_id = %s AND event_idempotency_key = %s",
                    (job_id, event_idempotency_key),
                ).fetchone()
            ):
                # Serialized behind the FOR UPDATE row lock: concurrent
                # same-key retries converge on one event.
                return False
            current_status: ReportJobStatus = existing["status"]
            self._append_status_event(
                connection=connection,
                job_id=job_id,
                from_status=current_status,
                to_status=current_status,
                event_type=event_type,
                message=message,
                event_payload=event_payload,
                event_idempotency_key=event_idempotency_key,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                created_at=utc_now(),
            )
            return True

    def list_unresolved_archive_ambiguous_attempts(
        self, job_id: str
    ) -> list[ReportRerenderAttemptRecord]:
        """Every attempt whose archive outcome is still ambiguous - no limit,
        because an unresolved commit outside any page is exactly the row a
        duplicate correction would slip past."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM report_rerender_attempt
                WHERE report_job_id = %s
                  AND status = 'failed'
                  AND failure_category IN (
                      'archive_storage_failed', 'archive_execution_failed',
                      'archive_outcome_unknown', 'archive_handoff_failed'
                  )
                  AND retry_eligible = TRUE
                ORDER BY updated_at DESC, created_at DESC, rerender_attempt_id DESC
                """,
                (job_id,),
            ).fetchall()
        return [_rerender_attempt_from_row(row) for row in rows]

    def record_adopted_rerender_outcome(
        self,
        *,
        job: ReportJobLedgerRecord,
        idempotency_key: str,
        actor: str,
        reason: str,
        correlation_id: str,
        trace_id: str,
        adopted_attempt: ReportRerenderAttemptRecord,
        archive_document_id: str,
    ) -> ReportRerenderAttemptRecord:
        """Bind an archive-adoption outcome durably to the INCOMING request
        key (see the sqlite ledger for the rationale)."""

        if not idempotency_key or not idempotency_key.strip():
            raise MissingIdempotencyKeyError("missing_idempotency_key")
        normalized_key = idempotency_key.strip()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM report_rerender_attempt
                WHERE report_job_id = %s AND idempotency_key = %s
                """,
                (job.job_id, normalized_key),
            ).fetchone()
            if existing:
                return _rerender_attempt_from_row(existing)
            now = utc_now()
            attempt_id = f"rrnd_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO report_rerender_attempt (
                    rerender_attempt_id, report_job_id, idempotency_key, status,
                    snapshot_id, snapshot_hash, previous_render_job_id,
                    previous_archive_document_id, render_job_id, render_output_format,
                    render_template_id, render_template_version,
                    archive_request_id, archive_document_id, archive_completed_at,
                    retry_eligible, requested_by, reason, correlation_id, trace_id,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, 'archived', %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, FALSE, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    job.job_id,
                    normalized_key,
                    adopted_attempt.snapshot_id,
                    adopted_attempt.snapshot_hash,
                    adopted_attempt.previous_render_job_id,
                    adopted_attempt.previous_archive_document_id,
                    adopted_attempt.render_job_id,
                    "pdf",
                    "portfolio-review",
                    "v1",
                    f"arch_{adopted_attempt.render_job_id}",
                    archive_document_id,
                    now,
                    actor,
                    reason,
                    correlation_id,
                    trace_id,
                    now,
                    now,
                ),
            )
            self._append_status_event(
                connection=connection,
                job_id=job.job_id,
                from_status=job.status,
                to_status=job.status,
                event_type="job_rerender_archived",
                message=(
                    f"Rerender adopted committed correction {archive_document_id} "
                    f"from attempt {adopted_attempt.rerender_attempt_id}."
                ),
                event_payload={
                    "archive_document_id": archive_document_id,
                    "rerender_attempt_id": attempt_id,
                    "adopted_from_attempt_id": adopted_attempt.rerender_attempt_id,
                },
                event_idempotency_key=normalized_key,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = %s",
                (attempt_id,),
            ).fetchone()
            assert row is not None
            return _rerender_attempt_from_row(row)

    def create_rerender_attempt(
        self,
        *,
        job: ReportJobLedgerRecord,
        snapshot_id: str,
        snapshot_hash: str,
        idempotency_key: str,
        actor: str,
        reason: str,
        correlation_id: str,
        trace_id: str,
    ) -> tuple[ReportRerenderAttemptRecord, bool]:
        if not idempotency_key or not idempotency_key.strip():
            raise MissingIdempotencyKeyError("missing_idempotency_key")
        normalized_key = idempotency_key.strip()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT *
                FROM report_rerender_attempt
                WHERE report_job_id = %s AND idempotency_key = %s
                """,
                (job.job_id, normalized_key),
            ).fetchone()
            if existing:
                return _rerender_attempt_from_row(existing), False
            now = utc_now()
            attempt_id = f"rrnd_{uuid4().hex}"
            render_job_id = f"rdr_{attempt_id}_pdf"
            connection.execute(
                """
                INSERT INTO report_rerender_attempt (
                    rerender_attempt_id, report_job_id, idempotency_key, status,
                    snapshot_id, snapshot_hash, previous_render_job_id,
                    previous_archive_document_id, render_job_id, render_output_format,
                    render_template_id, render_template_version, retry_eligible,
                    requested_by, reason, correlation_id, trace_id, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    attempt_id,
                    job.job_id,
                    normalized_key,
                    "rendering",
                    snapshot_id,
                    snapshot_hash,
                    job.render_job_id,
                    job.archive_document_id,
                    render_job_id,
                    "pdf",
                    "portfolio-review",
                    "v1",
                    False,
                    actor,
                    reason,
                    correlation_id,
                    trace_id,
                    now,
                    now,
                ),
            )
            self._append_status_event(
                connection=connection,
                job_id=job.job_id,
                from_status=job.status,
                to_status=job.status,
                event_type="job_rerender_requested",
                message=f"Report rerender requested from snapshot {snapshot_id}.",
                event_payload={
                    "snapshot_id": snapshot_id,
                    "snapshot_hash": snapshot_hash,
                    "rerender_attempt_id": attempt_id,
                },
                event_idempotency_key=normalized_key,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = %s",
                (attempt_id,),
            ).fetchone()
            assert row is not None
            return _rerender_attempt_from_row(row), True

    def mark_rerender_rendered(
        self,
        *,
        rerender_attempt_id: str,
        render_job_id: str,
        artifact_sha256: str | None,
        bounded_determinism_fingerprint: str | None,
        runtime_engine: str | None,
        runtime_engine_version: str | None,
        render_duration_ms: int | None,
    ) -> ReportRerenderAttemptRecord:
        with self._connect() as connection:
            return self._update_rerender_attempt(
                connection=connection,
                rerender_attempt_id=rerender_attempt_id,
                status="rendered",
                render_job_id=render_job_id,
                artifact_sha256=artifact_sha256,
                bounded_determinism_fingerprint=bounded_determinism_fingerprint,
                runtime_engine=runtime_engine,
                runtime_engine_version=runtime_engine_version,
                render_duration_ms=render_duration_ms,
            )

    def mark_rerender_archiving(
        self,
        *,
        rerender_attempt_id: str,
        archive_request_id: str,
    ) -> ReportRerenderAttemptRecord:
        with self._connect() as connection:
            return self._update_rerender_attempt(
                connection=connection,
                rerender_attempt_id=rerender_attempt_id,
                status="archiving",
                archive_request_id=archive_request_id,
            )

    def mark_rerender_archived(
        self,
        *,
        rerender_attempt_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
        archive_document_id: str,
    ) -> ReportRerenderAttemptRecord:
        with self._connect() as connection:
            archived = self._update_rerender_attempt(
                connection=connection,
                rerender_attempt_id=rerender_attempt_id,
                status="archived",
                archive_document_id=archive_document_id,
                archive_completed_at=utc_now(),
                clear_failure=True,
            )
            self._append_status_event(
                connection=connection,
                job_id=archived.report_job_id,
                from_status="archived",
                to_status="archived",
                event_type="job_rerender_archived",
                message=f"Report rerender archived as correction document {archive_document_id}.",
                event_payload={
                    "rerender_attempt_id": rerender_attempt_id,
                    "render_job_id": archived.render_job_id,
                    "archive_document_id": archive_document_id,
                },
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                created_at=utc_now(),
            )
            return archived

    def mark_rerender_failed(
        self,
        *,
        rerender_attempt_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
        failure_category: str,
        failure_message: str,
        retry_eligible: bool,
    ) -> ReportRerenderAttemptRecord:
        with self._connect() as connection:
            failed = self._update_rerender_attempt(
                connection=connection,
                rerender_attempt_id=rerender_attempt_id,
                status="failed",
                failure_category=failure_category,
                failure_message=failure_message,
                retry_eligible=retry_eligible,
            )
            self._append_status_event(
                connection=connection,
                job_id=failed.report_job_id,
                from_status="archived",
                to_status="archived",
                event_type="job_rerender_failed",
                message=failure_message,
                event_payload={
                    "rerender_attempt_id": rerender_attempt_id,
                    "failure_message": failure_message,
                },
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                created_at=utc_now(),
            )
            return failed

    def list_jobs(self, *, filters: ReportJobListFilters) -> list[ReportJobLedgerRecord]:
        return [_record_from_row(row) for row in self._list_job_rows(filters)]

    def list_job_owner_snapshots(
        self, *, filters: ReportJobListFilters
    ) -> list[ReportJobOwnerSnapshot]:
        return [
            ReportJobOwnerSnapshot(
                record=_record_from_row(row),
                source_event_version=int(row["source_event_version"]),
            )
            for row in self._list_job_rows(filters)
        ]

    def _list_job_rows(self, filters: ReportJobListFilters) -> list[Mapping[str, Any]]:
        where_clauses = ["1=1"]
        params: list[Any] = []

        if filters.tenant_id:
            where_clauses.append("req.tenant_id = %s")
            params.append(filters.tenant_id)
        if filters.region:
            where_clauses.append("req.region = %s")
            params.append(filters.region)
        if filters.status:
            where_clauses.append("job.status = %s")
            params.append(filters.status)
        if filters.report_type:
            where_clauses.append("req.report_type = %s")
            params.append(filters.report_type)
        if filters.portfolio_id:
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(
                        req.portfolio_scope_json -> 'portfolio_ids'
                    ) AS pid(value)
                    WHERE pid.value = %s
                )
                """
            )
            params.append(filters.portfolio_id)
        if filters.as_of_date:
            where_clauses.append("req.as_of_date = %s")
            params.append(filters.as_of_date)
        if filters.idempotency_key:
            where_clauses.append("req.idempotency_key = %s")
            params.append(filters.idempotency_key)
        if filters.correlation_id:
            where_clauses.append("req.correlation_id = %s")
            params.append(filters.correlation_id)
        if filters.created_from:
            where_clauses.append("job.created_at >= %s")
            params.append(filters.created_from)
        if filters.created_to:
            where_clauses.append("job.created_at <= %s")
            params.append(filters.created_to)

        query = f"""
            SELECT
                req.report_request_id,
                req.report_type,
                req.portfolio_scope_json AS request_portfolio_scope_json,
                req.requested_output_formats_json,
                req.as_of_date,
                req.reporting_currency,
                req.options_json,
                req.trigger_type,
                req.triggered_by,
                req.caller_application,
                req.tenant_id,
                req.region,
                req.booking_center_code,
                req.role,
                req.idempotency_key,
                req.request_hash,
                req.correlation_id,
                req.trace_id,
                req.created_at AS request_created_at,
                job.report_job_id,
                job.portfolio_scope_json AS job_portfolio_scope_json,
                job.status,
                job.failure_category,
                job.failure_message,
                job.current_step,
                job.retry_eligible,
                job.cancel_requested,
                job.created_at AS job_created_at,
                job.updated_at,
                job.started_at,
                job.completed_at,
                job.cancelled_at,
                job.render_job_id,
                job.render_output_format,
                job.render_template_id,
                job.render_template_version,
                job.render_template_publication,
                job.render_artifact_sha256,
                job.render_bounded_determinism_fingerprint,
                job.render_runtime_engine,
                job.render_runtime_engine_version,
                job.render_duration_ms,
                job.archive_request_id,
                job.archive_document_id,
                job.archive_completed_at,
                job.accepted_document_contract_json,
                (
                    SELECT COUNT(*)
                    FROM report_status_event event
                    WHERE event.report_job_id = job.report_job_id
                ) AS source_event_version
            FROM report_request req
            JOIN report_job job ON job.report_request_id = req.report_request_id
            WHERE {" AND ".join(where_clauses)}
            ORDER BY job.created_at DESC, job.report_job_id DESC
            LIMIT %s
        """
        params.append(filters.limit)

        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return cast(list[Mapping[str, Any]], rows)

    def mark_collecting_data(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            return self._transition_job(
                connection=connection,
                job_id=job_id,
                to_status="collecting_data",
                failure_category=None,
                failure_message=None,
                current_step="collecting_data",
                retry_eligible=False,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                event_type="job_collecting_data",
                event_message="Portfolio review input capture started.",
                set_started_at=True,
                set_completed_at=False,
                render_job_id=None,
                render_output_format=None,
                render_template_id=None,
                render_template_version=None,
                render_artifact_sha256=None,
                render_bounded_determinism_fingerprint=None,
                render_runtime_engine=None,
                render_runtime_engine_version=None,
                render_duration_ms=None,
                archive_request_id=None,
                archive_document_id=None,
                archive_completed_at=None,
            )

    def mark_data_ready(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            return self._transition_job(
                connection=connection,
                job_id=job_id,
                to_status="data_ready",
                failure_category=None,
                failure_message=None,
                current_step="data_ready",
                retry_eligible=False,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                event_type="job_data_ready",
                event_message="Portfolio review snapshot and lineage captured.",
                set_started_at=True,
                set_completed_at=False,
                render_job_id=None,
                render_output_format=None,
                render_template_id=None,
                render_template_version=None,
                render_artifact_sha256=None,
                render_bounded_determinism_fingerprint=None,
                render_runtime_engine=None,
                render_runtime_engine_version=None,
                render_duration_ms=None,
                archive_request_id=None,
                archive_document_id=None,
                archive_completed_at=None,
            )

    def mark_rendering(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
        render_job_id: str,
        output_format: str,
        template_id: str,
        template_version: str,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            return self._transition_job(
                connection=connection,
                job_id=job_id,
                to_status="rendering",
                failure_category=None,
                failure_message=None,
                current_step="rendering",
                retry_eligible=False,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                event_type="job_rendering",
                event_message="Portfolio review render started.",
                set_started_at=True,
                set_completed_at=False,
                render_job_id=render_job_id,
                render_output_format=output_format,
                render_template_id=template_id,
                render_template_version=template_version,
                render_artifact_sha256=None,
                render_bounded_determinism_fingerprint=None,
                render_runtime_engine=None,
                render_runtime_engine_version=None,
                render_duration_ms=None,
                archive_request_id=None,
                archive_document_id=None,
                archive_completed_at=None,
            )

    def mark_completed(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
        render_job_id: str,
        output_format: str,
        template_id: str,
        template_version: str,
        template_publication: str | None,
        artifact_sha256: str | None,
        bounded_determinism_fingerprint: str | None,
        runtime_engine: str | None,
        runtime_engine_version: str | None,
        render_duration_ms: int | None,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            return self._transition_job(
                connection=connection,
                job_id=job_id,
                to_status="completed",
                failure_category=None,
                failure_message=None,
                current_step="completed",
                retry_eligible=False,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                event_type="job_completed",
                event_message="Portfolio review render completed.",
                set_started_at=True,
                set_completed_at=True,
                render_job_id=render_job_id,
                render_output_format=output_format,
                render_template_id=template_id,
                render_template_version=template_version,
                render_template_publication=template_publication,
                render_artifact_sha256=artifact_sha256,
                render_bounded_determinism_fingerprint=bounded_determinism_fingerprint,
                render_runtime_engine=runtime_engine,
                render_runtime_engine_version=runtime_engine_version,
                render_duration_ms=render_duration_ms,
                archive_request_id=None,
                archive_document_id=None,
                archive_completed_at=None,
            )

    def mark_archiving(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
        archive_request_id: str,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            return self._transition_job(
                connection=connection,
                job_id=job_id,
                to_status="archiving",
                failure_category=None,
                failure_message=None,
                current_step="archiving",
                retry_eligible=False,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                event_type="job_archiving",
                event_message="Portfolio review archive handoff started.",
                set_started_at=True,
                set_completed_at=False,
                render_job_id=None,
                render_output_format=None,
                render_template_id=None,
                render_template_version=None,
                render_artifact_sha256=None,
                render_bounded_determinism_fingerprint=None,
                render_runtime_engine=None,
                render_runtime_engine_version=None,
                render_duration_ms=None,
                archive_request_id=archive_request_id,
                archive_document_id=None,
                archive_completed_at=None,
            )

    def mark_archived(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
        archive_request_id: str,
        archive_document_id: str,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            return self._transition_job(
                connection=connection,
                job_id=job_id,
                to_status="archived",
                failure_category=None,
                failure_message=None,
                current_step="archived",
                retry_eligible=False,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                event_type="job_archived",
                event_message="Portfolio review archived successfully.",
                set_started_at=True,
                set_completed_at=False,
                render_job_id=None,
                render_output_format=None,
                render_template_id=None,
                render_template_version=None,
                render_artifact_sha256=None,
                render_bounded_determinism_fingerprint=None,
                render_runtime_engine=None,
                render_runtime_engine_version=None,
                render_duration_ms=None,
                archive_request_id=archive_request_id,
                archive_document_id=archive_document_id,
                archive_completed_at=utc_now(),
            )

    def mark_failed(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
        failure_category: str,
        failure_message: str,
        retry_eligible: bool,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            return self._transition_job(
                connection=connection,
                job_id=job_id,
                to_status="failed",
                failure_category=failure_category,
                failure_message=failure_message,
                current_step="failed",
                retry_eligible=retry_eligible,
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                event_type="job_failed",
                event_message=failure_message,
                set_started_at=True,
                set_completed_at=True,
                render_job_id=None,
                render_output_format=None,
                render_template_id=None,
                render_template_version=None,
                render_artifact_sha256=None,
                render_bounded_determinism_fingerprint=None,
                render_runtime_engine=None,
                render_runtime_engine_version=None,
                render_duration_ms=None,
                archive_request_id=None,
                archive_document_id=None,
                archive_completed_at=None,
            )

    def cancel_job(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
    ) -> ReportJobLedgerRecord:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT status FROM report_job WHERE report_job_id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if not existing:
                raise ReportJobNotFoundError("report_job_not_found")
            current_status = str(existing["status"])
            if not is_report_job_cancellable(current_status):
                raise InvalidReportJobTransitionError("report_job_cannot_be_cancelled")

            now = utc_now()
            connection.execute(
                """
                UPDATE report_job
                SET status = %s, failure_category = %s, failure_message = %s, current_step = %s,
                    retry_eligible = %s, cancel_requested = %s, updated_at = %s, cancelled_at = %s
                WHERE report_job_id = %s
                """,
                (
                    "cancelled",
                    "cancelled",
                    "Report job cancelled before render or archive processing.",
                    "cancelled",
                    False,
                    True,
                    now,
                    now,
                    job_id,
                ),
            )
            self._append_status_event(
                connection=connection,
                job_id=job_id,
                from_status=current_status,
                to_status="cancelled",
                event_type="job_cancelled",
                message="Report job cancelled before render or archive processing.",
                event_payload={
                    "current_step": "cancelled",
                    "cancel_requested": True,
                },
                actor=actor,
                correlation_id=correlation_id,
                trace_id=trace_id,
                created_at=now,
            )
            row = connection.execute(
                "SELECT report_request_id FROM report_job WHERE report_job_id = %s",
                (job_id,),
            ).fetchone()
            if not row:
                raise ReportJobNotFoundError("report_job_not_found")
            return self._load_by_request_id(connection, str(row["report_request_id"]))

    def _existing_or_conflict(
        self,
        connection: Connection[Mapping[str, Any]],
        existing: Mapping[str, Any],
        request_hash: str,
    ) -> ReportJobLedgerRecord:
        record = self._load_by_request_id(connection, str(existing["report_request_id"]))
        if existing["request_hash"] != request_hash and request_hash != (
            client_identity_hash_from_record(record)
        ):
            # See the SQLite ledger: the stored record's own persisted
            # request is the evolution-proof comparison basis.
            raise IdempotencyConflictError("idempotency_key_reused_with_different_request")
        return record

    def _append_status_event(
        self,
        *,
        connection: Connection[Mapping[str, Any]],
        job_id: str,
        from_status: str | None,
        to_status: ReportJobStatus,
        event_type: str,
        message: str | None,
        event_payload: dict[str, Any] | None = None,
        event_idempotency_key: str | None = None,
        actor: str,
        correlation_id: str,
        trace_id: str,
        created_at: datetime,
    ) -> None:
        contract = build_report_status_event_contract(
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            event_payload=event_payload,
            event_idempotency_key=event_idempotency_key,
        )
        connection.execute(
            """
            INSERT INTO report_status_event (
                status_event_id, report_job_id, from_status, to_status, event_type,
                event_schema_version, event_family, event_payload_json, event_idempotency_key,
                message, actor, created_at, correlation_id, trace_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"rse_{uuid4().hex}",
                job_id,
                from_status,
                to_status,
                event_type,
                contract.schema_version,
                contract.event_family,
                Jsonb(contract.event_payload),
                contract.event_idempotency_key,
                message,
                actor,
                created_at,
                correlation_id,
                trace_id,
            ),
        )

    def _transition_job(
        self,
        *,
        connection: Connection[Mapping[str, Any]],
        job_id: str,
        to_status: ReportJobStatus,
        failure_category: str | None,
        failure_message: str | None,
        current_step: str,
        retry_eligible: bool,
        actor: str,
        correlation_id: str,
        trace_id: str,
        event_type: str,
        event_message: str | None,
        set_started_at: bool,
        set_completed_at: bool,
        render_job_id: str | None,
        render_output_format: str | None,
        render_template_id: str | None,
        render_template_version: str | None,
        render_template_publication: str | None = None,
        render_artifact_sha256: str | None,
        render_bounded_determinism_fingerprint: str | None,
        render_runtime_engine: str | None,
        render_runtime_engine_version: str | None,
        render_duration_ms: int | None,
        archive_request_id: str | None,
        archive_document_id: str | None,
        archive_completed_at: datetime | None,
    ) -> ReportJobLedgerRecord:
        existing = connection.execute(
            "SELECT status, started_at FROM report_job WHERE report_job_id = %s FOR UPDATE",
            (job_id,),
        ).fetchone()
        if not existing:
            raise ReportJobNotFoundError("report_job_not_found")
        current_status = str(existing["status"])
        if current_status == to_status:
            row = connection.execute(
                "SELECT report_request_id FROM report_job WHERE report_job_id = %s",
                (job_id,),
            ).fetchone()
            assert row is not None
            return self._load_by_request_id(connection, str(row["report_request_id"]))
        if not is_report_job_transition_allowed(
            current_status=current_status,
            to_status=to_status,
        ):
            raise InvalidReportJobTransitionError("report_job_invalid_transition")

        now = utc_now()
        started_at = existing["started_at"] or (now if set_started_at else None)
        completed_at = now if set_completed_at else None
        connection.execute(
            """
            UPDATE report_job
            SET status = %s, failure_category = %s, failure_message = %s, current_step = %s,
                retry_eligible = %s, updated_at = %s, started_at = %s, completed_at = %s,
                render_job_id = COALESCE(%s, render_job_id),
                render_output_format = COALESCE(%s, render_output_format),
                render_template_id = COALESCE(%s, render_template_id),
                render_template_version = COALESCE(%s, render_template_version),
                render_template_publication = COALESCE(%s, render_template_publication),
                render_artifact_sha256 = COALESCE(%s, render_artifact_sha256),
                render_bounded_determinism_fingerprint = COALESCE(
                    %s,
                    render_bounded_determinism_fingerprint
                ),
                render_runtime_engine = COALESCE(%s, render_runtime_engine),
                render_runtime_engine_version = COALESCE(%s, render_runtime_engine_version),
                render_duration_ms = COALESCE(%s, render_duration_ms),
                archive_request_id = COALESCE(%s, archive_request_id),
                archive_document_id = COALESCE(%s, archive_document_id),
                archive_completed_at = COALESCE(%s, archive_completed_at)
            WHERE report_job_id = %s
            """,
            (
                to_status,
                failure_category,
                failure_message,
                current_step,
                retry_eligible,
                now,
                started_at,
                completed_at,
                render_job_id,
                render_output_format,
                render_template_id,
                render_template_version,
                render_template_publication,
                render_artifact_sha256,
                render_bounded_determinism_fingerprint,
                render_runtime_engine,
                render_runtime_engine_version,
                render_duration_ms,
                archive_request_id,
                archive_document_id,
                archive_completed_at,
                job_id,
            ),
        )
        self._append_status_event(
            connection=connection,
            job_id=job_id,
            from_status=current_status,
            to_status=to_status,
            event_type=event_type,
            message=event_message,
            event_payload=_transition_event_payload(
                current_step=current_step,
                failure_category=failure_category,
                failure_message=failure_message,
                render_job_id=render_job_id,
                render_output_format=render_output_format,
                render_template_id=render_template_id,
                render_template_version=render_template_version,
                render_artifact_sha256=render_artifact_sha256,
                render_bounded_determinism_fingerprint=render_bounded_determinism_fingerprint,
                render_runtime_engine=render_runtime_engine,
                render_runtime_engine_version=render_runtime_engine_version,
                render_duration_ms=render_duration_ms,
                archive_request_id=archive_request_id,
                archive_document_id=archive_document_id,
            ),
            actor=actor,
            correlation_id=correlation_id,
            trace_id=trace_id,
            created_at=now,
        )
        row = connection.execute(
            "SELECT report_request_id FROM report_job WHERE report_job_id = %s",
            (job_id,),
        ).fetchone()
        assert row is not None
        return self._load_by_request_id(connection, str(row["report_request_id"]))

    def _update_rerender_attempt(
        self,
        *,
        connection: Connection[Mapping[str, Any]],
        rerender_attempt_id: str,
        status: str,
        render_job_id: str | None = None,
        artifact_sha256: str | None = None,
        bounded_determinism_fingerprint: str | None = None,
        runtime_engine: str | None = None,
        runtime_engine_version: str | None = None,
        render_duration_ms: int | None = None,
        archive_request_id: str | None = None,
        archive_document_id: str | None = None,
        archive_completed_at: datetime | None = None,
        failure_category: str | None = None,
        failure_message: str | None = None,
        retry_eligible: bool | None = None,
        clear_failure: bool = False,
    ) -> ReportRerenderAttemptRecord:
        existing = connection.execute(
            "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = %s FOR UPDATE",
            (rerender_attempt_id,),
        ).fetchone()
        if not existing:
            raise ReportJobNotFoundError("report_rerender_attempt_not_found")
        connection.execute(
            """
            UPDATE report_rerender_attempt
            SET status = %s,
                render_job_id = COALESCE(%s, render_job_id),
                render_artifact_sha256 = COALESCE(%s, render_artifact_sha256),
                render_bounded_determinism_fingerprint = COALESCE(
                    %s,
                    render_bounded_determinism_fingerprint
                ),
                render_runtime_engine = COALESCE(%s, render_runtime_engine),
                render_runtime_engine_version = COALESCE(%s, render_runtime_engine_version),
                render_duration_ms = COALESCE(%s, render_duration_ms),
                archive_request_id = COALESCE(%s, archive_request_id),
                archive_document_id = COALESCE(%s, archive_document_id),
                archive_completed_at = COALESCE(%s, archive_completed_at),
                failure_category = CASE WHEN %s THEN NULL
                    ELSE COALESCE(%s, failure_category) END,
                failure_message = CASE WHEN %s THEN NULL
                    ELSE COALESCE(%s, failure_message) END,
                retry_eligible = CASE WHEN %s THEN FALSE
                    ELSE COALESCE(%s, retry_eligible) END,
                updated_at = %s
            WHERE rerender_attempt_id = %s
            """,
            (
                status,
                render_job_id,
                artifact_sha256,
                bounded_determinism_fingerprint,
                runtime_engine,
                runtime_engine_version,
                render_duration_ms,
                archive_request_id,
                archive_document_id,
                archive_completed_at,
                clear_failure,
                failure_category,
                clear_failure,
                failure_message,
                clear_failure,
                retry_eligible,
                utc_now(),
                rerender_attempt_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = %s",
            (rerender_attempt_id,),
        ).fetchone()
        assert row is not None
        return _rerender_attempt_from_row(row)

    def _load_by_request_id(
        self,
        connection: Connection[Mapping[str, Any]],
        request_id: str,
    ) -> ReportJobLedgerRecord:
        row = connection.execute(
            """
            SELECT
                req.report_request_id,
                req.report_type,
                req.portfolio_scope_json AS request_portfolio_scope_json,
                req.requested_output_formats_json,
                req.as_of_date,
                req.reporting_currency,
                req.options_json,
                req.trigger_type,
                req.triggered_by,
                req.caller_application,
                req.tenant_id,
                req.region,
                req.booking_center_code,
                req.role,
                req.idempotency_key,
                req.request_hash,
                req.correlation_id,
                req.trace_id,
                req.created_at AS request_created_at,
                job.report_job_id,
                job.portfolio_scope_json AS job_portfolio_scope_json,
                job.status,
                job.failure_category,
                job.failure_message,
                job.current_step,
                job.retry_eligible,
                job.cancel_requested,
                job.created_at AS job_created_at,
                job.updated_at,
                job.started_at,
                job.completed_at,
                job.cancelled_at,
                job.render_job_id,
                job.render_output_format,
                job.render_template_id,
                job.render_template_version,
                job.render_template_publication,
                job.render_artifact_sha256,
                job.render_bounded_determinism_fingerprint,
                job.render_runtime_engine,
                job.render_runtime_engine_version,
                job.render_duration_ms,
                job.archive_request_id,
                job.archive_document_id,
                job.archive_completed_at,
                job.accepted_document_contract_json
            FROM report_request req
            JOIN report_job job ON job.report_request_id = req.report_request_id
            WHERE req.report_request_id = %s
            """,
            (request_id,),
        ).fetchone()
        if not row:
            raise ReportJobNotFoundError("report_job_not_found")
        return _record_from_row(row)


def _dt_from_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _date_from_value(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def _work_item_from_row(row: Mapping[str, Any]) -> ReportJobWorkItem:
    return ReportJobWorkItem(
        work_item_id=str(row["work_item_id"]),
        report_job_id=str(row["report_job_id"]),
        status=row["status"],
        attempt_count=int(row["attempt_count"]),
        available_at=_dt_from_value(row["available_at"]) or utc_now(),
        lease_owner=row.get("lease_owner"),
        lease_token=row.get("lease_token"),
        lease_acquired_at=_dt_from_value(row.get("lease_acquired_at")),
        lease_expires_at=_dt_from_value(row.get("lease_expires_at")),
        last_error_category=row.get("last_error_category"),
        last_error_summary=row.get("last_error_summary"),
        created_at=_dt_from_value(row["created_at"]) or utc_now(),
        updated_at=_dt_from_value(row["updated_at"]) or utc_now(),
        completed_at=_dt_from_value(row.get("completed_at")),
    )


def _record_from_row(row: Mapping[str, Any]) -> ReportJobLedgerRecord:
    return ReportJobLedgerRecord(
        request_id=str(row["report_request_id"]),
        job_id=str(row["report_job_id"]),
        report_type=str(row["report_type"]),
        portfolio_scope=dict(row["request_portfolio_scope_json"]),
        requested_output_formats=list(row["requested_output_formats_json"]),
        as_of_date=_date_from_value(row["as_of_date"]),
        reporting_currency=row["reporting_currency"],
        options=dict(row["options_json"]),
        trigger_type=str(row["trigger_type"]),
        triggered_by=str(row["triggered_by"]),
        caller_application=str(row["caller_application"]),
        tenant_id=str(row["tenant_id"]),
        region=str(row["region"]),
        booking_center_code=row["booking_center_code"],
        role=row["role"],
        idempotency_key=str(row["idempotency_key"]),
        request_hash=str(row["request_hash"]),
        status=row["status"],
        failure_category=row["failure_category"],
        failure_message=row["failure_message"],
        current_step=str(row["current_step"]),
        retry_eligible=bool(row["retry_eligible"]),
        cancel_requested=bool(row["cancel_requested"]),
        created_at=_dt_from_value(row["job_created_at"]) or utc_now(),
        updated_at=_dt_from_value(row["updated_at"]) or utc_now(),
        started_at=_dt_from_value(row["started_at"]),
        completed_at=_dt_from_value(row["completed_at"]),
        cancelled_at=_dt_from_value(row["cancelled_at"]),
        correlation_id=str(row["correlation_id"]),
        trace_id=str(row["trace_id"]),
        render_job_id=row.get("render_job_id"),
        render_output_format=row.get("render_output_format"),
        render_template_id=row.get("render_template_id"),
        accepted_document_contract=(
            row.get("accepted_document_contract_json")
            if isinstance(row.get("accepted_document_contract_json"), dict)
            else None
        ),
        render_template_version=row.get("render_template_version"),
        render_template_publication=row.get("render_template_publication"),
        render_artifact_sha256=row.get("render_artifact_sha256"),
        render_bounded_determinism_fingerprint=row.get("render_bounded_determinism_fingerprint"),
        render_runtime_engine=row.get("render_runtime_engine"),
        render_runtime_engine_version=row.get("render_runtime_engine_version"),
        render_duration_ms=row.get("render_duration_ms"),
        archive_request_id=row.get("archive_request_id"),
        archive_document_id=row.get("archive_document_id"),
        archive_completed_at=_dt_from_value(row.get("archive_completed_at")),
    )


def _rerender_attempt_from_row(row: Mapping[str, Any]) -> ReportRerenderAttemptRecord:
    return ReportRerenderAttemptRecord(
        rerender_attempt_id=str(row["rerender_attempt_id"]),
        report_job_id=str(row["report_job_id"]),
        idempotency_key=str(row["idempotency_key"]),
        status=row["status"],
        snapshot_id=str(row["snapshot_id"]),
        snapshot_hash=str(row["snapshot_hash"]),
        previous_render_job_id=row.get("previous_render_job_id"),
        previous_archive_document_id=row.get("previous_archive_document_id"),
        render_job_id=str(row["render_job_id"]),
        render_output_format=str(row["render_output_format"]),
        render_template_id=str(row["render_template_id"]),
        render_template_version=str(row["render_template_version"]),
        render_artifact_sha256=row.get("render_artifact_sha256"),
        render_bounded_determinism_fingerprint=row.get("render_bounded_determinism_fingerprint"),
        render_runtime_engine=row.get("render_runtime_engine"),
        render_runtime_engine_version=row.get("render_runtime_engine_version"),
        render_duration_ms=row.get("render_duration_ms"),
        archive_request_id=row.get("archive_request_id"),
        archive_document_id=row.get("archive_document_id"),
        archive_completed_at=_dt_from_value(row.get("archive_completed_at")),
        failure_category=row.get("failure_category"),
        failure_message=row.get("failure_message"),
        retry_eligible=bool(row["retry_eligible"]),
        requested_by=str(row["requested_by"]),
        reason=str(row["reason"]),
        correlation_id=str(row["correlation_id"]),
        trace_id=str(row["trace_id"]),
        created_at=_dt_from_value(row["created_at"]) or utc_now(),
        updated_at=_dt_from_value(row["updated_at"]) or utc_now(),
    )


def _relationship_from_row(row: Mapping[str, Any]) -> ReportJobRelationshipRecord:
    return ReportJobRelationshipRecord(
        relationship_id=str(row["relationship_id"]),
        relationship_type=row["relationship_type"],
        source_report_job_id=str(row["source_report_job_id"]),
        derived_report_job_id=str(row["derived_report_job_id"]),
        source_status=row["source_status"],
        derived_status=row["derived_status"],
        source_failure_category=row.get("source_failure_category"),
        derived_failure_category=row.get("derived_failure_category"),
        archive_consequence=row.get("archive_consequence"),
        previous_archive_document_id=row.get("previous_archive_document_id"),
        new_archive_document_id=row.get("new_archive_document_id"),
        actor=str(row["actor"]),
        reason=str(row["reason"]),
        created_at=_dt_from_value(row["created_at"]) or utc_now(),
        updated_at=_dt_from_value(row["updated_at"]) or utc_now(),
    )


def _bounded_relationship_reason(reason: str) -> str:
    normalized = " ".join((reason or "").split())
    if not normalized:
        return "not_provided"
    return normalized[:240]


def _event_from_row(row: Mapping[str, Any]) -> ReportStatusEvent:
    event_type = str(row["event_type"])
    contract = legacy_report_status_event_contract(
        event_type=event_type,
        from_status=row["from_status"],
        to_status=row["to_status"],
    )
    raw_payload = row.get("event_payload_json")
    event_payload = (
        dict(raw_payload) if isinstance(raw_payload, Mapping) else contract.event_payload
    )
    return ReportStatusEvent(
        status_event_id=str(row["status_event_id"]),
        report_job_id=str(row["report_job_id"]),
        from_status=row["from_status"],
        to_status=row["to_status"],
        event_type=event_type,
        event_schema_version=str(row.get("event_schema_version") or contract.schema_version),
        event_family=str(row.get("event_family") or contract.event_family),
        event_payload=event_payload,
        event_idempotency_key=row.get("event_idempotency_key"),
        message=row["message"],
        actor=str(row["actor"]),
        created_at=_dt_from_value(row["created_at"]) or utc_now(),
        correlation_id=str(row["correlation_id"]),
        trace_id=str(row["trace_id"]),
    )
