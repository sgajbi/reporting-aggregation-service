from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import Settings, settings
from app.report_batch_orchestrator.contracts import BatchFrequency, BatchSelectorMode
from app.report_batch_orchestrator.ledger import BatchIdempotencyConflictError
from app.report_batch_orchestrator.models import (
    BatchCreateRequest,
    BatchCycleRequest,
    PortfolioBatchCandidate,
    ReportBatchRecord,
)
from app.report_batch_orchestrator.schedule import (
    materialize_cycle,
    scheduled_batch_idempotency_key,
)
from app.report_ordering_catalogue.validation import (
    ReportOrderingSubmissionError,
    validate_report_ordering_submission,
)
from app.reporting_jobs.ledger import canonical_json
from app.reporting_jobs.models import ReportCallerContext

_LOGGER = logging.getLogger("report_batch_scheduler")


class BatchScheduleConfigError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


class BatchScheduleManifestEntry(BaseModel):
    portfolio_id: str = Field(..., min_length=1)
    source_system: str = Field("operator-manifest", min_length=1)
    source_object: str = Field("BatchScheduleManifestEntry", min_length=1)


class BatchScheduleDefinition(BaseModel):
    schedule_id: str = Field(..., min_length=1)
    enabled: bool = True
    stable_cycle_identity: bool = Field(
        False,
        description=(
            "When true, the scheduled-batch idempotency identity is the schedule id "
            "alone, so one cycle yields exactly one batch across content updates. "
            "Stored definitions set this: patching a schedule after its period "
            "already materialized must converge on the existing batch, with the new "
            "content applying from the next cycle. Configuration schedules keep the "
            "content-hash identity."
        ),
    )
    selector_mode: BatchSelectorMode = "explicit_portfolio_list"
    frequency: BatchFrequency
    as_of_date: date
    portfolio_ids: list[str] = Field(default_factory=list)
    manifest_entries: list[BatchScheduleManifestEntry] = Field(default_factory=list)
    manifest_source: str | None = None
    manifest_version: str | None = None
    manifest_hash: str | None = None
    requested_output_formats: list[str] = Field(default_factory=lambda: ["pdf"])
    reporting_currency: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    max_batch_size: int = Field(250, ge=1)
    explicit_period_start: date | None = None
    explicit_period_end: date | None = None

    @model_validator(mode="after")
    def _validate_selector_source(self) -> "BatchScheduleDefinition":
        if self.selector_mode == "explicit_portfolio_list" and not self.portfolio_ids:
            raise ValueError("explicit_portfolio_list schedule requires portfolio_ids.")
        if self.selector_mode == "batch_manifest" and not self.manifest_entries:
            raise ValueError("batch_manifest schedule requires manifest_entries.")
        manifest_ids = [entry.portfolio_id for entry in self.manifest_entries]
        if len(manifest_ids) != len(set(manifest_ids)):
            raise ValueError("batch_manifest schedule contains duplicate manifest portfolio ids.")
        if self.selector_mode == "selected_subset":
            raise ValueError("selected_subset schedules require a governed subset source.")
        try:
            validate_report_ordering_submission(
                report_family_id="portfolio_review",
                ordering_mode_id="governed_schedule",
                requested_output_formats=self.requested_output_formats,
                options=self.options,
            )
        except ReportOrderingSubmissionError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc
        return self


@dataclass(frozen=True)
class BatchSchedulerConfig:
    scheduler_id: str
    interval_seconds: float
    tenant_id: str
    region: str
    booking_center_code: str | None
    role: str
    schedules: tuple[BatchScheduleDefinition, ...]


@dataclass(frozen=True)
class BatchSchedulerMaterialization:
    schedule_id: str
    batch_id: str
    idempotency_key: str
    item_count: int
    status: str


@dataclass(frozen=True)
class BatchSchedulerRunResult:
    scheduler_id: str
    attempted_count: int
    materialized: tuple[BatchSchedulerMaterialization, ...]
    skipped_schedule_ids: tuple[str, ...]
    #: Schedules refused because tenant ownership could not be proven (issue
    #: #177). Deliberately distinct from `skipped_schedule_ids`: a skip is
    #: "nothing to do", a refusal is "this would have attributed portfolios to
    #: a tenant on no evidence". Collapsing them hides a governance stop inside
    #: ordinary quiet.
    refused_schedule_ids: tuple[str, ...] = ()


