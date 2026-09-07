from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.idea_evidence_intake.materialization_contract import (
    IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION,
)
from app.idea_evidence_intake.models import (
    IdeaEvidencePackIntakeRequest,
    IdeaEvidencePackMaterializationRequest,
)
from app.idea_evidence_intake.recovery import recovery_identity_from_request
from app.idea_evidence_intake.service import (
    IdeaEvidenceIntakeConflictError,
    IdeaEvidenceIntakeLedger,
    build_proof_pack_report_job_request_from_idea_evidence,
)
from app.reporting_jobs.models import ReportCallerContext


def test_idea_evidence_intake_accepts_source_safe_not_certified_handoff() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    accepted_at = datetime(2026, 6, 24, 8, 30, tzinfo=UTC)

    response = ledger.accept(
        _request(),
        tenant_id="tenant-sg",
        idempotency_key="idea-report-intake-001",
        accepted_at_utc=accepted_at,
        correlation_id="corr-idea-report-intake",
    )

    assert response.intake_status == "accepted"
    assert response.producer == "lotus-idea"
    assert response.owned_product == "lotus-report:ClientReportEvidencePack:v1"
    assert response.route_existence_proven is True
    assert response.materialization_proven is False
    assert response.creates_report_job is False
    assert response.creates_rendered_output is False
    assert response.creates_archive_record is False
    assert response.grants_client_publication_authority is False
    assert response.supportability_status == "not_certified"
    assert response.accepted_at_utc == accepted_at
    assert "rendered_output_creation_missing" in response.remaining_blockers
    assert "archive_record_creation_missing" in response.remaining_blockers
    assert "client_publication_authority_blocked" in response.remaining_blockers


def test_idea_evidence_intake_is_idempotent_for_same_payload() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    request = _request()

    first = ledger.accept(request, tenant_id="tenant-sg", idempotency_key="idea-report-intake-001")
    second = ledger.accept(request, tenant_id="tenant-sg", idempotency_key="idea-report-intake-001")

    assert second == first
    assert len(ledger.snapshot()) == 1


def test_idea_evidence_intake_conflicts_when_idempotency_payload_changes() -> None:
    ledger = IdeaEvidenceIntakeLedger()
    ledger.accept(_request(), tenant_id="tenant-sg", idempotency_key="idea-report-intake-001")

    with pytest.raises(IdeaEvidenceIntakeConflictError):
        ledger.accept(
            _request(report_evidence_pack_id="irep_changed"),
            tenant_id="tenant-sg",
            idempotency_key="idea-report-intake-001",
        )


def test_idea_evidence_intake_replays_same_payload_across_fresh_durable_ledger(
    tmp_path,
) -> None:
    db_path = tmp_path / "idea-intake.sqlite3"
    first_ledger = IdeaEvidenceIntakeLedger(db_path)
    second_ledger = IdeaEvidenceIntakeLedger(db_path)
    accepted_at = datetime(2026, 6, 24, 8, 30, tzinfo=UTC)
    caller_context = ReportCallerContext(
        triggered_by="advisor-123",
        caller_application="lotus-idea",
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        role="advisor",
        correlation_id="corr-idea-report-intake",
        trace_id="trace-idea-report-intake",
    )

    first = first_ledger.accept(
        _request(),
        tenant_id="tenant-sg",
        idempotency_key="idea-report-intake-001",
        accepted_at_utc=accepted_at,
        correlation_id="corr-idea-report-intake",
        trace_id="trace-idea-report-intake",
        caller_context=caller_context,
    )
    second = second_ledger.accept(
        _request(),
        tenant_id="tenant-sg",
        idempotency_key="idea-report-intake-001",
        correlation_id="corr-idea-report-intake-retry",
        trace_id="trace-idea-report-intake-retry",
        caller_context=caller_context,
    )

    assert second == first
    records = second_ledger.snapshot()
    assert len(records) == 1
    record = records[("tenant-sg", "idea-report-intake-001")]
    assert record.payload_fingerprint.startswith("sha256:")
    assert record.response == first
    assert record.accepted_at_utc == accepted_at
    assert record.caller_context["triggered_by"] == "advisor-123"
    assert record.caller_context["caller_application"] == "lotus-idea"
    assert record.caller_context["correlation_id"] == "corr-idea-report-intake"
    assert record.caller_context["trace_id"] == "trace-idea-report-intake"


def test_idea_evidence_intake_conflicts_across_fresh_durable_ledger(tmp_path) -> None:
    db_path = tmp_path / "idea-intake.sqlite3"
    first_ledger = IdeaEvidenceIntakeLedger(db_path)
    first_ledger.accept(_request(), tenant_id="tenant-sg", idempotency_key="idea-report-intake-001")

    restarted_ledger = IdeaEvidenceIntakeLedger(db_path)

    with pytest.raises(IdeaEvidenceIntakeConflictError):
        restarted_ledger.accept(
            _request(report_evidence_pack_id="irep_changed"),
            tenant_id="tenant-sg",
            idempotency_key="idea-report-intake-001",
        )


