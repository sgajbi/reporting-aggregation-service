"""Stored recurring-schedule definitions: cadence math, governance, and audit (issue #167)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.report_batch_orchestrator.ledger import ReportBatchLedger
from app.report_batch_orchestrator.schedule_definitions import (
    BatchScheduleDefinitionCreateRequest,
    BatchScheduleDefinitionUpdateRequest,
    ScheduleDefinitionError,
    ScheduleDefinitionService,
    due_as_of_date,
    month_end,
    next_run_at,
    quarter_end,
    stored_schedule_to_definition,
)
from app.reporting_jobs.models import ReportCallerContext

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)


def _caller(tenant_id: str = "tenant-sg", region: str = "APAC"):
    return ReportCallerContext(
        triggered_by="advisor-123",
        caller_application="lotus-gateway",
        tenant_id=tenant_id,
        region=region,
        booking_center_code="SG",
        role="advisor",
        correlation_id="corr-schedule-1",
        trace_id="trace-schedule-1",
    )


def _service(tmp_path: Path) -> ScheduleDefinitionService:
    return ScheduleDefinitionService(ReportBatchLedger(tmp_path / "schedules.sqlite3"))


def _create_request(**overrides) -> BatchScheduleDefinitionCreateRequest:
    payload = {
        "cadence": "quarter_end",
        "portfolio_ids": ["PB_SG_GLOBAL_BAL_001"],
        "requested_output_formats": ["pdf"],
        "reporting_currency": "USD",
        "options": {"sections": ["OVERVIEW", "PERFORMANCE"]},
    }
    payload.update(overrides)
    return BatchScheduleDefinitionCreateRequest(**payload)


def test_month_and_quarter_ends_handle_boundaries() -> None:
    assert month_end(date(2026, 2, 1)) == date(2026, 2, 28)
    assert month_end(date(2028, 2, 15)) == date(2028, 2, 29)
    assert month_end(date(2026, 12, 31)) == date(2026, 12, 31)
    assert quarter_end(date(2026, 1, 1)) == date(2026, 3, 31)
    assert quarter_end(date(2026, 8, 29)) == date(2026, 9, 30)
    assert quarter_end(date(2026, 12, 31)) == date(2026, 12, 31)


def test_a_new_schedule_never_backfills_periods_before_its_creation() -> None:
    created = date(2026, 2, 10)
    # The previous quarter end (Dec 31) predates creation - nothing is due.
    assert due_as_of_date("quarter_end", today=date(2026, 2, 20), created_on=created) is None
    # From the first quarter end after creation it is due.
    assert due_as_of_date("quarter_end", today=date(2026, 3, 31), created_on=created) == (
        date(2026, 3, 31)
    )
    # And remains the due cycle until the next period boundary.
    assert due_as_of_date("quarter_end", today=date(2026, 4, 2), created_on=created) == (
        date(2026, 3, 31)
    )


def test_next_run_at_reports_the_upcoming_cycle_including_year_rollover() -> None:
    created = date(2026, 8, 29)
    assert next_run_at("quarter_end", today=created, created_on=created) == date(2026, 9, 30)
    assert next_run_at("monthly_end", today=created, created_on=created) == date(2026, 8, 31)
    assert next_run_at("monthly_end", today=date(2026, 12, 15), created_on=created) == (
        date(2026, 12, 31)
    )
    # Display projects the upcoming boundary; whether the previous boundary's
    # cycle ran is the batch ledger's truth, guarded by deterministic idempotency.
    assert next_run_at("quarter_end", today=date(2026, 10, 2), created_on=created) == (
        date(2026, 12, 31)
    )
    # A schedule created mid-period points at that period's own end.
    assert next_run_at("quarter_end", today=date(2026, 9, 30), created_on=created) == (
        date(2026, 9, 30)
    )


def test_create_binds_governance_identity_from_the_caller(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    assert schedule.tenant_id == "tenant-sg"
    assert schedule.region == "APAC"
    assert schedule.booking_center_code == "SG"
    assert schedule.owner_actor == "advisor-123"
    assert schedule.enabled is True
    audit = service.list_audit(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert [record.action for record in audit] == ["created"]
    assert audit[0].changes["definition"]["schedule_id"] == schedule.schedule_id


def test_create_without_tenant_scope_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ScheduleDefinitionError) as excinfo:
        service.create_schedule(
            request=_create_request(), caller_context=_caller(tenant_id=""), now=NOW
        )
    assert excinfo.value.code == "schedule_scope_unresolved"


def test_create_rejects_ungoverned_ordering_options(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ScheduleDefinitionError):
        service.create_schedule(
            request=_create_request(options={"sections": ["NOT_A_SECTION"]}),
            caller_context=_caller(),
            now=NOW,
        )


def test_an_identical_create_retry_converges_on_the_existing_schedule(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    retried = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    assert retried.schedule_id == first.schedule_id
    assert len(service.list_schedules(caller_context=_caller())) == 1


def test_a_different_definition_creates_a_second_schedule(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    second = service.create_schedule(
        request=_create_request(cadence="monthly_end"), caller_context=_caller(), now=NOW
    )

    assert second.schedule_id != first.schedule_id
    assert len(service.list_schedules(caller_context=_caller())) == 2


def test_schedules_are_tenant_fenced_without_an_existence_oracle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    foreign = _caller(tenant_id="tenant-uk")
    assert service.list_schedules(caller_context=foreign) == []
    with pytest.raises(ScheduleDefinitionError) as real_id:
        service.get_schedule(schedule_id=schedule.schedule_id, caller_context=foreign)
    with pytest.raises(ScheduleDefinitionError) as fake_id:
        service.get_schedule(schedule_id="rbsc_does_not_exist", caller_context=foreign)
    assert real_id.value.code == fake_id.value.code == "batch_schedule_not_found"


def test_update_applies_a_diff_and_audits_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    updated = service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(reporting_currency="SGD"),
        caller_context=_caller(),
        now=NOW,
    )

    assert updated.reporting_currency == "SGD"
    audit = service.list_audit(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert [record.action for record in audit] == ["created", "updated"]
    assert audit[-1].changes == {"reporting_currency": {"from": "USD", "to": "SGD"}}


def test_a_no_change_update_converges_without_rewriting(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    unchanged = service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(reporting_currency="USD"),
        caller_context=_caller(),
        now=NOW,
    )

    assert unchanged.updated_at is None
    audit = service.list_audit(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert [record.action for record in audit] == ["created"]


def test_disable_and_enable_are_audited_as_their_own_actions(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(enabled=False),
        caller_context=_caller(),
        now=NOW,
    )
    service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(enabled=True),
        caller_context=_caller(),
        now=NOW,
    )

    audit = service.list_audit(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert [record.action for record in audit] == ["created", "disabled", "enabled"]


def test_update_rejects_an_ungoverned_result_without_saving(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    with pytest.raises(ScheduleDefinitionError):
        service.update_schedule(
            schedule_id=schedule.schedule_id,
            request=BatchScheduleDefinitionUpdateRequest(options={"sections": ["NOT_A_SECTION"]}),
            caller_context=_caller(),
            now=NOW,
        )

    stored = service.get_schedule(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert stored.options == {"sections": ["OVERVIEW", "PERFORMANCE"]}
    audit = service.list_audit(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert [record.action for record in audit] == ["created"]


def test_due_definitions_bridge_into_the_scheduler_shape(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    disabled = service.create_schedule(
        request=_create_request(cadence="monthly_end"), caller_context=_caller(), now=NOW
    )
    service.update_schedule(
        schedule_id=disabled.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(enabled=False),
        caller_context=_caller(),
        now=NOW,
    )

    at_quarter_end = service.due_definitions_for_scheduler(
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        today=date(2026, 9, 30),
    )
    assert [definition.schedule_id for definition in at_quarter_end] == [schedule.schedule_id]
    definition = at_quarter_end[0]
    assert definition.selector_mode == "explicit_portfolio_list"
    assert definition.frequency == "quarterly"
    assert definition.as_of_date == date(2026, 9, 30)
    assert definition.portfolio_ids == ["PB_SG_GLOBAL_BAL_001"]

    before_due = service.due_definitions_for_scheduler(
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        today=date(2026, 8, 30),
    )
    assert before_due == []
    foreign_tenant = service.due_definitions_for_scheduler(
        tenant_id="tenant-uk",
        region="APAC",
        booking_center_code="SG",
        today=date(2026, 9, 30),
    )
    assert foreign_tenant == []
    foreign_region = service.due_definitions_for_scheduler(
        tenant_id="tenant-sg",
        region="EMEA",
        booking_center_code="SG",
        today=date(2026, 9, 30),
    )
    assert foreign_region == []
    foreign_booking_center = service.due_definitions_for_scheduler(
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="HK",
        today=date(2026, 9, 30),
    )
    assert foreign_booking_center == []
    # Exact match includes None: a scheduler without a configured booking centre
    # must not run a schedule bound to one.
    no_booking_center = service.due_definitions_for_scheduler(
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code=None,
        today=date(2026, 9, 30),
    )
    assert no_booking_center == []


def test_stored_schedule_to_definition_validates_through_the_scheduler_model(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(
        request=_create_request(cadence="monthly_end"), caller_context=_caller(), now=NOW
    )

    definition = stored_schedule_to_definition(schedule, as_of_date=date(2026, 8, 31))

    assert definition.frequency == "monthly"
    assert definition.requested_output_formats == ["pdf"]
    assert definition.max_batch_size == schedule.max_batch_size


def test_create_rejects_more_portfolios_than_max_batch_size(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ScheduleDefinitionError) as excinfo:
        service.create_schedule(
            request=_create_request(portfolio_ids=["PB_1", "PB_2"], max_batch_size=1),
            caller_context=_caller(),
            now=NOW,
        )
    assert excinfo.value.code == "schedule_exceeds_max_batch_size"


def test_update_rejects_shrinking_the_bound_below_the_portfolio_count(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(
        request=_create_request(portfolio_ids=["PB_1", "PB_2"], max_batch_size=5),
        caller_context=_caller(),
        now=NOW,
    )
    with pytest.raises(ScheduleDefinitionError) as excinfo:
        service.update_schedule(
            schedule_id=schedule.schedule_id,
            request=BatchScheduleDefinitionUpdateRequest(max_batch_size=1),
            caller_context=_caller(),
            now=NOW,
        )
    assert excinfo.value.code == "schedule_exceeds_max_batch_size"


def test_an_explicit_null_clears_the_reporting_currency(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    assert schedule.reporting_currency == "USD"

    cleared = service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest.model_validate({"reporting_currency": None}),
        caller_context=_caller(),
        now=NOW,
    )
    assert cleared.reporting_currency is None

    omitted = service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(),
        caller_context=_caller(),
        now=NOW,
    )
    assert omitted.reporting_currency is None
    audit = service.list_audit(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert [record.action for record in audit] == ["created", "updated"]


def test_definition_and_audit_commit_atomically(tmp_path: Path) -> None:
    """A schedule must never exist without the audit record that explains it."""

    ledger = ReportBatchLedger(tmp_path / "schedules.sqlite3")
    service = ScheduleDefinitionService(ledger)

    def _fail(connection, record):
        raise RuntimeError("audit write failed")

    original = ledger._write_schedule_audit
    ledger._write_schedule_audit = _fail
    try:
        with pytest.raises(RuntimeError):
            service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    finally:
        ledger._write_schedule_audit = original

    assert ledger.list_schedule_definitions("tenant-sg") == []


def test_stored_definitions_fold_into_the_scheduler_pass_itself() -> None:
    """The daemon loop and the HTTP route share one materialization path: the
    scheduler folds due stored definitions on every pass (review finding on #197 -
    HTTP-only folding left API-created schedules dead in the deployed daemon)."""

    import asyncio

    from app.report_batch_orchestrator.scheduler import (
        BatchSchedulerConfig,
        ReportBatchScheduler,
    )

    captured: dict = {}

    class _LedgerSpy:
        def create_batch(self, *, request, caller_context, idempotency_key):
            captured["request"] = request
            captured["idempotency_key"] = idempotency_key

            class _Batch:
                batch_id = "rbch_daemon"
                status = "materialized"
                item_count = len(request.portfolio_ids)

            return _Batch()

    class _Portfolios:
        async def get_portfolio_detail(self, portfolio_id, correlation_id=None):
            return 200, {"portfolio_id": portfolio_id, "tenant_id": "tenant-sg", "status": "active"}

    class _StoredSource:
        def due_definitions_for_scheduler(self, *, tenant_id, region, booking_center_code, today):
            captured["scope"] = (tenant_id, region, booking_center_code, today)
            return [stored_schedule_to_definition(_stored_schedule(), as_of_date=date(2026, 9, 30))]

    def _stored_schedule():
        from app.report_batch_orchestrator.schedule_definitions import (
            StoredBatchSchedule,
        )

        return StoredBatchSchedule(
            schedule_id="rbsc_daemon",
            tenant_id="tenant-sg",
            region="APAC",
            booking_center_code="SG",
            owner_actor="advisor-123",
            enabled=True,
            cadence="quarter_end",
            portfolio_ids=["PB_SG_GLOBAL_BAL_001"],
            requested_output_formats=["pdf"],
            reporting_currency="USD",
            options={"sections": ["OVERVIEW", "PERFORMANCE"]},
            max_batch_size=10,
            cadence_effective_on=NOW.date(),
            created_at=NOW,
            updated_at=None,
        )

    scheduler = ReportBatchScheduler(
        batch_ledger=_LedgerSpy(),
        portfolio_source=_Portfolios(),
        stored_schedule_source=_StoredSource(),
    )
    config = BatchSchedulerConfig(
        scheduler_id="daemon-test",
        interval_seconds=60.0,
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        role="scheduler",
        schedules=(),
    )
    result = asyncio.run(
        scheduler.run_due_schedules(
            config=config,
            caller_context=_caller(),
            evaluation_date=date(2026, 9, 30),
        )
    )

    assert captured["scope"] == ("tenant-sg", "APAC", "SG", date(2026, 9, 30))
    assert [entry.schedule_id for entry in result.materialized] == ["rbsc_daemon"]
    assert captured["request"].options["batch_schedule_id"] == "rbsc_daemon"


def test_the_same_definition_in_another_region_is_a_distinct_schedule(tmp_path: Path) -> None:
    """The create-convergence fingerprint carries execution scope: identical content
    created from another region or booking centre is a different schedule, not an
    echo of the first one."""

    service = _service(tmp_path)
    apac = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    emea_caller = ReportCallerContext(
        triggered_by="advisor-456",
        caller_application="lotus-gateway",
        tenant_id="tenant-sg",
        region="EMEA",
        booking_center_code="UK",
        role="advisor",
        correlation_id="corr-schedule-2",
        trace_id="trace-schedule-2",
    )
    emea = service.create_schedule(request=_create_request(), caller_context=emea_caller, now=NOW)

    assert emea.schedule_id != apac.schedule_id
    assert emea.region == "EMEA"


def test_a_racing_identical_create_converges_on_the_database_winner(tmp_path: Path) -> None:
    """Two overlapping identical creates cannot both pass the read: the partial
    unique index on enabled fingerprints makes the second insert converge on the
    first schedule instead of persisting a duplicate."""

    ledger = ReportBatchLedger(tmp_path / "schedules.sqlite3")
    service = ScheduleDefinitionService(ledger)
    first = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    # Simulate the race: bypass the service-level read-echo so the insert itself
    # hits the unique index, exactly as a concurrent request would.
    original_list = ledger.list_schedule_definitions
    ledger.list_schedule_definitions = lambda tenant_id: []
    try:
        racer = service.create_schedule(
            request=_create_request(), caller_context=_caller(), now=NOW
        )
    finally:
        ledger.list_schedule_definitions = original_list

    assert racer.schedule_id == first.schedule_id
    assert len(ledger.list_schedule_definitions("tenant-sg")) == 1


def test_a_stale_concurrent_update_is_refused_not_overwritten(tmp_path: Path) -> None:
    """Lost-update protection: an update whose loaded revision is no longer current
    is refused with a typed conflict instead of silently rewriting every column."""

    from app.report_batch_orchestrator.schedule_definitions import StaleScheduleRevision

    ledger = ReportBatchLedger(tmp_path / "schedules.sqlite3")
    service = ScheduleDefinitionService(ledger)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    # A concurrent writer lands first.
    service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(reporting_currency="SGD"),
        caller_context=_caller(),
        now=NOW,
    )

    # The stale writer replays against the old revision at the storage layer.
    stale = schedule.model_copy(update={"max_batch_size": 7, "revision": 2})
    with pytest.raises(StaleScheduleRevision):
        ledger.save_schedule_definition(stale.model_copy(update={"revision": 99}))

    # And through the service the conflict surfaces as a typed refusal when the
    # store reports staleness.
    class _StaleStore:
        def __getattr__(self, name):
            return getattr(ledger, name)

        def save_schedule_definition_with_audit(self, schedule, record):
            raise StaleScheduleRevision(schedule.schedule_id)

    stale_service = ScheduleDefinitionService(_StaleStore())
    with pytest.raises(ScheduleDefinitionError) as excinfo:
        stale_service.update_schedule(
            schedule_id=schedule.schedule_id,
            request=BatchScheduleDefinitionUpdateRequest(max_batch_size=9),
            caller_context=_caller(),
            now=NOW,
        )
    assert excinfo.value.code == "batch_schedule_update_conflict"

    current = service.get_schedule(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert current.reporting_currency == "SGD"
    assert current.max_batch_size == _create_request().max_batch_size


def test_cycle_identity_survives_a_cadence_change() -> None:
    """A monthly schedule switched to quarterly after September 30 materialized
    still resolves September 30; the cycle key must converge, not duplicate."""

    from app.report_batch_orchestrator.scheduler import (
        stored_schedule_cycle_idempotency_key,
    )

    before = stored_schedule_cycle_idempotency_key(
        schedule_id="rbsc_x", as_of_date=date(2026, 9, 30)
    )
    after = stored_schedule_cycle_idempotency_key(
        schedule_id="rbsc_x", as_of_date=date(2026, 9, 30)
    )
    other_cycle = stored_schedule_cycle_idempotency_key(
        schedule_id="rbsc_x", as_of_date=date(2026, 10, 31)
    )
    other_schedule = stored_schedule_cycle_idempotency_key(
        schedule_id="rbsc_y", as_of_date=date(2026, 9, 30)
    )

    assert before == after
    assert len({before, other_cycle, other_schedule}) == 3


def test_a_cadence_change_anchors_dueness_to_its_effective_date(tmp_path: Path) -> None:
    """Switching quarterly to monthly on August 15 must not owe July 31: due-ness
    anchors on the cadence's effective date, not the schedule's creation date."""

    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    service = _service(tmp_path)
    created_at = _datetime(2026, 5, 10, 9, 0, tzinfo=_UTC)
    schedule = service.create_schedule(
        request=_create_request(), caller_context=_caller(), now=created_at
    )

    changed_at = _datetime(2026, 8, 15, 9, 0, tzinfo=_UTC)
    switched = service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(cadence="monthly_end"),
        caller_context=_caller(),
        now=changed_at,
    )
    assert switched.cadence_effective_on == changed_at.date()

    # July 31 predates the cadence change: nothing due on August 16.
    assert (
        service.due_definitions_for_scheduler(
            tenant_id="tenant-sg",
            region="APAC",
            booking_center_code="SG",
            today=date(2026, 8, 16),
        )
        == []
    )
    # The first month end after the change is due.
    due = service.due_definitions_for_scheduler(
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        today=date(2026, 8, 31),
    )
    assert [definition.as_of_date for definition in due] == [date(2026, 8, 31)]

    # A non-cadence update leaves the anchor alone.
    service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(reporting_currency="SGD"),
        caller_context=_caller(),
        now=_datetime(2026, 9, 1, 9, 0, tzinfo=_UTC),
    )
    unchanged = service.get_schedule(schedule_id=schedule.schedule_id, caller_context=_caller())
    assert unchanged.cadence_effective_on == changed_at.date()


def test_an_update_that_duplicates_another_enabled_schedule_is_refused(
    tmp_path: Path,
) -> None:
    """Making one enabled schedule identical to another violates the fingerprint
    index on the update path too, and surfaces as a typed conflict."""

    service = _service(tmp_path)
    service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    other = service.create_schedule(
        request=_create_request(cadence="monthly_end"), caller_context=_caller(), now=NOW
    )

    with pytest.raises(ScheduleDefinitionError) as excinfo:
        service.update_schedule(
            schedule_id=other.schedule_id,
            request=BatchScheduleDefinitionUpdateRequest(cadence="quarter_end"),
            caller_context=_caller(),
            now=NOW,
        )
    assert excinfo.value.code == "batch_schedule_duplicate_definition"

    survivor = service.get_schedule(schedule_id=other.schedule_id, caller_context=_caller())
    assert survivor.cadence == "monthly_end"


def test_blank_portfolio_ids_and_oversized_bounds_are_rejected_at_the_model() -> None:
    with pytest.raises(ValueError):
        BatchScheduleDefinitionCreateRequest(cadence="quarter_end", portfolio_ids=["   "])
    with pytest.raises(ValueError):
        BatchScheduleDefinitionCreateRequest(
            cadence="quarter_end",
            portfolio_ids=["PB_1"],
            max_batch_size=1001,
        )
    with pytest.raises(ValueError):
        BatchScheduleDefinitionUpdateRequest(portfolio_ids=[""])
    with pytest.raises(ValueError):
        BatchScheduleDefinitionUpdateRequest(max_batch_size=2000)


def test_a_failing_stored_schedule_does_not_abort_the_pass() -> None:
    """One advisor's stale schedule (inactive portfolio, upstream hiccup) is
    contained: the pass logs, skips it, and still materializes the rest.
    Configured schedules keep raising - their failures are deployment defects."""

    import asyncio

    from app.report_batch_orchestrator.scheduler import (
        BatchSchedulerConfig,
        ReportBatchScheduler,
    )

    class _LedgerSpy:
        def __init__(self):
            self.created = []

        def create_batch(self, *, request, caller_context, idempotency_key):
            if "PB_BROKEN" in request.portfolio_ids:
                raise ValueError("inactive_portfolio")
            self.created.append(idempotency_key)

            class _Batch:
                batch_id = "rbch_ok"
                status = "materialized"
                item_count = 1

            return _Batch()

    class _Portfolios:
        async def get_portfolio_detail(self, portfolio_id, correlation_id=None):
            return 200, {"portfolio_id": portfolio_id, "tenant_id": "tenant-sg", "status": "active"}

    def _stored(schedule_id: str, portfolio: str):
        from app.report_batch_orchestrator.schedule_definitions import (
            StoredBatchSchedule,
        )

        return stored_schedule_to_definition(
            StoredBatchSchedule(
                schedule_id=schedule_id,
                tenant_id="tenant-sg",
                region="APAC",
                booking_center_code="SG",
                owner_actor="advisor-123",
                enabled=True,
                cadence="quarter_end",
                portfolio_ids=[portfolio],
                requested_output_formats=["pdf"],
                reporting_currency="USD",
                options={"sections": ["OVERVIEW", "PERFORMANCE"]},
                max_batch_size=10,
                cadence_effective_on=NOW.date(),
                created_at=NOW,
                updated_at=None,
            ),
            as_of_date=date(2026, 9, 30),
        )

    class _StoredSource:
        def due_definitions_for_scheduler(self, **kwargs):
            return [_stored("rbsc_broken", "PB_BROKEN"), _stored("rbsc_healthy", "PB_OK")]

    ledger = _LedgerSpy()
    scheduler = ReportBatchScheduler(
        batch_ledger=ledger,
        portfolio_source=_Portfolios(),
        stored_schedule_source=_StoredSource(),
    )
    config = BatchSchedulerConfig(
        scheduler_id="containment-test",
        interval_seconds=60.0,
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        role="scheduler",
        schedules=(),
    )
    result = asyncio.run(
        scheduler.run_due_schedules(
            config=config,
            caller_context=_caller(),
            evaluation_date=date(2026, 9, 30),
        )
    )

    assert "rbsc_broken" in result.skipped_schedule_ids
    assert [entry.schedule_id for entry in result.materialized] == ["rbsc_healthy"]

    # Infrastructure faults are not properties of one schedule: they propagate
    # and fail the pass loudly instead of masquerading as ordinary skips.
    class _InfraDownLedger:
        def create_batch(self, *, request, caller_context, idempotency_key):
            raise RuntimeError("postgres unavailable")

    infra_scheduler = ReportBatchScheduler(
        batch_ledger=_InfraDownLedger(),
        portfolio_source=_Portfolios(),
        stored_schedule_source=_StoredSource(),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(
            infra_scheduler.run_due_schedules(
                config=config,
                caller_context=_caller(),
                evaluation_date=date(2026, 9, 30),
            )
        )


def test_scheduler_downtime_backfills_every_missed_period(tmp_path: Path) -> None:
    """A monthly schedule whose daemon restarts on September 1 owes July 31 AND
    August 31 - the latest boundary alone would silently swallow July's pack."""

    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from app.report_batch_orchestrator.schedule_definitions import due_as_of_dates

    service = _service(tmp_path)
    schedule = service.create_schedule(
        request=_create_request(cadence="monthly_end"),
        caller_context=_caller(),
        now=_datetime(2026, 6, 10, 9, 0, tzinfo=_UTC),
    )

    due = service.due_definitions_for_scheduler(
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        today=date(2026, 9, 1),
    )
    assert [definition.as_of_date for definition in due] == [
        date(2026, 6, 30),
        date(2026, 7, 31),
        date(2026, 8, 31),
    ]
    assert all(d.schedule_id == schedule.schedule_id for d in due)

    # The backfill window is bounded and ascending; truncation keeps the most
    # recent periods.
    bounded = due_as_of_dates(
        "monthly_end",
        today=date(2026, 9, 1),
        created_on=date(2024, 1, 1),
        limit=3,
    )
    assert bounded == [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)]