class BatchScheduleSummaryResponse(BaseModel):
    schedule_id: str = Field(
        ...,
        description="Governed schedule identifier from REPORT_BATCH_SCHEDULES_JSON.",
        examples=["monthly-sg-global-bal"],
    )
    enabled: bool = Field(
        ...,
        description="Whether this configured schedule is eligible for a scheduler pass.",
        examples=[True],
    )
    selector_mode: BatchSelectorMode = Field(
        ...,
        description="Configured selector mode for this schedule.",
        examples=["explicit_portfolio_list"],
    )
    frequency: BatchFrequency = Field(
        ...,
        description="Configured production cycle frequency.",
        examples=["monthly"],
    )
    as_of_date: date = Field(
        ...,
        description="Business as-of date used to materialize the configured cycle.",
        examples=["2026-04-22"],
    )
    portfolio_count: int = Field(
        ...,
        ge=0,
        description="Number of explicit portfolio identifiers configured on the schedule.",
        examples=[1],
    )
    manifest_entry_count: int = Field(
        ...,
        ge=0,
        description="Number of inline manifest entries configured on the schedule.",
        examples=[0],
    )
    requested_output_formats: list[str] = Field(
        ...,
        description="Output formats requested for every batch item materialized by this schedule.",
        examples=[["pdf"]],
    )
    reporting_currency: str | None = Field(
        default=None,
        description="Optional reporting currency configured for materialized batch items.",
        examples=["USD"],
    )
    max_batch_size: int = Field(
        ...,
        ge=1,
        description="Maximum materialized item count allowed for this schedule.",
        examples=[250],
    )
    manifest_source: str | None = Field(
        default=None,
        description="Governed inline manifest source when selector_mode is batch_manifest.",
        examples=["ops-manifest-apac-monthly"],
    )
    manifest_version: str | None = Field(
        default=None,
        description="Governed inline manifest version when selector_mode is batch_manifest.",
        examples=["2026-04"],
    )
    manifest_hash: str | None = Field(
        default=None,
        description="Stable manifest hash supplied or computed for inline manifest schedules.",
        examples=["manifest-hash-001"],
    )
    option_keys: list[str] = Field(
        default_factory=list,
        description="Sorted option keys configured for this schedule without exposing values.",
        examples=[["sections", "benchmark_code"]],
    )


class BatchScheduleListResponse(BaseModel):
    scheduler_id: str = Field(
        ...,
        description="Stable scheduler identity used for configured scheduler passes.",
        examples=["lotus-report-batch-scheduler-1"],
    )
    interval_seconds: float = Field(
        ...,
        ge=0,
        description="Configured daemon interval for the internal scheduler process.",
        examples=[60.0],
    )
    tenant_id: str = Field(
        ...,
        description="Tenant context used when materializing configured schedules.",
        examples=["tenant-sg"],
    )
    region: str = Field(
        ...,
        description="Region context used when materializing configured schedules.",
        examples=["APAC"],
    )
    booking_center_code: str | None = Field(
        default=None,
        description="Optional booking-center context used when materializing configured schedules.",
        examples=["SG"],
    )
    schedule_count: int = Field(
        ...,
        ge=0,
        description="Total configured schedule count.",
        examples=[1],
    )
    enabled_schedule_count: int = Field(
        ...,
        ge=0,
        description="Configured schedule count eligible for a scheduler pass.",
        examples=[1],
    )
    schedules: list[BatchScheduleSummaryResponse] = Field(
        ...,
        description="Configured report batch schedules.",
    )


class BatchSchedulerRunRequest(BaseModel):
    pass_sequence: int = Field(
        1,
        ge=1,
        description=(
            "Deterministic pass sequence used to derive scheduler correlation and trace ids for "
            "this bounded operator-triggered scheduler pass."
        ),
        examples=[1],
    )
    evaluation_date: date | None = Field(
        None,
        description=(
            "Optional evaluation date for stored schedule due-ness, defaulting to today. "
            "Lets an operator simulate a period-end run; configured schedules are unaffected "
            "because their as-of dates are fixed in configuration."
        ),
        examples=["2026-09-30"],
    )


class BatchSchedulerMaterializationResponse(BaseModel):
    schedule_id: str = Field(
        ...,
        description="Configured schedule that produced or reused this durable batch.",
        examples=["monthly-sg-global-bal"],
    )
    batch_id: str = Field(
        ...,
        description="Durable report batch identifier.",
        examples=["rbch_2f6d1a8f2ef24f019e7d7f37507f352c"],
    )
    idempotency_key: str = Field(
        ...,
        description="Deterministic scheduled batch idempotency key.",
        examples=["scheduled-batch-2f6d1a8f2ef24f019e7d7f37507f352c"],
    )
    item_count: int = Field(
        ...,
        ge=0,
        description="Materialized item count for this schedule.",
        examples=[1],
    )
    status: str = Field(
        ...,
        description="Batch status after materialization or idempotent reuse.",
        examples=["materialized"],
    )