def test_idea_evidence_intake_durable_insert_race_replays_existing_record(tmp_path) -> None:
    db_path = tmp_path / "idea-intake.sqlite3"
    first_ledger = IdeaEvidenceIntakeLedger(db_path)
    first_ledger.accept(_request(), tenant_id="tenant-sg", idempotency_key="idea-report-intake-001")
    record = first_ledger.snapshot()[("tenant-sg", "idea-report-intake-001")]

    restarted_ledger = IdeaEvidenceIntakeLedger(db_path)

    stored = restarted_ledger._store_record(  # noqa: SLF001 - race contract regression test
        record,
        request=_request(),
        correlation_id="corr-idea-report-intake-retry",
        trace_id="trace-idea-report-intake-retry",
    )

    assert stored == record


def test_idea_evidence_intake_durable_insert_race_conflicts_on_changed_fingerprint(
    tmp_path,
) -> None:
    db_path = tmp_path / "idea-intake.sqlite3"
    first_ledger = IdeaEvidenceIntakeLedger(db_path)
    first_ledger.accept(_request(), tenant_id="tenant-sg", idempotency_key="idea-report-intake-001")
    record = first_ledger.snapshot()[("tenant-sg", "idea-report-intake-001")]
    changed_record = replace(record, payload_fingerprint="sha256:changed")

    restarted_ledger = IdeaEvidenceIntakeLedger(db_path)

    with pytest.raises(IdeaEvidenceIntakeConflictError):
        restarted_ledger._store_record(  # noqa: SLF001 - race contract regression test
            changed_record,
            request=_request(),
            correlation_id="corr-idea-report-intake-retry",
            trace_id="trace-idea-report-intake-retry",
        )


def test_idea_evidence_intake_normalizes_naive_accepted_at_for_durable_records(
    tmp_path,
) -> None:
    db_path = tmp_path / "idea-intake.sqlite3"
    ledger = IdeaEvidenceIntakeLedger(db_path)
    accepted_at = datetime(2026, 6, 24, 8, 30)

    ledger.accept(
        _request(),
        tenant_id="tenant-sg",
        idempotency_key="idea-report-intake-001",
        accepted_at_utc=accepted_at,
    )

    record = IdeaEvidenceIntakeLedger(db_path).snapshot()[("tenant-sg", "idea-report-intake-001")]
    assert record.accepted_at_utc == datetime(2026, 6, 24, 8, 30, tzinfo=UTC)


def test_idea_evidence_intake_rolls_back_failed_durable_write_context(tmp_path) -> None:
    ledger = IdeaEvidenceIntakeLedger(tmp_path / "idea-intake.sqlite3")

    with pytest.raises(RuntimeError, match="boom"):
        with ledger._connect() as connection:  # noqa: SLF001 - rollback regression test
            connection.execute(
                "INSERT INTO idea_evidence_intake ("
                "tenant_id, idempotency_key, intake_id, payload_fingerprint, response_json, "
                "caller_context_json, report_evidence_pack_id, conversion_intent_id, "
                "candidate_id, evidence_packet_id, evidence_content_fingerprint, producer, "
                "supportability_status, accepted_at_utc, created_at_utc"
                ") VALUES ("
                "'tenant-sg', 'rolled-back-key', 'intake', 'sha256:test', '{}', '{}', "
                "'irep', 'icnv', "
                "'icand', 'ievp', 'sha256:test', 'lotus-idea', 'not_certified', "
                "'2026-06-24T08:30:00Z', '2026-06-24T08:30:00Z'"
                ")"
            )
            raise RuntimeError("boom")

    assert ("tenant-sg", "rolled-back-key") not in ledger.snapshot()


