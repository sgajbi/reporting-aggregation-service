"""Two tenants may choose the same idempotency key (report#350).

`report_request` was keyed on the caller-supplied key alone, in the ledger that
backs every report job route rather than one handoff. Measured through the real
HTTP materialization route before this change:

    tenant-a  -> 202
    tenant-b  -> 409 {"code": "idempotency_conflict",
                      "message": "Idempotency-Key was reused with different idea evidence content."}

Tenant B reused nothing and sent identical content. It collided with another
tenant's request, and the refusal named a cause that was not true. Where the
bodies match, both tenants resolved to ONE stored `report_request_id`, which is
the more serious half: one tenant reading another's job.

The defect had two layers, and fixing only the visible one would have left the
production backend broken: four `WHERE idempotency_key` lookups with no tenant
predicate, and a column-level `UNIQUE` on `idempotency_key` in both schemas that
would refuse the second tenant's insert regardless of the query.

`report_request_id` is deliberately not re-derived. lotus-idea confirmed under
C5-X03 that the materialization receipt identities are immutable, so a retained
receipt must recover unchanged.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.reporting_jobs.ledger import IdempotencyConflictError, ReportJobLedger
from app.reporting_jobs.models import PortfolioReviewJobRequest, ReportCallerContext

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
SHARED_KEY = "a-key-two-tenants-both-chose"


def _caller(tenant_id: str) -> ReportCallerContext:
    return ReportCallerContext(
        trigger_type="user",
        triggered_by="advisor-123",
        caller_application="lotus-gateway",
        tenant_id=tenant_id,
        region="APAC",
        booking_center_code="SG",
        role=None,
        correlation_id="corr-350",
        trace_id="trace-350",
    )


def _request(portfolio: str) -> PortfolioReviewJobRequest:
    return PortfolioReviewJobRequest.model_validate(
        {
            "portfolio_scope": {"portfolio_ids": [portfolio]},
            "as_of_date": "2026-04-22",
            "requested_output_formats": ["pdf"],
            "reporting_currency": "USD",
            "options": {"sections": ["OVERVIEW"]},
        }
    )


def _ledger(tmp_path: Path) -> ReportJobLedger:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    ledger.ensure_schema()
    return ledger


def _submit(ledger: ReportJobLedger, tenant_id: str, *, portfolio: str = "PB_SG_GLOBAL_BAL_001"):
    return ledger.submit_portfolio_review_job(
        request=_request(portfolio),
        caller_context=_caller(tenant_id),
        idempotency_key=SHARED_KEY,
    )


def test_two_tenants_hold_the_same_key_with_identical_bodies(tmp_path: Path) -> None:
    """The serious half: identical intent used to resolve to one stored request.

    Both tenants must get their own `report_request_id`. A shared one means one
    tenant reading the other's job by presenting a string it guessed.
    """
    ledger = _ledger(tmp_path)

    first = _submit(ledger, TENANT_A)
    second = _submit(ledger, TENANT_B)

    assert first.request_id != second.request_id
    assert first.job_id != second.job_id


def test_two_tenants_hold_the_same_key_with_differing_bodies(tmp_path: Path) -> None:
    """The denial half, which was unconditional and mis-described.

    Different content under one key across two tenants is not a conflict at
    all. Before this change tenant B was refused with a message stating it had
    reused its own key with different content.
    """
    ledger = _ledger(tmp_path)

    first = _submit(ledger, TENANT_A, portfolio="PB_SG_GLOBAL_BAL_001")
    second = _submit(ledger, TENANT_B, portfolio="PB_SG_GLOBAL_BAL_002")

    assert first.request_id != second.request_id


def test_same_tenant_replay_still_converges(tmp_path: Path) -> None:
    """The control this change must not weaken.

    Scoping identity by tenant is only correct if genuine replay inside one
    tenant still returns the same record rather than minting a second job.
    """
    ledger = _ledger(tmp_path)

    first = _submit(ledger, TENANT_A)
    replay = _submit(ledger, TENANT_A)

    assert replay.request_id == first.request_id
    assert replay.job_id == first.job_id


def test_same_tenant_conflict_still_conflicts(tmp_path: Path) -> None:
    """The other half of the control: a real reuse must still be refused."""
    ledger = _ledger(tmp_path)
    _submit(ledger, TENANT_A, portfolio="PB_SG_GLOBAL_BAL_001")

    with pytest.raises(IdempotencyConflictError):
        _submit(ledger, TENANT_A, portfolio="PB_SG_GLOBAL_BAL_999")


def test_a_retained_ledger_is_upgraded_and_keeps_its_request_ids(tmp_path: Path) -> None:
    """A file written before #350 carries the old single-key UNIQUE.

    `CREATE TABLE IF NOT EXISTS` is a no-op against it, so the constraint would
    survive while every query looked correct and the second tenant's insert
    failed at the database. The table is rebuilt, and the retained
    `report_request_id` must not move -- a consumer may already hold it.
    """
    path = tmp_path / "jobs.sqlite3"
    ledger = _ledger(tmp_path)
    retained = _submit(ledger, TENANT_A)

    # Re-create the pre-#350 constraint on the retained file.
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX IF EXISTS report_request_tenant_idempotency_key")
        connection.execute(
            "CREATE UNIQUE INDEX report_request_legacy_key ON report_request (idempotency_key)"
        )
        connection.commit()
    finally:
        connection.close()

    upgraded = ReportJobLedger(path)
    upgraded.ensure_schema()
    second = _submit(upgraded, TENANT_B)

    assert second.request_id != retained.request_id

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT tenant_id, report_request_id FROM report_request WHERE idempotency_key = ?",
            (SHARED_KEY,),
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()

    stored = {row["tenant_id"]: row["report_request_id"] for row in rows}
    assert stored[TENANT_A] == retained.request_id, (
        "a retained request id must survive the rebuild unchanged"
    )
    assert stored[TENANT_B] == second.request_id
    assert "report_request_pre_350" not in tables, "the renamed table must not survive"