def test_partial_candidate_resolution_refuses_the_stored_cycle() -> None:
    """A transient upstream miss must not shrink the pack: the stable cycle key
    would permanently claim the period for a subset."""

    import asyncio

    from app.report_batch_orchestrator.schedule_definitions import StoredBatchSchedule
    from app.report_batch_orchestrator.scheduler import (
        BatchSchedulerConfig,
        ReportBatchScheduler,
    )

    class _LedgerSpy:
        def __init__(self):
            self.created = []

        def create_batch(self, *, request, caller_context, idempotency_key):
            self.created.append(idempotency_key)
            raise AssertionError("a partial cycle must never reach the ledger")

    class _FlakyPortfolios:
        async def get_portfolio_detail(self, portfolio_id, correlation_id=None):
            if portfolio_id == "PB_DOWN":
                return 503, {}
            return 200, {"portfolio_id": portfolio_id, "tenant_id": "tenant-sg", "status": "active"}

    class _StoredSource:
        def due_definitions_for_scheduler(self, **kwargs):
            return [
                stored_schedule_to_definition(
                    StoredBatchSchedule(
                        schedule_id="rbsc_partial",
                        tenant_id="tenant-sg",
                        region="APAC",
                        booking_center_code="SG",
                        owner_actor="advisor-123",
                        enabled=True,
                        cadence="quarter_end",
                        portfolio_ids=["PB_OK", "PB_DOWN"],
                        requested_output_formats=["pdf"],
                        reporting_currency="USD",
                        options={"sections": ["OVERVIEW", "PERFORMANCE"]},
                        max_batch_size=10,
                        cadence_effective_on=NOW.date(),
                        created_at=NOW,
                        updated_at=None,
                    ),
                    as_of_date=date(2026, 9, 30),
                )
            ]

    ledger = _LedgerSpy()
    scheduler = ReportBatchScheduler(
        batch_ledger=ledger,
        portfolio_source=_FlakyPortfolios(),
        stored_schedule_source=_StoredSource(),
    )
    config = BatchSchedulerConfig(
        scheduler_id="partial-test",
        interval_seconds=60.0,
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        role="scheduler",
        schedules=(),
    )
    result = asyncio.run(
        scheduler.run_due_schedules(
            config=config,
            caller_context=_caller(),
            evaluation_date=date(2026, 9, 30),
        )
    )

    assert result.skipped_schedule_ids == ("rbsc_partial",)
    assert result.materialized == ()
    assert ledger.created == []


