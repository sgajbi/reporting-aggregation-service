import os
import re
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.report_batch_orchestrator.dispatch import ReportBatchDispatcher
from app.report_batch_orchestrator.execution import BatchItemExecutionResult
from app.report_batch_orchestrator.ledger import (
    MissingBatchIdempotencyKeyError,
    ReportBatchLedger,
)
from app.report_batch_orchestrator.models import BatchCreateRequest, BatchDispatchPolicy
from app.report_batch_orchestrator.replay import (
    ReportBatchItemReplayService,
    get_report_batch_item_replay_service,
)
from app.report_batch_orchestrator.scheduler import (
    BatchScheduleConfigError,
    BatchScheduleDefinition,
    BatchSchedulerConfig,
    ReportBatchScheduler,
)
from app.report_batch_orchestrator.service import (
    get_report_batch_ledger,
    get_report_batch_scheduler,
    get_report_batch_worker,
)
from app.report_batch_orchestrator.worker import BatchWorkerRunResult, ReportBatchWorker
from app.reporting_jobs.ledger import ReportJobLedger, _dt_to_text
from app.reporting_jobs.models import PortfolioReviewJobRequest, ReportCallerContext
from app.reporting_jobs.service import get_report_job_ledger
from app.routers.report_batches import get_report_batch_scheduler_config


def _client(tmp_path):
    ledger = ReportBatchLedger(tmp_path / "batches.sqlite3")
    app.dependency_overrides[get_report_batch_ledger] = lambda: ledger
    return TestClient(app), ledger


def _client_with_report_jobs(tmp_path):
    batch_ledger = ReportBatchLedger(tmp_path / "batches.sqlite3")
    report_ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    replay_service = ReportBatchItemReplayService(
        batch_ledger=batch_ledger,
        report_job_ledger=report_ledger,
    )
    app.dependency_overrides[get_report_batch_ledger] = lambda: batch_ledger
    app.dependency_overrides[get_report_job_ledger] = lambda: report_ledger
    app.dependency_overrides[get_report_batch_item_replay_service] = lambda: replay_service
    return TestClient(app), batch_ledger, report_ledger


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def _headers(idempotency_key: str = "batch-portfolio-review-2026-04-22") -> dict[str, str]:
    return {
        "Idempotency-Key": idempotency_key,
        "X-Actor-Id": "advisor-123",
        "X-Caller-Application": "lotus-gateway",
        "X-Tenant-Id": "tenant-sg",
        "X-Region": "APAC",
        "X-Booking-Center-Code": "SG",
        "X-Role": "advisor",
        "X-Correlation-ID": "corr-batch-1",
        "X-Trace-ID": "trace-batch-1",
    }


def _operation_metric_value(
    metrics_body: str,
    *,
    operation: str,
    status: str,
    failure_category: str,
) -> float:
    pattern = re.compile(
        rf'lotus_report_operations_total\{{failure_category="{re.escape(failure_category)}",'
        rf'operation="{re.escape(operation)}",status="{re.escape(status)}"\}} ([0-9.]+)'
    )
    match = pattern.search(metrics_body)
    assert match is not None
    return float(match.group(1))


def _caller_context() -> ReportCallerContext:
    return ReportCallerContext(
        triggered_by="advisor-123",
        caller_application="lotus-gateway",
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        role="advisor",
        correlation_id="corr-batch-1",
        trace_id="trace-batch-1",
    )


def _payload() -> dict[str, object]:
    return {
        "selector_mode": "explicit_portfolio_list",
        "portfolio_ids": ["PB_SG_GLOBAL_BAL_001", "PB_SG_GLOBAL_BAL_002"],
        "source_candidates": [
            {
                "portfolio_id": "PB_SG_GLOBAL_BAL_001",
                "tenant_id": "tenant-sg",
                "region": "APAC",
                "active": True,
                "selected": True,
                "source_system": "lotus-core",
                "source_object": "PortfolioScope",
            },
            {
                "portfolio_id": "PB_SG_GLOBAL_BAL_002",
                "tenant_id": "tenant-sg",
                "region": "APAC",
                "active": True,
                "selected": True,
                "source_system": "lotus-core",
                "source_object": "PortfolioScope",
            },
        ],
        "as_of_date": "2026-04-22",
        "requested_output_formats": ["pdf"],
        "reporting_currency": "USD",
        "options": {"sections": ["OVERVIEW", "PERFORMANCE"]},
        "max_batch_size": 250,
    }


def test_report_batch_rejects_configuration_outside_the_published_catalogue(tmp_path):
    client, ledger = _client(tmp_path)
    payload = _payload()
    payload["options"] = {"template_id": "unapproved-client-template"}
    try:
        response = client.post(
            "/reports/batches",
            json=payload,
            headers=_headers("invalid-batch-configuration"),
        )
    finally:
        _clear_overrides()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "unsupported_report_configuration",
            "message": (
                "One or more report configuration fields are not available for this report family."
            ),
        }
    }
    assert ledger.batch_pressure_snapshot().dispatch_ready_items == 0


def _failed_batch_item_with_report_job(client, batch_ledger, report_ledger):
    batch = client.post(
        "/reports/batches",
        json=_payload(),
        headers=_headers("batch-item-replay-source"),
    ).json()
    batch_id = batch["batch_id"]
    item = batch_ledger.get_batch(batch_id).items[0]
    leased = batch_ledger.acquire_dispatch_items(
        batch_id=batch_id,
        worker_id="worker-replay-test",
        lease_seconds=300,
        limit=1,
    )[0]
    report_job = report_ledger.create_portfolio_review_job(
        request=PortfolioReviewJobRequest(
            portfolio_scope={"portfolio_ids": [item.portfolio_id]},
            as_of_date="2026-04-22",
            requested_output_formats=["pdf"],
            reporting_currency="USD",
            options={"sections": ["OVERVIEW", "PERFORMANCE"]},
        ),
        caller_context=_caller_context(),
        idempotency_key=leased.item_idempotency_key,
    )
    waiting = batch_ledger.mark_item_waiting_on_report_job(
        batch_item_id=leased.batch_item_id,
        lease_token=leased.lease_token,
        report_job_id=report_job.job_id,
    )
    report_ledger.mark_failed(
        job_id=report_job.job_id,
        actor="advisor-123",
        correlation_id="corr-batch-1",
        trace_id="trace-batch-1",
        failure_category="upstream_data_failed",
        failure_message="Upstream timeout.",
        retry_eligible=True,
    )
    failed_item = batch_ledger.mark_item_failed(
        batch_item_id=waiting.batch_item_id,
        error_category="upstream_data_failed",
        error_summary="Upstream timeout.",
        retryable=True,
    )
    return batch_id, failed_item, report_job


def _advance_report_job_to_archived(report_ledger, report_job, *, document_id: str):
    transition_context = {
        "actor": "advisor-123",
        "correlation_id": "corr-batch-1",
        "trace_id": "trace-batch-1",
    }
    report_ledger.mark_collecting_data(job_id=report_job.job_id, **transition_context)
    report_ledger.mark_data_ready(job_id=report_job.job_id, **transition_context)
    report_ledger.mark_rendering(
        job_id=report_job.job_id,
        render_job_id=f"rdr_{report_job.job_id}_pdf",
        output_format="pdf",
        template_id="portfolio-review",
        template_version="v1",
        **transition_context,
    )
    report_ledger.mark_completed(
        job_id=report_job.job_id,
        render_job_id=f"rdr_{report_job.job_id}_pdf",
        output_format="pdf",
        template_id="portfolio-review",
        template_version="v1",
        template_publication="development",
        artifact_sha256="a" * 64,
        bounded_determinism_fingerprint="b" * 64,
        runtime_engine="typst",
        runtime_engine_version="0.14.2",
        render_duration_ms=842,
        **transition_context,
    )
    archive_request_id = f"arch_{report_job.job_id}_pdf"
    report_ledger.mark_archiving(
        job_id=report_job.job_id,
        archive_request_id=archive_request_id,
        **transition_context,
    )
    return report_ledger.mark_archived(
        job_id=report_job.job_id,
        archive_request_id=archive_request_id,
        archive_document_id=document_id,
        **transition_context,
    )


class _WorkerRunSuccess:
    async def run_once(self, **kwargs):
        return BatchWorkerRunResult(
            batch_id=kwargs["batch_id"],
            batch_status_before="materialized",
            batch_status_after="completed",
            recovered_count=0,
            leased_count=1,
            dispatched_count=1,
            executed_count=1,
            report_job_ids=["rjob_batch_run_once"],
            back_pressure_reasons=[],
            execution_results=[
                BatchItemExecutionResult(
                    batch_id=kwargs["batch_id"],
                    batch_item_id="rbci_batch_run_once",
                    report_job_id="rjob_batch_run_once",
                    item_status="succeeded",
                    report_job_status="archived",
                )
            ],
        )


