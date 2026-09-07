"""Enumerated candidates must carry Core's projected tenant, not Report's config (#177).

Broad discovery already refuses to invent ownership. Enumerated selectors still
stamped: they fetched Core's portfolio detail, checked only that `portfolio_id`
matched, and then built the candidate with `tenant_id=tenant_id` -- the
scheduler's own configuration, which is configuration wearing the costume of
evidence.

`lotus-core#1094` made `tenant_id` a required, source-owned field on
`PortfolioRecord`, so its absence is now a real signal rather than the permanent
condition it used to be. That is what makes three states meaningful:

  matching  -> schedule, carrying the value Core returned
  mismatch  -> refuse: the portfolio genuinely belongs to another tenant
  absent    -> refuse: Core answered without the field this decision depends on

Externally a refused candidate is simply not scheduled, so a caller cannot tell
"not yours" from "not there". Internally the operator must, because they are
different failures -- one is a real ownership boundary, the other is a source
that stopped supplying evidence.
"""

from __future__ import annotations

import logging

import pytest

from app.report_batch_orchestrator.ledger import ReportBatchLedger
from app.report_batch_orchestrator.scheduler import ReportBatchScheduler
from tests.unit.report_batch_orchestrator.test_scheduler import (  # type: ignore[import-not-found]
    _caller_context,
    _config,
    _dropped_record,
    _PortfolioSource,
    _schedule,
)

PORTFOLIO = "PB_SG_GLOBAL_BAL_001"
OURS = "tenant-sg"
THEIRS = "tenant-hk"


def _scheduler(
    tmp_path, payload: dict[str, object]
) -> tuple[ReportBatchScheduler, ReportBatchLedger]:
    ledger = ReportBatchLedger(tmp_path / "scheduled.sqlite3")
    source = _PortfolioSource({PORTFOLIO: (200, payload)})
    return ReportBatchScheduler(batch_ledger=ledger, portfolio_source=source), ledger


async def _run(scheduler: ReportBatchScheduler):
    return await scheduler.run_due_schedules(
        config=_config(_schedule(portfolio_ids=[PORTFOLIO])),
        caller_context=_caller_context(),
    )


@pytest.mark.asyncio
async def test_a_portfolio_owned_by_another_tenant_is_refused(tmp_path, caplog) -> None:
    """ "Not yours." Core answered, the id matched, the owner is someone else.

    Before this, the candidate was built with Report's configured tenant and the
    schedule proceeded -- attributing another tenant's portfolio on the strength
    of a configuration value.
    """
    scheduler, _ = _scheduler(
        tmp_path, {"portfolio_id": PORTFOLIO, "status": "active", "tenant_id": THEIRS}
    )

    with caplog.at_level(logging.WARNING, logger="report_batch_scheduler"):
        result = await _run(scheduler)

    assert result.materialized == ()
    fields = _dropped_record(caplog)
    assert fields["reason_codes"] == ["source_tenant_mismatch"]


@pytest.mark.asyncio
async def test_a_portfolio_with_no_projected_tenant_is_refused(tmp_path, caplog) -> None:
    """ "Not proven." Core answered without the field the decision depends on.

    Refusing rather than falling back is the whole point: falling back to the
    configured tenant is exactly the defect, and it would be indistinguishable
    from a correct attribution afterwards.
    """
    scheduler, _ = _scheduler(tmp_path, {"portfolio_id": PORTFOLIO, "status": "active"})

    with caplog.at_level(logging.WARNING, logger="report_batch_scheduler"):
        result = await _run(scheduler)

    assert result.materialized == ()
    fields = _dropped_record(caplog)
    assert fields["reason_codes"] == ["source_tenant_absent"]


@pytest.mark.asyncio
async def test_a_blank_projected_tenant_is_refused_like_an_absent_one(tmp_path, caplog) -> None:
    """Present-but-empty is not a declaration.

    An empty string would otherwise pass a presence check and then fail an
    equality check, landing in the mismatch bucket and telling an operator the
    portfolio belongs to someone else. It does not -- nobody said who owns it.
    """
    scheduler, _ = _scheduler(
        tmp_path, {"portfolio_id": PORTFOLIO, "status": "active", "tenant_id": "   "}
    )

    with caplog.at_level(logging.WARNING, logger="report_batch_scheduler"):
        result = await _run(scheduler)

    assert result.materialized == ()
    fields = _dropped_record(caplog)
    assert fields["reason_codes"] == ["source_tenant_absent"]


@pytest.mark.asyncio
async def test_the_two_refusals_are_indistinguishable_from_outside(tmp_path) -> None:
    """A caller must not be able to use the scheduler as an ownership oracle.

    "Belongs to another tenant" and "nobody said who owns it" produce byte-identical
    outward results. Only the drop log separates them.
    """
    mismatch, _ = _scheduler(
        tmp_path / "a", {"portfolio_id": PORTFOLIO, "status": "active", "tenant_id": THEIRS}
    )
    absent, _ = _scheduler(tmp_path / "b", {"portfolio_id": PORTFOLIO, "status": "active"})

    mismatch_result = await _run(mismatch)
    absent_result = await _run(absent)

    assert mismatch_result.materialized == absent_result.materialized == ()
    assert mismatch_result.refused_schedule_ids == absent_result.refused_schedule_ids
    assert mismatch_result.skipped_schedule_ids == absent_result.skipped_schedule_ids
    assert mismatch_result.attempted_count == absent_result.attempted_count


@pytest.mark.asyncio
async def test_a_matching_projection_schedules_and_carries_core_s_value(tmp_path) -> None:
    """The control, and where the tenant now comes from.

    The value is read from Core's payload rather than the scheduler's config.
    They are equal here by construction, which is exactly why the assertion has
    to be that the candidate was built at all and that scheduling proceeded --
    the change is about provenance, and provenance is only observable when the
    two disagree, which the three tests above cover.
    """
    scheduler, _ = _scheduler(
        tmp_path, {"portfolio_id": PORTFOLIO, "status": "active", "tenant_id": OURS}
    )

    result = await _run(scheduler)

    assert result.materialized != (), "a projection that matches must still schedule"
    assert result.refused_schedule_ids == ()
