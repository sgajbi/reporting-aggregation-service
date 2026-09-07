"""Tenant isolation of `report_request`, proven on PostgreSQL (report#350).

The unit suite proves the decision on SQLite. This proves it on the backend that
actually runs, because the two disagree in the way that matters: the defect was
not only a missing `WHERE` predicate but a column-level `UNIQUE` on
`idempotency_key`, and only a real database refuses the second tenant's insert.

Concurrency is included deliberately. Two tenants submitting the same raw key at
the same instant is the case a serialised test cannot distinguish from two
sequential submissions, and it is the one that exercises the new
`(tenant_id, idempotency_key)` constraint under contention rather than in
isolation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from app.reporting_jobs.models import PortfolioReviewJobRequest, ReportCallerContext
from app.reporting_jobs.postgres_ledger import PostgresReportJobLedger

DATABASE_URL = os.getenv("REPORT_JOB_LEDGER_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="REPORT_JOB_LEDGER_DATABASE_URL is required for the PostgreSQL isolation proof",
)

TENANT_A = "tenant-isolation-a"
TENANT_B = "tenant-isolation-b"


@pytest.fixture(autouse=True)
def remove_this_suite_s_records() -> Iterator[None]:
    """Leave the shared database exactly as this suite found it.

    The integration session provisions one isolated database and every test in
    it shares that database. Submitting a job enqueues a **pending work item**,
    and `claim_work_items(limit=1)` claims one pending item from the whole
    table -- so residue from this file made
    `test_postgres_report_submission_persists_and_recovers_durable_work` claim
    one of ours and find none of its own. It failed in CI while passing locally,
    because locally these tests ran alone.

    The pre-existing test's assumption that it is the only producer of pending
    work is fragile, but the fragility is not its fault to carry: a proof that
    perturbs its neighbours is not finished.
    """
    yield
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(
            """
            DELETE FROM report_job_work_item
            WHERE report_job_id IN (
                SELECT j.report_job_id
                FROM report_job j
                JOIN report_request r ON r.report_request_id = j.report_request_id
                WHERE r.tenant_id = ANY(%s)
            )
            """,
            ([TENANT_A, TENANT_B],),
        )
        connection.execute(
            """
            DELETE FROM report_status_event
            WHERE report_job_id IN (
                SELECT j.report_job_id
                FROM report_job j
                JOIN report_request r ON r.report_request_id = j.report_request_id
                WHERE r.tenant_id = ANY(%s)
            )
            """,
            ([TENANT_A, TENANT_B],),
        )
        connection.execute(
            """
            DELETE FROM report_job
            WHERE report_request_id IN (
                SELECT report_request_id FROM report_request WHERE tenant_id = ANY(%s)
            )
            """,
            ([TENANT_A, TENANT_B],),
        )
        connection.execute(
            "DELETE FROM report_request WHERE tenant_id = ANY(%s)", ([TENANT_A, TENANT_B],)
        )


def _ledger() -> PostgresReportJobLedger:
    return PostgresReportJobLedger(DATABASE_URL)


def _caller(tenant_id: str, suffix: str) -> ReportCallerContext:
    return ReportCallerContext(
        triggered_by="advisor-123",
        caller_application="lotus-gateway",
        tenant_id=tenant_id,
        region="APAC",
        booking_center_code="SG",
        role="advisor",
        correlation_id=f"corr-350-{suffix}",
        trace_id=f"trace-350-{suffix}",
    )


def _request(portfolio: str) -> PortfolioReviewJobRequest:
    return PortfolioReviewJobRequest(
        portfolio_scope={"portfolio_ids": [portfolio]},
        as_of_date="2026-04-22",
        requested_output_formats=["json"],
        reporting_currency="USD",
        options={"sections": ["OVERVIEW"]},
    )


def _submit(ledger: PostgresReportJobLedger, tenant: str, key: str, portfolio: str):
    return ledger.submit_portfolio_review_job(
        request=_request(portfolio),
        caller_context=_caller(tenant, key),
        idempotency_key=key,
    )


def test_two_tenants_coexist_under_one_raw_key_with_identical_bodies() -> None:
    """The serious half, on the real backend.

    Identical intent from two tenants used to resolve to one stored request, so
    the second tenant read the first's job by presenting a string it guessed.
    """
    ledger = _ledger()
    key = f"shared-key-{uuid4().hex[:12]}"
    portfolio = f"PB_SG_GLOBAL_BAL_{uuid4().hex[:8]}"

    first = _submit(ledger, TENANT_A, key, portfolio)
    second = _submit(ledger, TENANT_B, key, portfolio)

    assert first.request_id != second.request_id
    assert first.job_id != second.job_id
    assert first.tenant_id == TENANT_A
    assert second.tenant_id == TENANT_B


def test_two_tenants_coexist_under_one_raw_key_with_differing_bodies() -> None:
    """The denial half. Before this, tenant B was refused with an untrue reason."""
    ledger = _ledger()
    key = f"shared-key-{uuid4().hex[:12]}"

    first = _submit(ledger, TENANT_A, key, f"PB_A_{uuid4().hex[:8]}")
    second = _submit(ledger, TENANT_B, key, f"PB_B_{uuid4().hex[:8]}")

    assert first.request_id != second.request_id


def test_concurrent_submissions_from_two_tenants_both_succeed() -> None:
    """Contention, not sequence.

    Both callers are released from one barrier so the two inserts genuinely
    contend for the `(tenant_id, idempotency_key)` constraint. Under the old
    single-column UNIQUE one of them had to lose, whatever the query said.
    """
    key = f"shared-key-{uuid4().hex[:12]}"
    portfolio = f"PB_SG_GLOBAL_BAL_{uuid4().hex[:8]}"
    barrier = Barrier(2)

    def submit(tenant: str):
        ledger = _ledger()
        barrier.wait(timeout=10)
        return _submit(ledger, tenant, key, portfolio)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit, TENANT_A), executor.submit(submit, TENANT_B)]
        records = [future.result(timeout=30) for future in futures]

    assert len({record.request_id for record in records}) == 2, (
        "both tenants must hold their own request under contention"
    )
    assert {record.tenant_id for record in records} == {TENANT_A, TENANT_B}


def test_same_tenant_concurrent_replay_converges_on_one_record() -> None:
    """The control under contention, which is where it is hardest.

    Two identical submissions from ONE tenant racing each other must still
    produce a single request. Tenant scoping is only correct if it does not
    quietly turn a replay into a second job.
    """
    key = f"shared-key-{uuid4().hex[:12]}"
    portfolio = f"PB_SG_GLOBAL_BAL_{uuid4().hex[:8]}"
    barrier = Barrier(2)

    def submit():
        ledger = _ledger()
        barrier.wait(timeout=10)
        return _submit(ledger, TENANT_A, key, portfolio)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit) for _ in range(2)]
        records = [future.result(timeout=30) for future in futures]

    assert len({record.request_id for record in records}) == 1, (
        "a same-tenant replay must converge, not mint a second request"
    )


def test_a_tenants_record_is_not_visible_to_another_tenant_after_restart() -> None:
    """Restart: a new ledger instance reads the same isolated records.

    A fresh adapter is the deployed shape after a process restart, and the
    isolation must come from the stored identity rather than anything the first
    instance held.
    """
    key = f"shared-key-{uuid4().hex[:12]}"
    portfolio = f"PB_SG_GLOBAL_BAL_{uuid4().hex[:8]}"

    first = _submit(_ledger(), TENANT_A, key, portfolio)
    second = _submit(_ledger(), TENANT_B, key, portfolio)

    restarted = _ledger()
    replay_a = _submit(restarted, TENANT_A, key, portfolio)
    replay_b = _submit(restarted, TENANT_B, key, portfolio)

    assert replay_a.request_id == first.request_id
    assert replay_b.request_id == second.request_id
    assert replay_a.request_id != replay_b.request_id