class BatchSchedulerRunResponse(BaseModel):
    scheduler_id: str = Field(
        ...,
        description="Stable scheduler identity used for the bounded pass.",
        examples=["lotus-report-batch-scheduler-1"],
    )
    attempted_count: int = Field(
        ...,
        ge=0,
        description="Enabled schedule count attempted during this pass.",
        examples=[1],
    )
    materialized_count: int = Field(
        ...,
        ge=0,
        description="Number of durable batches materialized or idempotently reused.",
        examples=[1],
    )
    skipped_schedule_ids: list[str] = Field(
        default_factory=list,
        description="Enabled schedule ids skipped because no eligible candidates were resolved.",
        examples=[[]],
    )
    refused_schedule_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Enabled schedule ids refused because tenant ownership could not be proven for a "
            "broad discovery selector (issue #177). Distinct from skipped: a skip means there "
            "was nothing to do, a refusal means materializing would have attributed portfolios "
            "to a tenant on no evidence. Broad scheduling stays refused until lotus-core "
            "projects the authoritative tenant on portfolio discovery."
        ),
        examples=[[]],
    )
    materialized: list[BatchSchedulerMaterializationResponse] = Field(
        default_factory=list,
        description="Durable batch materialization results for this pass.",
    )
    correlation_id: str = Field(
        ...,
        description="Scheduler correlation id used for this bounded pass.",
        examples=["corr-batch-scheduler-1-abc123def456"],
    )
    trace_id: str = Field(
        ...,
        description="Scheduler trace id used for this bounded pass.",
        examples=["trace1234567890abcdef1234567890ab"],
    )


BATCH_SCHEDULE_LIST_RESPONSE_EXAMPLE: dict[str, Any] = {
    "scheduler_id": "lotus-report-batch-scheduler-1",
    "interval_seconds": 60.0,
    "tenant_id": "tenant-sg",
    "region": "APAC",
    "booking_center_code": "SG",
    "schedule_count": 1,
    "enabled_schedule_count": 1,
    "schedules": [
        {
            "schedule_id": "monthly-sg-global-bal",
            "enabled": True,
            "selector_mode": "explicit_portfolio_list",
            "frequency": "monthly",
            "as_of_date": "2026-04-22",
            "portfolio_count": 1,
            "manifest_entry_count": 0,
            "requested_output_formats": ["pdf"],
            "reporting_currency": "USD",
            "max_batch_size": 250,
            "manifest_source": None,
            "manifest_version": None,
            "manifest_hash": None,
            "option_keys": ["sections"],
        }
    ],
}

BATCH_SCHEDULER_RUN_REQUEST_EXAMPLE: dict[str, Any] = {"pass_sequence": 1}

BATCH_SCHEDULER_RUN_RESPONSE_EXAMPLE: dict[str, Any] = {
    "scheduler_id": "lotus-report-batch-scheduler-1",
    "attempted_count": 1,
    "materialized_count": 1,
    "skipped_schedule_ids": [],
    "materialized": [
        {
            "schedule_id": "monthly-sg-global-bal",
            "batch_id": "rbch_2f6d1a8f2ef24f019e7d7f37507f352c",
            "idempotency_key": "scheduled-batch-2f6d1a8f2ef24f019e7d7f37507f352c",
            "item_count": 1,
            "status": "materialized",
        }
    ],
    "correlation_id": "corr-batch-scheduler-1-abc123def456",
    "trace_id": "trace1234567890abcdef1234567890ab",
}


