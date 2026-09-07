"""Carry the SQLite intake ledger into PostgreSQL (report#326 slice 3).

This is the step that makes the backend flip decidable. Until every existing
record is in PostgreSQL, pointing a deployment at the new table starts it from
an empty intake ledger -- report rows surviving while the evidence that
validated them does not, which is the unverifiable-replay state the
materialization route refuses.

Three properties the transfer has to have, in the order they matter:

**Nothing is invented.** Rows are copied field by field from the SQLite file,
not rebuilt from the record type: `IdeaEvidenceIntakeRecord` does not model the
source identity columns, so reconstructing rows through it would silently drop
`conversion_intent_id`, `candidate_id`, the evidence packet identity and the
correlation ids. The two JSON payloads and the two instants change
representation -- TEXT to JSONB, TEXT to TIMESTAMPTZ -- and nothing else does.

**Interruption is survivable.** Each row commits on its own, so a run killed
half way leaves a prefix rather than a rollback, and re-running completes it.
`ON CONFLICT DO NOTHING` makes that safe.

**Skipping is not the same as matching.** `ON CONFLICT DO NOTHING` also skips a
row whose key already exists with *different* content, which would quietly
leave a corrupted target looking like a completed transfer. So the verification
pass compares content for every source row rather than counting rows, and a
single mismatch fails the transfer.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.idea_evidence_intake.service import as_utc_instant

#: Copied verbatim. Order matters: it is the INSERT's column order.
TRANSFERRED_COLUMNS = (
    "tenant_id",
    "idempotency_key",
    "intake_id",
    "payload_fingerprint",
    "response_json",
    "caller_context_json",
    "report_evidence_pack_id",
    "conversion_intent_id",
    "candidate_id",
    "evidence_packet_id",
    "evidence_content_fingerprint",
    "producer",
    "supportability_status",
    "accepted_at_utc",
    "created_at_utc",
    "correlation_id",
    "trace_id",
)

#: Compared after transfer. Every column that carries identity or evidence --
#: comparing only the fingerprint would pass a row whose stored response had
#: been altered, and comparing only counts would pass a skipped conflict.
VERIFIED_COLUMNS = tuple(
    column for column in TRANSFERRED_COLUMNS if column not in {"accepted_at_utc", "created_at_utc"}
)

_INSERT = f"""
INSERT INTO idea_evidence_intake ({", ".join(TRANSFERRED_COLUMNS)})
VALUES ({", ".join(["%s"] * len(TRANSFERRED_COLUMNS))})
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
"""


class IntakeTransferError(RuntimeError):
    """The target does not faithfully hold what the source had."""


@dataclass
class TransferReport:
    source_records: int = 0
    inserted: int = 0
    already_present: int = 0
    verified: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.mismatches and self.verified == self.source_records

    def summary(self) -> str:
        return (
            f"source={self.source_records} inserted={self.inserted} "
            f"already_present={self.already_present} verified={self.verified} "
            f"mismatches={len(self.mismatches)}"
        )


def transfer_intake_ledger(
    *,
    sqlite_path: Path | str,
    database_url: str,
    allow_missing_source: bool = False,
) -> TransferReport:
    """Copy every SQLite intake record into PostgreSQL and prove it arrived.

    Raises `IntakeTransferError` if any record is missing or differs. Safe to
    re-run: a completed transfer reports every record as already present and
    verifies them again, which is also how an operator confirms a cutover
    without changing anything.

    `allow_missing_source` accepts an absent ledger file as a verified
    zero-record transfer. Off by default and deliberately explicit: the file is
    created on the first request, so a deployment that has served none has no
    file, but "no records" and "wrong path" are the same observation from here.
    Conflating them silently is how a cutover moves nothing and reports
    success, so the operator states which one they are looking at.
    """
    report = TransferReport()
    if allow_missing_source and not Path(sqlite_path).exists():
        return report
    source_rows = list(_read_source_rows(sqlite_path))
    report.source_records = len(source_rows)

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        for row in source_rows:
            # One row per transaction: an interrupted run leaves a prefix that
            # a re-run completes, rather than discarding work already done.
            with connection.transaction():
                cursor = connection.execute(_INSERT, _insert_parameters(row))
            if cursor.rowcount == 1:
                report.inserted += 1
            else:
                report.already_present += 1

        for row in source_rows:
            mismatch = _verify_row(connection, row)
            if mismatch:
                report.mismatches.append(mismatch)
            else:
                report.verified += 1

    if not report.complete:
        raise IntakeTransferError(
            f"intake transfer incomplete: {report.summary()}; "
            f"first mismatch: {report.mismatches[0] if report.mismatches else 'none'}"
        )
    return report


def _read_source_rows(sqlite_path: Path | str) -> Iterator[Mapping[str, Any]]:
    path = Path(sqlite_path)
    if not path.exists():
        raise IntakeTransferError(f"intake ledger file not found: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM idea_evidence_intake ORDER BY created_at_utc, idempotency_key"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        yield _with_tenant({key: row[key] for key in row.keys()})


def _with_tenant(row: dict[str, Any]) -> dict[str, Any]:
    """The source row with its tenant established, or a refusal.

    A ledger file written before report#344 has no `tenant_id` column at all --
    the tenant was stored only inside `caller_context_json`, where nothing read
    it for identity. This lifts it out so the transferred row carries the
    attribution the target's primary key now requires.

    A row whose context holds no tenant is refused rather than defaulted. A
    defaulted tenant would make one tenant the owner of another's retained
    receipt, which is worse than the defect being repaired because the result
    would be indistinguishable from a genuine record. Refusing leaves the row
    in the source file, visible and attributable by an operator who can see the
    surrounding evidence.
    """

    existing = row.get("tenant_id")
    if isinstance(existing, str) and existing.strip():
        row["tenant_id"] = existing.strip()
        return row

    context = row.get("caller_context_json")
    tenant = None
    if isinstance(context, str) and context.strip():
        try:
            tenant = json.loads(context).get("tenant_id")
        except json.JSONDecodeError as exc:
            raise IntakeTransferError(
                f"{row.get('idempotency_key')!r}: caller_context_json is not valid JSON"
            ) from exc
    if not isinstance(tenant, str) or not tenant.strip():
        raise IntakeTransferError(
            f"{row.get('idempotency_key')!r}: no tenant in caller_context_json. "
            "Attribute this row deliberately before transferring it; the transfer "
            "will not default or discard it (report#344)."
        )
    row["tenant_id"] = tenant.strip()
    return row


def _insert_parameters(row: Mapping[str, Any]) -> Sequence[Any]:
    """Source values, with only the two representation changes applied."""
    values: list[Any] = []
    for column in TRANSFERRED_COLUMNS:
        value = row[column]
        if column in {"response_json", "caller_context_json"}:
            values.append(Jsonb(json.loads(value)))
        elif column in {"accepted_at_utc", "created_at_utc"}:
            values.append(_parse_instant(value))
        else:
            values.append(value)
    return values


def _parse_instant(value: str) -> datetime:
    """SQLite stored these as the writer's ISO text, with `Z` for UTC."""
    return as_utc_instant(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _verify_row(connection: psycopg.Connection[dict[str, Any]], row: Mapping[str, Any]) -> str:
    """Empty when the target row faithfully holds the source row."""
    key = row["idempotency_key"]
    tenant = row["tenant_id"]
    target = connection.execute(
        "SELECT * FROM idea_evidence_intake WHERE tenant_id = %s AND idempotency_key = %s",
        (tenant, key),
    ).fetchone()
    if target is None:
        return f"{tenant}/{key}: missing from target"

    for column in VERIFIED_COLUMNS:
        source_value = row[column]
        target_value = target[column]
        if column in {"response_json", "caller_context_json"}:
            # Compare parsed, not textual: JSONB does not preserve key order or
            # whitespace, so a textual comparison would report every row as
            # differing while the evidence is identical.
            if json.loads(source_value) != target_value:
                return f"{key}: {column} differs"
        elif source_value != target_value:
            return f"{key}: {column} differs (source={source_value!r} target={target_value!r})"

    for column in ("accepted_at_utc", "created_at_utc"):
        if _parse_instant(row[column]) != as_utc_instant(target[column]):
            return f"{key}: {column} differs as an instant"

    return ""


def main(argv: Sequence[str] | None = None) -> int:
    """CLI for the transfer, runnable inside the deployed image.

    Lives here rather than only in `scripts/` because the image ships `src/`
    and not `scripts/`, and the ledger being transferred is in a volume mounted
    into that image -- so the operator path has to be
    `python -m app.idea_evidence_intake.transfer`.
    """
    parser = argparse.ArgumentParser(description="Transfer the SQLite intake ledger to PostgreSQL.")
    parser.add_argument(
        "--sqlite-path",
        default=os.environ.get(
            "IDEA_EVIDENCE_INTAKE_LEDGER_PATH", "data/idea-evidence-intake.sqlite3"
        ),
        help="The SQLite intake ledger to read. In the deployed image this is under /app/data.",
    )
    parser.add_argument(
        "--allow-missing-source",
        action="store_true",
        help=(
            "Accept an absent ledger file as a verified zero-record transfer. "
            "For a deployment that has never accepted an intake -- state it "
            "deliberately, because an absent file is also what a wrong path "
            "looks like."
        ),
    )
    args = parser.parse_args(argv)

    database_url = os.environ.get("REPORT_JOB_LEDGER_DATABASE_URL")
    if not database_url:
        print("REPORT_JOB_LEDGER_DATABASE_URL is required to transfer the intake ledger.")
        return 1

    try:
        report = transfer_intake_ledger(
            sqlite_path=args.sqlite_path,
            database_url=database_url,
            allow_missing_source=args.allow_missing_source,
        )
    except IntakeTransferError as exc:
        print(f"Intake ledger transfer FAILED: {exc}")
        return 1

    print(f"Intake ledger transfer complete: {report.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