class _WorkerRunPaused:
    async def run_once(self, **kwargs):
        return BatchWorkerRunResult(
            batch_id=kwargs["batch_id"],
            batch_status_before="paused",
            batch_status_after="paused",
            recovered_count=0,
            leased_count=0,
            dispatched_count=0,
            executed_count=0,
            skipped_reason="batch_not_runnable:paused",
        )


class _PortfolioSource:
    async def get_portfolio_detail(self, portfolio_id, correlation_id=None):
        return 200, {
            "portfolio_id": portfolio_id,
            # report#177: Core projects the owning tenant and the scheduler now
            # requires it. A fake that omits it is a response shape Core no
            # longer produces, so the candidate would be correctly refused.
            "tenant_id": "tenant-sg",
            "status": "active",
        }

    async def list_portfolios(self, correlation_id=None):
        return 200, {
            "portfolios": [
                {
                    "portfolio_id": "PB_SG_GLOBAL_BAL_001",
                    "status": "active",
                }
            ]
        }


class _SchedulerForApi:
    def __init__(self, ledger, stored_schedule_source=None):
        self._scheduler = ReportBatchScheduler(
            batch_ledger=ledger,
            portfolio_source=_PortfolioSource(),
            stored_schedule_source=stored_schedule_source,
        )

    async def run_due_schedules(self, **kwargs):
        return await self._scheduler.run_due_schedules(**kwargs)


class _SchedulerFailure:
    async def run_due_schedules(self, **_kwargs):
        raise ValueError("scheduler_materialization_failed")


def _scheduler_config() -> BatchSchedulerConfig:
    return BatchSchedulerConfig(
        scheduler_id="scheduler-api-unit",
        interval_seconds=60.0,
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        role="system",
        schedules=(
            BatchScheduleDefinition(
                schedule_id="monthly-sg-global-bal-api",
                enabled=True,
                selector_mode="explicit_portfolio_list",
                frequency="monthly",
                as_of_date=date(2026, 4, 22),
                portfolio_ids=["PB_SG_GLOBAL_BAL_001"],
                requested_output_formats=["pdf"],
                reporting_currency="USD",
                options={"sections": ["OVERVIEW"]},
            ),
            BatchScheduleDefinition(
                schedule_id="disabled-schedule-api",
                enabled=False,
                selector_mode="explicit_portfolio_list",
                frequency="monthly",
                as_of_date=date(2026, 4, 22),
                portfolio_ids=["PB_SG_GLOBAL_BAL_002"],
            ),
        ),
    )


def test_report_batch_create_status_and_control_endpoints(tmp_path):
    client, _ledger = _client(tmp_path)
    try:
        create_response = client.post("/reports/batches", json=_payload(), headers=_headers())
        assert create_response.status_code == 202
        handle = create_response.json()
        batch_id = handle["batch_id"]

        status_response = client.get(f"/reports/batches/{batch_id}", headers=_headers())
        assert status_response.status_code == 200
        status_body = status_response.json()
        assert status_body["status"] == "materialized"
        assert status_body["status_counts"] == {"materialized": 2}
        assert [item["portfolio_id"] for item in status_body["items"]] == [
            "PB_SG_GLOBAL_BAL_001",
            "PB_SG_GLOBAL_BAL_002",
        ]

        pause_response = client.post(f"/reports/batches/{batch_id}:pause", headers=_headers())
        resume_response = client.post(f"/reports/batches/{batch_id}:resume", headers=_headers())
        retry_response = client.post(
            f"/reports/batches/{batch_id}:retry-failed", headers=_headers()
        )
        recover_response = client.post(
            f"/reports/batches/{batch_id}:recover-expired-leases",
            headers=_headers(),
        )
        cancel_response = client.post(f"/reports/batches/{batch_id}:cancel", headers=_headers())
        cancelled_status = client.get(f"/reports/batches/{batch_id}", headers=_headers()).json()

        assert pause_response.status_code == 200
        assert pause_response.json()["status"] == "paused"
        assert resume_response.status_code == 200
        assert resume_response.json()["status"] == "materialized"
        assert retry_response.status_code == 200
        assert retry_response.json()["affected_count"] == 0
        assert recover_response.status_code == 200
        assert recover_response.json()["recovered_count"] == 0
        assert cancel_response.status_code == 200
        assert cancel_response.json()["affected_count"] == 2
        assert cancelled_status["status"] == "cancelled"
        assert cancelled_status["status_counts"] == {"cancelled": 2}
    finally:
        _clear_overrides()


def test_report_batch_item_status_endpoint_returns_item_and_404s(tmp_path):
    client, _ledger = _client(tmp_path)
    try:
        create_response = client.post("/reports/batches", json=_payload(), headers=_headers())
        assert create_response.status_code == 202
        batch = create_response.json()
        batch_id = batch["batch_id"]

        status_response = client.get(f"/reports/batches/{batch_id}", headers=_headers())
        assert status_response.status_code == 200
        items = status_response.json()["items"]
        first_item = items[0]

        item_response = client.get(
            f"/reports/batches/{batch_id}/items/{first_item['batch_item_id']}",
            headers=_headers(),
        )
        assert item_response.status_code == 200
        item_body = item_response.json()

        assert item_body["batch_item_id"] == first_item["batch_item_id"]
        assert item_body["portfolio_id"] == "PB_SG_GLOBAL_BAL_001"
        assert item_body["status"] == "materialized"
        assert item_body["report_job_id"] is None
        assert item_body["report_job_status"] is None
        assert item_body["archive_document_id"] is None
        assert item_body["retry_eligible"] is False

        missing_item_response = client.get(
            f"/reports/batches/{batch_id}/items/rbci_missing_item",
            headers=_headers(),
        )
        assert missing_item_response.status_code == 404
        assert missing_item_response.json()["detail"]["code"] == "report_batch_item_not_found"

        missing_batch_item_response = client.get(
            f"/reports/batches/rbch_missing/items/{first_item['batch_item_id']}",
            headers=_headers(),
        )
        assert missing_batch_item_response.status_code == 404
        assert missing_batch_item_response.json()["detail"]["code"] == "report_batch_not_found"
    finally:
        _clear_overrides()


def test_report_batch_status_reads_hide_cross_tenant_batches_before_job_lookup(tmp_path):
    client, batch_ledger = _client(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("cross-tenant-status-source"),
        ).json()
        leased_item = batch_ledger.acquire_dispatch_items(
            batch_id=batch["batch_id"],
            worker_id="cross-tenant-status-test",
            lease_seconds=300,
            limit=1,
        )[0]
        linked_item = batch_ledger.mark_item_waiting_on_report_job(
            batch_item_id=leased_item.batch_item_id,
            lease_token=leased_item.lease_token,
            report_job_id="rjob_cross_tenant_must_not_be_queried",
        )

        class _JobLookupMustNotRun:
            def get_archive_statuses_by_job_ids(self, _job_ids, *, tenant_id):
                raise AssertionError("Cross-tenant reads must stop before report-job lookup.")

        app.dependency_overrides[get_report_job_ledger] = lambda: _JobLookupMustNotRun()
        other_tenant_headers = _headers()
        other_tenant_headers["X-Tenant-Id"] = "tenant-uk"

        batch_response = client.get(
            f"/reports/batches/{batch['batch_id']}",
            headers=other_tenant_headers,
        )
        item_response = client.get(
            f"/reports/batches/{batch['batch_id']}/items/{linked_item.batch_item_id}",
            headers=other_tenant_headers,
        )

        expected_not_found = {
            "detail": {
                "code": "report_batch_not_found",
                "message": "Report batch was not found.",
            }
        }
        assert batch_response.status_code == 404
        assert item_response.status_code == 404
        assert batch_response.json() == expected_not_found
        assert item_response.json() == expected_not_found
        for response_body in (batch_response.text, item_response.text):
            assert "tenant-sg" not in response_body
            assert "PB_SG_GLOBAL_BAL_001" not in response_body
            assert "archive_document_id" not in response_body
    finally:
        _clear_overrides()