class CorePortfolioSource(Protocol):
    async def get_portfolio_detail(
        self,
        portfolio_id: str,
        correlation_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class BatchScheduleLedger(Protocol):
    def create_batch(
        self,
        *,
        request: BatchCreateRequest,
        caller_context: ReportCallerContext,
        idempotency_key: str | None,
    ) -> ReportBatchRecord: ...

    def has_batch_for_idempotency_key(self, idempotency_key: str) -> bool: ...

    def has_batch_for_schedule_cycle(
        self,
        *,
        tenant_id: str,
        region: str,
        schedule_id: str,
        period_start: str,
        period_end: str,
        as_of_date: str,
    ) -> bool: ...


def batch_scheduler_config_from_settings(source: Settings = settings) -> BatchSchedulerConfig:
    schedules = _parse_schedule_definitions(source.batch_schedules_json)
    return BatchSchedulerConfig(
        scheduler_id=source.batch_scheduler_id,
        interval_seconds=source.batch_scheduler_interval_seconds,
        tenant_id=source.batch_scheduler_tenant_id,
        region=source.batch_scheduler_region,
        booking_center_code=source.batch_scheduler_booking_center_code,
        role=source.batch_scheduler_role,
        schedules=tuple(schedules),
    )


def batch_schedule_list_response(config: BatchSchedulerConfig) -> BatchScheduleListResponse:
    schedules = [_schedule_summary(schedule) for schedule in config.schedules]
    return BatchScheduleListResponse(
        scheduler_id=config.scheduler_id,
        interval_seconds=config.interval_seconds,
        tenant_id=config.tenant_id,
        region=config.region,
        booking_center_code=config.booking_center_code,
        schedule_count=len(schedules),
        enabled_schedule_count=sum(1 for schedule in config.schedules if schedule.enabled),
        schedules=schedules,
    )


def batch_scheduler_run_response(
    *,
    result: BatchSchedulerRunResult,
    caller_context: ReportCallerContext,
) -> BatchSchedulerRunResponse:
    return BatchSchedulerRunResponse(
        scheduler_id=result.scheduler_id,
        attempted_count=result.attempted_count,
        materialized_count=len(result.materialized),
        skipped_schedule_ids=list(result.skipped_schedule_ids),
        refused_schedule_ids=list(result.refused_schedule_ids),
        materialized=[
            BatchSchedulerMaterializationResponse(
                schedule_id=item.schedule_id,
                batch_id=item.batch_id,
                idempotency_key=item.idempotency_key,
                item_count=item.item_count,
                status=item.status,
            )
            for item in result.materialized
        ],
        correlation_id=caller_context.correlation_id,
        trace_id=caller_context.trace_id,
    )


def batch_scheduler_caller_context(
    config: BatchSchedulerConfig,
    *,
    pass_sequence: int,
) -> ReportCallerContext:
    suffix = _stable_short_hash(
        {
            "scheduler_id": config.scheduler_id,
            "pass_sequence": pass_sequence,
            "schedule_ids": [schedule.schedule_id for schedule in config.schedules],
        },
        length=12,
    )
    trace_id = _stable_short_hash(
        {
            "scheduler_id": config.scheduler_id,
            "pass_sequence": pass_sequence,
            "tenant_id": config.tenant_id,
            "region": config.region,
        },
        length=32,
    )
    return ReportCallerContext(
        trigger_type="system",
        triggered_by=config.scheduler_id,
        caller_application="lotus-report-batch-scheduler",
        tenant_id=config.tenant_id,
        region=config.region,
        booking_center_code=config.booking_center_code,
        role=config.role,
        correlation_id=f"corr-batch-scheduler-{pass_sequence}-{suffix}",
        trace_id=trace_id,
    )


class StoredScheduleSource(Protocol):
    """Provider of due stored schedule definitions for one scheduler scope."""

    def due_definitions_for_scheduler(
        self,
        *,
        tenant_id: str,
        region: str,
        booking_center_code: str | None,
        today: date,
    ) -> list[BatchScheduleDefinition]: ...


def _log_candidates_dropped(
    *,
    schedule_id: str,
    dropped: list[tuple[int, str]],
) -> None:
    """Record that a source refused candidates, without naming them.

    A bare `continue` made a refusing dependency indistinguishable from a
    portfolio that is not there: the pass reported success with nothing
    materialized, and nothing said why. Report still declines the candidates --
    attributing a portfolio it could not read is the defect this scheduler
    exists to avoid (issue #177) -- but the decline is now operable.

    Deliberately no `portfolio_id`. It is a client-sensitive identifier and
    `SAFE_OPERATOR_LOOKUP_FIELDS` excludes it, while `JsonFormatter` copies
    these fields verbatim into retained logs. The schedule is named instead,
    and its portfolio list is configuration the operator already holds.

    Aggregated per schedule for the same reason it is safer: one record saying
    twelve candidates were refused with 401 is what an operator acts on, where
    twelve records per pass is noise. Status codes are kept distinct because
    they differ operationally -- a 401 means Report is not presenting what the
    source requires, a 404 means the portfolio is absent.
    """
    if not dropped:
        return
    _LOGGER.warning(
        "scheduled_batch_candidates_dropped",
        extra={
            "extra_fields": {
                "schedule_id": schedule_id,
                "dropped_count": len(dropped),
                "source_status_codes": sorted({status for status, _ in dropped}),
                "reason_codes": sorted({reason for _, reason in dropped}),
                "source_system": "lotus-core",
            }
        },
    )


def _tenant_attribution_is_a_stamp(schedule: BatchScheduleDefinition) -> bool:
    """Would materializing this schedule attribute portfolios on no evidence?

    `all_active_portfolios` asks lotus-core an unqualified question - "every
    active portfolio" - and Report then labels whatever comes back with its own
    CONFIGURED tenant. Nothing in that exchange proves the returned portfolios
    belong to that tenant, so the label is configuration wearing the costume of
    evidence, and a portfolio owned by tenant B can be materialized into a
    durable batch attributed to tenant A (issue #177).

    Enumerated selectors are a different claim: a person or an authoritative
    contract named those specific portfolios under that tenant - the Gateway's
    trusted-scope front door for stored schedules, a deployment operator for
    configured ones. That is weaker than a source-owned tenant, which is why
    #177 stays open, but it is a claim someone made rather than one Report
    invented, so it is not refused here.

    lotus-core has owned the authoritative tenant since core#1076; when its
    discovery projects it (core#798 S2+), this becomes a verification against
    the source instead of a refusal.
    """

    return schedule.selector_mode == "all_active_portfolios"


#: Internal reasons for dropping a candidate on tenant evidence. Deliberately
#: two values, not one. Externally a refused candidate is simply not scheduled,
#: so a caller cannot tell "not yours" from "not there" -- but an operator
#: reading the drop log must, because they are different failures: a mismatch is
#: a portfolio genuinely owned by someone else, while an absent projection means
#: Core answered without the field this decision depends on.
SOURCE_TENANT_MISMATCH = "source_tenant_mismatch"
SOURCE_TENANT_ABSENT = "source_tenant_absent"


def _projected_tenant(
    payload: Mapping[str, Any], *, expected_tenant_id: str
) -> tuple[str | None, str | None]:
    """Return Core's projected tenant, or `(None, reason)` refusing the candidate.

    Three states, not two. `lotus-core#1094` made `tenant_id` a required,
    source-owned field on `PortfolioRecord`, so its absence is now a real signal
    rather than the permanent condition it used to be -- and absence must refuse
    rather than fall back, because falling back is precisely the defect: the
    scheduler stamping its own configuration onto a candidate and presenting
    configuration as evidence of ownership.

    A match returns Core's value rather than the caller's. They are equal here by
    construction, but taking it from the payload means the tenant on the
    candidate came from the source that owns it.
    """
    projected = str(payload.get("tenant_id") or "").strip()
    if not projected:
        return None, SOURCE_TENANT_ABSENT
    if projected != expected_tenant_id:
        return None, SOURCE_TENANT_MISMATCH
    return projected, None


class ReportBatchScheduler:
    def __init__(
        self,
        *,
        batch_ledger: BatchScheduleLedger,
        portfolio_source: CorePortfolioSource,
        stored_schedule_source: StoredScheduleSource | None = None,
    ) -> None:
        self._batch_ledger = batch_ledger
        self._portfolio_source = portfolio_source
        self._stored_schedule_source = stored_schedule_source

    async def run_due_schedules(
        self,
        *,
        config: BatchSchedulerConfig,
        caller_context: ReportCallerContext,
        evaluation_date: date | None = None,
    ) -> BatchSchedulerRunResult:
        materialized: list[BatchSchedulerMaterialization] = []
        skipped: list[str] = []
        refused: list[str] = []
        enabled_schedules = [schedule for schedule in config.schedules if schedule.enabled]
        if self._stored_schedule_source is not None:
            # Stored definitions join every pass - the daemon loop included, not just
            # the operator-triggered HTTP route - under the scheduler's full scope.
            enabled_schedules.extend(
                self._stored_schedule_source.due_definitions_for_scheduler(
                    tenant_id=config.tenant_id,
                    region=config.region,
                    booking_center_code=config.booking_center_code,
                    today=evaluation_date or date.today(),
                )
            )

        for schedule in enabled_schedules:
            if _tenant_attribution_is_a_stamp(schedule):
                # Refuse BEFORE discovery: asking the unqualified question and
                # then declining to use the answer would still have fetched a
                # portfolio set this scheduler cannot attribute.
                _LOGGER.warning(
                    "scheduled_batch_refused_unprovable_tenancy",
                    extra={
                        "extra_fields": {
                            "schedule_id": schedule.schedule_id,
                            "selector_mode": schedule.selector_mode,
                            "scheduler_id": config.scheduler_id,
                            "reason_code": "tenant_attribution_unprovable",
                        }
                    },
                )
                refused.append(schedule.schedule_id)
                continue
            candidates = await self._resolve_candidates(
                schedule=schedule,
                caller_context=caller_context,
                tenant_id=config.tenant_id,
                region=config.region,
            )
            if not candidates:
                skipped.append(schedule.schedule_id)
                continue
            if schedule.stable_cycle_identity and len(candidates) != len(schedule.portfolio_ids):
                # A transient upstream miss must not shrink the pack: the stable
                # cycle key would permanently claim this period for a subset, and
                # the dropped portfolio's report would never exist. Refuse the
                # cycle now; the next pass retries with the key unconsumed.
                _LOGGER.warning(
                    "stored_schedule_partial_resolution",
                    extra={
                        "extra_fields": {
                            "schedule_id": schedule.schedule_id,
                            "requested_count": len(schedule.portfolio_ids),
                            "resolved_count": len(candidates),
                        }
                    },
                )
                skipped.append(schedule.schedule_id)
                continue

            cycle = materialize_cycle(_cycle_request(schedule))
            portfolio_ids = [candidate.portfolio_id for candidate in candidates]
            request = BatchCreateRequest(
                selector_mode=schedule.selector_mode,
                portfolio_ids=portfolio_ids,
                source_candidates=candidates,
                as_of_date=cycle.as_of_date,
                requested_output_formats=schedule.requested_output_formats,
                reporting_currency=schedule.reporting_currency,
                options=_batch_options(schedule, cycle),
                max_batch_size=schedule.max_batch_size,
            )
            if schedule.stable_cycle_identity:
                # The key must survive cadence changes too: a monthly schedule
                # switched to quarterly after September 30 materialized still
                # resolves the same as-of date, and hashing frequency or period
                # bounds would mint a second batch for it. Schedule id plus as-of
                # date is the whole identity of a stored schedule's cycle.
                idempotency_key = stored_schedule_cycle_idempotency_key(
                    schedule_id=schedule.schedule_id,
                    as_of_date=cycle.as_of_date,
                )
            else:
                idempotency_key = scheduled_batch_idempotency_key(
                    caller_context=caller_context,
                    selector_mode=request.selector_mode,
                    cycle=cycle,
                    selector_identity=_selector_identity(schedule, portfolio_ids),
                )
                if self._batch_ledger.has_batch_for_schedule_cycle(
                    tenant_id=caller_context.tenant_id,
                    region=caller_context.region,
                    schedule_id=schedule.schedule_id,
                    period_start=cycle.period_start.isoformat(),
                    period_end=cycle.period_end.isoformat(),
                    as_of_date=cycle.as_of_date.isoformat(),
                ):
                    # This schedule's business cycle already has a batch -
                    # recognised by its durably recorded schedule id and
                    # period bounds, which is exact for every historical
                    # identity formula and template configuration. One
                    # cycle, one batch: mint nothing.
                    skipped.append(schedule.schedule_id)
                    continue
            try:
                batch = self._batch_ledger.create_batch(
                    request=request,
                    caller_context=caller_context,
                    idempotency_key=idempotency_key,
                )
            except BatchIdempotencyConflictError:
                # The idempotency key IS the business-cycle identity, so a
                # conflict means THIS cycle already has a batch - whatever
                # options hash it was created under (including batches minted
                # in the window when template values still rode the options).
                # One cycle yields one batch: updated content applies from
                # the next period, and this pass mints nothing.
                skipped.append(schedule.schedule_id)
                continue
            except ValueError:
                if not schedule.stable_cycle_identity:
                    raise
                # One advisor's stale stored schedule - a portfolio gone inactive,
                # an oversized pack, a validation refusal - must not abort the
                # whole pass and starve every other schedule. The catch is the
                # domain-failure family only (ValueError, which Pydantic's
                # ValidationError subclasses): infrastructure faults - PostgreSQL
                # down, pool exhausted, schema invalid - propagate and fail the
                # pass loudly, because they are not properties of one schedule.
                # Configured schedules keep raising even for domain failures:
                # theirs are deployment defects an operator must see.
                _LOGGER.exception(
                    "stored_schedule_materialization_failed",
                    extra={
                        "extra_fields": {
                            "schedule_id": schedule.schedule_id,
                            "tenant_id": config.tenant_id,
                        }
                    },
                )
                skipped.append(schedule.schedule_id)
                continue
            materialized.append(_materialization(schedule, batch, idempotency_key))

        return BatchSchedulerRunResult(
            scheduler_id=config.scheduler_id,
            attempted_count=len(enabled_schedules),
            materialized=tuple(materialized),
            skipped_schedule_ids=tuple(skipped),
            refused_schedule_ids=tuple(refused),
        )

    async def _resolve_candidates(
        self,
        *,
        schedule: BatchScheduleDefinition,
        caller_context: ReportCallerContext,
        tenant_id: str,
        region: str,
    ) -> list[PortfolioBatchCandidate]:
        if schedule.selector_mode == "batch_manifest":
            return await self._resolve_manifest_candidates(
                schedule=schedule,
                caller_context=caller_context,
                tenant_id=tenant_id,
                region=region,
            )

        candidates: list[PortfolioBatchCandidate] = []
        dropped: list[tuple[int, str]] = []
        for portfolio_id in schedule.portfolio_ids:
            status_code, payload = await self._portfolio_source.get_portfolio_detail(
                portfolio_id,
                correlation_id=caller_context.correlation_id,
            )
            if status_code != 200:
                dropped.append((status_code, "source_refused"))
                continue
            if str(payload.get("portfolio_id") or "") != portfolio_id:
                dropped.append((status_code, "source_identity_mismatch"))
                continue
            projected_tenant, tenant_refusal = _projected_tenant(
                payload, expected_tenant_id=tenant_id
            )
            if projected_tenant is None:
                dropped.append((status_code, tenant_refusal or SOURCE_TENANT_ABSENT))
                continue
            candidates.append(
                PortfolioBatchCandidate(
                    portfolio_id=portfolio_id,
                    tenant_id=projected_tenant,
                    region=region,
                    active=str(payload.get("status") or "").lower() == "active",
                    selected=True,
                    source_system="lotus-core",
                    source_object="Portfolio",
                )
            )
        _log_candidates_dropped(schedule_id=schedule.schedule_id, dropped=dropped)
        return candidates

    async def _resolve_manifest_candidates(
        self,
        *,
        schedule: BatchScheduleDefinition,
        caller_context: ReportCallerContext,
        tenant_id: str,
        region: str,
    ) -> list[PortfolioBatchCandidate]:
        manifest_by_id = {entry.portfolio_id: entry for entry in schedule.manifest_entries}
        candidates: list[PortfolioBatchCandidate] = []
        dropped: list[tuple[int, str]] = []
        for portfolio_id in manifest_by_id:
            status_code, payload = await self._portfolio_source.get_portfolio_detail(
                portfolio_id,
                correlation_id=caller_context.correlation_id,
            )
            if status_code != 200:
                dropped.append((status_code, "source_refused"))
                continue
            if str(payload.get("portfolio_id") or "") != portfolio_id:
                dropped.append((status_code, "source_identity_mismatch"))
                continue
            projected_tenant, tenant_refusal = _projected_tenant(
                payload, expected_tenant_id=tenant_id
            )
            if projected_tenant is None:
                dropped.append((status_code, tenant_refusal or SOURCE_TENANT_ABSENT))
                continue
            entry = manifest_by_id[portfolio_id]
            candidates.append(
                _candidate_from_portfolio_payload(
                    payload,
                    tenant_id=projected_tenant,
                    region=region,
                    selected=True,
                    source_system=entry.source_system,
                    source_object=entry.source_object,
                )
            )
        _log_candidates_dropped(schedule_id=schedule.schedule_id, dropped=dropped)
        return candidates


def _parse_schedule_definitions(raw: str) -> list[BatchScheduleDefinition]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BatchScheduleConfigError(
            "invalid_batch_schedules_json",
            "REPORT_BATCH_SCHEDULES_JSON must be valid JSON.",
        ) from exc
    if not isinstance(loaded, list):
        raise BatchScheduleConfigError(
            "invalid_batch_schedules_json",
            "REPORT_BATCH_SCHEDULES_JSON must be a JSON array.",
        )
    try:
        return [BatchScheduleDefinition.model_validate(item) for item in loaded]
    except ValidationError as exc:
        raise BatchScheduleConfigError(
            "invalid_batch_schedule_definition",
            "REPORT_BATCH_SCHEDULES_JSON contains an invalid schedule definition.",
        ) from exc


def _schedule_summary(schedule: BatchScheduleDefinition) -> BatchScheduleSummaryResponse:
    return BatchScheduleSummaryResponse(
        schedule_id=schedule.schedule_id,
        enabled=schedule.enabled,
        selector_mode=schedule.selector_mode,
        frequency=schedule.frequency,
        as_of_date=schedule.as_of_date,
        portfolio_count=len(schedule.portfolio_ids),
        manifest_entry_count=len(schedule.manifest_entries),
        requested_output_formats=schedule.requested_output_formats,
        reporting_currency=schedule.reporting_currency,
        max_batch_size=schedule.max_batch_size,
        manifest_source=schedule.manifest_source,
        manifest_version=schedule.manifest_version,
        manifest_hash=_manifest_hash(schedule)
        if schedule.selector_mode == "batch_manifest"
        else None,
        option_keys=sorted(schedule.options),
    )


def _cycle_request(schedule: BatchScheduleDefinition) -> BatchCycleRequest:
    return BatchCycleRequest(
        frequency=schedule.frequency,
        as_of_date=schedule.as_of_date,
        explicit_period_start=schedule.explicit_period_start,
        explicit_period_end=schedule.explicit_period_end,
    )


def _batch_options(schedule: BatchScheduleDefinition, cycle: Any) -> dict[str, Any]:
    options = {
        **schedule.options,
        "batch_schedule_id": schedule.schedule_id,
        "batch_selector_mode": schedule.selector_mode,
        "batch_frequency": cycle.frequency,
        "batch_period_start": cycle.period_start.isoformat(),
        "batch_period_end": cycle.period_end.isoformat(),
        # Template/package versions deliberately absent: they are
        # presentation contracts resolved at job acceptance, nothing
        # downstream consumes them from options, and hashing them here made
        # a mid-cycle presentation deployment CONFLICT the scheduler pass
        # (same template-free key, different request hash) instead of
        # converging idempotently.
    }
    if schedule.selector_mode == "batch_manifest":
        options["batch_manifest_source"] = schedule.manifest_source or "inline-schedule-manifest"
        options["batch_manifest_version"] = schedule.manifest_version or "v1"
        options["batch_manifest_hash"] = _manifest_hash(schedule)
    return options


def stored_schedule_cycle_idempotency_key(*, schedule_id: str, as_of_date: date) -> str:
    """One idempotency identity per stored schedule and as-of date.

    Deliberately independent of content, frequency, and period bounds: content
    updates apply from the next cycle, and a cadence change that re-selects an
    already-materialized as-of date converges instead of duplicating the pack.
    """

    digest = _stable_short_hash(
        {"schedule_id": schedule_id, "as_of_date": as_of_date.isoformat()},
        length=32,
    )
    return f"scheduled-batch-{digest}"


def _selector_identity(schedule: BatchScheduleDefinition, portfolio_ids: list[str]) -> str:
    return _stable_short_hash(
        {
            "schedule_id": schedule.schedule_id,
            "selector_mode": schedule.selector_mode,
            "portfolio_ids": portfolio_ids,
            "manifest_hash": _manifest_hash(schedule)
            if schedule.selector_mode == "batch_manifest"
            else None,
            "requested_output_formats": sorted(schedule.requested_output_formats),
            "reporting_currency": schedule.reporting_currency,
            "options": schedule.options,
            "max_batch_size": schedule.max_batch_size,
        },
        length=32,
    )


def _manifest_hash(schedule: BatchScheduleDefinition) -> str:
    if schedule.manifest_hash:
        return schedule.manifest_hash
    return _stable_short_hash(
        {
            "manifest_source": schedule.manifest_source or "inline-schedule-manifest",
            "manifest_version": schedule.manifest_version or "v1",
            "entries": [entry.model_dump(mode="json") for entry in schedule.manifest_entries],
        },
        length=32,
    )


def _portfolio_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("portfolios")
    if rows is None:
        rows = payload.get("items")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _candidate_from_portfolio_payload(
    payload: dict[str, Any],
    *,
    tenant_id: str,
    region: str,
    selected: bool,
    source_system: str = "lotus-core",
    source_object: str,
) -> PortfolioBatchCandidate:
    return PortfolioBatchCandidate(
        portfolio_id=str(payload.get("portfolio_id") or "").strip(),
        tenant_id=tenant_id,
        region=region,
        active=str(payload.get("status") or "").lower() == "active",
        selected=selected,
        source_system=source_system,
        source_object=source_object,
    )


def _stable_short_hash(payload: dict[str, Any], *, length: int) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:length]


def _materialization(
    schedule: BatchScheduleDefinition,
    batch: ReportBatchRecord,
    idempotency_key: str,
) -> BatchSchedulerMaterialization:
    return BatchSchedulerMaterialization(
        schedule_id=schedule.schedule_id,
        batch_id=batch.batch_id,
        idempotency_key=idempotency_key,
        item_count=batch.item_count,
        status=batch.status,
    )
