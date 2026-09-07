from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping, Protocol

from app.idea_evidence_intake.materialization_contract import (
    IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION,
)
from app.idea_evidence_intake.models import (
    IdeaEvidencePackIntakeRequest,
    IdeaEvidencePackIntakeResponse,
    IdeaEvidencePackMaterializationRequest,
)
from app.idea_evidence_intake.recovery import recovery_identity_from_request
from app.idea_evidence_intake.retention_policy import IdeaEvidenceRetentionPolicy
from app.reporting_jobs.models import (
    DpmProofPackReportInput,
    ProofPackReportJobRequest,
    ReportCallerContext,
)

REPORT_IDEA_EVIDENCE_INTAKE_ROUTE = "POST /reports/idea-evidence-packs"
REPORT_IDEA_EVIDENCE_INTAKE_BLOCKERS = (
    "report_evidence_pack_live_materialization_proof_missing",
    "rendered_output_creation_missing",
    "archive_record_creation_missing",
    "client_publication_authority_blocked",
)
REPORT_IDEA_EVIDENCE_INTAKE_EVIDENCE_REFS = (
    "POST /reports/idea-evidence-packs",
    "contracts/idea-evidence-intake/lotus-report-idea-evidence-pack-intake.v1.json",
    "src/app/idea_evidence_intake/service.py",
    "src/app/routers/idea_evidence_intake.py",
    "tests/unit/test_idea_evidence_intake_service.py",
    "tests/integration/test_idea_evidence_intake_api.py",
)


class IdeaEvidenceIntakeConflictError(ValueError):
    pass


@dataclass(frozen=True)
class IdeaEvidenceIntakeRecord:
    #: The admitted tenant, which is half of this record's identity. An
    #: idempotency key is a value the *caller* chooses to name its own retry,
    #: so it is only unique within a caller. Keying on it alone made one
    #: tenant's choice of string constrain every other tenant's, and let a
    #: lookup return a record belonging to someone else (report#344).
    tenant_id: str
    intake_id: str
    idempotency_key: str
    payload_fingerprint: str
    response: IdeaEvidencePackIntakeResponse
    caller_context: dict[str, object]
    accepted_at_utc: datetime
    created_at_utc: datetime


class IdeaEvidenceIntakePort(Protocol):
    """What the intake route needs from a ledger, whatever backs it.

    Three methods, and `has_record` is not incidental: report#334 refuses a
    legacy replay that no prior intake validated, and that question must be
    answerable before `accept()` stores anything. A backend that cannot answer
    it cannot host this route.

    `tenant_id` is a required argument rather than something read out of the
    optional `caller_context`. An authority that a backend may or may not find
    is not an authority: making it a parameter means a ledger cannot be called
    without one, and a caller cannot supply it inside the business payload
    (report#344).
    """

    def accept(
        self,
        request: IdeaEvidencePackIntakeRequest,
        *,
        tenant_id: str,
        idempotency_key: str,
        accepted_at_utc: datetime | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        caller_context: ReportCallerContext | None = None,
    ) -> IdeaEvidencePackIntakeResponse: ...

    def has_record(self, *, tenant_id: str, idempotency_key: str) -> bool: ...

    def snapshot(self) -> Mapping[tuple[str, str], IdeaEvidenceIntakeRecord]: ...