def test_report_batch_status_composes_archived_and_delayed_document_truth(tmp_path):
    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("batch-archive-document-status"),
        ).json()
        leased_items = batch_ledger.acquire_dispatch_items(
            batch_id=batch["batch_id"],
            worker_id="worker-archive-status-test",
            lease_seconds=300,
            limit=2,
        )

        linked_items = []
        report_jobs = []
        for leased in leased_items:
            report_job = report_ledger.create_portfolio_review_job(
                request=PortfolioReviewJobRequest(
                    portfolio_scope={"portfolio_ids": [leased.portfolio_id]},
                    as_of_date="2026-04-22",
                    requested_output_formats=["pdf"],
                    reporting_currency="USD",
                    options={"sections": ["OVERVIEW", "PERFORMANCE"]},
                ),
                caller_context=_caller_context(),
                idempotency_key=leased.item_idempotency_key,
            )
            linked_items.append(
                batch_ledger.mark_item_waiting_on_report_job(
                    batch_item_id=leased.batch_item_id,
                    lease_token=leased.lease_token,
                    report_job_id=report_job.job_id,
                )
            )
            report_jobs.append(report_job)

        archived_job = _advance_report_job_to_archived(
            report_ledger,
            report_jobs[0],
            document_id="doc_batch_portfolio_001",
        )
        batch_ledger.mark_item_succeeded(
            batch_item_id=linked_items[0].batch_item_id,
            report_job_id=archived_job.job_id,
        )
        report_ledger.mark_collecting_data(
            job_id=report_jobs[1].job_id,
            actor="advisor-123",
            correlation_id="corr-batch-1",
            trace_id="trace-batch-1",
        )

        response = client.get(
            f"/reports/batches/{batch['batch_id']}",
            headers=_headers(),
        )
        assert response.status_code == 200
        items_by_portfolio = {item["portfolio_id"]: item for item in response.json()["items"]}

        archived_item = items_by_portfolio[leased_items[0].portfolio_id]
        assert archived_item["status"] == "succeeded"
        assert archived_item["report_job_status"] == "archived"
        assert archived_item["archive_document_id"] == "doc_batch_portfolio_001"

        delayed_item = items_by_portfolio[leased_items[1].portfolio_id]
        assert delayed_item["status"] == "waiting_on_report_job"
        assert delayed_item["report_job_status"] == "collecting_data"
        assert delayed_item["archive_document_id"] is None

        item_response = client.get(
            (f"/reports/batches/{batch['batch_id']}/items/{linked_items[0].batch_item_id}"),
            headers=_headers(),
        )
        assert item_response.status_code == 200
        assert item_response.json()["archive_document_id"] == "doc_batch_portfolio_001"
    finally:
        _clear_overrides()


def test_report_batch_status_fails_closed_for_missing_linked_report_job(tmp_path):
    client, batch_ledger, _report_ledger = _client_with_report_jobs(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("batch-missing-report-job-status"),
        ).json()
        leased = batch_ledger.acquire_dispatch_items(
            batch_id=batch["batch_id"],
            worker_id="worker-missing-job-test",
            lease_seconds=300,
            limit=1,
        )[0]
        linked = batch_ledger.mark_item_waiting_on_report_job(
            batch_item_id=leased.batch_item_id,
            lease_token=leased.lease_token,
            report_job_id="rjob_missing_source_record",
        )

        response = client.get(
            f"/reports/batches/{batch['batch_id']}/items/{linked.batch_item_id}",
            headers=_headers(),
        )

        assert response.status_code == 200
        assert response.json()["report_job_id"] == "rjob_missing_source_record"
        assert response.json()["report_job_status"] is None
        assert response.json()["archive_document_id"] is None
    finally:
        _clear_overrides()


def test_report_batch_status_keeps_correction_resolution_at_archive_boundary(tmp_path):
    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("batch-correction-boundary"),
        ).json()
        leased = batch_ledger.acquire_dispatch_items(
            batch_id=batch["batch_id"],
            worker_id="worker-correction-boundary-test",
            lease_seconds=300,
            limit=1,
        )[0]
        source_job = report_ledger.create_portfolio_review_job(
            request=PortfolioReviewJobRequest(
                portfolio_scope={"portfolio_ids": [leased.portfolio_id]},
                as_of_date="2026-04-22",
                requested_output_formats=["pdf"],
                reporting_currency="USD",
                options={"sections": ["OVERVIEW", "PERFORMANCE"]},
            ),
            caller_context=_caller_context(),
            idempotency_key=leased.item_idempotency_key,
        )
        linked = batch_ledger.mark_item_waiting_on_report_job(
            batch_item_id=leased.batch_item_id,
            lease_token=leased.lease_token,
            report_job_id=source_job.job_id,
        )
        archived_source = _advance_report_job_to_archived(
            report_ledger,
            source_job,
            document_id="doc_original_batch_output",
        )
        batch_ledger.mark_item_succeeded(
            batch_item_id=linked.batch_item_id,
            report_job_id=source_job.job_id,
        )

        replacement_job = report_ledger.create_portfolio_review_job(
            request=PortfolioReviewJobRequest(
                portfolio_scope={"portfolio_ids": [leased.portfolio_id]},
                as_of_date="2026-04-22",
                requested_output_formats=["pdf"],
                reporting_currency="USD",
                options={"sections": ["OVERVIEW", "PERFORMANCE"]},
            ),
            caller_context=_caller_context(),
            idempotency_key=f"{leased.item_idempotency_key}:replacement",
        )
        archived_replacement = _advance_report_job_to_archived(
            report_ledger,
            replacement_job,
            document_id="doc_replacement_output",
        )
        report_ledger.upsert_job_relationship(
            source_job=archived_source,
            derived_job=archived_replacement,
            relationship_type="regenerate_replacement",
            actor="advisor-123",
            reason="Corrected portfolio review output.",
            archive_consequence="replacement",
            previous_archive_document_id="doc_original_batch_output",
            new_archive_document_id="doc_replacement_output",
        )

        response = client.get(
            f"/reports/batches/{batch['batch_id']}/items/{linked.batch_item_id}",
            headers=_headers(),
        )

        assert response.status_code == 200
        assert response.json()["report_job_id"] == source_job.job_id
        assert response.json()["report_job_status"] == "archived"
        assert response.json()["archive_document_id"] == "doc_original_batch_output"
        assert response.json()["archive_document_id"] != "doc_replacement_output"
    finally:
        _clear_overrides()


def test_report_batch_item_replay_relinks_failed_item_idempotently(tmp_path):
    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    try:
        batch_id, failed_item, source_job = _failed_batch_item_with_report_job(
            client,
            batch_ledger,
            report_ledger,
        )
        headers = _headers(f"batch-item-replay-{failed_item.batch_item_id}-same-key")

        first = client.post(
            f"/reports/batches/{batch_id}/items/{failed_item.batch_item_id}/replay",
            json={"reason": "Retry item after upstream service recovered."},
            headers=headers,
        )
        assert first.status_code == 202
        body = first.json()
        assert body["batch_id"] == batch_id
        assert body["batch_item_id"] == failed_item.batch_item_id
        assert body["source_report_job_id"] == source_job.job_id
        assert body["replayed_report_job_id"] != source_job.job_id
        assert body["item_status"] == "waiting_on_report_job"
        replayed_item = batch_ledger.get_batch_item(batch_id, failed_item.batch_item_id)
        assert replayed_item.report_job_id == body["replayed_report_job_id"]
        assert replayed_item.retry_eligible is False
        replayed_status = client.get(
            f"/reports/batches/{batch_id}/items/{failed_item.batch_item_id}",
            headers=_headers(),
        )
        assert replayed_status.status_code == 200
        assert replayed_status.json()["report_job_id"] == body["replayed_report_job_id"]
        assert replayed_status.json()["report_job_status"] == "accepted"
        assert replayed_status.json()["archive_document_id"] is None
        second = client.post(
            f"/reports/batches/{batch_id}/items/{failed_item.batch_item_id}/replay",
            json={"reason": "Retry item after upstream service recovered."},
            headers=headers,
        )
        different_key = client.post(
            f"/reports/batches/{batch_id}/items/{failed_item.batch_item_id}/replay",
            json={"reason": "Different replay command must not duplicate the relink."},
            headers=_headers(f"batch-item-replay-{failed_item.batch_item_id}-different-key"),
        )

        assert second.status_code == 202
        assert second.json() == first.json()
        assert different_key.status_code == 409
        assert [
            event.event_type
            for event in report_ledger.list_status_events(source_job.job_id)
            if event.event_type == "batch_item_replay_requested"
        ] == ["batch_item_replay_requested"]
        metrics_body = client.get("/metrics").text
        assert (
            _operation_metric_value(
                metrics_body,
                operation="replay_command",
                status="accepted",
                failure_category="none",
            )
            >= 2.0
        )
        assert (
            _operation_metric_value(
                metrics_body,
                operation="replay_command",
                status="failed",
                failure_category="report_batch_item_cannot_be_replayed",
            )
            >= 1.0
        )
        operation_metric_lines = "\n".join(
            line
            for line in metrics_body.splitlines()
            if line.startswith("lotus_report_operations_total")
        )
        assert "batch_item_id" not in operation_metric_lines
        assert "report_job_id" not in operation_metric_lines
    finally:
        _clear_overrides()