def test_crud_is_fenced_by_the_full_execution_scope(tmp_path: Path) -> None:
    """Same tenant, different region or booking centre: the schedule is invisible
    and unmodifiable - it only ever runs under its exact bound identity."""

    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)

    same_tenant_other_region = ReportCallerContext(
        triggered_by="advisor-456",
        caller_application="lotus-gateway",
        tenant_id="tenant-sg",
        region="EMEA",
        booking_center_code="SG",
        role="advisor",
        correlation_id="corr-region",
        trace_id="trace-region",
    )
    assert service.list_schedules(caller_context=same_tenant_other_region) == []
    with pytest.raises(ScheduleDefinitionError):
        service.get_schedule(
            schedule_id=schedule.schedule_id,
            caller_context=same_tenant_other_region,
        )

    same_tenant_other_centre = ReportCallerContext(
        triggered_by="advisor-789",
        caller_application="lotus-gateway",
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="HK",
        role="advisor",
        correlation_id="corr-centre",
        trace_id="trace-centre",
    )
    with pytest.raises(ScheduleDefinitionError):
        service.update_schedule(
            schedule_id=schedule.schedule_id,
            request=BatchScheduleDefinitionUpdateRequest(enabled=False),
            caller_context=same_tenant_other_centre,
            now=NOW,
        )