def test_idea_evidence_materialization_maps_to_source_owned_proof_pack_request() -> None:
    request = IdeaEvidencePackMaterializationRequest(
        idea_evidence_pack=_request(),
        portfolio_id="PB_SG_GLOBAL_BAL_001",
        mandate_id="MANDATE_PB_SG_GLOBAL_BAL_001",
        as_of_date="2026-06-24",
        requested_output_formats=["pdf"],
        reporting_currency="USD",
        options={"retention_policy_id": "generated-report-standard"},
    )

    report_job_request = build_proof_pack_report_job_request_from_idea_evidence(request)

    assert report_job_request.requested_output_formats == ["pdf"]
    proof_pack_input = report_job_request.proof_pack_report_input.model_dump(
        mode="json",
        exclude_none=True,
    )
    assert proof_pack_input["proof_pack_id"] == "irep_001"
    assert proof_pack_input["source_contract_version"] == (
        "lotus_idea_evidence_pack_report_input.v1"
    )
    assert proof_pack_input["portfolio_id"] == "PB_SG_GLOBAL_BAL_001"
    assert proof_pack_input["retention_policy"] == "generated-report-standard"
    assert proof_pack_input["evidence_ref"] == {
        "source_system": "lotus-idea",
        "source_type": "LOTUS_IDEA_EVIDENCE_PACK_REPORT_INPUT",
        "source_id": "irep_001:lotus_idea_evidence_pack_report_input",
        "content_hash": "sha256:idea-evidence-content",
    }
    assert proof_pack_input["client_publication_authority_granted"] is False
    assert proof_pack_input["sections"][0]["section_type"] == "IDEA_SOURCE_EVIDENCE"
    assert report_job_request.options[IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION] == {
        "report_evidence_pack_id": "irep_001",
        "conversion_intent_id": "icnv_001",
        "candidate_id": "icand_001",
        "evidence_packet_id": "ievp_001",
        "evidence_content_fingerprint": "sha256:idea-evidence-content",
        "source_contract_version": "lotus_idea_evidence_pack_report_input.v1",
        "owned_product": "lotus-report:ClientReportEvidencePack:v1",
        "portfolio_id": "PB_SG_GLOBAL_BAL_001",
    }


def test_idea_evidence_materialization_replaces_spoofed_recovery_identity() -> None:
    request = IdeaEvidencePackMaterializationRequest(
        idea_evidence_pack=_request(),
        portfolio_id="PB_SG_GLOBAL_BAL_001",
        as_of_date="2026-06-24",
        requested_output_formats=["json"],
        options={IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION: {"candidate_id": "spoofed"}},
    )

    report_job_request = build_proof_pack_report_job_request_from_idea_evidence(request)

    identity = report_job_request.options[IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION]
    assert identity["candidate_id"] == "icand_001"
    assert identity["portfolio_id"] == "PB_SG_GLOBAL_BAL_001"


def _request(report_evidence_pack_id: str = "irep_001") -> IdeaEvidencePackIntakeRequest:
    return IdeaEvidencePackIntakeRequest(
        report_evidence_pack_id=report_evidence_pack_id,
        conversion_intent_id="icnv_001",
        candidate_id="icand_001",
        purpose="CLIENT_REPORT_EVIDENCE",
        evidence_packet_id="ievp_001",
        evidence_content_fingerprint="sha256:idea-evidence-content",
        source_signal_ids=("sig_high_cash_001",),
        source_summaries=(
            {
                "product_id": "lotus-core:HoldingsAsOf:v1",
                "source_system": "lotus-core",
                "product_version": "v1",
                "as_of_date": "2026-06-24",
                "generated_at_utc": "2026-06-24T08:00:00Z",
                "data_quality_status": "complete",
                "freshness": "fresh",
            },
        ),
        reason_codes=("HIGH_CASH_REVIEWED_FOR_REPORT",),
        retention_policy_ref="generated-report-standard",
        requested_at_utc=datetime(2026, 6, 24, 8, 15, tzinfo=UTC),
    )


def test_a_client_supplied_reserved_recovery_key_never_persists_as_server_truth() -> None:
    """Reserved namespace means reserved: a caller-supplied value under the
    server-derived recovery-identity key is unconditionally REPLACED with
    the server derivation at acceptance - a forged value never persists as
    server truth (and the recovery path additionally fail-closes on any
    stored identity inconsistent with the record's own proof-pack facts)."""

    request = IdeaEvidencePackMaterializationRequest(
        idea_evidence_pack=_request(),
        portfolio_id="PB_SG_GLOBAL_BAL_001",
        mandate_id="MANDATE_PB_SG_GLOBAL_BAL_001",
        as_of_date="2026-06-24",
        requested_output_formats=["pdf"],
        reporting_currency="USD",
        options={
            "retention_policy_id": "generated-report-standard",
            IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION: {
                "forged": "by-client",
                "portfolio_id": "PB_ATTACKER_001",
            },
        },
    )

    report_job_request = build_proof_pack_report_job_request_from_idea_evidence(request)

    stored = report_job_request.options[IDEA_MATERIALIZATION_RECOVERY_IDENTITY_OPTION]
    assert stored != {"forged": "by-client", "portfolio_id": "PB_ATTACKER_001"}
    assert stored == recovery_identity_from_request(request).model_dump(mode="json")
    assert stored["portfolio_id"] == "PB_SG_GLOBAL_BAL_001"