def test_report_batch_item_replay_rejects_completed_and_leased_items(tmp_path):
    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    try:
        completed_batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("batch-item-replay-completed-source"),
        ).json()
        completed_leased = batch_ledger.acquire_dispatch_items(
            batch_id=completed_batch["batch_id"],
            worker_id="worker-replay-test",
            lease_seconds=300,
            limit=1,
        )[0]
        completed_job = report_ledger.create_portfolio_review_job(
            request=PortfolioReviewJobRequest(
                portfolio_scope={"portfolio_ids": [completed_leased.portfolio_id]},
                as_of_date="2026-04-22",
                requested_output_formats=["pdf"],
                reporting_currency="USD",
                options={"sections": ["OVERVIEW", "PERFORMANCE"]},
            ),
            caller_context=_caller_context(),
            idempotency_key=completed_leased.item_idempotency_key,
        )
        completed_waiting = batch_ledger.mark_item_waiting_on_report_job(
            batch_item_id=completed_leased.batch_item_id,
            lease_token=completed_leased.lease_token,
            report_job_id=completed_job.job_id,
        )
        completed_item = batch_ledger.mark_item_succeeded(
            batch_item_id=completed_waiting.batch_item_id,
            report_job_id=completed_job.job_id,
        )
        completed_response = client.post(
            f"/reports/batches/{completed_batch['batch_id']}/items/{completed_item.batch_item_id}/replay",
            json={"reason": "Should be rejected."},
            headers=_headers(f"batch-item-replay-{completed_item.batch_item_id}-completed"),
        )

        leased_batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("batch-item-replay-leased-source"),
        ).json()
        leased_item = batch_ledger.acquire_dispatch_items(
            batch_id=leased_batch["batch_id"],
            worker_id="worker-replay-test",
            lease_seconds=300,
            limit=1,
        )[0]
        leased_response = client.post(
            f"/reports/batches/{leased_batch['batch_id']}/items/{leased_item.batch_item_id}/replay",
            json={"reason": "Should be rejected."},
            headers=_headers(f"batch-item-replay-{leased_item.batch_item_id}-leased"),
        )

        assert completed_response.status_code == 409
        assert leased_response.status_code == 409
        assert completed_response.json()["detail"]["code"] == (
            "report_batch_item_cannot_be_replayed"
        )
        assert leased_response.json()["detail"]["code"] == "report_batch_item_cannot_be_replayed"
    finally:
        _clear_overrides()


def test_report_batch_item_replay_rejects_retry_ceiling(tmp_path):
    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    try:
        batch_id, failed_item, _source_job = _failed_batch_item_with_report_job(
            client,
            batch_ledger,
            report_ledger,
        )
        for _ in range(2):
            failed_item = batch_ledger.mark_item_failed(
                batch_item_id=failed_item.batch_item_id,
                error_category="upstream_data_failed",
                error_summary="Upstream timeout.",
                retryable=True,
            )

        response = client.post(
            f"/reports/batches/{batch_id}/items/{failed_item.batch_item_id}/replay",
            json={"reason": "Retry ceiling should prevent replay."},
            headers=_headers(f"batch-item-replay-{failed_item.batch_item_id}-terminal"),
        )

        assert failed_item.status == "failed_terminal"
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "report_batch_item_cannot_be_replayed"
    finally:
        _clear_overrides()


def test_report_batch_item_replay_error_mappings(tmp_path):
    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    try:
        batch_id, failed_item, _source_job = _failed_batch_item_with_report_job(
            client,
            batch_ledger,
            report_ledger,
        )

        missing_key = client.post(
            f"/reports/batches/{batch_id}/items/{failed_item.batch_item_id}/replay",
            json={"reason": "Missing idempotency key."},
            headers={key: value for key, value in _headers().items() if key != "Idempotency-Key"},
        )
        missing_item = client.post(
            f"/reports/batches/{batch_id}/items/rbit_missing/replay",
            json={"reason": "Missing item."},
            headers=_headers("batch-item-replay-missing-item"),
        )

        assert missing_key.status_code == 400
        assert missing_key.json()["detail"]["code"] == "missing_idempotency_key"
        assert missing_item.status_code == 404
        assert missing_item.json()["detail"]["code"] == "report_batch_item_not_found"
        metrics_body = client.get("/metrics").text
        assert (
            _operation_metric_value(
                metrics_body,
                operation="replay_command",
                status="failed",
                failure_category="missing_idempotency_key",
            )
            >= 1.0
        )
        assert (
            _operation_metric_value(
                metrics_body,
                operation="replay_command",
                status="failed",
                failure_category="report_batch_item_not_found",
            )
            >= 1.0
        )
    finally:
        _clear_overrides()


def test_report_batch_item_replay_legacy_cannot_replay_mapping() -> None:
    class _CannotReplayService:
        def replay_item(self, **_kwargs):
            raise ValueError("report_batch_item_cannot_be_replayed")

    app.dependency_overrides[get_report_batch_item_replay_service] = lambda: _CannotReplayService()
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/batches/rbch_replay/items/rbit_replay/replay",
            json={"reason": "Unsupported legacy replay state."},
            headers=_headers("batch-item-replay-legacy-conflict"),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "report_batch_item_cannot_be_replayed"
    finally:
        _clear_overrides()


def test_report_batch_scheduler_admin_list_and_run_due_endpoints(tmp_path):
    client, ledger = _client(tmp_path)
    app.dependency_overrides[get_report_batch_scheduler_config] = _scheduler_config
    app.dependency_overrides[get_report_batch_scheduler] = lambda: _SchedulerForApi(ledger)
    try:
        list_response = client.get("/reports/batch-schedules", headers=_headers())
        run_response = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 7},
            headers=_headers("scheduler-run-api"),
        )

        assert list_response.status_code == 200
        list_body = list_response.json()
        assert list_body["scheduler_id"] == "scheduler-api-unit"
        assert list_body["schedule_count"] == 2
        assert list_body["enabled_schedule_count"] == 1
        assert list_body["schedules"][0]["schedule_id"] == "monthly-sg-global-bal-api"
        assert list_body["schedules"][0]["option_keys"] == ["sections"]

        assert run_response.status_code == 200
        run_body = run_response.json()
        assert run_body["scheduler_id"] == "scheduler-api-unit"
        assert run_body["attempted_count"] == 1
        assert run_body["materialized_count"] == 1
        assert run_body["correlation_id"].startswith("corr-batch-scheduler-7-")
        assert run_body["materialized"][0]["schedule_id"] == "monthly-sg-global-bal-api"
        assert run_body["materialized"][0]["item_count"] == 1
        assert run_body["materialized"][0]["idempotency_key"].startswith("scheduled-batch-")
        metrics_body = client.get("/metrics").text
        assert (
            'lotus_report_operations_total{failure_category="none",'
            'operation="batch_scheduler_pass",status="completed"}'
        ) in metrics_body
        assert (
            'lotus_report_batch_scheduler_last_schedules{outcome="attempted"} 1.0'
        ) in metrics_body

        status_response = client.get(
            f"/reports/batches/{run_body['materialized'][0]['batch_id']}",
            headers=_headers("scheduler-status-api"),
        )
        assert status_response.status_code == 200
        status_body = status_response.json()
        assert status_body["materialized_portfolio_ids"] == ["PB_SG_GLOBAL_BAL_001"]
        assert status_body["status_counts"] == {"materialized": 1}
    finally:
        _clear_overrides()


def test_report_batch_scheduler_admin_routes_return_product_safe_errors(monkeypatch) -> None:
    def invalid_config() -> BatchSchedulerConfig:
        raise BatchScheduleConfigError(
            code="batch_scheduler_config_invalid",
            message="Configured report batch schedules could not be loaded.",
        )

    client = TestClient(app)
    try:
        monkeypatch.setattr(
            "app.routers.report_batches.batch_scheduler_config_from_settings",
            invalid_config,
        )

        list_config_error = client.get("/reports/batch-schedules", headers=_headers())
        run_config_error = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 8},
            headers=_headers("scheduler-run-invalid-config-api"),
        )

        monkeypatch.setattr(
            "app.routers.report_batches.batch_scheduler_config_from_settings",
            lambda: _scheduler_config(),
        )
        app.dependency_overrides[get_report_batch_scheduler_config] = _scheduler_config
        app.dependency_overrides[get_report_batch_scheduler] = lambda: _SchedulerFailure()
        run_failure = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 9},
            headers=_headers("scheduler-run-failure-api"),
        )

        assert list_config_error.status_code == 400
        assert list_config_error.json()["detail"] == {
            "code": "batch_scheduler_config_invalid",
            "message": "Configured report batch schedules could not be loaded.",
        }
        assert run_config_error.status_code == 400
        assert run_config_error.json()["detail"]["code"] == "batch_scheduler_config_invalid"
        assert run_failure.status_code == 409
        assert run_failure.json()["detail"] == {
            "code": "batch_scheduler_run_failed",
            "message": "Report batch scheduler pass could not be completed.",
        }
    finally:
        _clear_overrides()