def test_portfolio_ids_are_stored_stripped(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(
        request=_create_request(portfolio_ids=["  PB_SG_GLOBAL_BAL_001  "]),
        caller_context=_caller(),
        now=NOW,
    )
    assert schedule.portfolio_ids == ["PB_SG_GLOBAL_BAL_001"]


def test_detail_snapshot_returns_definition_and_audit_together(tmp_path: Path) -> None:
    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(reporting_currency="SGD"),
        caller_context=_caller(),
        now=NOW,
    )

    snapshot, audit = service.get_schedule_with_audit(
        schedule_id=schedule.schedule_id, caller_context=_caller()
    )
    assert snapshot.reporting_currency == "SGD"
    assert [record.action for record in audit] == ["created", "updated"]


def test_update_model_edges_and_store_roundtrip(tmp_path: Path) -> None:
    """Small contract edges: explicit-None portfolio list is a no-op, duplicate ids
    in an update are dropped, and the SQLite store's standalone audit methods and
    absent-id snapshot behave."""

    assert BatchScheduleDefinitionUpdateRequest(portfolio_ids=None).portfolio_ids is None

    service = _service(tmp_path)
    schedule = service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    deduped = service.update_schedule(
        schedule_id=schedule.schedule_id,
        request=BatchScheduleDefinitionUpdateRequest(portfolio_ids=["PB_A", "PB_A", "PB_B"]),
        caller_context=_caller(),
        now=NOW,
    )
    assert deduped.portfolio_ids == ["PB_A", "PB_B"]

    ledger = ReportBatchLedger(tmp_path / "standalone.sqlite3")
    from app.report_batch_orchestrator.schedule_definitions import (
        BatchScheduleAuditRecord,
    )

    record = BatchScheduleAuditRecord(
        audit_id="rbsa_standalone",
        schedule_id="rbsc_missing",
        action="created",
        actor="advisor-123",
        correlation_id="corr-standalone",
        changes={},
        created_at=NOW,
    )
    ledger.append_schedule_audit(record)
    assert ledger.list_schedule_audit("rbsc_missing") == [record]
    assert ledger.get_schedule_definition_with_audit("rbsc_absent") == (None, [])


