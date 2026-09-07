"""PostgreSQL-backed Idea evidence intake ledger (report#326 slice 2).

The intake ledger is the last production Report store on SQLite. Keeping it
there leaves its durability independent of the PostgreSQL rows describing the
same request, and those two can diverge: report#334 had to add a fail-closed
refusal for the state where a pre-identity report row survives while the SQLite
intake ledger has been lost or reset. Co-locating them removes that class --
one transaction, one backup, one restore.

This ledger presents the same surface as the SQLite one and shares
`build_intake_record`, so what a request *means* cannot drift between engines.
Only persistence and its conflict signal differ.

Not wired to production by default. `REPORT_IDEA_EVIDENCE_INTAKE_LEDGER_BACKEND`
selects it, and SQLite remains the default until the transfer of existing
records is delivered and accepted (slice 3). Switching a live deployment before
then would silently start from an empty ledger, which is exactly the
unverifiable-replay state #334 refuses.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from psycopg import Connection
from psycopg import errors as psycopg_errors
from psycopg.types.json import Jsonb

from app.idea_evidence_intake.models import (
    IdeaEvidencePackIntakeRequest,
    IdeaEvidencePackIntakeResponse,
)
from app.idea_evidence_intake.service import (
    IdeaEvidenceIntakeConflictError,
    IdeaEvidenceIntakeRecord,
    build_intake_record,
    payload_fingerprint_of,
)
from app.postgres import PostgresConnectionProvider
from app.reporting_jobs.models import ReportCallerContext

_INSERT = """
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
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class PostgresIdeaEvidenceIntakeLedger:
    """Same ledger, different store. Schema comes from migration 024."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        connection_provider: PostgresConnectionProvider | None = None,
    ) -> None:
        if connection_provider is None:
            if database_url is None:
                raise ValueError("idea_evidence_intake_ledger_database_url_required")
            connection_provider = PostgresConnectionProvider(database_url=database_url)
            self._owns_connection_provider = True
        else:
            self._owns_connection_provider = False
        self._connection_provider = connection_provider

    @contextmanager
    def _connect(self) -> Iterator[Connection[Mapping[str, Any]]]:
        with self._connection_provider.connection() as connection:
            yield connection

    def close(self) -> None:
        if self._owns_connection_provider:
            self._connection_provider.close()

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
        return self._store_record(
            record,
            request=request,
            correlation_id=correlation_id,
            trace_id=trace_id,
        ).response

    def has_record(self, *, tenant_id: str, idempotency_key: str) -> bool:
        """Whether this ledger held a prior intake under that key.

        Read before `accept()` stores anything, so that a legacy replay nothing
        validated can be refused without the refused attempt becoming the
        history that excuses its retry (report#334).
        """
        return self._get_record(tenant_id, idempotency_key) is not None

    def snapshot(self) -> Mapping[tuple[str, str], IdeaEvidenceIntakeRecord]:
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
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM idea_evidence_intake WHERE tenant_id = %s AND idempotency_key = %s",
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
        try:
            with self._connect() as connection:
                connection.execute(
                    _INSERT,
                    (
                        record.tenant_id,
                        record.idempotency_key,
                        record.intake_id,
                        record.payload_fingerprint,
                        Jsonb(json.loads(record.response.model_dump_json())),
                        Jsonb(record.caller_context),
                        request.report_evidence_pack_id,
                        request.conversion_intent_id,
                        request.candidate_id,
                        request.evidence_packet_id,
                        request.evidence_content_fingerprint,
                        request.producer,
                        request.supportability_status,
                        record.accepted_at_utc,
                        record.created_at_utc,
                        correlation_id,
                        trace_id,
                    ),
                )
        except psycopg_errors.UniqueViolation as exc:
            # Two requests raced on the same key. The winner's record decides:
            # an identical payload replays, a different one conflicts. Same
            # resolution the SQLite ledger makes on IntegrityError, so a
            # concurrent replay behaves identically on either engine.
            existing = self._get_record(record.tenant_id, record.idempotency_key)
            if existing and existing.payload_fingerprint == record.payload_fingerprint:
                return existing
            raise IdeaEvidenceIntakeConflictError("idea evidence intake payload changed") from exc
        return record


def _record_from_row(row: Mapping[str, Any]) -> IdeaEvidenceIntakeRecord:
    """Rebuild a record from a PostgreSQL row.

    Deliberately not the SQLite mapper. JSONB comes back already parsed and
    TIMESTAMPTZ as an aware datetime, where SQLite returns strings for both --
    so reusing that mapper would parse already-parsed values and re-parse
    datetimes that never were text.
    """
    return IdeaEvidenceIntakeRecord(
        intake_id=str(row["intake_id"]),
        tenant_id=str(row["tenant_id"]),
        idempotency_key=str(row["idempotency_key"]),
        payload_fingerprint=str(row["payload_fingerprint"]),
        response=IdeaEvidencePackIntakeResponse.model_validate(row["response_json"]),
        caller_context=dict(row["caller_context_json"]),
        accepted_at_utc=_as_utc(row["accepted_at_utc"]),
        created_at_utc=_as_utc(row["created_at_utc"]),
    )


def _as_utc(value: datetime) -> datetime:
    """TIMESTAMPTZ returns an aware datetime in the session timezone.

    Normalised to UTC so records compare and serialise identically to the
    SQLite ledger's, which stores an explicit `Z` string.
    """
    return value.astimezone(UTC)