def as_utc_instant(value: datetime) -> datetime:
    """The same instant, always aware and in UTC.

    A naive value is read as UTC, which is the rule the SQLite writer already
    applied on its way to storage. Applying it here instead means the record,
    both backends and the receipt carry one instant: PostgreSQL binds naive
    values to TIMESTAMPTZ using the session TimeZone, so leaving one naive made
    the stored instant depend on which engine wrote it and on how the server
    happened to be configured.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def build_intake_record(
    request: IdeaEvidencePackIntakeRequest,
    *,
    tenant_id: str,
    idempotency_key: str,
    payload_fingerprint: str,
    accepted_at_utc: datetime | None = None,
    correlation_id: str | None = None,
    caller_context: ReportCallerContext | None = None,
) -> IdeaEvidenceIntakeRecord:
    """The record a newly accepted request produces, independent of storage.

    Shared by the SQLite and PostgreSQL ledgers (report#326). What a request
    means is not a property of where it is kept, and duplicating this per engine
    is how the two would drift into disagreeing about the same intake.
    """
    accepted_at = as_utc_instant(accepted_at_utc) if accepted_at_utc else datetime.now(UTC)
    intake_id = _intake_id(tenant_id, idempotency_key, payload_fingerprint)
    response = IdeaEvidencePackIntakeResponse(
        intake_id=intake_id,
        intake_status="accepted",
        report_evidence_pack_id=request.report_evidence_pack_id,
        conversion_intent_id=request.conversion_intent_id,
        candidate_id=request.candidate_id,
        producer="lotus-idea",
        owned_product="lotus-report:ClientReportEvidencePack:v1",
        supportability_status="not_certified",
        route_existence_proven=True,
        materialization_proven=False,
        creates_report_job=False,
        creates_rendered_output=False,
        creates_archive_record=False,
        grants_client_publication_authority=False,
        remaining_blockers=REPORT_IDEA_EVIDENCE_INTAKE_BLOCKERS,
        evidence_refs=REPORT_IDEA_EVIDENCE_INTAKE_EVIDENCE_REFS,
        accepted_at_utc=accepted_at,
        correlation_id=correlation_id,
    )
    return IdeaEvidenceIntakeRecord(
        tenant_id=tenant_id,
        intake_id=intake_id,
        idempotency_key=idempotency_key,
        payload_fingerprint=payload_fingerprint,
        response=response,
        caller_context=caller_context.model_dump(mode="json") if caller_context else {},
        accepted_at_utc=accepted_at,
        created_at_utc=datetime.now(UTC),
    )


class IdeaEvidenceIntakeMigrationError(RuntimeError):
    """A retained ledger file could not be carried onto the tenant-scoped schema."""


#: One definition, used both to create a fresh table and to rebuild an existing
#: one. Two copies would be free to disagree, and the rebuild's copy is the one
#: nobody looks at until a deployment with retained data is upgraded.
_TENANT_SCOPED_DDL = """
CREATE TABLE IF NOT EXISTS idea_evidence_intake (
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    intake_id TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    caller_context_json TEXT NOT NULL,
    report_evidence_pack_id TEXT NOT NULL,
    conversion_intent_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    evidence_packet_id TEXT NOT NULL,
    evidence_content_fingerprint TEXT NOT NULL,
    producer TEXT NOT NULL,
    supportability_status TEXT NOT NULL,
    accepted_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    correlation_id TEXT,
    trace_id TEXT,
    PRIMARY KEY (tenant_id, idempotency_key)
)
"""


class IdeaEvidenceIntakeLedger:
    def __init__(self, database_path: Path | str | None = None) -> None:
        self._database_path = Path(database_path) if database_path is not None else None
        self._records_by_key: dict[tuple[str, str], IdeaEvidenceIntakeRecord] = {}
        if self._database_path is not None:
            self._ensure_schema()

    def accept(
        self,
        request: IdeaEvidencePackIntakeRequest,
        *,
        tenant_id: str,
        idempotency_key: str,
        accepted_at_utc: datetime | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        caller_context: ReportCallerContext | None = None,
    ) -> IdeaEvidencePackIntakeResponse:
        payload_fingerprint = payload_fingerprint_of(request)
        existing = self._get_record(tenant_id, idempotency_key)
        if existing:
            if existing.payload_fingerprint != payload_fingerprint:
                raise IdeaEvidenceIntakeConflictError("idea evidence intake payload changed")
            return existing.response

        record = build_intake_record(
            request,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            payload_fingerprint=payload_fingerprint,
            accepted_at_utc=accepted_at_utc,
            correlation_id=correlation_id,
            caller_context=caller_context,
        )
        stored_record = self._store_record(
            record,
            request=request,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )
        return stored_record.response

    def has_record(self, *, tenant_id: str, idempotency_key: str) -> bool:
        """Whether this ledger holds a prior intake for that tenant and key.

        Used to establish that a legacy replay was genuinely validated against
        a stored record. An emptied or restored-from-elsewhere ledger accepts a
        request as new, so its acceptance proves nothing about a replay.

        Scoped by tenant because this gates a refusal. Unscoped, one tenant's
        history satisfied another tenant's check and the fail-closed refusal
        added by report#334 was skipped on evidence the caller had no
        relationship to (report#344).
        """
        return self._get_record(tenant_id, idempotency_key) is not None

    def snapshot(self) -> Mapping[tuple[str, str], IdeaEvidenceIntakeRecord]:
        """Every record, keyed by its full identity.

        Keyed by ``(tenant_id, idempotency_key)`` rather than the key alone, so
        a reader cannot address a record without saying whose it is.
        """
        if self._database_path is None:
            return MappingProxyType(dict(self._records_by_key))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM idea_evidence_intake
                ORDER BY created_at_utc, tenant_id, idempotency_key
                """
            ).fetchall()
        return MappingProxyType(
            {
                (str(row["tenant_id"]), str(row["idempotency_key"])): _record_from_row(row)
                for row in rows
            }
        )

    def _get_record(self, tenant_id: str, idempotency_key: str) -> IdeaEvidenceIntakeRecord | None:
        if self._database_path is None:
            return self._records_by_key.get((tenant_id, idempotency_key))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM idea_evidence_intake WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key),
            ).fetchone()
        return _record_from_row(row) if row else None

    def _store_record(
        self,
        record: IdeaEvidenceIntakeRecord,
        *,
        request: IdeaEvidencePackIntakeRequest,
        correlation_id: str | None,
        trace_id: str | None,
    ) -> IdeaEvidenceIntakeRecord:
        if self._database_path is None:
            self._records_by_key[(record.tenant_id, record.idempotency_key)] = record
            return record

        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO idea_evidence_intake (
                        tenant_id,
                        idempotency_key,
                        intake_id,
                        payload_fingerprint,
                        response_json,
                        caller_context_json,
                        report_evidence_pack_id,
                        conversion_intent_id,
                        candidate_id,
                        evidence_packet_id,
                        evidence_content_fingerprint,
                        producer,
                        supportability_status,
                        accepted_at_utc,
                        created_at_utc,
                        correlation_id,
                        trace_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.tenant_id,
                        record.idempotency_key,
                        record.intake_id,
                        record.payload_fingerprint,
                        record.response.model_dump_json(),
                        json.dumps(record.caller_context, sort_keys=True, separators=(",", ":")),
                        request.report_evidence_pack_id,
                        request.conversion_intent_id,
                        request.candidate_id,
                        request.evidence_packet_id,
                        request.evidence_content_fingerprint,
                        request.producer,
                        request.supportability_status,
                        _dt_to_text(record.accepted_at_utc),
                        _dt_to_text(record.created_at_utc),
                        correlation_id,
                        trace_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = self._get_record(record.tenant_id, record.idempotency_key)
                if existing and existing.payload_fingerprint == record.payload_fingerprint:
                    return existing
                raise IdeaEvidenceIntakeConflictError(
                    "idea evidence intake payload changed"
                ) from exc
        return record

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        assert self._database_path is not None
        if self._database_path != Path(":memory:"):
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate_to_tenant_identity(self, connection: sqlite3.Connection) -> None:
        """Carry a pre-report#344 ledger file onto the tenant-scoped schema.

        `CREATE TABLE IF NOT EXISTS` below is a no-op against an existing file,
        so without this an already-deployed SQLite ledger keeps the old table
        and the first tenant-scoped read fails with
        `no such column: tenant_id` -- every existing deployment unusable after
        rollout rather than migrated.

        Mirrors migration 025 for PostgreSQL, including its refusal: a row whose
        stored caller context holds no tenant is not defaulted and not dropped.
        SQLite cannot alter a primary key in place, so the table is rebuilt and
        the rows copied inside the caller's transaction -- an interrupted run
        leaves the original table untouched.
        """

        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'idea_evidence_intake'"
        ).fetchone()
        if table is None:
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(idea_evidence_intake)")}
        if "tenant_id" in columns:
            return

        unattributed = connection.execute(
            """
            SELECT idempotency_key FROM idea_evidence_intake
            WHERE COALESCE(TRIM(json_extract(caller_context_json, '$.tenant_id')), '') = ''
            """
        ).fetchall()
        if unattributed:
            keys = ", ".join(repr(str(row[0])) for row in unattributed[:5])
            raise IdeaEvidenceIntakeMigrationError(
                f"{len(unattributed)} intake row(s) have no tenant in caller_context_json "
                f"(for example {keys}). Attribute them deliberately before starting with the "
                "tenant-scoped schema; this migration will not default or discard them "
                "(report#344)."
            )

        connection.execute(
            "ALTER TABLE idea_evidence_intake RENAME TO idea_evidence_intake_pre_344"
        )
        connection.execute(_TENANT_SCOPED_DDL)
        connection.execute(
            """
            INSERT INTO idea_evidence_intake (
                tenant_id, idempotency_key, intake_id, payload_fingerprint, response_json,
                caller_context_json, report_evidence_pack_id, conversion_intent_id, candidate_id,
                evidence_packet_id, evidence_content_fingerprint, producer, supportability_status,
                accepted_at_utc, created_at_utc, correlation_id, trace_id
            )
            SELECT
                TRIM(json_extract(caller_context_json, '$.tenant_id')),
                idempotency_key, intake_id, payload_fingerprint, response_json,
                caller_context_json, report_evidence_pack_id, conversion_intent_id, candidate_id,
                evidence_packet_id, evidence_content_fingerprint, producer, supportability_status,
                accepted_at_utc, created_at_utc, correlation_id, trace_id
            FROM idea_evidence_intake_pre_344
            """
        )
        carried = connection.execute("SELECT count(*) FROM idea_evidence_intake").fetchone()[0]
        original = connection.execute(
            "SELECT count(*) FROM idea_evidence_intake_pre_344"
        ).fetchone()[0]
        if carried != original:
            raise IdeaEvidenceIntakeMigrationError(
                f"carried {carried} of {original} intake row(s); refusing to drop the original"
            )
        connection.execute("DROP TABLE idea_evidence_intake_pre_344")

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            self._migrate_to_tenant_identity(connection)
            connection.execute(_TENANT_SCOPED_DDL)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_idea_evidence_intake_source
                ON idea_evidence_intake(report_evidence_pack_id, evidence_packet_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_idea_evidence_intake_created
                ON idea_evidence_intake(created_at_utc)
                """
            )


def payload_fingerprint_of(request: IdeaEvidencePackIntakeRequest) -> str:
    """The fingerprint that decides replay from conflict.

    Public because the SQLite and PostgreSQL ledgers must compute it
    identically: a request that replays on one engine and conflicts on the
    other would make the store an input to the decision (report#326).
    """
    payload = request.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _record_from_row(row: sqlite3.Row) -> IdeaEvidenceIntakeRecord:
    response = IdeaEvidencePackIntakeResponse.model_validate_json(row["response_json"])
    return IdeaEvidenceIntakeRecord(
        intake_id=str(row["intake_id"]),
        tenant_id=str(row["tenant_id"]),
        idempotency_key=str(row["idempotency_key"]),
        payload_fingerprint=str(row["payload_fingerprint"]),
        response=response,
        caller_context=json.loads(row["caller_context_json"]),
        accepted_at_utc=_dt_from_text(row["accepted_at_utc"]),
        created_at_utc=_dt_from_text(row["created_at_utc"]),
    )


def _dt_to_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dt_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _intake_id(tenant_id: str, idempotency_key: str, payload_fingerprint: str) -> str:
    """The intake identity, derived from the tenant as well as the key.

    Length-prefixed rather than ``:``-joined. The idempotency key is caller
    supplied and may contain any character, so a plain separator makes the
    preimage ambiguous: ``("a:b", "c")`` and ``("a", "b:c")`` produce the same
    string and therefore the same identity. Adding the tenant to a ``:``-joined
    preimage would have reintroduced exactly the cross-tenant collision this
    change exists to remove -- one tenant could choose a key that made its
    intake collide with another's (report#344).

    Prefixing each component with its length makes the encoding injective, so
    distinct triples cannot share a digest.

    Records written before this change retain their stored ``intake_id``; this
    derivation applies to intakes accepted from here on.
    """

    parts = (tenant_id, idempotency_key, payload_fingerprint)
    preimage = "".join(f"{len(part)}:{part}" for part in parts)
    digest = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return "idea_intake_" + digest[:24]


def build_proof_pack_report_job_request_from_idea_evidence(
    request: IdeaEvidencePackMaterializationRequest,
    *,
    retention_policy: IdeaEvidenceRetentionPolicy | None = None,
) -> ProofPackReportJobRequest:
    evidence_pack = request.idea_evidence_pack
    proof_pack_input = {
        "contract_version": "1.0",
        "source_contract_version": "lotus_idea_evidence_pack_report_input.v1",
        "proof_pack_id": evidence_pack.report_evidence_pack_id,
        "proof_pack_content_hash": evidence_pack.evidence_content_fingerprint,
        "portfolio_id": request.portfolio_id,
        "mandate_id": request.mandate_id or "not_available",
        "as_of_date": request.as_of_date,
        "generated_at": evidence_pack.requested_at_utc.isoformat(),
        "report_title": f"Idea Evidence Pack - {evidence_pack.report_evidence_pack_id}",
        "report_audience": ["advisor", "investment_control", "audit"],
        "state": "READY_FOR_REPORT_MATERIALIZATION",
        "decision_summary": {
            "recommended_action": "review_opportunity_evidence",
            "rationale": ", ".join(evidence_pack.reason_codes),
        },
        "supportability": {
            "status": "READY",
            "reason_codes": tuple(evidence_pack.reason_codes),
        },
        "sections": _source_summary_sections(evidence_pack),
        "markdown_summary": _markdown_summary(evidence_pack),
        "source_hashes": {
            "idea_evidence_packet": evidence_pack.evidence_content_fingerprint,
        },
        "redaction_policy": "NO_RAW_PAYLOADS",
        "retention_policy": evidence_pack.retention_policy_ref,
        "evidence_ref": {
            "source_system": "lotus-idea",
            "source_type": "LOTUS_IDEA_EVIDENCE_PACK_REPORT_INPUT",
            "source_id": (
                f"{evidence_pack.report_evidence_pack_id}:lotus_idea_evidence_pack_report_input"
            ),
            "content_hash": evidence_pack.evidence_content_fingerprint,
        },
        "content_hash": evidence_pack.evidence_content_fingerprint,
        "source_lineage": [
            {
                "source_system": "lotus-idea",
                "source_type": "IdeaEvidencePacket",
                "source_id": evidence_pack.evidence_packet_id,
                "content_hash": evidence_pack.evidence_content_fingerprint,
            }
        ],
        "client_publication_authority_granted": False,
    }
    options = dict(request.options)
    # Reserved, server-derived identity used for read-only lost-response recovery.
    # A caller-supplied value under this key is deliberately replaced.
    options[IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION] = recovery_identity_from_request(
        request
    ).model_dump(mode="json")
    if retention_policy is not None:
        options["retention_policy"] = {
            "policy_ref": retention_policy.policy_ref,
            "policy_version": retention_policy.policy_version,
            "retention_start_event": retention_policy.retention_start_event,
            "retention_duration_days": retention_policy.retention_duration_days,
            "approval_authority": retention_policy.approval_authority,
            "residency_region": retention_policy.residency_region,
            "legal_hold_active": retention_policy.legal_hold_active,
            "erasure_action": retention_policy.erasure_action,
            "archive_handoff_policy": retention_policy.archive_handoff_policy,
        }
    return ProofPackReportJobRequest(
        proof_pack_report_input=DpmProofPackReportInput.model_validate(proof_pack_input),
        requested_output_formats=request.requested_output_formats,
        reporting_currency=request.reporting_currency,
        options=options,
    )


def _source_summary_sections(
    request: IdeaEvidencePackIntakeRequest,
) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    for index, summary in enumerate(request.source_summaries, start=1):
        section_id = f"idea_source_{index}"
        sections.append(
            {
                "section_id": section_id,
                "section_type": "IDEA_SOURCE_EVIDENCE",
                "state": "READY",
                "title": f"{summary.source_system} evidence summary",
                "summary": (
                    f"{summary.product_id} {summary.product_version} as of "
                    f"{summary.as_of_date}: {summary.data_quality_status}, "
                    f"{summary.freshness}."
                ),
                "reason_codes": tuple(request.reason_codes),
                "facts": {},
                "metrics": {},
                "evidence_refs": [
                    {
                        "source_system": summary.source_system,
                        "product_id": summary.product_id,
                        "product_version": summary.product_version,
                        "as_of_date": summary.as_of_date,
                    }
                ],
                "source_refs": [
                    {
                        "source_system": summary.source_system,
                        "product_id": summary.product_id,
                    }
                ],
                "content_hash": request.evidence_content_fingerprint,
            }
        )
    return sections


def _markdown_summary(request: IdeaEvidencePackIntakeRequest) -> str:
    reason_codes = ", ".join(request.reason_codes)
    source_count = len(request.source_summaries)
    return (
        "# Idea Evidence Pack\n\n"
        f"- Report evidence pack: {request.report_evidence_pack_id}\n"
        f"- Evidence packet: {request.evidence_packet_id}\n"
        f"- Source summary count: {source_count}\n"
        f"- Reason codes: {reason_codes}\n"
        "- Client publication authority: blocked\n"
    )
