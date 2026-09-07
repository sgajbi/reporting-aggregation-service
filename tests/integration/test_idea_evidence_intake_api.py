from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.idea_evidence_intake.materialization_contract import (
    IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION,
)
from app.idea_evidence_intake.service import IdeaEvidenceIntakeLedger
from app.main import app
from app.reporting_jobs.ledger import MissingIdempotencyKeyError, ReportJobLedger
from app.reporting_jobs.models import ReportJobListFilters
from app.reporting_jobs.service import get_report_job_ledger
from app.reporting_lineage.models import (
    ReportInputSnapshotCreateRequest,
    ReportUpstreamCallCreateRequest,
)
from app.reporting_lineage.service import get_portfolio_review_snapshot_capture_service
from app.reporting_lineage.store import ReportInputSnapshotStore
from app.reporting_render.service import (
    PortfolioReviewRenderOrchestrationService,
    get_portfolio_review_render_orchestration_service,
)
from app.routers.idea_evidence_intake import (
    get_idea_evidence_intake_ledger,
    get_idea_evidence_retention_policy_resolver,
)


def test_idea_evidence_intake_route_accepts_handoff_without_materialization() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: ledger
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs",
            json=_payload(),
            headers=_headers("idea-report-intake-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    assert body["intake_status"] == "accepted"
    assert body["route_existence_proven"] is True
    assert body["materialization_proven"] is False
    assert body["creates_report_job"] is False
    assert body["creates_rendered_output"] is False
    assert body["creates_archive_record"] is False
    assert body["grants_client_publication_authority"] is False
    assert body["supportability_status"] == "not_certified"
    assert body["correlation_id"] == "corr-idea-report-intake"
    assert "POST /reports/idea-evidence-packs" in body["evidence_refs"]


def test_idea_evidence_intake_route_replays_same_idempotency_key() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: ledger
    client = TestClient(app)
    try:
        first = client.post(
            "/reports/idea-evidence-packs",
            json=_payload(),
            headers=_headers("idea-report-intake-001"),
        )
        second = client.post(
            "/reports/idea-evidence-packs",
            json=_payload(),
            headers=_headers("idea-report-intake-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == first.json()


def test_idea_evidence_intake_route_conflicts_on_changed_payload_replay() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: ledger
    client = TestClient(app)
    changed_payload = {**_payload(), "report_evidence_pack_id": "irep_changed"}
    try:
        first = client.post(
            "/reports/idea-evidence-packs",
            json=_payload(),
            headers=_headers("idea-report-intake-001"),
        )
        second = client.post(
            "/reports/idea-evidence-packs",
            json=changed_payload,
            headers=_headers("idea-report-intake-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idea_evidence_intake_conflict"


def test_idea_evidence_intake_route_conflicts_after_fresh_ledger_restart(tmp_path) -> None:
    db_path = tmp_path / "idea-intake.sqlite3"
    first_ledger = IdeaEvidenceIntakeLedger(db_path)
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: first_ledger
    client = TestClient(app)
    try:
        first = client.post(
            "/reports/idea-evidence-packs",
            json=_payload(),
            headers=_headers("idea-report-intake-restart"),
        )
    finally:
        app.dependency_overrides.clear()

    restarted_ledger = IdeaEvidenceIntakeLedger(db_path)
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: restarted_ledger
    changed_payload = {**_payload(), "report_evidence_pack_id": "irep_changed"}
    try:
        replay = client.post(
            "/reports/idea-evidence-packs",
            json=changed_payload,
            headers=_headers("idea-report-intake-restart"),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "idea_evidence_intake_conflict"
    records = restarted_ledger.snapshot()
    assert (
        records[("tenant-sg", "idea-report-intake-restart")].caller_context["triggered_by"]
        == "advisor-123"
    )
    assert records[("tenant-sg", "idea-report-intake-restart")].caller_context["trace_id"] == (
        "trace-idea-report-intake"
    )


def test_idea_evidence_intake_route_rejects_publication_or_render_claims() -> None:
    client = TestClient(app)
    payload = {
        **_payload(),
        "grants_client_publication_authority": True,
        "creates_rendered_output": True,
        "creates_archive_record": True,
    }

    response = client.post(
        "/reports/idea-evidence-packs",
        json=payload,
        headers=_headers("idea-report-intake-unsafe"),
    )

    assert response.status_code == 422


def test_idea_evidence_intake_route_requires_idempotency_key() -> None:
    client = TestClient(app)
    headers = _headers("idea-report-intake-missing-key")
    headers.pop("Idempotency-Key")

    response = client.post("/reports/idea-evidence-packs", json=_payload(), headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "missing_idempotency_key"


def test_idea_evidence_intake_rejects_unknown_policy_before_persistence() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: ledger
    payload = {**_payload(), "retention_policy_ref": "unknown-policy"}
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs",
            json=payload,
            headers=_headers("idea-report-intake-unknown-policy"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_retention_policy"
    assert ledger.snapshot() == {}


def test_idea_evidence_intake_rejects_policy_for_wrong_tenant() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: ledger
    headers = {**_headers("idea-report-intake-wrong-tenant"), "X-Tenant-Id": "tenant-uk"}
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs",
            json=_payload(),
            headers=headers,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "retention_policy_tenant_mismatch"
    assert ledger.snapshot() == {}


def test_idea_evidence_materialization_route_creates_archived_report_job(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    intake_ledger = IdeaEvidenceIntakeLedger()
    capture_service = _IdeaEvidenceCaptureService(ledger, lineage_store)
    render_service = PortfolioReviewRenderOrchestrationService(
        render_client=_SuccessfulRenderClient(),
        snapshot_store=lineage_store,
        job_ledger=ledger,
    )
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: intake_ledger
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        capture_service
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        render_service
    )
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=_headers("idea-report-materialization-001"),
        )
        replay = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=_headers("idea-report-materialization-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert replay.status_code == 202
    body = response.json()
    assert replay.json() == body
    assert body["status"] == "archived"
    assert body["materialization_status"] == "archived"
    assert body["producer"] == "lotus-idea"
    assert body["source_authority"] == {
        "idea_evidence": "lotus-idea",
        "report_materialization": "lotus-report",
        "rendering": "lotus-render",
        "archive_record": "lotus-archive",
        "client_publication": "blocked",
    }
    assert body["report_package_identity"] == {
        "report_evidence_pack_id": "irep_001",
        "conversion_intent_id": "icnv_001",
        "candidate_id": "icand_001",
        "evidence_packet_id": "ievp_001",
        "evidence_content_fingerprint": "sha256:idea-evidence-content",
        "source_contract_version": "lotus_idea_evidence_pack_report_input.v1",
        "owned_product": "lotus-report:ClientReportEvidencePack:v1",
    }
    assert body["materialization_proven"] is True
    assert body["creates_report_job"] is True
    assert body["creates_rendered_output"] is True
    assert body["creates_archive_record"] is True
    assert body["grants_client_publication_authority"] is False
    assert body["supported_feature_promoted"] is False
    assert body["supportability_status"] == "not_certified"
    assert body["render_job_id"] == f"rdr_{body['report_job_id']}_pdf"
    assert body["archive_document_id"] == "doc_idea_evidence_pack_001"
    assert body["remaining_blockers"] == [
        "client_publication_authority_blocked",
        "supported_feature_promotion_missing",
    ]
    assert (
        "contracts/idea-evidence-materialization/"
        "lotus-report-idea-evidence-pack-materialization.v1.json"
    ) in body["evidence_refs"]
    record = ledger.get_job(body["report_job_id"])
    assert record.report_type == "proof_pack"
    assert record.archive_document_id == "doc_idea_evidence_pack_001"
    snapshot = lineage_store.get_snapshot_by_job(body["report_job_id"])
    assert snapshot.lineage_summary["source_services"] == ["lotus-idea"]
    upstream_calls = lineage_store.list_upstream_calls(snapshot.snapshot_id)
    assert upstream_calls[0].service_name == "lotus-idea"
    assert upstream_calls[0].endpoint == "/reports/idea-evidence-packs/materializations"
    assert upstream_calls[0].contract_version == "LotusIdeaEvidencePackReportInput.1.0"


def test_idea_evidence_materialization_recovers_exact_receipt_after_restart(tmp_path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    ledger = ReportJobLedger(database_path)
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _IdeaEvidenceCaptureService(ledger, lineage_store)
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    payload = {**_materialization_payload(), "requested_output_formats": ["json"]}
    client = TestClient(app)
    try:
        submitted = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-recovery"),
        )
        restarted_ledger = ReportJobLedger(database_path)
        app.dependency_overrides[get_report_job_ledger] = lambda: restarted_ledger
        recovered = client.get(
            "/reports/idea-evidence-packs/materializations",
            params=_recovery_query("idea-report-materialization-recovery"),
            headers=_recovery_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert submitted.status_code == 202
    assert recovered.status_code == 200
    assert recovered.json() == submitted.json()
    assert submitted.json()["source_event_version"] > 0
    assert len(restarted_ledger.list_jobs(filters=ReportJobListFilters(limit=2))) == 1


def test_idea_evidence_materialization_post_replays_legacy_record_without_recovery_identity(
    tmp_path,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    ledger = ReportJobLedger(database_path)
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _IdeaEvidenceCaptureService(ledger, lineage_store)
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    payload = {**_materialization_payload(), "requested_output_formats": ["json"]}
    client = TestClient(app)
    try:
        submitted = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-legacy-replay"),
        )
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT options_json FROM report_request WHERE idempotency_key = ?",
                ("idea-report-materialization-legacy-replay",),
            ).fetchone()
            assert row is not None
            options = json.loads(row[0])
            options.pop(IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION)
            connection.execute(
                "UPDATE report_request SET options_json = ? WHERE idempotency_key = ?",
                (json.dumps(options), "idea-report-materialization-legacy-replay"),
            )
            # closing() only closes. Unlike `with sqlite3.connect(...)`, which commits
            # on clean exit, it leaves the transaction open -- so the strip must be
            # committed explicitly before the replay is issued against it.
            connection.commit()

        replayed = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-legacy-replay"),
        )
        recovered = client.get(
            "/reports/idea-evidence-packs/materializations",
            params=_recovery_query("idea-report-materialization-legacy-replay"),
            headers=_recovery_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert submitted.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json() == submitted.json()
    assert recovered.status_code == 409
    assert recovered.json()["detail"]["code"] == "idea_materialization_identity_conflict"


def test_idea_evidence_materialization_refuses_legacy_replay_when_intake_ledger_was_lost(
    tmp_path,
) -> None:
    """A legacy replay validated by nothing must refuse, not echo the original.

    The POST fallback for pre-identity records is justified by the intake
    ledger having already compared this request's payload fingerprint --
    candidate_id and conversion_intent_id included -- against a stored one. If
    that ledger has been lost or reset, it accepts the request as new and
    compares nothing, while the report-side request hash still matches because
    those fields are absent from it. A materially different request would then
    receive the original request's response.
    """
    database_path = tmp_path / "jobs.sqlite3"
    ledger = ReportJobLedger(database_path)
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    # A capture that leaves the job `accepted`, so the replay reaches the
    # capture branch. With a job already carried to `data_ready` neither branch
    # is reachable and the test passes whether or not the refusal comes first --
    # which is exactly how the first version of this test missed the ordering.
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _AcceptedLeavingCaptureService()
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    payload = {**_materialization_payload(), "requested_output_formats": ["json"]}
    client = TestClient(app)
    try:
        submitted = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-lost-intake"),
        )
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT options_json FROM report_request WHERE idempotency_key = ?",
                ("idea-report-materialization-lost-intake",),
            ).fetchone()
            assert row is not None
            options = json.loads(row[0])
            options.pop(IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION)
            connection.execute(
                "UPDATE report_request SET options_json = ? WHERE idempotency_key = ?",
                (json.dumps(options), "idea-report-materialization-lost-intake"),
            )
            connection.commit()

        # The disaster-recovery state: PostgreSQL kept the report row, the
        # SQLite intake ledger did not survive.
        (tmp_path / "intake.sqlite3").unlink()

        # From here, any work done on the replay's behalf fails the test.
        app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
            _UnexpectedCaptureService()
        )

        altered = {
            **payload,
            "idea_evidence_pack": {
                **payload["idea_evidence_pack"],
                "candidate_id": "icand_substituted",
            },
        }
        replayed = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=altered,
            headers=_headers("idea-report-materialization-lost-intake"),
        )
        # The same altered request again. If the refused attempt was itself
        # stored in the intake ledger, this one finds a prior record and the
        # refusal turns into an acceptance -- the guard defeated by a retry.
        retried = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=altered,
            headers=_headers("idea-report-materialization-lost-intake"),
        )
    finally:
        app.dependency_overrides.clear()

    assert submitted.status_code == 202
    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "idea_materialization_identity_conflict"
    # Not the original receipt under a different candidate.
    assert replayed.json() != submitted.json()
    # A refusal must be repeatable. Refusing once and accepting the identical
    # request on retry is not a refusal, and is what happens if the rejected
    # attempt is persisted as history for the next one to find.
    assert retried.status_code == 409
    assert retried.json()["detail"]["code"] == "idea_materialization_identity_conflict"
    assert retried.json() != submitted.json()
    # The refusal came before any work: _UnexpectedCaptureService and
    # _UnexpectedRenderService raise if the rejected request reached them, so a
    # 409 that had already advanced the job would fail here rather than pass.


def test_idea_materialization_recovery_advances_only_with_owner_events(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _IdeaEvidenceCaptureService(ledger, lineage_store)
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    client = TestClient(app)
    key = "idea-report-materialization-owner-version"
    payload = {**_materialization_payload(), "requested_output_formats": ["json"]}
    try:
        submitted = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers(key),
        )
        unchanged = client.get(
            "/reports/idea-evidence-packs/materializations",
            params=_recovery_query(key),
            headers=_recovery_headers(),
        )
        job_id = submitted.json()["report_job_id"]
        ledger.mark_failed(
            job_id=job_id,
            actor="report-worker",
            correlation_id="corr-owner-version",
            trace_id="trace-owner-version",
            failure_category="operator_intervention_required",
            failure_message="Owner-side materialization correction required.",
            retry_eligible=True,
        )
        advanced = client.get(
            "/reports/idea-evidence-packs/materializations",
            params=_recovery_query(key),
            headers=_recovery_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert submitted.status_code == 202
    assert unchanged.status_code == 200
    assert advanced.status_code == 200
    assert unchanged.json() == submitted.json()
    assert advanced.json()["source_event_version"] == (submitted.json()["source_event_version"] + 1)
    assert advanced.json()["materialization_status"] == "failed"
    assert advanced.json()["report_job_id"] == submitted.json()["report_job_id"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reportEvidencePackId", "irep_changed"),
        ("conversionIntentId", "icnv_changed"),
        ("candidateId", "icand_changed"),
        ("evidencePacketId", "ievp_changed"),
        ("evidenceContentFingerprint", "sha256:changed"),
        ("portfolioId", "PB_SG_CHANGED_001"),
    ],
)
def test_idea_evidence_materialization_recovery_rejects_identity_drift(
    tmp_path,
    field: str,
    value: str,
) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _IdeaEvidenceCaptureService(ledger, lineage_store)
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    client = TestClient(app)
    payload = {**_materialization_payload(), "requested_output_formats": ["json"]}
    try:
        submitted = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-drift"),
        )
        query = _recovery_query("idea-report-materialization-drift")
        query[field] = value
        recovered = client.get(
            "/reports/idea-evidence-packs/materializations",
            params=query,
            headers=_recovery_headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert submitted.status_code == 202
    assert recovered.status_code == 409
    assert recovered.json()["detail"]["code"] == "idea_materialization_identity_conflict"


def test_idea_evidence_materialization_recovery_is_tenant_scoped(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _IdeaEvidenceCaptureService(ledger, lineage_store)
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    client = TestClient(app)
    payload = {**_materialization_payload(), "requested_output_formats": ["json"]}
    try:
        submitted = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-tenant-scope"),
        )
        headers = _recovery_headers()
        headers["X-Tenant-Id"] = "tenant-other"
        recovered = client.get(
            "/reports/idea-evidence-packs/materializations",
            params=_recovery_query("idea-report-materialization-tenant-scope"),
            headers=headers,
        )
    finally:
        app.dependency_overrides.clear()

    assert submitted.status_code == 202
    assert recovered.status_code == 404
    assert recovered.json()["detail"]["code"] == "idea_materialization_not_found"


@pytest.mark.parametrize("denial", ["wrong_caller", "missing_capability"])
def test_idea_evidence_materialization_recovery_denies_before_repository_io(
    denial: str,
) -> None:
    app.dependency_overrides[get_report_job_ledger] = lambda: _UnexpectedListLedger()
    client = TestClient(app)
    headers = _recovery_headers()
    if denial == "wrong_caller":
        headers["X-Caller-Application"] = "lotus-workbench"
    else:
        headers.pop("X-Capabilities")
    try:
        response = client.get(
            "/reports/idea-evidence-packs/materializations",
            params=_recovery_query("idea-report-materialization-denied"),
            headers=headers,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "idea_materialization_recovery_forbidden"


def test_idea_evidence_materialization_recovery_openapi_is_exact_and_read_only() -> None:
    openapi = app.openapi()
    operation = openapi["paths"]["/reports/idea-evidence-packs/materializations"]["get"]
    query_parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "query"
    }

    assert set(query_parameters) == {
        "idempotencyKey",
        "reportEvidencePackId",
        "conversionIntentId",
        "candidateId",
        "evidencePacketId",
        "evidenceContentFingerprint",
        "portfolioId",
    }
    assert all(parameter["required"] for parameter in query_parameters.values())
    assert {"200", "403", "404", "409", "422"} <= set(operation["responses"])
    assert "requestBody" not in operation
    response_schema = openapi["components"]["schemas"]["IdeaEvidencePackMaterializationResponse"]
    assert "source_event_version" in response_schema["required"]
    assert response_schema["properties"]["source_event_version"]["exclusiveMinimum"] == 0


def test_idea_evidence_materialization_rejects_non_idea_caller_before_side_effects(
    tmp_path,
) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    intake_ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: intake_ledger
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _UnexpectedCaptureService()
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    headers = _headers("idea-report-materialization-wrong-caller")
    headers["X-Caller-Application"] = "lotus-workbench"
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=headers,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "idea_materialization_forbidden"
    assert intake_ledger.snapshot() == {}
    assert ledger.list_jobs(filters=ReportJobListFilters(limit=2)) == []


def test_idea_evidence_materialization_records_archive_failure_without_retry(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    capture_service = _IdeaEvidenceCaptureService(ledger, lineage_store)
    render_service = PortfolioReviewRenderOrchestrationService(
        render_client=_SuccessfulRenderClient(archive_state="archive_failed"),
        snapshot_store=lineage_store,
        job_ledger=ledger,
    )
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        capture_service
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        render_service
    )
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=_headers("idea-report-materialization-archive-failure"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    record = ledger.get_job(response.json()["report_job_id"])
    assert record.status == "failed"
    assert record.failure_category == "archive_handoff_failed"
    assert "archive_unreachable" in (record.failure_message or "")
    # Proof-pack jobs have no replay/resolution path; a fresh order identity
    # would defeat archive idempotency, so the posture is non-retryable.
    assert record.retry_eligible is False
    assert record.archive_document_id is None


def test_idea_evidence_materialization_route_can_capture_json_only_proof(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    intake_ledger = IdeaEvidenceIntakeLedger()
    capture_service = _IdeaEvidenceCaptureService(ledger, lineage_store)
    render_service = PortfolioReviewRenderOrchestrationService(
        render_client=_UnexpectedRenderClient(),
        snapshot_store=lineage_store,
        job_ledger=ledger,
    )
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: intake_ledger
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        capture_service
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        render_service
    )
    payload = {**_materialization_payload(), "requested_output_formats": ["json"]}
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-json-only"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "data_ready"
    assert body["materialization_status"] == "data_ready"
    assert body["creates_report_job"] is True
    assert body["creates_rendered_output"] is False
    assert body["creates_archive_record"] is False
    assert body["render_job_id"] is None
    assert body["archive_document_id"] is None
    assert body["report_package_identity"]["report_evidence_pack_id"] == "irep_001"
    record = ledger.get_job(body["report_job_id"])
    assert record.requested_output_formats == ["json"]
    assert record.archive_document_id is None
    snapshot = lineage_store.get_snapshot_by_job(body["report_job_id"])
    assert snapshot.lineage_summary["source_type"] == "LOTUS_IDEA_EVIDENCE_PACK_REPORT_INPUT"


@pytest.mark.parametrize(
    ("requested_output_formats", "expected_code"),
    [
        (["docx"], "unsupported_report_output_format"),
        (["pdf", "pdf"], "duplicate_report_output_format"),
    ],
)
def test_idea_evidence_materialization_rejects_invalid_formats_before_side_effects(
    tmp_path,
    requested_output_formats: list[str],
    expected_code: str,
) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    intake_ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: intake_ledger
    app.dependency_overrides[get_idea_evidence_retention_policy_resolver] = lambda: (
        _UnexpectedRetentionPolicyResolver()
    )
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _UnexpectedCaptureService()
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    payload = {
        **_materialization_payload(),
        "requested_output_formats": requested_output_formats,
    }
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers(f"idea-report-materialization-{expected_code}"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code
    assert intake_ledger.snapshot() == {}
    assert ledger.list_jobs(filters=ReportJobListFilters(limit=10)) == []


def test_idea_evidence_materialization_propagates_active_legal_hold(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    capture_service = _IdeaEvidenceCaptureService(ledger, lineage_store)
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: _intake_ledger(tmp_path)
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        capture_service
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    payload = _materialization_payload()
    payload["requested_output_formats"] = ["json"]
    payload["idea_evidence_pack"] = {
        **_payload(),
        "retention_policy_ref": "generated-report-legal-hold",
    }
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-legal-hold"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    record = ledger.get_job(response.json()["report_job_id"])
    policy = record.options["retention_policy"]
    assert policy["legal_hold_active"] is True
    assert policy["retention_start_event"] == "REPORT_ARCHIVED"
    assert policy["archive_handoff_policy"] == "lotus-archive:idea-evidence-retention:v1"


def test_idea_evidence_materialization_route_requires_idempotency_key(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _UnexpectedCaptureService()
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    client = TestClient(app)
    headers = _headers("idea-report-materialization-missing-key")
    headers.pop("Idempotency-Key")

    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=headers,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "missing_idempotency_key"


def test_idea_evidence_materialization_route_validates_as_of_date_before_intake(
    tmp_path,
) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    intake_ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: intake_ledger
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _UnexpectedCaptureService()
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    payload = {**_materialization_payload(), "as_of_date": "not-a-date"}
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-invalid-date"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert intake_ledger.snapshot() == {}


def test_idea_evidence_materialization_route_conflicts_on_changed_payload_replay(
    tmp_path,
) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    intake_ledger = IdeaEvidenceIntakeLedger()
    capture_service = _IdeaEvidenceCaptureService(ledger, lineage_store)
    render_service = PortfolioReviewRenderOrchestrationService(
        render_client=_SuccessfulRenderClient(),
        snapshot_store=lineage_store,
        job_ledger=ledger,
    )
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: intake_ledger
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        capture_service
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        render_service
    )
    changed_payload = {
        **_materialization_payload(),
        "idea_evidence_pack": {
            **_payload(),
            "report_evidence_pack_id": "irep_changed",
        },
    }
    client = TestClient(app)
    try:
        first = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=_headers("idea-report-materialization-conflict"),
        )
        second = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=changed_payload,
            headers=_headers("idea-report-materialization-conflict"),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idempotency_conflict"


def test_idea_evidence_materialization_route_maps_ledger_missing_key_error() -> None:
    app.dependency_overrides[get_report_job_ledger] = lambda: _MissingKeyReportJobLedger()
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _UnexpectedCaptureService()
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    client = TestClient(app)
    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=_headers("idea-report-materialization-ledger-missing-key"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "missing_idempotency_key"


def test_idea_evidence_materialization_route_keeps_publication_blocked(tmp_path) -> None:
    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        _UnexpectedCaptureService()
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        _UnexpectedRenderService()
    )
    client = TestClient(app)
    payload = {
        **_materialization_payload(),
        "grants_client_publication_authority": True,
    }

    try:
        response = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=payload,
            headers=_headers("idea-report-materialization-unsafe"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def _payload() -> dict[str, object]:
    return {
        "report_evidence_pack_id": "irep_001",
        "conversion_intent_id": "icnv_001",
        "candidate_id": "icand_001",
        "purpose": "CLIENT_REPORT_EVIDENCE",
        "evidence_packet_id": "ievp_001",
        "evidence_content_fingerprint": "sha256:idea-evidence-content",
        "source_signal_ids": ["sig_high_cash_001"],
        "source_summaries": [
            {
                "product_id": "lotus-core:HoldingsAsOf:v1",
                "source_system": "lotus-core",
                "product_version": "v1",
                "as_of_date": "2026-06-24",
                "generated_at_utc": "2026-06-24T08:00:00Z",
                "data_quality_status": "complete",
                "freshness": "fresh",
            }
        ],
        "reason_codes": ["HIGH_CASH_REVIEWED_FOR_REPORT"],
        "report_source_authority": "lotus-report",
        "render_source_authority": "lotus-render",
        "archive_source_authority": "lotus-archive",
        "boundary": "REPORT_INTAKE_ONLY",
        "retention_policy_ref": "generated-report-standard",
        "requested_at_utc": "2026-06-24T08:15:00Z",
        "grants_client_publication_authority": False,
        "creates_rendered_output": False,
        "creates_archive_record": False,
        "producer": "lotus-idea",
        "supportability_status": "not_certified",
    }


def _intake_ledger(tmp_path) -> IdeaEvidenceIntakeLedger:
    """One file-backed intake ledger per test, as production has.

    Returned fresh per request on purpose: production does the same, and the
    shared file is what makes a second request see the first one's record.
    """
    return IdeaEvidenceIntakeLedger(tmp_path / "intake.sqlite3")


def _materialization_payload() -> dict[str, object]:
    return {
        "idea_evidence_pack": _payload(),
        "portfolio_id": "PB_SG_GLOBAL_BAL_001",
        "mandate_id": "MANDATE_PB_SG_GLOBAL_BAL_001",
        "as_of_date": "2026-06-24",
        "requested_output_formats": ["pdf"],
        "reporting_currency": "USD",
        "options": {"retention_policy_id": "generated-report-standard"},
        "boundary": "REPORT_JOB_MATERIALIZATION",
        "grants_client_publication_authority": False,
        "producer": "lotus-idea",
        "supportability_status": "not_certified",
    }


def _headers(idempotency_key: str) -> dict[str, str]:
    return {
        "Idempotency-Key": idempotency_key,
        "X-Actor-Id": "advisor-123",
        "X-Caller-Application": "lotus-idea",
        "X-Tenant-Id": "tenant-sg",
        "X-Region": "APAC",
        "X-Booking-Center-Code": "SG",
        "X-Role": "advisor",
        "X-Correlation-ID": "corr-idea-report-intake",
        "X-Trace-ID": "trace-idea-report-intake",
    }


def _recovery_headers() -> dict[str, str]:
    headers = _headers("unused-for-read")
    headers.pop("Idempotency-Key")
    headers["X-Capabilities"] = "report.idea-materialization.recover"
    return headers


def _recovery_query(idempotency_key: str) -> dict[str, str]:
    return {
        "idempotencyKey": idempotency_key,
        "reportEvidencePackId": "irep_001",
        "conversionIntentId": "icnv_001",
        "candidateId": "icand_001",
        "evidencePacketId": "ievp_001",
        "evidenceContentFingerprint": "sha256:idea-evidence-content",
        "portfolioId": "PB_SG_GLOBAL_BAL_001",
    }


class _IdeaEvidenceCaptureService:
    def __init__(self, ledger: ReportJobLedger, lineage_store: ReportInputSnapshotStore) -> None:
        self._ledger = ledger
        self._lineage_store = lineage_store

    async def capture_for_job(self, job):
        self._ledger.mark_collecting_data(
            job_id=job.job_id,
            actor=job.triggered_by,
            correlation_id=job.correlation_id,
            trace_id=job.trace_id,
        )
        report_input = job.options["proof_pack_report_input"]
        snapshot = self._lineage_store.create_snapshot(
            ReportInputSnapshotCreateRequest(
                report_job_id=job.job_id,
                report_type=job.report_type,
                report_data_contract_version="dpm_proof_pack_report_input.v1",
                portfolio_scope=job.portfolio_scope,
                as_of_date=job.as_of_date,
                snapshot_payload=report_input,
                snapshot_storage_ref=None,
                supportability_status="complete",
                completeness_status="complete",
                lineage_summary={
                    "source_services": ["lotus-idea"],
                    "call_count": 1,
                    "supportability_status": "complete",
                    "completeness_status": "complete",
                    "proof_pack_id": report_input["proof_pack_id"],
                    "source_type": "LOTUS_IDEA_EVIDENCE_PACK_REPORT_INPUT",
                    "source_hash": report_input["content_hash"],
                },
                captured_at=datetime.now(UTC),
                correlation_id=job.correlation_id,
                trace_id=job.trace_id,
            )
        )
        self._lineage_store.create_upstream_calls(
            snapshot_id=snapshot.snapshot_id,
            calls=[
                ReportUpstreamCallCreateRequest(
                    service_name="lotus-idea",
                    endpoint="/reports/idea-evidence-packs/materializations",
                    method="POST",
                    contract_version="LotusIdeaEvidencePackReportInput.1.0",
                    request_hash=report_input["proof_pack_content_hash"],
                    response_hash=report_input["content_hash"],
                    response_ref=report_input["evidence_ref"]["source_id"],
                    status_code=200,
                    latency_ms=0,
                    supportability_status="complete",
                    completeness_status="complete",
                    failure_category="none",
                    failure_message=None,
                    captured_at=datetime.now(UTC),
                    correlation_id=job.correlation_id,
                    trace_id=job.trace_id,
                )
            ],
        )
        return self._ledger.mark_data_ready(
            job_id=job.job_id,
            actor=job.triggered_by,
            correlation_id=job.correlation_id,
            trace_id=job.trace_id,
        )


class _AcceptedLeavingCaptureService:
    """Returns the job untouched, leaving it `accepted`.

    Not a shortcut: a test about what happens *before* capture needs a job that
    still has capture ahead of it.
    """

    async def capture_for_job(self, job):
        return job


class _UnexpectedCaptureService:
    async def capture_for_job(self, job):
        raise AssertionError("Invalid materialization requests must not capture snapshots")


class _UnexpectedRetentionPolicyResolver:
    def resolve(self, **kwargs):
        raise AssertionError(
            f"Invalid materialization requests must not resolve retention: {kwargs}"
        )


class _UnexpectedRenderService:
    async def render_for_job(self, job):
        raise AssertionError("Invalid materialization requests must not render reports")


class _SuccessfulRenderClient:
    def __init__(self, *, archive_state="archived_verified"):
        self.archive_state = archive_state

    async def submit_render_package(self, payload, **kwargs):
        return 201, {
            "status": "rendered",
            "template_id": payload["template_id"],
            "template_version": payload["template_version"],
            "render_job_id": payload["render_job_id"],
            "artifact_sha256": "sha256:idea-evidence-rendered-pdf",
            "bounded_determinism_fingerprint": "fingerprint-idea-evidence",
            "runtime_engine": "typst",
            "runtime_engine_version": "0.14.2",
            "render_duration_ms": 420,
            "artifact_base64": "JVBERi0xLjQ=",
            "archive_state": self.archive_state,
            "archive_document_id": (
                "doc_idea_evidence_pack_001" if self.archive_state == "archived_verified" else None
            ),
            "archive_detail": (
                "archive_unreachable: Archive is temporarily unavailable."
                if self.archive_state == "archive_failed"
                else None
            ),
        }


class _UnexpectedRenderClient:
    async def submit_render_package(self, payload, **kwargs):
        raise AssertionError("JSON-only materialization must not call render")


class _MissingKeyReportJobLedger:
    def list_job_owner_snapshots(self, **kwargs):
        # The route verifies legacy identity before storing anything, so this
        # is reached before create_proof_pack_report_job. Empty means no prior
        # row, which is not a refusal -- letting the request through to the
        # missing-key failure this fake exists to produce.
        return []

    def create_proof_pack_report_job(self, **kwargs):
        raise MissingIdempotencyKeyError("missing idempotency key")


class _UnexpectedListLedger:
    def list_job_owner_snapshots(self, **kwargs):
        raise AssertionError(f"Unauthorized recovery must not query the repository: {kwargs}")


class _TwoTenantRetentionPolicyResolver:
    """Authorises both tenants under test.

    The shipped policy authorises `tenant-sg` alone, and retention authority is
    a different control from intake identity. Left in place it would refuse the
    second tenant for the wrong reason, and a test asserting isolation would
    pass without exercising isolation.
    """

    def resolve(self, *, policy_ref, tenant_id, producer, at_utc=None):
        from app.idea_evidence_intake.retention_policy import IdeaEvidenceRetentionPolicy

        return IdeaEvidenceRetentionPolicy(
            policy_ref=policy_ref,
            policy_version="1.0.0",
            purpose="GOVERNED_CLIENT_REPORT_EVIDENCE",
            retention_start_event="REPORT_ARCHIVED",
            retention_duration_days=2557,
            approval_authority="lotus-report-information-governance",
            residency_region="APAC",
            authorized_tenants=frozenset({"tenant-a", "tenant-b"}),
            authorized_producers=frozenset({"lotus-idea"}),
            legal_hold_active=False,
            erasure_action="REDACT_EVIDENCE_REFERENCES_AFTER_APPROVAL",
            archive_handoff_policy="lotus-archive:idea-evidence-retention:v1",
            effective_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        )


def _tenant_headers(tenant_id: str, idempotency_key: str) -> dict[str, str]:
    headers = _headers(idempotency_key)
    headers["X-Tenant-Id"] = tenant_id
    return headers


def test_materialization_is_tenant_scoped_for_a_shared_idempotency_key(tmp_path) -> None:
    """The route that carries the fail-closed refusal, which the isolation
    suite did not reach.

    Those tests all drive `POST ""`. This is the OTHER route using the same
    ledger, and it is the one where `has_record` gates the legacy-replay
    refusal added by report#334 -- so an unscoped answer here skipped a refusal
    on another tenant's evidence, which is the most consequential form of the
    defect. Holding the route fixed across a suite is the same unproven-constant
    trap as holding the key fixed.
    """

    ledger = ReportJobLedger(tmp_path / "jobs.sqlite3")
    lineage_store = ReportInputSnapshotStore(tmp_path / "lineage.sqlite3")
    intake_ledger = IdeaEvidenceIntakeLedger(tmp_path / "intake.sqlite3")
    capture_service = _IdeaEvidenceCaptureService(ledger, lineage_store)
    render_service = PortfolioReviewRenderOrchestrationService(
        render_client=_SuccessfulRenderClient(),
        snapshot_store=lineage_store,
        job_ledger=ledger,
    )
    app.dependency_overrides[get_report_job_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: intake_ledger
    app.dependency_overrides[get_idea_evidence_retention_policy_resolver] = lambda: (
        _TwoTenantRetentionPolicyResolver()
    )
    app.dependency_overrides[get_portfolio_review_snapshot_capture_service] = lambda: (
        capture_service
    )
    app.dependency_overrides[get_portfolio_review_render_orchestration_service] = lambda: (
        render_service
    )
    client = TestClient(app)
    shared_key = "a-materialization-key-two-tenants-chose"
    try:
        first = client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=_tenant_headers("tenant-a", shared_key),
        )
        # Issued for its effect on the ledger, not for its status: see the note
        # below on why this route's response code is deliberately unasserted.
        client.post(
            "/reports/idea-evidence-packs/materializations",
            json=_materialization_payload(),
            headers=_tenant_headers("tenant-b", shared_key),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202

    # The intake ledger is scoped: both tenants hold their own row under their
    # own identity, addressed by (tenant, key). This is what report#344 fixed,
    # and this route reaches it through `has_record` -- the read that gates the
    # fail-closed legacy-replay refusal from report#334.
    stored = intake_ledger.snapshot()
    assert set(stored) == {("tenant-a", shared_key), ("tenant-b", shared_key)}
    assert stored[("tenant-a", shared_key)].tenant_id == "tenant-a"
    assert stored[("tenant-b", shared_key)].tenant_id == "tenant-b"

    # Deliberately NOT asserted here: `second.status_code`. The report job
    # ledger keys `report_request` on the idempotency key alone
    # (`reporting_jobs/ledger.py:728`), so tenant B is currently refused 409
    # `idempotency_conflict` -- "Idempotency-Key was reused with different idea
    # evidence content", which is untrue: B sent identical content and reused
    # nothing. That is the same defect in a second, larger store serving every
    # report route, filed as #350. Asserting the 409 here would pin a
    # defect's current behaviour as though it were the contract.