def test_report_batch_create_accepts_all_active_and_manifest_selectors(tmp_path):
    client, _ledger = _client(tmp_path)
    try:
        all_active_payload = _payload()
        all_active_payload["selector_mode"] = "all_active_portfolios"
        all_active_payload["portfolio_ids"] = []
        all_active_response = client.post(
            "/reports/batches",
            json=all_active_payload,
            headers=_headers("batch-all-active-api"),
        )

        manifest_payload = _payload()
        manifest_payload["selector_mode"] = "batch_manifest"
        manifest_payload["portfolio_ids"] = ["PB_SG_GLOBAL_BAL_001"]
        manifest_payload["source_candidates"] = [
            {
                "portfolio_id": "PB_SG_GLOBAL_BAL_001",
                "tenant_id": "tenant-sg",
                "region": "APAC",
                "active": True,
                "selected": True,
                "source_system": "lotus-operations",
                "source_object": "BatchManifest",
            }
        ]
        manifest_payload["options"] = {
            "batch_manifest_source": "ops-manifest-apac-monthly",
            "batch_manifest_hash": "manifest-hash-001",
        }
        manifest_response = client.post(
            "/reports/batches",
            json=manifest_payload,
            headers=_headers("batch-manifest-api"),
        )

        assert all_active_response.status_code == 202
        all_active_status = client.get(
            all_active_response.json()["status_url"],
            headers=_headers("batch-all-active-status"),
        ).json()
        assert all_active_status["selector_mode"] == "all_active_portfolios"
        assert all_active_status["materialized_portfolio_ids"] == [
            "PB_SG_GLOBAL_BAL_001",
            "PB_SG_GLOBAL_BAL_002",
        ]

        assert manifest_response.status_code == 202
        manifest_status = client.get(
            manifest_response.json()["status_url"],
            headers=_headers("batch-manifest-status"),
        ).json()
        assert manifest_status["selector_mode"] == "batch_manifest"
        assert manifest_status["materialized_portfolio_ids"] == ["PB_SG_GLOBAL_BAL_001"]
    finally:
        _clear_overrides()


def test_report_batch_run_once_endpoint_returns_operator_safe_result(tmp_path):
    client, _ledger = _client(tmp_path)
    app.dependency_overrides[get_report_batch_worker] = lambda: _WorkerRunSuccess()
    try:
        create_response = client.post("/reports/batches", json=_payload(), headers=_headers())
        batch_id = create_response.json()["batch_id"]

        response = client.post(
            f"/reports/batches/{batch_id}:run-once",
            json={
                "worker_id": "lotus-report-batch-worker-unit",
                "recover_expired_leases": True,
                "runtime_load": {
                    "active_batches": 0,
                    "active_items": 0,
                    "active_upstream_jobs": 0,
                    "active_render_jobs": 0,
                    "active_archive_jobs": 0,
                },
            },
            headers=_headers(),
        )

        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "completed"
        assert body["batch_status_before"] == "materialized"
        assert body["batch_status_after"] == "completed"
        assert body["leased_count"] == 1
        assert body["dispatched_count"] == 1
        assert body["executed_count"] == 1
        assert body["report_job_ids"] == ["rjob_batch_run_once"]
        assert body["execution_results"] == [
            {
                "batch_item_id": "rbci_batch_run_once",
                "report_job_id": "rjob_batch_run_once",
                "item_status": "succeeded",
                "report_job_status": "archived",
                "failure_category": None,
                "retry_eligible": False,
            }
        ]
        assert body["status_url"] == f"/reports/batches/{batch_id}"
        metrics_body = client.get("/metrics").text
        assert (
            'lotus_report_operations_total{failure_category="none",'
            'operation="batch_worker_run",status="completed"}'
        ) in metrics_body
        assert 'lotus_report_batch_runtime_last_items{item_state="executed"} 1.0' in metrics_body
        assert (
            'lotus_report_batch_pressure_last_counts{pressure_state="dispatch_ready_items"} 2.0'
        ) in metrics_body
        assert (
            'lotus_report_batch_pressure_last_counts{pressure_state="active_items"} 0.0'
            in metrics_body
        )
    finally:
        _clear_overrides()


