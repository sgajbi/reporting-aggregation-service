from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, TypeAlias
from uuid import uuid4

from app.config import settings
from app.report_ordering_catalogue.template_resolution import (
    accepted_document_contract,
    job_template_identity,
)
from app.reporting_jobs.event_contracts import (
    build_report_status_event_contract,
    legacy_report_status_event_contract,
)
from app.reporting_jobs.lease_telemetry import record_report_job_work_lease_event
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

ReportJobRequest: TypeAlias = (
    PortfolioReviewJobRequest
    | OutcomeReviewReportJobRequest
    | ProofPackReportJobRequest
    | WaveReportJobRequest
)


#: The `report_request` shape, defined once because two things create it: the
#: first-run schema and the report#350 rebuild that replaces the old
#: single-key UNIQUE. A second copy would drift the moment a column is added,
#: and the rebuild would silently write a narrower table than the one the
#: service expects.
_REPORT_REQUEST_DDL = """
CREATE TABLE IF NOT EXISTS report_request (
    report_request_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    portfolio_scope_json TEXT NOT NULL,
    requested_output_formats_json TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    reporting_currency TEXT,
    options_json TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    caller_application TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    region TEXT NOT NULL,
    booking_center_code TEXT,
    role TEXT,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class ReportJobLedgerMigrationError(RuntimeError):
    """A retained `report_request` upgrade could not be completed safely.

    Raised rather than swallowed: a partial rebuild would leave retained
    requests in a renamed table while the live one answered empty, and every
    replay would then look like a new request.
    """


class MissingIdempotencyKeyError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class ReportJobNotFoundError(ValueError):
    pass


class InvalidReportJobTransitionError(ValueError):
    pass


class InvalidReportJobWorkTransitionError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def resolve_job_accepted_contract(
    *,
    report_type: str,
    output_formats: list[str] | None,
    inherited_template: tuple[str | None, str | None] | None,
    inherited_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """The one contract-minting path both ledgers share.

    A replay RECOVERS an accepted job and inherits its contract verbatim;
    a legacy source without one yields a freshly minted contract that
    still carries the inherited template pair - the one axis legacy
    acceptance persisted. Every other acceptance resolves the current
    governed contract exactly once, here.
    """

    if inherited_contract is not None:
        return inherited_contract
    return dict(
        accepted_document_contract(
            report_type,
            output_formats,
            input_snapshot_contract_version=settings.contract_version,
            inherited_template=inherited_template,
        )
    )


#: Request-option keys the SERVER derives and injects after acceptance
#: validation. They are excluded from the idempotency identity: the hash
#: answers "did the CLIENT ask for the same thing", and a deployment that
#: starts (or stops) enriching options must never change the identity of
#: an unchanged accepted request. The owning module pins its constant to
#: this registry by test.
SERVER_DERIVED_REQUEST_OPTION_KEYS = frozenset({"idea_materialization_recovery_identity"})


def client_identity_hash_from_record(record: "ReportJobLedgerRecord") -> str:
    """The CLIENT identity of a stored request, recomputed from its own
    persisted fields with every server-derived option stripped - the
    evolution-proof comparison basis for records hashed under any earlier
    enrichment policy."""

    return _client_identity_hash(
        report_type=record.report_type,
        portfolio_scope=record.portfolio_scope,
        as_of_date_iso=record.as_of_date.isoformat(),
        output_formats=record.requested_output_formats,
        reporting_currency=record.reporting_currency,
        options=record.options,
        tenant_id=record.tenant_id,
        region=record.region,
        strip_server_derived=True,
    )


def compute_request_hash(
    *,
    report_type: str,
    request: ReportJobRequest,
    caller_context: ReportCallerContext,
    include_server_derived_options: bool = False,
) -> str:
    portfolio_scope, as_of_date, output_formats, reporting_currency, options = _request_parts(
        report_type=report_type,
        request=request,
    )
    return _client_identity_hash(
        report_type=report_type,
        portfolio_scope=portfolio_scope,
        as_of_date_iso=as_of_date.isoformat(),
        output_formats=output_formats,
        reporting_currency=reporting_currency,
        options=options,
        tenant_id=caller_context.tenant_id,
        region=caller_context.region,
        strip_server_derived=not include_server_derived_options,
    )


def _client_identity_hash(
    *,
    report_type: str,
    portfolio_scope: dict[str, Any],
    as_of_date_iso: str,
    output_formats: list[str],
    reporting_currency: str | None,
    options: dict[str, Any],
    tenant_id: str | None,
    region: str | None,
    strip_server_derived: bool,
) -> str:
    """The ONE construction of the client identity hash.

    Both identity questions - "what is this incoming request's identity"
    (compute_request_hash) and "what is this stored record's client
    identity under the current policy" (client_identity_hash_from_record) -
    MUST build the identical payload, normalization included: when they
    diverge, a legacy record whose declared-set lists were stored unsorted
    conflicts with its own identical retry. Sharing the builder makes that
    divergence unexpressible.
    """

    if strip_server_derived:
        options = {
            key: value
            for key, value in options.items()
            if key not in SERVER_DERIVED_REQUEST_OPTION_KEYS
        }
    hash_payload = {
        "report_type": report_type,
        "portfolio_scope": _set_normalized(portfolio_scope, "portfolio_ids"),
        "as_of_date": as_of_date_iso,
        "requested_output_formats": sorted(output_formats),
        "reporting_currency": reporting_currency,
        "options": _set_normalized(options, "sections", "allocation_dimensions"),
        "tenant_id": tenant_id,
        "region": region,
    }
    return hashlib.sha256(canonical_json(hash_payload).encode("utf-8")).hexdigest()


def _set_normalized(payload: dict[str, Any], *set_keys: str) -> dict[str, Any]:
    """The request-identity twin of the series key's declared-set rule.

    The identity contract (ReportSeriesKey.canonical) knows exactly which
    request lists are SETS - sections and allocation_dimensions at the top
    level, portfolio_ids inside the scope - and composition consumes them
    as sets, so a reordered retry is the SAME client intent and must
    converge on the original job instead of raising a false idempotency
    conflict. Every other list keeps its order: a list in a request may be
    semantically ordered, and a generic sort would erase output-affecting
    semantics. Records stored under the order-sensitive hash still accept
    identical retries through the record-based comparison, which recomputes
    the client identity from the stored request under THIS policy.
    """

    normalized = dict(payload)
    for key in set_keys:
        value = normalized.get(key)
        if isinstance(value, (list, tuple)):
            normalized[key] = sorted({str(item) for item in value})
    return normalized


def _request_parts(
    *,
    report_type: str,
    request: ReportJobRequest,
) -> tuple[dict[str, Any], date, list[str], str | None, dict[str, Any]]:
    if isinstance(request, PortfolioReviewJobRequest):
        options = dict(request.options)
        if request.proposal_narrative_package is not None:
            options["proposal_narrative_package"] = request.proposal_narrative_package.model_dump(
                mode="json"
            )
        if request.proposal_memo_package is not None:
            options["proposal_memo_package"] = request.proposal_memo_package.model_dump(mode="json")
        return (
            request.portfolio_scope,
            request.as_of_date,
            request.requested_output_formats,
            request.reporting_currency,
            options,
        )
    if isinstance(request, ProofPackReportJobRequest):
        report_input = request.proof_pack_report_input.model_dump(mode="json")
        portfolio_id = request.proof_pack_report_input.portfolio_id.strip()
        if not portfolio_id:
            raise ValueError("proof_pack_report_input.portfolio_id is required")
        as_of_text = report_input.get("as_of_date") or report_input.get("generated_at")
        if not as_of_text:
            raise ValueError("proof_pack_report_input.as_of_date is required")
        as_of_date = date.fromisoformat(str(as_of_text)[:10])
        options = dict(request.options)
        options["proof_pack_report_input"] = report_input
        portfolio_scope = {
            "portfolio_ids": [portfolio_id],
            "proof_pack_id": report_input.get("proof_pack_id"),
        }
        return (
            portfolio_scope,
            as_of_date,
            request.requested_output_formats,
            request.reporting_currency,
            options,
        )
    if isinstance(request, WaveReportJobRequest):
        report_input = request.wave_report_input.model_dump(mode="json")
        wave_id = request.wave_report_input.wave_id.strip()
        if not wave_id:
            raise ValueError("wave_report_input.wave_id is required")
        as_of_text = report_input.get("as_of_date") or report_input.get("generated_at")
        if not as_of_text:
            raise ValueError("wave_report_input.as_of_date is required")
        as_of_date = date.fromisoformat(str(as_of_text)[:10])
        options = dict(request.options)
        options["wave_report_input"] = report_input
        portfolio_ids = [
            str(item.get("portfolio_id")).strip()
            for item in report_input.get("items", [])
            if isinstance(item, dict) and str(item.get("portfolio_id") or "").strip()
        ]
        portfolio_scope = {
            "portfolio_ids": sorted(set(portfolio_ids)),
            "wave_id": wave_id,
            "proof_pack_ids": sorted(
                {
                    str(item.get("proof_pack_id")).strip()
                    for item in report_input.get("items", [])
                    if isinstance(item, dict) and str(item.get("proof_pack_id") or "").strip()
                }
            ),
        }
        return (
            portfolio_scope,
            as_of_date,
            request.requested_output_formats,
            request.reporting_currency,
            options,
        )
    report_input = request.outcome_report_input.model_dump(mode="json")
    portfolio_id = request.outcome_report_input.portfolio_id.strip()
    if not portfolio_id:
        raise ValueError("outcome_report_input.portfolio_id is required")
    review_window = report_input.get("review_window")
    review_window_payload = review_window if isinstance(review_window, dict) else {}
    as_of_text = (
        review_window_payload.get("end_date")
        or review_window_payload.get("period_end")
        or report_input.get("generated_at")
    )
    if not as_of_text:
        raise ValueError("outcome_report_input review window end date is required")
    as_of_date = date.fromisoformat(str(as_of_text)[:10])
    options = dict(request.options)
    options["outcome_report_input"] = report_input
    portfolio_scope = {
        "portfolio_ids": [portfolio_id],
        "outcome_review_id": report_input.get("outcome_review_id"),
    }
    return (
        portfolio_scope,
        as_of_date,
        request.requested_output_formats,
        request.reporting_currency,
        options,
    )


@dataclass(frozen=True)
class PendingArchiveLineage:
    job_id: str
    oldest_created_at: datetime | None


class ReportJobLedger:
    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self.ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._db_path != Path(":memory:"):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_report_request_to_tenant_identity(connection: sqlite3.Connection) -> None:
        """Rebuild `report_request` when it still carries the single-key UNIQUE.

        Detected from the schema rather than assumed from a version counter:
        a unique index covering exactly `idempotency_key` is the old identity,
        whatever created it.

        The whole rebuild runs in one explicit transaction. Python's `sqlite3`
        opens a transaction implicitly before DML but NOT before DDL, so without
        `BEGIN IMMEDIATE` the rename and the create autocommit one statement at
        a time -- and a process dying between them would restart, find
        `report_request` already present, and serve an empty ledger while every
        retained request sat in the renamed table.
        """
        legacy_unique = False
        for index_row in connection.execute("PRAGMA index_list(report_request)").fetchall():
            if not index_row["unique"]:
                continue
            columns = [
                str(column_row["name"])
                for column_row in connection.execute(
                    f"PRAGMA index_info({index_row['name']!r})"
                ).fetchall()
            ]
            if columns == ["idempotency_key"]:
                legacy_unique = True
                break
        if not legacy_unique:
            return

        # `ALTER TABLE ... RENAME` rewrites other tables' foreign-key references
        # to follow the rename, so `report_job` would start pointing at the
        # renamed table and the drop would fail with a FOREIGN KEY violation.
        # `legacy_alter_table` keeps those references naming `report_request`,
        # which is the table about to be recreated under that name.
        #
        # Both pragmas are no-ops inside a transaction, so they are set first.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns = [
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(report_request)").fetchall()
            ]
            column_list = ", ".join(columns)
            connection.execute("ALTER TABLE report_request RENAME TO report_request_pre_350")
            connection.execute(_REPORT_REQUEST_DDL)
            connection.execute(
                f"INSERT INTO report_request ({column_list}) "
                f"SELECT {column_list} FROM report_request_pre_350"
            )
            carried = connection.execute("SELECT count(*) AS n FROM report_request").fetchone()["n"]
            retained = connection.execute(
                "SELECT count(*) AS n FROM report_request_pre_350"
            ).fetchone()["n"]
            if carried != retained:
                raise ReportJobLedgerMigrationError(
                    f"report_request upgrade carried {carried} of {retained} retained requests"
                )
            connection.execute("DROP TABLE report_request_pre_350")
            # Integrity is asserted rather than assumed: foreign keys were
            # disabled for the rebuild, so this is the only thing standing
            # between a silent orphan and a ledger that answers wrongly.
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ReportJobLedgerMigrationError(
                    f"report_request upgrade left {len(violations)} foreign-key violation(s)"
                )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
        connection.commit()

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(_REPORT_REQUEST_DDL)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS report_job (
                    report_job_id TEXT PRIMARY KEY,
                    report_request_id TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    portfolio_scope_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    failure_category TEXT,
                    failure_message TEXT,
                    current_step TEXT NOT NULL,
                    retry_eligible INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT,
                    render_job_id TEXT,
                    render_output_format TEXT,
                    render_template_id TEXT,
                    render_template_version TEXT,
                    render_template_publication TEXT,
                    render_artifact_sha256 TEXT,
                    render_bounded_determinism_fingerprint TEXT,
                    render_runtime_engine TEXT,
                    render_runtime_engine_version TEXT,
                    render_duration_ms INTEGER,
                    archive_request_id TEXT,
                    archive_document_id TEXT,
                    archive_completed_at TEXT,
                    accepted_document_contract_json TEXT,
                    FOREIGN KEY(report_request_id) REFERENCES report_request(report_request_id)
                )
                """
            )
            # report#350: identity moved from the caller's idempotency key alone
            # to (tenant_id, idempotency_key). A file created before that carries
            # a column-level UNIQUE on idempotency_key, which SQLite implements
            # as an auto-index that cannot be dropped and cannot be altered --
            # so the table is rebuilt, exactly as migration 025 rebuilt the
            # intake ledger. `CREATE TABLE IF NOT EXISTS` above is a no-op
            # against an existing file and would leave the old constraint in
            # place while every query looked correct.
            self._migrate_report_request_to_tenant_identity(connection)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS report_request_tenant_idempotency_key
                ON report_request (tenant_id, idempotency_key)
                """
            )
            # The accepted-document-contract column arrived after the table
            # (report#283); a database created before it evolves additively.
            job_columns = {
                str(column_row["name"])
                for column_row in connection.execute("PRAGMA table_info(report_job)").fetchall()
            }
            if "accepted_document_contract_json" not in job_columns:
                connection.execute(
                    "ALTER TABLE report_job ADD COLUMN accepted_document_contract_json TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS report_status_event (
                    status_event_id TEXT PRIMARY KEY,
                    report_job_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_schema_version TEXT NOT NULL DEFAULT 'report-status-event.v1',
                    event_family TEXT NOT NULL DEFAULT 'job_lifecycle',
                    event_payload_json TEXT NOT NULL DEFAULT '{}',
                    event_idempotency_key TEXT,
                    message TEXT,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    FOREIGN KEY(report_job_id) REFERENCES report_job(report_job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_status_event_job_created
                ON report_status_event(report_job_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS report_rerender_attempt (
                    rerender_attempt_id TEXT PRIMARY KEY,
                    report_job_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    previous_render_job_id TEXT,
                    previous_archive_document_id TEXT,
                    render_job_id TEXT NOT NULL,
                    render_output_format TEXT NOT NULL,
                    render_template_id TEXT NOT NULL,
                    render_template_version TEXT NOT NULL,
                    render_artifact_sha256 TEXT,
                    render_bounded_determinism_fingerprint TEXT,
                    render_runtime_engine TEXT,
                    render_runtime_engine_version TEXT,
                    render_duration_ms INTEGER,
                    archive_request_id TEXT,
                    archive_document_id TEXT,
                    archive_completed_at TEXT,
                    failure_category TEXT,
                    failure_message TEXT,
                    retry_eligible INTEGER NOT NULL,
                    requested_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(report_job_id, idempotency_key),
                    FOREIGN KEY(report_job_id) REFERENCES report_job(report_job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_rerender_attempt_job_created
                ON report_rerender_attempt(report_job_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS report_job_relationship (
                    relationship_id TEXT PRIMARY KEY,
                    source_report_job_id TEXT NOT NULL,
                    derived_report_job_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    source_status TEXT NOT NULL,
                    derived_status TEXT NOT NULL,
                    source_failure_category TEXT,
                    derived_failure_category TEXT,
                    archive_consequence TEXT,
                    previous_archive_document_id TEXT,
                    new_archive_document_id TEXT,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_report_job_id, derived_report_job_id, relationship_type),
                    FOREIGN KEY(source_report_job_id) REFERENCES report_job(report_job_id),
                    FOREIGN KEY(derived_report_job_id) REFERENCES report_job(report_job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_job_relationship_source_created
                ON report_job_relationship(source_report_job_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_job_relationship_derived_created
                ON report_job_relationship(derived_report_job_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS report_job_work_item (
                    work_item_id TEXT PRIMARY KEY,
                    report_job_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_acquired_at TEXT,
                    lease_expires_at TEXT,
                    last_error_category TEXT,
                    last_error_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(report_job_id) REFERENCES report_job(report_job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_job_work_runnable
                ON report_job_work_item(status, available_at, created_at)
                """
            )

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
        request: ReportJobRequest,
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
        # Template selection is an immutable job fact: a PDF-capable job is
        # stamped with its governed template id/version at ACCEPTANCE, so a
        # later deployment that changes the family default cannot change the
        # presentation contract this job was accepted under.
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

        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT report_request_id, request_hash
                    FROM report_request
                    WHERE tenant_id = ? AND idempotency_key = ?
                    """,
                    (caller_context.tenant_id, normalized_key),
                ).fetchone()
                if existing:
                    record = self._load_by_request_id(connection, existing["report_request_id"])
                    if existing[
                        "request_hash"
                    ] != request_hash and request_hash != client_identity_hash_from_record(record):
                        # Transitional acceptance: a record stored while ANY
                        # deployment's server-derived enrichment participated
                        # in the hash is compared by recomputing the CLIENT
                        # identity from its own persisted request - no
                        # dependency on reproducing historical enrichment, so
                        # the contract survives enrichment schema changes and
                        # removals alike. A changed business intent matches
                        # neither the stored hash nor the persisted client
                        # identity and still conflicts.
                        raise IdempotencyConflictError(
                            "idempotency_key_reused_with_different_request"
                        )
                    if enqueue:
                        self._ensure_work_item(connection, record=record)
                    return record

                replay_source_status: str | None = None
                replay_source_failure_category: str | None = None
                if replay_source_job_id is not None:
                    root_row = connection.execute(
                        """
                        WITH RECURSIVE ancestors(job_id) AS (
                            SELECT ?
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
                    source_row = connection.execute(
                        "SELECT status, failure_category, retry_eligible, "
                        "archive_document_id FROM report_job WHERE report_job_id = ?",
                        (replay_source_job_id,),
                    ).fetchone()
                    if not source_row:
                        raise ReportJobNotFoundError("report_job_not_found")
                    if (
                        source_row["status"] != "failed"
                        or not source_row["retry_eligible"]
                        or source_row["archive_document_id"]
                    ):
                        # The ledger lock serializes calls, not the service's
                        # resolver-then-create sequence: a concurrent resolver
                        # may have adopted a committed document meanwhile.
                        raise InvalidReportJobTransitionError("report_job_cannot_be_replayed")
                    replay_source_status = source_row["status"]
                    replay_source_failure_category = source_row["failure_category"]
                    live = connection.execute(
                        """
                        WITH RECURSIVE lineage(job_id) AS (
                            SELECT ?
                            UNION
                            SELECT rel.derived_report_job_id
                            FROM report_job_relationship rel
                            JOIN lineage ON rel.source_report_job_id = lineage.job_id
                            WHERE rel.relationship_type = 'failed_work_replay'
                        )
                        SELECT 1
                        FROM lineage
                        JOIN report_job member ON member.report_job_id = lineage.job_id
                        WHERE lineage.job_id != ?
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
                now_text = _dt_to_text(now)
                portfolio_scope_json = canonical_json(portfolio_scope)
                output_formats_json = canonical_json(sorted(output_formats))
                options_json = canonical_json(options)

                connection.execute(
                    """
                    INSERT INTO report_request (
                        report_request_id, report_type, portfolio_scope_json,
                        requested_output_formats_json, as_of_date, reporting_currency,
                        options_json, trigger_type, triggered_by, caller_application,
                        tenant_id, region, booking_center_code, role, idempotency_key,
                        request_hash, correlation_id, trace_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        report_type,
                        portfolio_scope_json,
                        output_formats_json,
                        as_of_date.isoformat(),
                        reporting_currency,
                        options_json,
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
                        now_text,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO report_job (
                        report_job_id, report_request_id, report_type, portfolio_scope_json,
                        status, failure_category, failure_message, current_step, retry_eligible,
                        cancel_requested, created_at, updated_at, started_at, completed_at,
                        cancelled_at, render_template_id, render_template_version,
                        accepted_document_contract_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        request_id,
                        report_type,
                        portfolio_scope_json,
                        "accepted",
                        None,
                        None,
                        "accepted",
                        0,
                        0,
                        now_text,
                        now_text,
                        None,
                        None,
                        None,
                        render_template_id,
                        render_template_version,
                        json.dumps(job_accepted_contract, sort_keys=True),
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
                        VALUES (?, ?, ?, 'failed_work_replay', ?, ?, ?, NULL,
                                NULL, NULL, NULL, ?, ?, ?, ?)
                        ON CONFLICT(source_report_job_id, derived_report_job_id, relationship_type)
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
                            _dt_to_text(now),
                            _dt_to_text(now),
                        ),
                    )
                if enqueue:
                    self._ensure_work_item(connection, record=record)
                return record

    def _ensure_work_item(
        self,
        connection: sqlite3.Connection,
        *,
        record: ReportJobLedgerRecord,
    ) -> None:
        if record.status in {"archived", "completed_with_warnings", "failed", "cancelled"}:
            return
        now_text = _dt_to_text(utc_now())
        connection.execute(
            """
            INSERT OR IGNORE INTO report_job_work_item (
                work_item_id, report_job_id, status, attempt_count, available_at,
                lease_owner, lease_token, lease_acquired_at, lease_expires_at,
                last_error_category, last_error_summary, created_at, updated_at, completed_at
            )
            VALUES (?, ?, 'pending', 0, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL)
            """,
            (f"rwork_{uuid4().hex}", record.job_id, now_text, now_text, now_text),
        )

    def get_work_item_for_job(self, job_id: str) -> ReportJobWorkItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM report_job_work_item WHERE report_job_id = ?",
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
        claimed_at_text = _dt_to_text(claimed_at)
        lease_expires_at_text = _dt_to_text(claimed_at + timedelta(seconds=lease_seconds))
        with self._lock:
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
                    WHERE status IN ('pending', 'retry_pending') AND available_at <= ?
                    ORDER BY available_at ASC, created_at ASC, work_item_id ASC
                    LIMIT ?
                    """,
                    (claimed_at_text, limit),
                ).fetchall()
                claimed: list[ReportJobWorkItem] = []
                for row in rows:
                    lease_token = f"rlease_{uuid4().hex}"
                    connection.execute(
                        """
                        UPDATE report_job_work_item
                        SET status = 'leased', attempt_count = attempt_count + 1,
                            lease_owner = ?, lease_token = ?, lease_acquired_at = ?,
                            lease_expires_at = ?, updated_at = ?
                        WHERE work_item_id = ?
                          AND status IN ('pending', 'retry_pending')
                        """,
                        (
                            worker_id,
                            lease_token,
                            claimed_at_text,
                            lease_expires_at_text,
                            claimed_at_text,
                            row["work_item_id"],
                        ),
                    )
                    claimed_row = connection.execute(
                        "SELECT * FROM report_job_work_item WHERE work_item_id = ?",
                        (row["work_item_id"],),
                    ).fetchone()
                    if claimed_row and claimed_row["lease_token"] == lease_token:
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
        completed_at_text = _dt_to_text(now or utc_now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE report_job_work_item
                SET status = 'completed', lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    updated_at = ?, completed_at = ?
                WHERE work_item_id = ? AND status = 'leased' AND lease_token = ?
                """,
                (completed_at_text, completed_at_text, work_item_id, lease_token),
            )
            if cursor.rowcount != 1:
                record_report_job_work_lease_event(outcome="stale_conflict")
                raise InvalidReportJobWorkTransitionError("report_job_work_lease_not_owned")
            row = connection.execute(
                "SELECT * FROM report_job_work_item WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if not row:
                raise InvalidReportJobWorkTransitionError("report_job_work_item_not_found")
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

        Waiting on owner-side work is not failure: the CLAIM charged an
        attempt, so deferral refunds it, keeping the budget scoped to real
        failures. One fenced UPDATE does everything - ownership check,
        refund, release - so no interleaving between a check and a write
        exists to race; a lease reclaimed by another worker simply fails
        the fence. Stale-work escalation is the OWNER's diagnostics
        contract, not a local poll count (report#303).
        """

        deferred_at = now or utc_now()
        with self._connect() as connection:
            deferred_rows = connection.execute(
                """
                UPDATE report_job_work_item
                SET status = 'retry_pending', lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    attempt_count = MAX(attempt_count - 1, 0),
                    available_at = ?, last_error_category = ?, last_error_summary = ?,
                    updated_at = ?
                WHERE work_item_id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    _dt_to_text(deferred_at + timedelta(seconds=max(delay_seconds, 1))),
                    wait_reason[:80],
                    "Waiting on owner-side work; the failure budget is untouched.",
                    _dt_to_text(deferred_at),
                    work_item_id,
                    lease_token,
                ),
            ).rowcount
            if deferred_rows != 1:
                record_report_job_work_lease_event(outcome="stale_conflict")
                raise InvalidReportJobWorkTransitionError("report_job_work_lease_not_owned")
            row = connection.execute(
                "SELECT * FROM report_job_work_item WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if not row:
                raise InvalidReportJobWorkTransitionError("report_job_work_item_not_found")
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
                WHERE work_item_id = ? AND status = 'leased' AND lease_token = ?
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
            # The ownership SELECT above and this UPDATE run under self._lock
            # inside ONE SQLite transaction: no interleaving can reclaim the
            # lease between them, so the SELECT's fencing is complete here.
            # (The PostgreSQL ledger fences its UPDATE too, because it has
            # no such process-wide lock.)
            connection.execute(
                """
                UPDATE report_job_work_item
                SET status = ?, lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    available_at = ?, last_error_category = ?, last_error_summary = ?,
                    updated_at = ?
                WHERE work_item_id = ?
                """,
                (
                    decision.status,
                    _dt_to_text(decision.available_at),
                    error_category[:80],
                    " ".join(error_summary.split())[:240],
                    _dt_to_text(failed_at),
                    work_item_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM report_job_work_item WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if not row:
                raise InvalidReportJobWorkTransitionError("report_job_work_item_not_found")
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
        connection: sqlite3.Connection,
        recovered_at: datetime,
        retry_policy: ReportJobWorkRetryPolicy,
    ) -> tuple[int, int]:
        recovered_at_text = _dt_to_text(recovered_at)
        recovered_count = 0
        exhausted_count = 0
        expired_rows = connection.execute(
            """
            SELECT work_item_id, attempt_count, lease_owner, lease_expires_at
            FROM report_job_work_item
            WHERE status = 'leased' AND lease_expires_at < ?
            ORDER BY lease_expires_at ASC, work_item_id ASC
            """,
            (recovered_at_text,),
        ).fetchall()
        for expired in expired_rows:
            lease_expired_at = _dt_from_text(expired["lease_expires_at"]) or recovered_at
            decision = decide_report_job_work_failure(
                attempt_count=int(expired["attempt_count"]),
                failed_at=lease_expired_at,
                retry_policy=retry_policy,
            )
            connection.execute(
                """
                UPDATE report_job_work_item
                SET status = ?, lease_owner = NULL, lease_token = NULL,
                    lease_acquired_at = NULL, lease_expires_at = NULL,
                    available_at = ?, last_error_category = 'expired_work_lease',
                    last_error_summary = 'Report job work lease expired before completion.',
                    updated_at = ?
                WHERE work_item_id = ? AND status = 'leased'
                """,
                (
                    decision.status,
                    _dt_to_text(decision.available_at),
                    recovered_at_text,
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
        connection: sqlite3.Connection,
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
            WHERE work.work_item_id = ?
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
            retry_eligible=1,
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
                "SELECT report_request_id FROM report_job WHERE report_job_id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                raise ReportJobNotFoundError("report_job_not_found")
            return self._load_by_request_id(connection, row["report_request_id"])

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

        records: list[ReportJobArchiveStatusRecord] = []
        with self._connect() as connection:
            for offset in range(0, len(unique_job_ids), 500):
                job_id_chunk = unique_job_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in job_id_chunk)
                rows = connection.execute(
                    f"""
                    SELECT
                        report_job.report_job_id,
                        report_job.status,
                        report_job.archive_document_id
                    FROM report_job
                    JOIN report_request
                      ON report_request.report_request_id = report_job.report_request_id
                    WHERE report_request.tenant_id = ?
                      AND report_job.report_job_id IN ({placeholders})
                    ORDER BY report_job.report_job_id ASC
                    """,
                    (tenant_id, *job_id_chunk),
                ).fetchall()
                records.extend(
                    ReportJobArchiveStatusRecord(
                        report_job_id=str(row["report_job_id"]),
                        status=row["status"],
                        archive_document_id=row["archive_document_id"],
                    )
                    for row in rows
                )
        return records

    def list_pending_archive_lineage(self, *, limit: int) -> list[PendingArchiveLineage]:
        """Jobs with lineage pairs still pending, oldest first, bounded.

        A pair is OUTSTANDING when its pending event has no recorded or
        refused outcome event for the same (source, target, transition)
        pair - matched by the pair suffix of the event idempotency keys, so
        settled history never re-enters the reconciliation pass.
        """

        with self._lock:
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
                    LIMIT ?
                    """,
                    (max(0, limit),),
                ).fetchall()
        return [
            PendingArchiveLineage(
                job_id=str(row["job_id"]),
                oldest_created_at=_dt_from_text(row["oldest_created_at"]),
            )
            for row in rows
        ]

    def list_status_events(self, job_id: str) -> list[ReportStatusEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM report_status_event
                WHERE report_job_id = ?
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
        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT relationship_id, created_at
                    FROM report_job_relationship
                    WHERE source_report_job_id = ?
                      AND derived_report_job_id = ?
                      AND relationship_type = ?
                    """,
                    (source_job.job_id, derived_job.job_id, relationship_type),
                ).fetchone()
                relationship_id = existing["relationship_id"] if existing else f"rjr_{uuid4().hex}"
                created_at = _dt_from_text(existing["created_at"]) if existing else now
                connection.execute(
                    """
                    INSERT INTO report_job_relationship (
                        relationship_id, source_report_job_id, derived_report_job_id,
                        relationship_type, source_status, derived_status,
                        source_failure_category, derived_failure_category,
                        archive_consequence, previous_archive_document_id,
                        new_archive_document_id, actor, reason, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_report_job_id, derived_report_job_id, relationship_type)
                    DO UPDATE SET
                        source_status = excluded.source_status,
                        derived_status = excluded.derived_status,
                        source_failure_category = excluded.source_failure_category,
                        derived_failure_category = excluded.derived_failure_category,
                        archive_consequence = excluded.archive_consequence,
                        previous_archive_document_id = excluded.previous_archive_document_id,
                        new_archive_document_id = excluded.new_archive_document_id,
                        actor = excluded.actor,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
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
                        _dt_to_text(created_at or now),
                        _dt_to_text(now),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM report_job_relationship WHERE relationship_id = ?",
                    (relationship_id,),
                ).fetchone()
                return _relationship_from_row(row)

    def list_job_relationships(self, job_id: str) -> list[ReportJobRelationshipRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM report_job_relationship
                WHERE source_report_job_id = ? OR derived_report_job_id = ?
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
                WHERE report_job_id = ?
                ORDER BY updated_at DESC, created_at DESC, rerender_attempt_id DESC
                LIMIT ?
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
        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT status FROM report_job WHERE report_job_id = ?",
                    (job_id,),
                ).fetchone()
                if not existing:
                    raise ReportJobNotFoundError("report_job_not_found")
                if (
                    skip_if_idempotency_key_exists
                    and event_idempotency_key
                    and connection.execute(
                        "SELECT 1 FROM report_status_event "
                        "WHERE report_job_id = ? AND event_idempotency_key = ?",
                        (job_id, event_idempotency_key),
                    ).fetchone()
                ):
                    # The duplicate check runs inside the same lock as the
                    # insert, so concurrent same-key retries converge on one
                    # event (the index on the key is non-unique by design).
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
                WHERE report_job_id = ?
                  AND status = 'failed'
                  AND failure_category IN (
                      'archive_storage_failed', 'archive_execution_failed',
                      'archive_outcome_unknown', 'archive_handoff_failed'
                  )
                  AND retry_eligible = 1
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
        key: the row carries the ADOPTED attempt's render identity (truthful
        provenance - this request's outcome IS that render) and is archived
        from birth, so a same-key retry of a lost adoption response converges
        on it instead of minting a fresh correction."""

        if not idempotency_key or not idempotency_key.strip():
            raise MissingIdempotencyKeyError("missing_idempotency_key")
        normalized_key = idempotency_key.strip()
        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT * FROM report_rerender_attempt
                    WHERE report_job_id = ? AND idempotency_key = ?
                    """,
                    (job.job_id, normalized_key),
                ).fetchone()
                if existing:
                    return _rerender_attempt_from_row(existing)
                now = utc_now()
                now_text = _dt_to_text(now)
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
                    VALUES (?, ?, ?, 'archived', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                            ?, ?, ?, ?, ?, ?)
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
                        now_text,
                        actor,
                        reason,
                        correlation_id,
                        trace_id,
                        now_text,
                        now_text,
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
                    "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = ?",
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
        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT *
                    FROM report_rerender_attempt
                    WHERE report_job_id = ? AND idempotency_key = ?
                    """,
                    (job.job_id, normalized_key),
                ).fetchone()
                if existing:
                    return _rerender_attempt_from_row(existing), False
                now = utc_now()
                now_text = _dt_to_text(now)
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        0,
                        actor,
                        reason,
                        correlation_id,
                        trace_id,
                        now_text,
                        now_text,
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
                    "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = ?",
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
                    message=(
                        f"Report rerender archived as correction document {archive_document_id}."
                    ),
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
        with self._lock:
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
        rows = self._list_job_rows(filters)
        records = [_record_from_row(row) for row in rows]
        # The Python matcher stays as the single semantic authority; the SQL
        # predicates exist so the LIMIT applies AFTER filtering - a tenant's
        # eligible row beyond other tenants' recent rows must still return.
        return [record for record in records if _record_matches_filters(record, filters)]

    def list_job_owner_snapshots(
        self, *, filters: ReportJobListFilters
    ) -> list[ReportJobOwnerSnapshot]:
        snapshots = [
            ReportJobOwnerSnapshot(
                record=_record_from_row(row),
                source_event_version=int(row["source_event_version"]),
            )
            for row in self._list_job_rows(filters)
        ]
        return [
            snapshot for snapshot in snapshots if _record_matches_filters(snapshot.record, filters)
        ]

    def _list_job_rows(self, filters: ReportJobListFilters) -> list[sqlite3.Row]:
        where_sql, where_params = _list_jobs_predicates(filters)
        with self._connect() as connection:
            rows = connection.execute(
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
                    job.cancelled_at
                    ,
                    job.render_job_id,
                    job.render_output_format,
                    job.render_template_id,
                    job.render_template_version,
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
                {where}
                ORDER BY job.created_at DESC, job.report_job_id DESC
                LIMIT ?
                """.format(where=where_sql),
                (*where_params, filters.limit),
            ).fetchall()
        return rows

    def mark_collecting_data(
        self,
        *,
        job_id: str,
        actor: str,
        correlation_id: str,
        trace_id: str,
    ) -> ReportJobLedgerRecord:
        with self._lock:
            with self._connect() as connection:
                return self._transition_job(
                    connection=connection,
                    job_id=job_id,
                    to_status="collecting_data",
                    failure_category=None,
                    failure_message=None,
                    current_step="collecting_data",
                    retry_eligible=0,
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
        with self._lock:
            with self._connect() as connection:
                return self._transition_job(
                    connection=connection,
                    job_id=job_id,
                    to_status="data_ready",
                    failure_category=None,
                    failure_message=None,
                    current_step="data_ready",
                    retry_eligible=0,
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
        with self._lock:
            with self._connect() as connection:
                return self._transition_job(
                    connection=connection,
                    job_id=job_id,
                    to_status="rendering",
                    failure_category=None,
                    failure_message=None,
                    current_step="rendering",
                    retry_eligible=0,
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
        with self._lock:
            with self._connect() as connection:
                return self._transition_job(
                    connection=connection,
                    job_id=job_id,
                    to_status="completed",
                    failure_category=None,
                    failure_message=None,
                    current_step="completed",
                    retry_eligible=0,
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
        with self._lock:
            with self._connect() as connection:
                return self._transition_job(
                    connection=connection,
                    job_id=job_id,
                    to_status="archiving",
                    failure_category=None,
                    failure_message=None,
                    current_step="archiving",
                    retry_eligible=0,
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
        with self._lock:
            with self._connect() as connection:
                return self._transition_job(
                    connection=connection,
                    job_id=job_id,
                    to_status="archived",
                    failure_category=None,
                    failure_message=None,
                    current_step="archived",
                    retry_eligible=0,
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
        with self._lock:
            with self._connect() as connection:
                return self._transition_job(
                    connection=connection,
                    job_id=job_id,
                    to_status="failed",
                    failure_category=failure_category,
                    failure_message=failure_message,
                    current_step="failed",
                    retry_eligible=1 if retry_eligible else 0,
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
        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT status FROM report_job WHERE report_job_id = ?",
                    (job_id,),
                ).fetchone()
                if not existing:
                    raise ReportJobNotFoundError("report_job_not_found")
                current_status = existing["status"]
                if not is_report_job_cancellable(str(current_status)):
                    raise InvalidReportJobTransitionError("report_job_cannot_be_cancelled")

                now = utc_now()
                now_text = _dt_to_text(now)
                connection.execute(
                    """
                    UPDATE report_job
                    SET status = ?, failure_category = ?, failure_message = ?, current_step = ?,
                        retry_eligible = ?, cancel_requested = ?, updated_at = ?, cancelled_at = ?
                    WHERE report_job_id = ?
                    """,
                    (
                        "cancelled",
                        "cancelled",
                        "Report job cancelled before render or archive processing.",
                        "cancelled",
                        0,
                        1,
                        now_text,
                        now_text,
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
                    "SELECT report_request_id FROM report_job WHERE report_job_id = ?",
                    (job_id,),
                ).fetchone()
                return self._load_by_request_id(connection, row["report_request_id"])

    def _append_status_event(
        self,
        *,
        connection: sqlite3.Connection,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rse_{uuid4().hex}",
                job_id,
                from_status,
                to_status,
                event_type,
                contract.schema_version,
                contract.event_family,
                canonical_json(contract.event_payload),
                contract.event_idempotency_key,
                message,
                actor,
                _dt_to_text(created_at),
                correlation_id,
                trace_id,
            ),
        )

    def _transition_job(
        self,
        *,
        connection: sqlite3.Connection,
        job_id: str,
        to_status: ReportJobStatus,
        failure_category: str | None,
        failure_message: str | None,
        current_step: str,
        retry_eligible: int,
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
            "SELECT status, started_at FROM report_job WHERE report_job_id = ?",
            (job_id,),
        ).fetchone()
        if not existing:
            raise ReportJobNotFoundError("report_job_not_found")
        current_status = str(existing["status"])
        if current_status == to_status:
            row = connection.execute(
                "SELECT report_request_id FROM report_job WHERE report_job_id = ?",
                (job_id,),
            ).fetchone()
            assert row is not None
            return self._load_by_request_id(connection, row["report_request_id"])
        if not is_report_job_transition_allowed(
            current_status=current_status,
            to_status=to_status,
        ):
            raise InvalidReportJobTransitionError("report_job_invalid_transition")

        now = utc_now()
        now_text = _dt_to_text(now)
        started_at = existing["started_at"] or (now_text if set_started_at else None)
        completed_at = now_text if set_completed_at else None
        connection.execute(
            """
            UPDATE report_job
            SET status = ?, failure_category = ?, failure_message = ?, current_step = ?,
                retry_eligible = ?, updated_at = ?, started_at = ?, completed_at = ?,
                render_job_id = COALESCE(?, render_job_id),
                render_output_format = COALESCE(?, render_output_format),
                render_template_id = COALESCE(?, render_template_id),
                render_template_version = COALESCE(?, render_template_version),
                render_template_publication = COALESCE(?, render_template_publication),
                render_artifact_sha256 = COALESCE(?, render_artifact_sha256),
                render_bounded_determinism_fingerprint = COALESCE(
                    ?,
                    render_bounded_determinism_fingerprint
                ),
                render_runtime_engine = COALESCE(?, render_runtime_engine),
                render_runtime_engine_version = COALESCE(?, render_runtime_engine_version),
                render_duration_ms = COALESCE(?, render_duration_ms),
                archive_request_id = COALESCE(?, archive_request_id),
                archive_document_id = COALESCE(?, archive_document_id),
                archive_completed_at = COALESCE(?, archive_completed_at)
            WHERE report_job_id = ?
            """,
            (
                to_status,
                failure_category,
                failure_message,
                current_step,
                retry_eligible,
                now_text,
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
                _dt_to_text(archive_completed_at) if archive_completed_at else None,
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
            "SELECT report_request_id FROM report_job WHERE report_job_id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        return self._load_by_request_id(connection, row["report_request_id"])

    def _update_rerender_attempt(
        self,
        *,
        connection: sqlite3.Connection,
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
            "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = ?",
            (rerender_attempt_id,),
        ).fetchone()
        if not existing:
            raise ReportJobNotFoundError("report_rerender_attempt_not_found")
        now_text = _dt_to_text(utc_now())
        connection.execute(
            """
            UPDATE report_rerender_attempt
            SET status = ?,
                render_job_id = COALESCE(?, render_job_id),
                render_artifact_sha256 = COALESCE(?, render_artifact_sha256),
                render_bounded_determinism_fingerprint = COALESCE(
                    ?,
                    render_bounded_determinism_fingerprint
                ),
                render_runtime_engine = COALESCE(?, render_runtime_engine),
                render_runtime_engine_version = COALESCE(?, render_runtime_engine_version),
                render_duration_ms = COALESCE(?, render_duration_ms),
                archive_request_id = COALESCE(?, archive_request_id),
                archive_document_id = COALESCE(?, archive_document_id),
                archive_completed_at = COALESCE(?, archive_completed_at),
                failure_category = CASE WHEN ? THEN NULL
                    ELSE COALESCE(?, failure_category) END,
                failure_message = CASE WHEN ? THEN NULL
                    ELSE COALESCE(?, failure_message) END,
                retry_eligible = CASE WHEN ? THEN 0
                    ELSE COALESCE(?, retry_eligible) END,
                updated_at = ?
            WHERE rerender_attempt_id = ?
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
                _dt_to_text(archive_completed_at) if archive_completed_at else None,
                clear_failure,
                failure_category,
                clear_failure,
                failure_message,
                clear_failure,
                1 if retry_eligible else 0 if retry_eligible is not None else None,
                now_text,
                rerender_attempt_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM report_rerender_attempt WHERE rerender_attempt_id = ?",
            (rerender_attempt_id,),
        ).fetchone()
        assert row is not None
        return _rerender_attempt_from_row(row)

    def _load_by_request_id(
        self,
        connection: sqlite3.Connection,
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
            WHERE req.report_request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if not row:
            raise ReportJobNotFoundError("report_job_not_found")
        return _record_from_row(row)


def _dt_to_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dt_from_text(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _work_item_from_row(row: sqlite3.Row) -> ReportJobWorkItem:
    return ReportJobWorkItem(
        work_item_id=row["work_item_id"],
        report_job_id=row["report_job_id"],
        status=row["status"],
        attempt_count=int(row["attempt_count"]),
        available_at=_dt_from_text(row["available_at"]) or utc_now(),
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_acquired_at=_dt_from_text(row["lease_acquired_at"]),
        lease_expires_at=_dt_from_text(row["lease_expires_at"]),
        last_error_category=row["last_error_category"],
        last_error_summary=row["last_error_summary"],
        created_at=_dt_from_text(row["created_at"]) or utc_now(),
        updated_at=_dt_from_text(row["updated_at"]) or utc_now(),
        completed_at=_dt_from_text(row["completed_at"]),
    )


def _optional_json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None


def _record_from_row(row: sqlite3.Row) -> ReportJobLedgerRecord:
    return ReportJobLedgerRecord(
        request_id=row["report_request_id"],
        job_id=row["report_job_id"],
        report_type=row["report_type"],
        portfolio_scope=json.loads(row["request_portfolio_scope_json"]),
        requested_output_formats=json.loads(row["requested_output_formats_json"]),
        as_of_date=date.fromisoformat(row["as_of_date"]),
        reporting_currency=row["reporting_currency"],
        options=json.loads(row["options_json"]),
        trigger_type=row["trigger_type"],
        triggered_by=row["triggered_by"],
        caller_application=row["caller_application"],
        tenant_id=row["tenant_id"],
        region=row["region"],
        booking_center_code=row["booking_center_code"],
        role=row["role"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        status=row["status"],
        failure_category=row["failure_category"],
        failure_message=row["failure_message"],
        current_step=row["current_step"],
        retry_eligible=bool(row["retry_eligible"]),
        cancel_requested=bool(row["cancel_requested"]),
        created_at=_dt_from_text(row["job_created_at"]) or utc_now(),
        updated_at=_dt_from_text(row["updated_at"]) or utc_now(),
        started_at=_dt_from_text(row["started_at"]),
        completed_at=_dt_from_text(row["completed_at"]),
        cancelled_at=_dt_from_text(row["cancelled_at"]),
        correlation_id=row["correlation_id"],
        trace_id=row["trace_id"],
        render_job_id=_optional_row_value(row, "render_job_id"),
        render_output_format=_optional_row_value(row, "render_output_format"),
        render_template_id=_optional_row_value(row, "render_template_id"),
        accepted_document_contract=_optional_json_object(
            _optional_row_value(row, "accepted_document_contract_json")
        ),
        render_template_version=_optional_row_value(row, "render_template_version"),
        render_template_publication=_optional_row_value(row, "render_template_publication"),
        render_artifact_sha256=_optional_row_value(row, "render_artifact_sha256"),
        render_bounded_determinism_fingerprint=_optional_row_value(
            row,
            "render_bounded_determinism_fingerprint",
        ),
        render_runtime_engine=_optional_row_value(row, "render_runtime_engine"),
        render_runtime_engine_version=_optional_row_value(row, "render_runtime_engine_version"),
        render_duration_ms=_optional_row_value(row, "render_duration_ms"),
        archive_request_id=_optional_row_value(row, "archive_request_id"),
        archive_document_id=_optional_row_value(row, "archive_document_id"),
        archive_completed_at=_dt_from_text(_optional_row_value(row, "archive_completed_at")),
    )


def _rerender_attempt_from_row(row: sqlite3.Row) -> ReportRerenderAttemptRecord:
    return ReportRerenderAttemptRecord(
        rerender_attempt_id=row["rerender_attempt_id"],
        report_job_id=row["report_job_id"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        snapshot_id=row["snapshot_id"],
        snapshot_hash=row["snapshot_hash"],
        previous_render_job_id=row["previous_render_job_id"],
        previous_archive_document_id=row["previous_archive_document_id"],
        render_job_id=row["render_job_id"],
        render_output_format=row["render_output_format"],
        render_template_id=row["render_template_id"],
        render_template_version=row["render_template_version"],
        render_artifact_sha256=row["render_artifact_sha256"],
        render_bounded_determinism_fingerprint=row["render_bounded_determinism_fingerprint"],
        render_runtime_engine=row["render_runtime_engine"],
        render_runtime_engine_version=row["render_runtime_engine_version"],
        render_duration_ms=row["render_duration_ms"],
        archive_request_id=row["archive_request_id"],
        archive_document_id=row["archive_document_id"],
        archive_completed_at=_dt_from_text(row["archive_completed_at"]),
        failure_category=row["failure_category"],
        failure_message=row["failure_message"],
        retry_eligible=bool(row["retry_eligible"]),
        requested_by=row["requested_by"],
        reason=row["reason"],
        correlation_id=row["correlation_id"],
        trace_id=row["trace_id"],
        created_at=_dt_from_text(row["created_at"]) or utc_now(),
        updated_at=_dt_from_text(row["updated_at"]) or utc_now(),
    )


def _relationship_from_row(row: sqlite3.Row) -> ReportJobRelationshipRecord:
    return ReportJobRelationshipRecord(
        relationship_id=row["relationship_id"],
        relationship_type=row["relationship_type"],
        source_report_job_id=row["source_report_job_id"],
        derived_report_job_id=row["derived_report_job_id"],
        source_status=row["source_status"],
        derived_status=row["derived_status"],
        source_failure_category=row["source_failure_category"],
        derived_failure_category=row["derived_failure_category"],
        archive_consequence=row["archive_consequence"],
        previous_archive_document_id=row["previous_archive_document_id"],
        new_archive_document_id=row["new_archive_document_id"],
        actor=row["actor"],
        reason=row["reason"],
        created_at=_dt_from_text(row["created_at"]) or utc_now(),
        updated_at=_dt_from_text(row["updated_at"]) or utc_now(),
    )


def _bounded_relationship_reason(reason: str) -> str:
    normalized = " ".join((reason or "").split())
    if not normalized:
        return "not_provided"
    return normalized[:240]


def _optional_row_value(row: sqlite3.Row, key: str) -> Any | None:
    keys = row.keys() if hasattr(row, "keys") else row
    if key not in keys:
        return None
    return row[key]


def _event_from_row(row: sqlite3.Row) -> ReportStatusEvent:
    event_type = row["event_type"]
    contract = legacy_report_status_event_contract(
        event_type=event_type,
        from_status=row["from_status"],
        to_status=row["to_status"],
    )
    event_schema_version = _optional_row_value(row, "event_schema_version")
    event_family = _optional_row_value(row, "event_family")
    event_payload_json = _optional_row_value(row, "event_payload_json")
    event_payload = (
        json.loads(event_payload_json)
        if isinstance(event_payload_json, str) and event_payload_json.strip()
        else contract.event_payload
    )
    return ReportStatusEvent(
        status_event_id=row["status_event_id"],
        report_job_id=row["report_job_id"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        event_type=event_type,
        event_schema_version=event_schema_version or contract.schema_version,
        event_family=event_family or contract.event_family,
        event_payload=event_payload,
        event_idempotency_key=_optional_row_value(row, "event_idempotency_key"),
        message=row["message"],
        actor=row["actor"],
        created_at=_dt_from_text(row["created_at"]) or utc_now(),
        correlation_id=row["correlation_id"],
        trace_id=row["trace_id"],
    )


def _transition_event_payload(
    *,
    current_step: str,
    failure_category: str | None,
    failure_message: str | None,
    render_job_id: str | None,
    render_output_format: str | None,
    render_template_id: str | None,
    render_template_version: str | None,
    render_artifact_sha256: str | None,
    render_bounded_determinism_fingerprint: str | None,
    render_runtime_engine: str | None,
    render_runtime_engine_version: str | None,
    render_duration_ms: int | None,
    archive_request_id: str | None,
    archive_document_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"current_step": current_step}
    optional_fields: dict[str, Any | None] = {
        "failure_category": failure_category,
        "failure_message": failure_message,
        "render_job_id": render_job_id,
        "render_output_format": render_output_format,
        "render_template_id": render_template_id,
        "render_template_version": render_template_version,
        "render_artifact_sha256": render_artifact_sha256,
        "render_bounded_determinism_fingerprint": render_bounded_determinism_fingerprint,
        "render_runtime_engine": render_runtime_engine,
        "render_runtime_engine_version": render_runtime_engine_version,
        "render_duration_ms": render_duration_ms,
        "archive_request_id": archive_request_id,
        "archive_document_id": archive_document_id,
    }
    payload.update({key: value for key, value in optional_fields.items() if value is not None})
    return payload


def _list_jobs_predicates(filters: ReportJobListFilters) -> tuple[str, list[object]]:
    """SQL predicates mirroring _record_matches_filters, applied BEFORE the
    LIMIT so pagination never silently drops eligible rows (report#292)."""

    clauses: list[str] = []
    params: list[object] = []
    for clause, value in (
        ("req.tenant_id = ?", filters.tenant_id),
        ("req.region = ?", filters.region),
        ("job.status = ?", filters.status),
        ("req.report_type = ?", filters.report_type),
        ("req.as_of_date = ?", filters.as_of_date),
        ("req.idempotency_key = ?", filters.idempotency_key),
        ("req.correlation_id = ?", filters.correlation_id),
    ):
        if value:
            clauses.append(clause)
            params.append(value)
    if filters.portfolio_id:
        # Exact membership in the scope's portfolio_ids: LIKE would treat _
        # and % in canonical ids as wildcards and match case-insensitively.
        clauses.append(
            """EXISTS (
                SELECT 1 FROM json_each(job.portfolio_scope_json, '$.portfolio_ids')
                WHERE json_each.value = ?
            )"""
        )
        params.append(filters.portfolio_id)
    # Creation bounds join the SQL predicates too: without them, same-tenant
    # jobs OUTSIDE the window could consume the limit and starve eligible
    # rows inside it. created_at is stored as the same ISO text
    # _dt_to_text writes, so lexicographic comparison is chronological.
    if filters.created_from:
        clauses.append("job.created_at >= ?")
        params.append(_dt_to_text(filters.created_from))
    if filters.created_to:
        clauses.append("job.created_at <= ?")
        params.append(_dt_to_text(filters.created_to))
    if not clauses:
        return "", []
    return "WHERE " + " AND ".join(clauses), params


def _record_matches_filters(record: ReportJobLedgerRecord, filters: ReportJobListFilters) -> bool:
    if filters.tenant_id and record.tenant_id != filters.tenant_id:
        return False
    if filters.region and record.region != filters.region:
        return False
    if filters.status and record.status != filters.status:
        return False
    if filters.report_type and record.report_type != filters.report_type:
        return False
    if filters.portfolio_id and (
        filters.portfolio_id not in record.portfolio_scope.get("portfolio_ids", [])
    ):
        return False
    if filters.as_of_date and record.as_of_date != filters.as_of_date:
        return False
    if filters.idempotency_key and record.idempotency_key != filters.idempotency_key:
        return False
    if filters.correlation_id and record.correlation_id != filters.correlation_id:
        return False
    if filters.created_from and record.created_at < filters.created_from:
        return False
    if filters.created_to and record.created_at > filters.created_to:
        return False
    return True