def test_a_racing_create_with_an_unresolvable_winner_is_a_typed_conflict(
    tmp_path: Path,
) -> None:
    """If the unique index fires but the winner cannot be read back (or belongs to
    another tenant), the caller gets a typed retryable conflict, not a 500."""

    from app.report_batch_orchestrator.schedule_definitions import (
        DuplicateScheduleDefinition,
    )

    ledger = ReportBatchLedger(tmp_path / "schedules.sqlite3")

    class _RacingStore:
        def __getattr__(self, name):
            return getattr(ledger, name)

        def list_schedule_definitions(self, tenant_id):
            return []

        def save_schedule_definition_with_audit(self, schedule, record):
            raise DuplicateScheduleDefinition("")

        def get_schedule_definition(self, schedule_id):
            return None

    service = ScheduleDefinitionService(_RacingStore())
    with pytest.raises(ScheduleDefinitionError) as excinfo:
        service.create_schedule(request=_create_request(), caller_context=_caller(), now=NOW)
    assert excinfo.value.code == "batch_schedule_conflict"


def test_backfill_truncation_is_logged_loudly(tmp_path: Path, caplog) -> None:
    """More owed periods than the window keeps is expired-by-policy - and loud."""

    import logging
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    service = _service(tmp_path)
    service.create_schedule(
        request=_create_request(cadence="monthly_end"),
        caller_context=_caller(),
        now=_datetime(2024, 1, 10, 9, 0, tzinfo=_UTC),
    )

    with caplog.at_level(logging.WARNING, logger="report_batch_scheduler"):
        due = service.due_definitions_for_scheduler(
            tenant_id="tenant-sg",
            region="APAC",
            booking_center_code="SG",
            today=date(2026, 8, 29),
        )

    assert len(due) == 12
    truncations = [
        record
        for record in caplog.records
        if record.getMessage() == "stored_schedule_backfill_truncated"
    ]
    assert len(truncations) == 1
