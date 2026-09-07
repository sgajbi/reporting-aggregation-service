"""A ledger file written before report#344 must be carried, not broken.

The SQLite ledger creates its table with `CREATE TABLE IF NOT EXISTS`, which is
a no-op against a file that already has one. Adding `tenant_id` to the schema
without an in-place migration therefore left every existing deployment with the
old table and a first read of `no such column: tenant_id` -- unusable after
rollout rather than migrated.

Reproduced before fixing: opening a pre-344 file and calling `has_record`
raised `sqlite3.OperationalError: no such column: tenant_id`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.idea_evidence_intake.models import IdeaEvidencePackIntakeResponse
from app.idea_evidence_intake.service import (
    REPORT_IDEA_EVIDENCE_INTAKE_BLOCKERS,
    REPORT_IDEA_EVIDENCE_INTAKE_EVIDENCE_REFS,
    IdeaEvidenceIntakeLedger,
    IdeaEvidenceIntakeMigrationError,
)

#: The schema exactly as it was before this change: the caller's idempotency
#: key alone as the primary key, and no tenant column at all.
_PRE_344_DDL = """
CREATE TABLE idea_evidence_intake (
    idempotency_key TEXT PRIMARY KEY,
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
    trace_id TEXT
)
"""


def _stored_response() -> str:
    return IdeaEvidencePackIntakeResponse(
        intake_id="idea_intake_written_before_344",
        intake_status="accepted",
        report_evidence_pack_id="irep_001",
        conversion_intent_id="icnv_001",
        candidate_id="icand_001",
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
        accepted_at_utc=datetime(2026, 6, 24, 8, 30, tzinfo=UTC),
        correlation_id=None,
    ).model_dump_json()


def _pre_344_file(path: Path, caller_context: dict[str, object]) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(_PRE_344_DDL)
        connection.execute(
            "INSERT INTO idea_evidence_intake VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "a-key-chosen-before-344",
                "idea_intake_written_before_344",
                "sha256:retained",
                _stored_response(),
                json.dumps(caller_context),
                "irep_001",
                "icnv_001",
                "icand_001",
                "ievp_001",
                "sha256:evidence",
                "lotus-idea",
                "not_certified",
                "2026-06-24T08:30:00Z",
                "2026-06-24T08:30:00Z",
                None,
                None,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_a_retained_row_is_carried_and_becomes_addressable_by_its_tenant(tmp_path) -> None:
    """The tenant was already stored -- in the caller context -- but unread."""

    path = _pre_344_file(
        tmp_path / "pre-344.sqlite3",
        {"tenant_id": "tenant-legacy", "triggered_by": "advisor-123"},
    )

    ledger = IdeaEvidenceIntakeLedger(path)
    stored = ledger.snapshot()

    assert list(stored) == [("tenant-legacy", "a-key-chosen-before-344")]
    assert (
        ledger.has_record(tenant_id="tenant-legacy", idempotency_key="a-key-chosen-before-344")
        is True
    )


def test_a_carried_row_is_not_visible_to_another_tenant(tmp_path) -> None:
    path = _pre_344_file(tmp_path / "pre-344.sqlite3", {"tenant_id": "tenant-legacy"})

    ledger = IdeaEvidenceIntakeLedger(path)

    assert (
        ledger.has_record(
            tenant_id="tenant-someone-else", idempotency_key="a-key-chosen-before-344"
        )
        is False
    )


def test_the_retained_receipt_keeps_its_original_intake_id(tmp_path) -> None:
    """A consumer may replay against this. Re-deriving it would break that.

    `_intake_id` now includes the tenant, so recomputing would hand back an
    identity the caller has never seen for a receipt it already holds.
    """

    path = _pre_344_file(tmp_path / "pre-344.sqlite3", {"tenant_id": "tenant-legacy"})

    record = IdeaEvidenceIntakeLedger(path).snapshot()[("tenant-legacy", "a-key-chosen-before-344")]

    assert record.intake_id == "idea_intake_written_before_344"
    assert record.response.intake_id == "idea_intake_written_before_344"


def test_an_unattributable_row_is_refused_and_left_in_place(tmp_path) -> None:
    """Neither defaulted nor dropped.

    A defaulted tenant would make one tenant the owner of another's retained
    receipt, and the result would be indistinguishable from a genuine record.
    Dropping destroys a receipt a consumer may still replay against. So the
    migration refuses, names the key, and leaves the original table untouched
    for an operator who can see the surrounding evidence.
    """

    path = _pre_344_file(tmp_path / "pre-344.sqlite3", {"triggered_by": "advisor-123"})

    with pytest.raises(IdeaEvidenceIntakeMigrationError) as exc:
        IdeaEvidenceIntakeLedger(path)

    assert "a-key-chosen-before-344" in str(exc.value)

    connection = sqlite3.connect(path)
    try:
        remaining = connection.execute("SELECT count(*) FROM idea_evidence_intake").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(idea_evidence_intake)")}
    finally:
        connection.close()

    assert remaining == 1
    assert "tenant_id" not in columns


def test_the_migration_is_idempotent(tmp_path) -> None:
    """Every process start opens the ledger. A second run must do nothing."""

    path = _pre_344_file(tmp_path / "pre-344.sqlite3", {"tenant_id": "tenant-legacy"})

    first = IdeaEvidenceIntakeLedger(path).snapshot()
    second = IdeaEvidenceIntakeLedger(path).snapshot()

    assert first == second
    assert len(second) == 1


def test_the_rebuild_leaves_no_shadow_table(tmp_path) -> None:
    """SQLite cannot alter a primary key, so the table is rebuilt.

    The renamed original must not survive: a leftover copy holds the same
    receipts under the unscoped key this change exists to remove.
    """

    path = _pre_344_file(tmp_path / "pre-344.sqlite3", {"tenant_id": "tenant-legacy"})
    IdeaEvidenceIntakeLedger(path)

    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()

    assert "idea_evidence_intake_pre_344" not in tables
    assert "idea_evidence_intake" in tables


def test_a_fresh_file_is_created_on_the_tenant_scoped_schema(tmp_path) -> None:
    ledger = IdeaEvidenceIntakeLedger(tmp_path / "fresh.sqlite3")

    assert ledger.snapshot() == {}


def test_an_interrupted_rebuild_leaves_the_original_table_intact(tmp_path, monkeypatch) -> None:
    """The rebuild is one transaction, because SQLite would not make it one.

    Python's `sqlite3` opens a transaction implicitly before DML but **not**
    before DDL. Without an explicit `BEGIN`, the rename and the create autocommit
    one statement at a time -- so a process dying between them would restart,
    find the tenant-scoped table already present, return early, and serve an
    empty ledger while every retained receipt sat in the renamed table,
    accepting old retries as new intakes.

    Interrupted at exactly that point: after the rename, during the create.
    """

    from app.idea_evidence_intake import service

    path = _pre_344_file(tmp_path / "pre-344.sqlite3", {"tenant_id": "tenant-legacy"})
    monkeypatch.setattr(
        service, "_TENANT_SCOPED_DDL", "CREATE TABLE idea_evidence_intake (this is not valid sql"
    )

    with pytest.raises(sqlite3.OperationalError):
        IdeaEvidenceIntakeLedger(path)

    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        surviving = connection.execute("SELECT count(*) FROM idea_evidence_intake").fetchone()[0]
    finally:
        connection.close()

    assert tables == {"idea_evidence_intake"}, "the rename must have rolled back"
    assert surviving == 1, "the retained receipt must still be addressable"


def test_the_migration_still_succeeds_after_an_interrupted_attempt(tmp_path, monkeypatch) -> None:
    """An interrupted upgrade must be recoverable by simply starting again."""

    from app.idea_evidence_intake import service

    path = _pre_344_file(tmp_path / "pre-344.sqlite3", {"tenant_id": "tenant-legacy"})
    monkeypatch.setattr(
        service, "_TENANT_SCOPED_DDL", "CREATE TABLE idea_evidence_intake (this is not valid sql"
    )
    with pytest.raises(sqlite3.OperationalError):
        IdeaEvidenceIntakeLedger(path)
    monkeypatch.undo()

    stored = IdeaEvidenceIntakeLedger(path).snapshot()

    assert list(stored) == [("tenant-legacy", "a-key-chosen-before-344")]
