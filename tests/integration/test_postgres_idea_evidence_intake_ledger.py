"""The PostgreSQL intake ledger must behave exactly as the SQLite one does.

report#326 moves the last production Report store off SQLite. The risk in that
move is not that the new ledger fails loudly -- it is that it *almost* agrees:
a replay recognised on one engine and treated as new on the other would make
the storage backend an input to an idempotency decision, and report#334 showed
what an intake ledger that forgets looks like from the outside (a replay that
cannot be told from a first submission).

So the central test here compares the two engines on the same requests rather
than asserting each in isolation.

Run against real PostgreSQL. Skipped without REPORT_JOB_LEDGER_DATABASE_URL,
which is how the other PostgreSQL proofs in this suite are gated.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

from app.idea_evidence_intake.models import IdeaEvidencePackIntakeRequest
from app.idea_evidence_intake.postgres_ledger import PostgresIdeaEvidenceIntakeLedger
from app.idea_evidence_intake.service import (
    IdeaEvidenceIntakeConflictError,
    IdeaEvidenceIntakeLedger,
    build_intake_record,
    payload_fingerprint_of,
)
from app.postgres import PostgresConnectionProvider
from app.reporting_jobs.models import ReportCallerContext
from app.reporting_persistence.schema import apply_report_schema_migrations


def _database_url() -> str:
    database_url = os.environ.get("REPORT_JOB_LEDGER_DATABASE_URL")
    if not database_url:
        pytest.skip("REPORT_JOB_LEDGER_DATABASE_URL is required for the intake ledger proof")
    return database_url


@pytest.fixture
def postgres_ledger() -> PostgresIdeaEvidenceIntakeLedger:
    """A ledger over a schema built by the real migrations.

    The ledger deliberately does not create its own schema: the table is
    governed by migration 024, and a ledger that quietly created what it needs
    would make the migration gate unable to observe a drift between them.
    """
    database_url = _database_url()
    with psycopg.connect(database_url) as connection:
        apply_report_schema_migrations(connection)
        connection.commit()
    return PostgresIdeaEvidenceIntakeLedger(database_url)


def _request(suffix: str, *, candidate_id: str = "icand_001") -> IdeaEvidencePackIntakeRequest:
    return IdeaEvidencePackIntakeRequest(
        report_evidence_pack_id=f"irep_{suffix}",
        conversion_intent_id="icnv_001",
        candidate_id=candidate_id,
        purpose="CLIENT_REPORT_EVIDENCE",
        evidence_packet_id=f"ievp_{suffix}",
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


def _caller_context(suffix: str) -> ReportCallerContext:
    return ReportCallerContext(
        triggered_by="advisor-123",
        caller_application="lotus-idea",
        tenant_id="tenant-sg",
        region="APAC",
        booking_center_code="SG",
        role="advisor",
        correlation_id=f"corr-intake-{suffix}",
        trace_id=f"trace-intake-{suffix}",
    )


def test_an_accepted_intake_round_trips_through_postgresql(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
) -> None:
    """JSONB and TIMESTAMPTZ must come back as what went in.

    The SQLite ledger stored both payloads and both instants as TEXT. Changing
    the column types is the reason for the move, so the round trip is the thing
    to prove -- a JSONB column that silently reorders keys or a TIMESTAMPTZ
    that returns a different instant would corrupt replay evidence rather than
    fail.
    """
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"

    accepted = postgres_ledger.accept(
        _request(suffix),
        tenant_id="tenant-sg",
        idempotency_key=key,
        caller_context=_caller_context(suffix),
        correlation_id=f"corr-intake-{suffix}",
    )

    stored = postgres_ledger.snapshot()[("tenant-sg", key)]
    assert stored.response == accepted
    assert stored.caller_context["tenant_id"] == "tenant-sg"
    assert stored.accepted_at_utc == accepted.accepted_at_utc
    assert stored.accepted_at_utc.tzinfo is not None


def test_has_record_answers_before_and_after_acceptance(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
) -> None:
    """report#334 refuses a legacy replay that no prior intake validated.

    That refusal reads `has_record` BEFORE `accept()` stores anything, so a
    backend whose answer only became true after its own write would defeat it.
    """
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"

    assert postgres_ledger.has_record(tenant_id="tenant-sg", idempotency_key=key) is False
    postgres_ledger.accept(_request(suffix), tenant_id="tenant-sg", idempotency_key=key)
    assert postgres_ledger.has_record(tenant_id="tenant-sg", idempotency_key=key) is True


def test_an_identical_replay_returns_the_original_receipt(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
) -> None:
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"

    first = postgres_ledger.accept(_request(suffix), tenant_id="tenant-sg", idempotency_key=key)
    replayed = postgres_ledger.accept(_request(suffix), tenant_id="tenant-sg", idempotency_key=key)

    assert replayed == first


def test_a_changed_payload_under_the_same_key_conflicts(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
) -> None:
    """The comparison report#334 depends on, including the fields the report
    request hash omits: `candidate_id` changes here and nothing else."""
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"

    postgres_ledger.accept(_request(suffix), tenant_id="tenant-sg", idempotency_key=key)

    with pytest.raises(IdeaEvidenceIntakeConflictError):
        postgres_ledger.accept(
            _request(suffix, candidate_id="icand_substituted"),
            tenant_id="tenant-sg",
            idempotency_key=key,
        )


def test_both_engines_produce_the_same_receipt_and_the_same_verdicts(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
    tmp_path,
) -> None:
    """The migration's real risk: two ledgers that almost agree.

    A replay recognised on one engine and treated as new on the other makes the
    storage backend an input to an idempotency decision. Compared here on the
    same three requests -- first acceptance, identical replay, changed payload
    -- because agreeing on the happy path while diverging on conflict is the
    failure that would survive a per-engine test.

    `intake_id` and the accepted instant are excluded from the comparison: the
    id is derived per acceptance and the instant is read from the clock, so
    they differ between two independent acceptances of the same request on any
    backend, including two SQLite ones.
    """
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"
    sqlite_ledger = IdeaEvidenceIntakeLedger(tmp_path / "intake.sqlite3")

    def _comparable(response: object) -> dict[str, object]:
        payload = dict(response.model_dump(mode="json"))  # type: ignore[attr-defined]
        payload.pop("intake_id", None)
        payload.pop("accepted_at_utc", None)
        return payload

    postgres_first = postgres_ledger.accept(
        _request(suffix), tenant_id="tenant-sg", idempotency_key=key
    )
    sqlite_first = sqlite_ledger.accept(
        _request(suffix), tenant_id="tenant-sg", idempotency_key=key
    )
    assert _comparable(postgres_first) == _comparable(sqlite_first)

    postgres_replay = postgres_ledger.accept(
        _request(suffix), tenant_id="tenant-sg", idempotency_key=key
    )
    sqlite_replay = sqlite_ledger.accept(
        _request(suffix), tenant_id="tenant-sg", idempotency_key=key
    )
    assert postgres_replay == postgres_first
    assert sqlite_replay == sqlite_first

    changed = _request(suffix, candidate_id="icand_substituted")
    with pytest.raises(IdeaEvidenceIntakeConflictError):
        postgres_ledger.accept(changed, tenant_id="tenant-sg", idempotency_key=key)
    with pytest.raises(IdeaEvidenceIntakeConflictError):
        sqlite_ledger.accept(changed, tenant_id="tenant-sg", idempotency_key=key)


def test_the_two_engines_agree_on_the_payload_fingerprint(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
    tmp_path,
) -> None:
    """The fingerprint is what decides replay from conflict.

    Asserted directly as well as through behaviour: if the two ever computed it
    differently, the behavioural test above would still pass for requests that
    happen to agree, and fail only for the ones that do not.
    """
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"
    sqlite_ledger = IdeaEvidenceIntakeLedger(tmp_path / "intake.sqlite3")

    postgres_ledger.accept(_request(suffix), tenant_id="tenant-sg", idempotency_key=key)
    sqlite_ledger.accept(_request(suffix), tenant_id="tenant-sg", idempotency_key=key)

    assert (
        postgres_ledger.snapshot()[("tenant-sg", key)].payload_fingerprint
        == sqlite_ledger.snapshot()[("tenant-sg", key)].payload_fingerprint
    )


def test_a_lost_insert_race_with_an_identical_payload_replays(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
) -> None:
    """Both requests reached the INSERT; the loser must return the winner's receipt.

    `_store_record` is driven directly because `accept()`'s pre-check catches a
    second identical request before it ever inserts, so the handler under test
    is unreachable through the public path. A thread barrier would show the two
    calls starting together, not both arriving at the INSERT, and it is the
    resolution rather than the timing that decides correctness here.
    """
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"
    request = _request(suffix)
    record = build_intake_record(
        request,
        tenant_id="tenant-sg",
        idempotency_key=key,
        payload_fingerprint=payload_fingerprint_of(request),
    )

    winner = postgres_ledger._store_record(
        record, request=request, correlation_id=None, trace_id=None
    )
    loser = postgres_ledger._store_record(
        record, request=request, correlation_id=None, trace_id=None
    )

    assert loser.intake_id == winner.intake_id
    assert loser.payload_fingerprint == winner.payload_fingerprint


def test_a_lost_insert_race_with_a_different_payload_conflicts(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
) -> None:
    """The same race, different payload: the key is taken by another request.

    Refusing is the only safe answer -- returning the winner's receipt would
    tell the caller its own request was accepted when a different one was.
    """
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"
    first = _request(suffix)
    postgres_ledger.accept(first, tenant_id="tenant-sg", idempotency_key=key)

    substituted = _request(suffix, candidate_id="icand_substituted")
    conflicting = build_intake_record(
        substituted,
        tenant_id="tenant-sg",
        idempotency_key=key,
        payload_fingerprint=payload_fingerprint_of(substituted),
    )

    with pytest.raises(IdeaEvidenceIntakeConflictError):
        postgres_ledger._store_record(
            conflicting, request=substituted, correlation_id=None, trace_id=None
        )


def test_a_ledger_needs_somewhere_to_connect() -> None:
    """Neither a URL nor a provider is a configuration error, not an empty ledger.

    An intake ledger that silently held nothing would look exactly like one that
    has been reset -- the state report#334 refuses -- so it must refuse to exist
    instead.
    """
    with pytest.raises(ValueError, match="database_url_required"):
        PostgresIdeaEvidenceIntakeLedger()


def test_a_supplied_connection_provider_is_not_closed_by_the_ledger() -> None:
    """Ownership decides who closes.

    The application shares one provider across ledgers, so a ledger closing a
    provider it was handed would shut the pool for everything else using it.
    """
    database_url = _database_url()
    provider = PostgresConnectionProvider(database_url=database_url)
    try:
        borrowed = PostgresIdeaEvidenceIntakeLedger(connection_provider=provider)
        borrowed.close()
        # Still usable: closing a borrowed provider would have broken this.
        assert (
            borrowed.has_record(tenant_id="tenant-sg", idempotency_key=f"absent-{uuid4().hex[:8]}")
            is False
        )
    finally:
        provider.close()


def test_a_ledger_that_owns_its_provider_closes_it() -> None:
    owned = PostgresIdeaEvidenceIntakeLedger(_database_url())
    owned.close()

    with pytest.raises(Exception):
        owned.has_record(tenant_id="tenant-sg", idempotency_key="any-key")


def test_a_naive_acceptance_instant_means_utc_on_both_engines(
    postgres_ledger: PostgresIdeaEvidenceIntakeLedger,
    tmp_path,
) -> None:
    """A caller-supplied naive instant must not depend on the backend.

    PostgreSQL binds a naive value to TIMESTAMPTZ using the session TimeZone,
    so leaving one naive made the stored instant depend on which engine wrote
    it and on how the server happened to be configured -- while the receipt
    kept the unshifted naive value, letting the typed column disagree with the
    response it belongs to.
    """
    suffix = uuid4().hex[:12]
    key = f"intake-{suffix}"
    naive = datetime(2026, 6, 24, 8, 15)
    assert naive.tzinfo is None

    accepted = postgres_ledger.accept(
        _request(suffix), tenant_id="tenant-sg", idempotency_key=key, accepted_at_utc=naive
    )
    sqlite_ledger = IdeaEvidenceIntakeLedger(tmp_path / "intake.sqlite3")
    sqlite_accepted = sqlite_ledger.accept(
        _request(suffix), tenant_id="tenant-sg", idempotency_key=key, accepted_at_utc=naive
    )

    expected = naive.replace(tzinfo=UTC)
    assert accepted.accepted_at_utc == expected
    assert sqlite_accepted.accepted_at_utc == expected
    assert postgres_ledger.snapshot()[("tenant-sg", key)].accepted_at_utc == expected
    assert sqlite_ledger.snapshot()[("tenant-sg", key)].accepted_at_utc == expected