def test_report_batch_run_once_endpoint_reports_non_runnable_batch(tmp_path):
    client, _ledger = _client(tmp_path)
    app.dependency_overrides[get_report_batch_worker] = lambda: _WorkerRunPaused()
    try:
        create_response = client.post("/reports/batches", json=_payload(), headers=_headers())
        batch_id = create_response.json()["batch_id"]

        response = client.post(
            f"/reports/batches/{batch_id}:run-once",
            json={"worker_id": "lotus-report-batch-worker-unit"},
            headers=_headers(),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        assert response.json()["skipped_reason"] == "batch_not_runnable:paused"
        assert response.json()["dispatched_count"] == 0
        assert response.json()["executed_count"] == 0
    finally:
        _clear_overrides()


def test_report_batch_create_is_idempotent_and_rejects_conflicting_request(tmp_path):
    client, _ledger = _client(tmp_path)
    try:
        first = client.post("/reports/batches", json=_payload(), headers=_headers())
        second = client.post("/reports/batches", json=_payload(), headers=_headers())
        changed_payload = _payload()
        changed_payload["reporting_currency"] = "EUR"
        conflict = client.post("/reports/batches", json=changed_payload, headers=_headers())

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["batch_id"] == second.json()["batch_id"]
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    finally:
        _clear_overrides()


def test_report_batch_create_rejects_missing_context_and_invalid_selector(tmp_path):
    client, _ledger = _client(tmp_path)
    try:
        missing_context = client.post(
            "/reports/batches",
            json=_payload(),
            headers={"Idempotency-Key": "batch-missing-context"},
        )
        invalid_payload = _payload()
        invalid_payload["portfolio_ids"] = ["PB_SG_GLOBAL_BAL_999"]
        invalid_selector = client.post(
            "/reports/batches",
            json=invalid_payload,
            headers=_headers("batch-invalid-selector"),
        )

        assert missing_context.status_code == 400
        assert missing_context.json()["detail"]["code"] == "missing_caller_context"
        assert invalid_selector.status_code == 400
        assert invalid_selector.json()["detail"]["code"] == "portfolio_not_found"
    finally:
        _clear_overrides()


def test_report_batch_create_rejects_missing_idempotency_key(tmp_path):
    client, _ledger = _client(tmp_path)
    headers = _headers()
    headers.pop("Idempotency-Key")
    try:
        response = client.post("/reports/batches", json=_payload(), headers=headers)

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "missing_idempotency_key"
    finally:
        _clear_overrides()


def test_report_batch_create_maps_ledger_missing_idempotency_error(tmp_path):
    class MissingIdempotencyLedger:
        def create_batch(self, **_kwargs):
            raise MissingBatchIdempotencyKeyError

    client = TestClient(app)
    app.dependency_overrides[get_report_batch_ledger] = lambda: MissingIdempotencyLedger()
    try:
        response = client.post("/reports/batches", json=_payload(), headers=_headers())

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "missing_idempotency_key"
    finally:
        _clear_overrides()


def test_report_batch_status_and_control_return_not_found(tmp_path):
    client, _ledger = _client(tmp_path)
    try:
        status_response = client.get("/reports/batches/rbch_missing", headers=_headers())
        pause_response = client.post("/reports/batches/rbch_missing:pause", headers=_headers())

        assert status_response.status_code == 404
        assert status_response.json()["detail"]["code"] == "report_batch_not_found"
        assert pause_response.status_code == 404
        assert pause_response.json()["detail"]["code"] == "report_batch_not_found"
    finally:
        _clear_overrides()


def test_report_batch_run_once_maps_worker_failures():
    class MissingBatchWorker:
        async def run_once(self, **_kwargs):
            raise ValueError("report_batch_not_found")

    class InconsistentBatchWorker:
        async def run_once(self, **_kwargs):
            raise RuntimeError("batch_item_missing_lease_token")

    client = TestClient(app)
    try:
        app.dependency_overrides[get_report_batch_worker] = lambda: MissingBatchWorker()
        missing = client.post(
            "/reports/batches/rbch_missing:run-once",
            json={"worker_id": "lotus-report-batch-worker-unit"},
            headers=_headers(),
        )

        app.dependency_overrides[get_report_batch_worker] = lambda: InconsistentBatchWorker()
        inconsistent = client.post(
            "/reports/batches/rbch_inconsistent:run-once",
            json={"worker_id": "lotus-report-batch-worker-unit"},
            headers=_headers(),
        )
        metrics_body = client.get("/metrics").text

        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "report_batch_not_found"
        assert inconsistent.status_code == 409
        assert inconsistent.json()["detail"]["code"] == "batch_worker_run_failed"
        assert (
            'lotus_report_operations_total{failure_category="batch_worker_runtime_error",'
            'operation="batch_worker_run",status="failed"}' in metrics_body
        )
    finally:
        _clear_overrides()


def test_report_batch_status_and_controls_map_unexpected_ledger_errors():
    class UnexpectedLedger:
        def get_batch(self, _batch_id):
            raise ValueError("unexpected_batch_condition")

        def pause_batch(self, **_kwargs):
            raise ValueError("unexpected_batch_condition")

        def resume_batch(self, **_kwargs):
            raise ValueError("unexpected_batch_condition")

        def cancel_batch(self, **_kwargs):
            raise ValueError("unexpected_batch_condition")

        def retry_failed_items(self, **_kwargs):
            raise ValueError("unexpected_batch_condition")

        def recover_expired_leases(self, **_kwargs):
            raise ValueError("unexpected_batch_condition")

    client = TestClient(app)
    app.dependency_overrides[get_report_batch_ledger] = lambda: UnexpectedLedger()
    try:
        responses = [
            client.get("/reports/batches/rbch_problem", headers=_headers()),
            client.post("/reports/batches/rbch_problem:pause", headers=_headers()),
            client.post("/reports/batches/rbch_problem:resume", headers=_headers()),
            client.post("/reports/batches/rbch_problem:cancel", headers=_headers()),
            client.post("/reports/batches/rbch_problem:retry-failed", headers=_headers()),
            client.post(
                "/reports/batches/rbch_problem:recover-expired-leases",
                headers=_headers(),
            ),
        ]

        assert {response.status_code for response in responses} == {400}
        assert {response.json()["detail"]["code"] for response in responses} == {
            "batch_operation_failed"
        }
    finally:
        _clear_overrides()


def test_report_batch_status_and_control_require_caller_context(tmp_path):
    client, _ledger = _client(tmp_path)
    try:
        status_response = client.get("/reports/batches/rbch_missing")
        pause_response = client.post("/reports/batches/rbch_missing:pause")

        assert status_response.status_code == 400
        assert status_response.json()["detail"]["code"] == "missing_caller_context"
        assert pause_response.status_code == 400
        assert pause_response.json()["detail"]["code"] == "missing_caller_context"
    finally:
        _clear_overrides()


def test_report_batch_openapi_examples_are_complete_and_product_safe():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    create_post = schema["paths"]["/reports/batches"]["post"]
    create_example = create_post["requestBody"]["content"]["application/json"]["example"]
    handle_example = create_post["responses"]["202"]["content"]["application/json"]["example"]
    status_get = schema["paths"]["/reports/batches/{batch_id}"]["get"]
    status_example = status_get["responses"]["200"]["content"]["application/json"]["example"]
    archived_status_example = status_get["responses"]["200"]["content"]["application/json"][
        "examples"
    ]["archived_document_available"]["value"]
    retry_post = schema["paths"]["/reports/batches/{batch_id}:retry-failed"]["post"]
    replay_post = schema["paths"]["/reports/batches/{batch_id}/items/{batch_item_id}/replay"][
        "post"
    ]
    replay_request = replay_post["requestBody"]["content"]["application/json"]["example"]
    replay_response = replay_post["responses"]["202"]["content"]["application/json"]["example"]
    run_once_post = schema["paths"]["/reports/batches/{batch_id}:run-once"]["post"]
    run_once_request = run_once_post["requestBody"]["content"]["application/json"]["example"]
    run_once_response = run_once_post["responses"]["200"]["content"]["application/json"]["example"]
    schedule_list_get = schema["paths"]["/reports/batch-schedules"]["get"]
    schedule_list_response = schedule_list_get["responses"]["200"]["content"]["application/json"][
        "example"
    ]
    schedule_run_post = schema["paths"]["/reports/batch-schedules:run-due"]["post"]
    schedule_run_request = schedule_run_post["requestBody"]["content"]["application/json"][
        "example"
    ]
    schedule_run_response = schedule_run_post["responses"]["200"]["content"]["application/json"][
        "example"
    ]

    assert create_example["selector_mode"] == "explicit_portfolio_list"
    assert handle_example["batch_id"].startswith("rbch_")
    assert status_example["status_counts"] == {"materialized": 2}
    assert archived_status_example["items"][0]["report_job_status"] == "archived"
    assert archived_status_example["items"][0]["archive_document_id"].startswith("doc_")
    item_schema = schema["components"]["schemas"]["BatchItemStatusResponse"]
    assert "report_job_status" in item_schema["properties"]
    assert "archive_document_id" in item_schema["properties"]
    assert (
        "Populated only when that job is archived"
        in item_schema["properties"]["archive_document_id"]["description"]
    )
    assert replay_request["reason"]
    assert replay_response["source_report_job_id"].startswith("rjob_")
    assert replay_response["replayed_report_job_id"].startswith("rjob_")
    assert replay_response["replayed_report_job_id"] != replay_response["source_report_job_id"]
    assert replay_response["item_status"] == "waiting_on_report_job"
    assert run_once_request["worker_id"] == "lotus-report-batch-worker-1"
    assert run_once_response["executed_count"] == 2
    assert schedule_list_response["schedule_count"] == 1
    assert schedule_run_request["pass_sequence"] == 1
    assert schedule_run_response["materialized_count"] == 1
    assert "Report Batches" in create_post["tags"]
    assert "Report Batch Schedules" in schedule_list_get["tags"]
    assert "Use this endpoint" in create_post["description"]
    assert "retryable failed batch items" in retry_post["description"]
    assert "failed retry-eligible batch item" in replay_post["description"]
    assert "single-batch operator action" in run_once_post["description"]
    assert "config-backed" in schedule_list_get["description"]
    assert "operator-triggered scheduler pass" in schedule_run_post["description"]
    assert "RFC-" not in str(create_example)
    assert "RFC-" not in str(handle_example)
    assert "RFC-" not in str(status_example)
    assert "RFC-" not in str(replay_request)
    assert "RFC-" not in str(replay_response)
    assert "RFC-" not in str(run_once_request)
    assert "RFC-" not in str(run_once_response)
    assert "RFC-" not in str(schedule_list_response)
    assert "RFC-" not in str(schedule_run_request)
    assert "RFC-" not in str(schedule_run_response)
    for schema_name in [
        "BatchHandleResponse",
        "BatchStatusResponse",
        "BatchItemStatusResponse",
        "BatchControlResponse",
        "BatchItemReplayRequest",
        "BatchItemReplayResponse",
        "BatchRecoveryResponse",
        "BatchWorkerRunRequest",
        "BatchWorkerRunResponse",
        "BatchWorkerItemExecutionResponse",
        "BatchScheduleDefinitionListResponse",
        "BatchScheduleDefinitionDetailResponse",
        "BatchScheduleDefinitionCreateRequest",
        "BatchScheduleDefinitionUpdateRequest",
        "StoredBatchScheduleResponse",
        "BatchScheduleAuditRecord",
        "BatchScheduleSummaryResponse",
        "BatchSchedulerRunRequest",
        "BatchSchedulerRunResponse",
        "BatchSchedulerMaterializationResponse",
    ]:
        properties = schema["components"]["schemas"][schema_name]["properties"]
        for property_contract in properties.values():
            assert property_contract.get("description")


def test_report_batch_ledger_service_factory_uses_runtime_settings():
    if not os.environ.get("REPORT_JOB_LEDGER_DATABASE_URL"):
        pytest.skip("REPORT_JOB_LEDGER_DATABASE_URL is required for the factory proof")
    get_report_batch_ledger.cache_clear()
    try:
        ledger = get_report_batch_ledger()

        assert ledger.__class__.__name__ == "PostgresReportBatchLedger"
    finally:
        get_report_batch_ledger.cache_clear()


def test_reporting_attention_endpoint_returns_operator_safe_scan(tmp_path):
    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    report_job = report_ledger.create_portfolio_review_job(
        request=PortfolioReviewJobRequest(
            portfolio_scope={"portfolio_ids": ["PB_SG_GLOBAL_BAL_001"]},
            as_of_date="2026-04-22",
            requested_output_formats=["pdf"],
            reporting_currency="USD",
            options={"sections": ["OVERVIEW", "PERFORMANCE"]},
        ),
        caller_context=_caller_context(),
        idempotency_key="attention-api-report-job",
    )
    with report_ledger._connect() as connection:
        connection.execute(
            "UPDATE report_job SET updated_at = ? WHERE report_job_id = ?",
            (_dt_to_text(datetime(2026, 4, 28, 11, 0, tzinfo=UTC)), report_job.job_id),
        )
    batch = batch_ledger.create_batch(
        request=BatchCreateRequest.model_validate(_payload()),
        caller_context=_caller_context(),
        idempotency_key="attention-api-batch",
    )
    [leased_item] = batch_ledger.acquire_dispatch_items(
        batch_id=batch.batch_id,
        worker_id="worker-1",
        lease_seconds=7200,
        limit=1,
        now=datetime(2026, 4, 28, 11, 0, tzinfo=UTC),
    )

    try:
        response = client.get(
            "/reports/operations/attention",
            params={
                "report_job_stuck_threshold_seconds": 1,
                "batch_item_stuck_threshold_seconds": 1,
                "sla_breach_threshold_seconds": 1,
                "max_events": 10,
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["event_count"] >= 2
    resource_ids = {event["resource_id"] for event in payload["events"]}
    assert report_job.job_id in resource_ids
    assert leased_item.batch_item_id in resource_ids
    serialized = response.text
    assert "PB_SG_GLOBAL_BAL_001" not in serialized
    assert "tenant-sg" not in serialized
    assert "corr-batch-1" not in serialized
    assert "trace-batch-1" not in serialized


def _control_paths(batch_id: str) -> dict[str, str]:
    return {
        "pause": f"/reports/batches/{batch_id}:pause",
        "resume": f"/reports/batches/{batch_id}:resume",
        "cancel": f"/reports/batches/{batch_id}:cancel",
        "retry-failed": f"/reports/batches/{batch_id}:retry-failed",
        "recover-expired-leases": f"/reports/batches/{batch_id}:recover-expired-leases",
    }


EXPECTED_BATCH_NOT_FOUND = {
    "detail": {
        "code": "report_batch_not_found",
        "message": "Report batch was not found.",
    }
}


def test_report_batch_control_routes_reject_cross_tenant_callers_without_mutating(tmp_path):
    client, batch_ledger = _client(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("cross-tenant-control-source"),
        ).json()
        batch_id = batch["batch_id"]
        before = batch_ledger.get_batch(batch_id)

        other_tenant_headers = _headers()
        other_tenant_headers["X-Tenant-Id"] = "tenant-uk"

        for control, path in _control_paths(batch_id).items():
            response = client.post(path, headers=other_tenant_headers)
            assert response.status_code == 404, control
            assert response.json() == EXPECTED_BATCH_NOT_FOUND, control
            assert "tenant-sg" not in response.text, control
            assert "PB_SG_GLOBAL_BAL_001" not in response.text, control

        after = batch_ledger.get_batch(batch_id)
        assert after.status == before.status
        assert after.updated_at == before.updated_at
        assert [item.status for item in after.items] == [item.status for item in before.items]
        assert [item.lease_token for item in after.items] == [
            item.lease_token for item in before.items
        ]
    finally:
        _clear_overrides()


def test_report_batch_control_routes_do_not_disclose_cross_tenant_existence(tmp_path):
    """A batch owned by another tenant must answer exactly like an unknown identifier."""

    client, _ = _client(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("cross-tenant-control-disclosure"),
        ).json()
        other_tenant_headers = _headers()
        other_tenant_headers["X-Tenant-Id"] = "tenant-uk"

        known = _control_paths(batch["batch_id"])
        unknown = _control_paths("rbch_does_not_exist")
        for control, path in known.items():
            cross_tenant = client.post(path, headers=other_tenant_headers)
            absent = client.post(unknown[control], headers=other_tenant_headers)
            assert cross_tenant.status_code == absent.status_code == 404, control
            assert cross_tenant.json() == absent.json() == EXPECTED_BATCH_NOT_FOUND, control
    finally:
        _clear_overrides()


def test_report_batch_control_routes_remain_available_to_the_owning_tenant(tmp_path):
    client, batch_ledger = _client(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("same-tenant-control-source"),
        ).json()
        batch_id = batch["batch_id"]

        paused = client.post(f"/reports/batches/{batch_id}:pause", headers=_headers())
        resumed = client.post(f"/reports/batches/{batch_id}:resume", headers=_headers())
        retried = client.post(f"/reports/batches/{batch_id}:retry-failed", headers=_headers())
        recovered = client.post(
            f"/reports/batches/{batch_id}:recover-expired-leases",
            headers=_headers(),
        )
        cancelled = client.post(f"/reports/batches/{batch_id}:cancel", headers=_headers())

        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert resumed.status_code == 200
        assert retried.status_code == 200
        assert recovered.status_code == 200
        assert cancelled.status_code == 200
        assert batch_ledger.get_batch(batch_id).status == "cancelled"
    finally:
        _clear_overrides()


def test_report_batch_run_once_rejects_cross_tenant_callers_without_mutating(tmp_path):
    """The operator run-once route reaches the real worker, which must admit the caller."""

    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    app.dependency_overrides[get_report_batch_worker] = lambda: ReportBatchWorker(
        batch_ledger=batch_ledger,
        dispatcher=ReportBatchDispatcher(
            batch_ledger=batch_ledger,
            report_job_ledger=report_ledger,
            policy=BatchDispatchPolicy(max_active_items=5),
        ),
        execution_service=_ExecutionMustNotRun(),
    )
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("cross-tenant-run-once-source"),
        ).json()
        batch_id = batch["batch_id"]
        before = batch_ledger.get_batch(batch_id)

        other_tenant_headers = _headers()
        other_tenant_headers["X-Tenant-Id"] = "tenant-uk"
        response = client.post(
            f"/reports/batches/{batch_id}:run-once",
            json={"worker_id": "lotus-report-batch-worker-cross-tenant"},
            headers=other_tenant_headers,
        )

        assert response.status_code == 404
        assert response.json() == EXPECTED_BATCH_NOT_FOUND
        assert "tenant-sg" not in response.text
        after = batch_ledger.get_batch(batch_id)
        assert after.status == before.status
        assert [item.status for item in after.items] == [item.status for item in before.items]
        assert [item.report_job_id for item in after.items] == [None, None]
    finally:
        _clear_overrides()


class _ExecutionMustNotRun:
    async def execute_item(self, *, batch_id: str, batch_item_id: str):
        raise AssertionError("Cross-tenant run-once must stop before item execution.")


BATCH_SCOPED_MUTATION_ROUTES: dict[str, dict[str, object] | None] = {
    "/reports/batches/{batch_id}:pause": None,
    "/reports/batches/{batch_id}:resume": None,
    "/reports/batches/{batch_id}:cancel": None,
    "/reports/batches/{batch_id}:retry-failed": None,
    "/reports/batches/{batch_id}:recover-expired-leases": None,
    "/reports/batches/{batch_id}:run-once": {
        "worker_id": "lotus-report-batch-worker-admission-sweep"
    },
    "/reports/batches/{batch_id}/items/{batch_item_id}/replay": {
        "reason": "Cross-tenant admission sweep."
    },
}


def _discovered_batch_scoped_mutation_routes() -> set[str]:
    """Discover from the published contract, so the sweep covers what callers can reach."""

    return {
        path
        for path, operations in app.openapi()["paths"].items()
        if "{batch_id}" in path and "post" in operations
    }


def test_every_batch_scoped_mutation_route_admits_the_caller(tmp_path):
    """Fail closed when a batch-scoped mutation route has no cross-tenant admission case.

    This is the durable half of #170: the five control routes it found were unfenced
    because nothing observed that they were unfenced. A new route added without an
    entry here fails this test rather than shipping unadmitted.
    """

    assert _discovered_batch_scoped_mutation_routes() == set(BATCH_SCOPED_MUTATION_ROUTES), (
        "A batch-scoped mutation route was added or removed without updating its "
        "cross-tenant admission case in BATCH_SCOPED_MUTATION_ROUTES."
    )

    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    app.dependency_overrides[get_report_batch_worker] = lambda: ReportBatchWorker(
        batch_ledger=batch_ledger,
        dispatcher=ReportBatchDispatcher(
            batch_ledger=batch_ledger,
            report_job_ledger=report_ledger,
            policy=BatchDispatchPolicy(max_active_items=5),
        ),
        execution_service=_ExecutionMustNotRun(),
    )
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("admission-sweep-source"),
        ).json()
        batch_id = batch["batch_id"]
        batch_item_id = batch_ledger.get_batch(batch_id).items[0].batch_item_id
        before = batch_ledger.get_batch(batch_id)

        other_tenant_headers = _headers("admission-sweep-attempt")
        other_tenant_headers["X-Tenant-Id"] = "tenant-uk"

        for template, body in BATCH_SCOPED_MUTATION_ROUTES.items():
            path = template.format(batch_id=batch_id, batch_item_id=batch_item_id)
            response = client.post(path, json=body, headers=other_tenant_headers)
            assert response.status_code == 404, template
            assert response.json() == EXPECTED_BATCH_NOT_FOUND, template
            assert "tenant-sg" not in response.text, template

        after = batch_ledger.get_batch(batch_id)
        assert after.status == before.status
        assert [item.status for item in after.items] == [item.status for item in before.items]
        assert [item.report_job_id for item in after.items] == [
            item.report_job_id for item in before.items
        ]
    finally:
        _clear_overrides()


def test_batch_status_does_not_project_a_cross_tenant_linked_report_job(tmp_path):
    """Pre-fix state can link a batch to another tenant's job; status must not read it.

    Admission passes because the batch is genuinely the caller's. The mismatch is on the
    far side of the link, so the tenant has to travel with the lookup.
    """

    client, batch_ledger, report_ledger = _client_with_report_jobs(tmp_path)
    try:
        batch = client.post(
            "/reports/batches",
            json=_payload(),
            headers=_headers("cross-tenant-status-projection"),
        ).json()
        batch_id = batch["batch_id"]

        foreign_caller = _caller_context().model_copy(update={"tenant_id": "tenant-uk"})
        foreign_job = report_ledger.create_portfolio_review_job(
            request=PortfolioReviewJobRequest(
                portfolio_scope={"portfolio_ids": ["PB_UK_SECRET_001"]},
                as_of_date="2026-04-22",
                requested_output_formats=["pdf"],
                reporting_currency="USD",
                options={"sections": ["OVERVIEW"]},
            ),
            caller_context=foreign_caller,
            idempotency_key="foreign-tenant-linked-job",
        )
        leased = batch_ledger.acquire_dispatch_items(
            batch_id=batch_id,
            worker_id="cross-tenant-projection",
            lease_seconds=300,
            limit=1,
        )[0]
        linked = batch_ledger.mark_item_waiting_on_report_job(
            batch_item_id=leased.batch_item_id,
            lease_token=leased.lease_token,
            report_job_id=foreign_job.job_id,
        )

        batch_status = client.get(f"/reports/batches/{batch_id}", headers=_headers())
        item_status = client.get(
            f"/reports/batches/{batch_id}/items/{linked.batch_item_id}",
            headers=_headers(),
        )

        assert batch_status.status_code == 200
        assert item_status.status_code == 200
        for body in (batch_status.text, item_status.text):
            assert "tenant-uk" not in body
            assert "PB_UK_SECRET_001" not in body
        assert item_status.json()["report_job_status"] is None
        assert item_status.json()["archive_document_id"] is None
    finally:
        _clear_overrides()


def _schedule_definition_payload(**overrides):
    payload = {
        "cadence": "quarter_end",
        "portfolio_ids": ["PB_SG_GLOBAL_BAL_001"],
        "requested_output_formats": ["pdf"],
        "reporting_currency": "USD",
        "options": {"sections": ["OVERVIEW", "PERFORMANCE"]},
    }
    payload.update(overrides)
    return payload


def test_recurring_schedule_definition_lifecycle_via_api(tmp_path):
    """Issue #167 acceptance 1-3 (Report side): create, list with next_run_at,
    audit trail, tenant fencing, and disable-without-history-loss."""

    from app.report_batch_orchestrator.schedule_definitions import ScheduleDefinitionService
    from app.routers.report_batches import schedule_definition_service_dependency

    client, ledger = _client(tmp_path)
    app.dependency_overrides[schedule_definition_service_dependency] = lambda: (
        ScheduleDefinitionService(ledger)
    )
    try:
        created = client.post(
            "/reports/batch-schedules",
            headers=_headers(),
            json=_schedule_definition_payload(),
        )
        assert created.status_code == 201
        body = created.json()
        schedule_id = body["schedule_id"]
        assert schedule_id.startswith("rbsc_")
        assert body["tenant_id"] == "tenant-sg"
        assert body["owner_actor"] == "advisor-123"
        assert body["next_run_at"]

        retried = client.post(
            "/reports/batch-schedules",
            headers=_headers(),
            json=_schedule_definition_payload(),
        )
        assert retried.status_code == 201
        assert retried.json()["schedule_id"] == schedule_id

        listing = client.get("/reports/batch-schedules", headers=_headers())
        assert listing.status_code == 200
        defined = listing.json()["defined_schedules"]
        assert [entry["schedule_id"] for entry in defined] == [schedule_id]
        assert defined[0]["next_run_at"]

        detail = client.get(f"/reports/batch-schedules/{schedule_id}", headers=_headers())
        assert detail.status_code == 200
        assert [record["action"] for record in detail.json()["audit"]] == ["created"]

        foreign = client.get(
            f"/reports/batch-schedules/{schedule_id}",
            headers={**_headers(), "X-Tenant-Id": "tenant-uk"},
        )
        assert foreign.status_code == 404
        assert foreign.json()["detail"]["code"] == "batch_schedule_not_found"

        disabled = client.patch(
            f"/reports/batch-schedules/{schedule_id}",
            headers=_headers(),
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        after = client.get(f"/reports/batch-schedules/{schedule_id}", headers=_headers())
        assert [record["action"] for record in after.json()["audit"]] == [
            "created",
            "disabled",
        ]

        rejected = client.post(
            "/reports/batch-schedules",
            headers=_headers(),
            json=_schedule_definition_payload(options={"sections": ["NOT_A_SECTION"]}),
        )
        assert rejected.status_code == 400
    finally:
        _clear_overrides()


def test_run_due_materializes_a_stored_schedule_with_lineage(tmp_path):
    """Issue #167 acceptance 1: a stored quarter-end schedule materializes a batch
    through the ordinary scheduler pass, and the items carry the schedule id in
    their lineage options exactly as configured schedules do."""

    from app.report_batch_orchestrator.schedule_definitions import ScheduleDefinitionService
    from app.routers.report_batches import schedule_definition_service_dependency

    client, ledger = _client(tmp_path)
    definition_service = ScheduleDefinitionService(ledger)
    app.dependency_overrides[get_report_batch_scheduler_config] = _scheduler_config
    app.dependency_overrides[get_report_batch_scheduler] = lambda: _SchedulerForApi(
        ledger, stored_schedule_source=definition_service
    )
    app.dependency_overrides[schedule_definition_service_dependency] = lambda: definition_service
    try:
        created = client.post(
            "/reports/batch-schedules",
            headers=_headers(),
            json=_schedule_definition_payload(),
        )
        assert created.status_code == 201
        schedule_id = created.json()["schedule_id"]
        next_run = created.json()["next_run_at"]

        run = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 11, "evaluation_date": next_run},
            headers=_headers("stored-schedule-run"),
        )
        assert run.status_code == 200
        run_body = run.json()
        stored_runs = [
            entry for entry in run_body["materialized"] if entry["schedule_id"] == schedule_id
        ]
        assert len(stored_runs) == 1
        batch = ledger.get_batch(stored_runs[0]["batch_id"])
        assert batch.options["batch_schedule_id"] == schedule_id
        assert batch.options["batch_frequency"] == "quarterly"
        assert batch.as_of_date.isoformat() == next_run

        rerun = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 12, "evaluation_date": next_run},
            headers=_headers("stored-schedule-rerun"),
        )
        assert rerun.status_code == 200
        rerun_entries = [
            entry for entry in rerun.json()["materialized"] if entry["schedule_id"] == schedule_id
        ]
        assert len(rerun_entries) == 1
        assert rerun_entries[0]["batch_id"] == stored_runs[0]["batch_id"]

        # Patching content after the period materialized must not mint a second
        # batch for the same cycle: stored schedules carry a stable cycle identity,
        # and the change applies from the next period.
        client.patch(
            f"/reports/batch-schedules/{schedule_id}",
            headers=_headers(),
            json={"reporting_currency": "SGD"},
        )
        patched_rerun = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 15, "evaluation_date": next_run},
            headers=_headers("stored-schedule-patched-rerun"),
        )
        assert patched_rerun.status_code == 200
        # One cycle, one batch: the already-materialized period is reported as
        # skipped and nothing new is minted; the new content applies next period.
        assert [
            entry
            for entry in patched_rerun.json()["materialized"]
            if entry["schedule_id"] == schedule_id
        ] == []
        assert schedule_id in patched_rerun.json()["skipped_schedule_ids"]

        before_due = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 13, "evaluation_date": "2026-08-30"},
            headers=_headers("stored-schedule-not-due"),
        )
        assert before_due.status_code == 200
        assert [
            entry
            for entry in before_due.json()["materialized"]
            if entry["schedule_id"] == schedule_id
        ] == []

        client.patch(
            f"/reports/batch-schedules/{schedule_id}",
            headers=_headers(),
            json={"enabled": False},
        )
        disabled_run = client.post(
            "/reports/batch-schedules:run-due",
            json={"pass_sequence": 14, "evaluation_date": next_run},
            headers=_headers("stored-schedule-disabled"),
        )
        assert disabled_run.status_code == 200
        assert [
            entry
            for entry in disabled_run.json()["materialized"]
            if entry["schedule_id"] == schedule_id
        ] == []
    finally:
        _clear_overrides()


