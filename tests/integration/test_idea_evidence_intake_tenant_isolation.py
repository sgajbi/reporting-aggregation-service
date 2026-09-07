"""Two tenants presenting one idempotency key must not share a record.

An idempotency key is a value the *caller* chooses to name its own retry, so it
is unique only within a caller. The intake ledger keyed on it alone, which made
one tenant's choice of string a global resource:

- with the same key and an identical body, tenant B received tenant A's stored
  response -- the same ``intake_id``, and only one row stored,
- with the same key and a different body, tenant B was refused with
  ``idea_evidence_intake_conflict``, a message blaming the caller for changing a
  payload it had never sent,
- and ``has_record`` -- which gates the fail-closed legacy-replay refusal added
  by report#334 -- answered from any tenant's history, so one caller's record
  could satisfy another caller's check.

These drive the actual HTTP route, so admission runs and the tenant under test
is the one the request was admitted with, not one passed to a ledger by hand
(report#344).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.idea_evidence_intake.retention_policy import (
    IdeaEvidenceRetentionPolicy,
    IdeaEvidenceRetentionPolicyResolver,
)
from app.idea_evidence_intake.service import IdeaEvidenceIntakeLedger
from app.main import app
from app.routers.idea_evidence_intake import (
    get_idea_evidence_intake_ledger,
    get_idea_evidence_retention_policy_resolver,
)

_ROUTE = "/reports/idea-evidence-packs"
_SHARED_KEY = "a-key-two-tenants-both-chose"


def _payload(report_evidence_pack_id: str = "irep_001") -> dict[str, object]:
    return {
        "report_evidence_pack_id": report_evidence_pack_id,
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


def _headers(tenant_id: str, idempotency_key: str = _SHARED_KEY) -> dict[str, str]:
    return {
        "Idempotency-Key": idempotency_key,
        "X-Actor-Id": "advisor-123",
        "X-Caller-Application": "lotus-idea",
        "X-Tenant-Id": tenant_id,
        "X-Region": "APAC",
        "X-Booking-Center-Code": "SG",
        "X-Role": "advisor",
        "X-Correlation-ID": f"corr-{tenant_id}",
        "X-Trace-ID": f"trace-{tenant_id}",
    }


class _BothTenantsAuthorized(IdeaEvidenceRetentionPolicyResolver):
    """A retention policy both test tenants may use.

    The shipped policy authorizes `tenant-sg` alone, and retention authority is
    a different control from intake identity. Leaving it in place would refuse
    the second tenant for the wrong reason and these tests would pass without
    exercising the thing they name.
    """

    def resolve(
        self,
        *,
        policy_ref: str,
        tenant_id: str,
        producer: str,
        at_utc: datetime | None = None,
    ) -> IdeaEvidenceRetentionPolicy:
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


@pytest.fixture
def client_and_ledger():
    ledger = IdeaEvidenceIntakeLedger()
    app.dependency_overrides[get_idea_evidence_intake_ledger] = lambda: ledger
    app.dependency_overrides[get_idea_evidence_retention_policy_resolver] = lambda: (
        _BothTenantsAuthorized()
    )
    try:
        yield TestClient(app), ledger
    finally:
        app.dependency_overrides.clear()


def test_two_tenants_sharing_a_key_and_a_body_get_their_own_records(client_and_ledger) -> None:
    """The measured defect, inverted.

    Before this change both requests resolved to one row and tenant B was told
    its intake was ``accepted`` while being handed A's receipt.
    """

    client, ledger = client_and_ledger

    first = client.post(_ROUTE, json=_payload(), headers=_headers("tenant-a"))
    second = client.post(_ROUTE, json=_payload(), headers=_headers("tenant-b"))

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["intake_id"] != second.json()["intake_id"]

    stored = ledger.snapshot()
    assert set(stored) == {("tenant-a", _SHARED_KEY), ("tenant-b", _SHARED_KEY)}
    assert stored[("tenant-a", _SHARED_KEY)].tenant_id == "tenant-a"
    assert stored[("tenant-b", _SHARED_KEY)].tenant_id == "tenant-b"


def test_a_second_tenant_is_not_refused_for_a_payload_it_never_sent(client_and_ledger) -> None:
    """The denial half, which was unconditional.

    Any two tenants choosing one key collided, and the second was refused with
    ``idea evidence intake payload changed`` -- untrue, and it would send them
    looking for a change on their own side that never happened.
    """

    client, _ = client_and_ledger

    client.post(_ROUTE, json=_payload(), headers=_headers("tenant-a"))
    other = client.post(
        _ROUTE,
        json=_payload(report_evidence_pack_id="irep_belonging_to_b"),
        headers=_headers("tenant-b"),
    )

    assert other.status_code == 202


def test_a_genuine_same_tenant_replay_still_returns_the_first_receipt(client_and_ledger) -> None:
    """Scoping must not break idempotency, which is the point of the key."""

    client, ledger = client_and_ledger

    first = client.post(_ROUTE, json=_payload(), headers=_headers("tenant-a"))
    replay = client.post(_ROUTE, json=_payload(), headers=_headers("tenant-a"))

    assert replay.status_code == 202
    assert replay.json()["intake_id"] == first.json()["intake_id"]
    assert len(ledger.snapshot()) == 1


def test_a_same_tenant_conflict_is_still_refused(client_and_ledger) -> None:
    """The refusal that is real must survive: one caller, one key, two bodies."""

    client, _ = client_and_ledger

    client.post(_ROUTE, json=_payload(), headers=_headers("tenant-a"))
    changed = client.post(
        _ROUTE,
        json=_payload(report_evidence_pack_id="irep_changed"),
        headers=_headers("tenant-a"),
    )

    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "idea_evidence_intake_conflict"


def test_a_foreign_record_does_not_reveal_itself_through_has_record(client_and_ledger) -> None:
    """`has_record` gates a refusal, so an unscoped answer skipped it.

    report#334 refuses a legacy replay that no prior intake validated. Reading
    that question without a tenant let one caller's stored record discharge a
    different caller's check -- the refusal was skipped on evidence the caller
    had no relationship to.
    """

    client, ledger = client_and_ledger

    client.post(_ROUTE, json=_payload(), headers=_headers("tenant-a"))

    assert ledger.has_record(tenant_id="tenant-a", idempotency_key=_SHARED_KEY) is True
    assert ledger.has_record(tenant_id="tenant-b", idempotency_key=_SHARED_KEY) is False


def test_the_intake_identity_is_derived_from_the_tenant(client_and_ledger) -> None:
    """Two tenants, one key, one body -- the identities must still differ.

    Derived rather than merely stored: an identity that did not include the
    tenant would collide even with the rows correctly separated.
    """

    client, _ = client_and_ledger

    a = client.post(_ROUTE, json=_payload(), headers=_headers("tenant-a")).json()
    b = client.post(_ROUTE, json=_payload(), headers=_headers("tenant-b")).json()

    assert a["intake_id"] != b["intake_id"]


def test_the_route_cannot_be_reached_without_an_admitted_tenant(client_and_ledger) -> None:
    """The tenant is admitted, not supplied in the payload.

    Refused before the ledger is touched, so an absent tenant cannot become an
    intake attributed to nobody.
    """

    client, ledger = client_and_ledger

    headers = _headers("tenant-a")
    headers.pop("X-Tenant-Id")
    response = client.post(_ROUTE, json=_payload(), headers=headers)

    assert response.status_code == 400
    assert ledger.snapshot() == {}