def test_schedule_definition_routes_fail_closed_without_tenant_scope(tmp_path):
    """A caller context whose tenant header is blank cannot define or see stored
    schedules: create refuses with a typed 400, the list omits stored definitions."""

    from app.report_batch_orchestrator.schedule_definitions import ScheduleDefinitionService
    from app.routers.report_batches import schedule_definition_service_dependency

    client, ledger = _client(tmp_path)
    app.dependency_overrides[schedule_definition_service_dependency] = lambda: (
        ScheduleDefinitionService(ledger)
    )
    app.dependency_overrides[get_report_batch_scheduler_config] = _scheduler_config
    blank_tenant = {**_headers(), "X-Tenant-Id": ""}
    try:
        # The caller-context dependency refuses a blank tenant before any schedule
        # code runs; the service keeps its own scope guard for non-HTTP callers.
        refused = client.post(
            "/reports/batch-schedules",
            headers=blank_tenant,
            json=_schedule_definition_payload(),
        )
        assert refused.status_code == 400
        assert refused.json()["detail"]["code"] == "missing_caller_context"

        listing = client.get("/reports/batch-schedules", headers=blank_tenant)
        assert listing.status_code == 400
        assert listing.json()["detail"]["code"] == "missing_caller_context"

        missing = client.patch(
            "/reports/batch-schedules/rbsc_absent",
            headers=_headers(),
            json={"enabled": False},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "batch_schedule_not_found"
    finally:
        _clear_overrides()
