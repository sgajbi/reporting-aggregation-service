"""Integrated lifecycle proof (#316) — the #283 closure evidence.

Production-shaped scenario on REAL PostgreSQL paths: two tenants sharing a
portfolio identifier and as-of date, a recurring cycle, a source restatement,
and a template/contract deployment change. Every stage below the provider is
the real machinery — Postgres ledger and snapshot store, the real capture
service (identity, factual boundary, coverage, coherence, lifecycle), the
real render orchestration and custody recording — with owner-shaped payloads
injected only at the upstream-provider and Render/Archive client boundaries,
the same seams the audited unit proofs use. No assertion is a fixture
stating its own intended posture: each one reads persisted rows or recorded
downstream payloads back and compares facts.

Each test is one numbered assertion from issue #316 (audit §5), named
`test_a<N>_...` so a failure names the broken invariant.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import os
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from app.report_batch_orchestrator.dispatch import ReportBatchDispatcher
from app.report_batch_orchestrator.postgres_ledger import PostgresReportBatchLedger
from app.report_batch_orchestrator.scheduler import (
    BatchScheduleDefinition,
    BatchSchedulerConfig,
    ReportBatchScheduler,
)
from app.reporting_jobs.execution import ReportJobExecutionService
from app.reporting_jobs.ledger import compute_request_hash
from app.reporting_jobs.models import (
    PortfolioReviewJobRequest,
    ReportCallerContext,
    ReportJobRegenerateRequest,
    ReportJobReplayRequest,
    ReportJobRerenderRequest,
)
from app.reporting_jobs.postgres_ledger import PostgresReportJobLedger
from app.reporting_jobs.work_queue import ReportJobWorkRetryPolicy
from app.reporting_jobs.worker import ReportJobWorker
from app.reporting_lineage.capture_service import (
    PortfolioReviewInputCapture,
    PortfolioReviewInputCaptureError,
    PortfolioReviewSnapshotCaptureService,
    _RecordedUpstreamCall,
)
from app.reporting_lineage.postgres_store import PostgresReportInputSnapshotStore
from app.reporting_render.service import PortfolioReviewRenderOrchestrationService
from tests.integration.postgres_adapter_ownership import own_postgres_adapter


def _database_url() -> str:
    database_url = os.environ.get("REPORT_JOB_LEDGER_DATABASE_URL")
    if not database_url:
        pytest.skip("REPORT_JOB_LEDGER_DATABASE_URL is required for the integrated proof")
    return database_url


TENANT_A = "tenant-sg"
TENANT_B = "tenant-hk"
SHARED_PORTFOLIO = "PB_SG_GLOBAL_BAL_001"
SHARED_AS_OF = "2026-04-22"


def _caller(*, tenant: str, suffix: str) -> ReportCallerContext:
    return ReportCallerContext(
        trigger_type="user",
        triggered_by=f"advisor-{tenant}",
        caller_application="lotus-gateway",
        tenant_id=tenant,
        region="APAC",
        booking_center_code="SG",
        role=None,
        correlation_id=f"corr-proof-{suffix}",
        trace_id=f"trace-proof-{suffix}",
    )


def _request(*, formats: list[str] | None = None) -> PortfolioReviewJobRequest:
    return PortfolioReviewJobRequest.model_validate(
        {
            "portfolio_scope": {"portfolio_ids": [SHARED_PORTFOLIO]},
            "as_of_date": SHARED_AS_OF,
            "requested_output_formats": formats or ["pdf"],
            "reporting_currency": "USD",
            "options": {"sections": ["OVERVIEW", "PERFORMANCE"]},
        }
    )


def _recorded_call(
    *, service_name: str = "lotus-core", endpoint: str, suffix: str
) -> _RecordedUpstreamCall:
    return _RecordedUpstreamCall(
        service_name=service_name,
        endpoint=endpoint,
        method="POST",
        contract_version="v1",
        request_payload={"portfolio_id": SHARED_PORTFOLIO, "as_of_date": SHARED_AS_OF},
        response_payload={"ok": True},
        response_ref=None,
        status_code=200,
        latency_ms=42,
        supportability_status="complete",
        completeness_status="complete",
        failure_category="none",
        failure_message=None,
        captured_at=datetime(2026, 4, 22, 9, 0, 2, tzinfo=UTC),
        correlation_id=f"corr-proof-{suffix}",
        trace_id=f"trace-proof-{suffix}",
    )


def _source_payload(
    *, tenant: str, restatement: str, suffix: str, content_tag: str | None = None
) -> dict:
    """Owner-shaped captured payload: source-stated revision evidence beside
    composition-instance metadata. Content varies by ``content_tag``
    (defaulting to the tenant) - assertion 1 passes a SHARED tag so that
    tenant identity alone, never content coincidence, carries its fence."""

    tag = content_tag if content_tag is not None else tenant
    return {
        "report_id": f"portfolio-review:{SHARED_PORTFOLIO}:{SHARED_AS_OF}",
        "portfolio_id": SHARED_PORTFOLIO,
        "as_of_date": SHARED_AS_OF,
        "contract_version": "v1",
        "generated_at": "2026-04-22T09:00:01Z",
        "correlation_id": f"corr-proof-{suffix}",
        "holdings": {
            "rows": [{"security_id": "SEC1", "market_value": f"100.25-{tag}"}],
            "sourceProduct": {
                "source_service": "lotus-core",
                "product_name": "HoldingsAsOf",
                "product_version": "v1",
                "as_of_date": SHARED_AS_OF,
                "generated_at": "2026-04-22T08:59:59Z",
                "restatement_version": restatement,
                "source_batch_fingerprint": f"core-batch-{tag}",
                "snapshot_id": f"core-snap-{tag}-{restatement}",
                "content_hash": f"sha256:holdings-{tag}-{restatement}",
                "reconciliation_status": "reconciled",
            },
        },
        # lotus-performance participates through its upstream call but the
        # shipped read payload states no sourceProduct evidence for it - the
        # vector records it as a declared bare participant (real coverage
        # posture: partial), which assertion 6 pins.
        "performance": {"summary": {"twr": "0.0412"}},
    }


class _ScenarioProvider:
    """Owner-shaped upstream boundary: yields the payload staged for each
    job, so cycles and restatements are driven by source facts, never by
    test-side mutation of persisted state."""

    def __init__(self) -> None:
        self.staged: dict[str, dict] = {}
        self.default_payload: dict | None = None

    def stage(self, job_id: str, payload: dict) -> None:
        self.staged[job_id] = payload

    async def collect_for_job(self, job):
        payload = self.staged.get(job.job_id) or self.default_payload
        suffix = job.correlation_id.removeprefix("corr-proof-")
        if payload is None:
            # An unstaged job is the scenario's SOURCE OUTAGE: surface it as
            # the capture error the real upstream boundary raises.
            raise PortfolioReviewInputCaptureError(
                original_error=RuntimeError("upstream_unavailable"),
                upstream_calls=[
                    _recorded_call(endpoint="/reporting/portfolio-summary/query", suffix=suffix)
                ],
            )
        return PortfolioReviewInputCapture(
            snapshot_payload=copy.deepcopy(payload),
            upstream_calls=[
                _recorded_call(endpoint="/reporting/portfolio-summary/query", suffix=suffix),
                _recorded_call(
                    service_name="lotus-performance",
                    endpoint="/performance/workspace-summary",
                    suffix=suffix,
                ),
            ],
        )


class _CustodyRenderClient:
    """Owner-shaped Render client (bb's stated contract facts): create-or-get
    submit keyed by render_job_id, archived_verified custody with a durable
    document id, every submitted package recorded for downstream-fact
    assertions."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.documents: dict[str, str] = {}
        self.template_publication = "published"

    async def submit_render_package(self, payload, **kwargs):
        self.payloads.append(copy.deepcopy(payload))
        render_job_id = payload["render_job_id"]
        # Convergence here is id-keyed only; the REAL owner keys idempotency
        # on (render_job_id, package hash) and refuses a CHANGED package
        # under the same id with 409 render_conflict. No proof scenario
        # resubmits a changed package - do not lean on this simplification
        # for one that does.
        document_id = self.documents.setdefault(render_job_id, f"doc_{uuid4().hex[:12]}")
        return 201, {
            "status": "rendered",
            "render_job_id": render_job_id,
            "template_id": payload["template_id"],
            "template_version": payload["template_version"],
            "template_publication": self.template_publication,
            "artifact_sha256": f"sha256:artifact-{render_job_id}",
            "bounded_determinism_fingerprint": f"fingerprint-{render_job_id}",
            "runtime_engine": "typst",
            "runtime_engine_version": "0.14.2",
            "render_duration_ms": 640,
            "artifact_base64": "JVBERi0xLjQ=",
            "archive_state": "archived_verified",
            "archive_document_id": document_id,
            "archive_detail": None,
        }


class _CustodyArchiveClient:
    """Owner-shaped Archive client for the correction/replacement seams:
    by-request-id lookup reports never-committed (404), lifecycle
    transitions acknowledged and recorded."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.lookups: list[str] = []
        self.lifecycle_transitions: list[dict] = []

    async def archive_document(self, payload, **kwargs):
        self.payloads.append(copy.deepcopy(payload))
        return 201, {"document_id": f"doc_{uuid4().hex[:12]}"}

    async def get_document_by_request_id(self, archive_request_id, **kwargs):
        self.lookups.append(archive_request_id)
        return 404, {"error": {"code": "document_not_found"}}

    async def record_lifecycle_transition(self, **kwargs):
        self.lifecycle_transitions.append(dict(kwargs))
        return 201, {
            "lifecycle_relationship_id": f"life_{uuid4().hex[:8]}",
            "source_document_id": kwargs["source_document_id"],
            "target_document_id": kwargs["target_document_id"],
        }


class _World:
    """One scenario world on the real PostgreSQL adapters."""

    def __init__(self) -> None:
        url = _database_url()
        self.ledger = own_postgres_adapter(PostgresReportJobLedger(url))
        self.store = own_postgres_adapter(PostgresReportInputSnapshotStore(url))
        self.provider = _ScenarioProvider()
        self.capture = PortfolioReviewSnapshotCaptureService(
            snapshot_store=self.store,
            job_ledger=self.ledger,
            portfolio_review_input_provider=self.provider,
        )
        self.render_client = _CustodyRenderClient()
        self.render = PortfolioReviewRenderOrchestrationService(
            render_client=self.render_client,
            snapshot_store=self.store,
            job_ledger=self.ledger,
        )

    def submit(self, *, tenant: str, suffix: str, request=None):
        return self.ledger.submit_portfolio_review_job(
            request=request or _request(),
            caller_context=_caller(tenant=tenant, suffix=suffix),
            idempotency_key=f"proof-{suffix}",
        )

    def park(self, job_id: str) -> None:
        """Cancel a job this test will never execute, so its pending work
        item cannot leak into other integration tests' claim pools (the
        proof shares the session's isolated database with every suite)."""

        record = self.ledger.get_job(job_id)
        self.ledger.cancel_job(
            job_id=job_id,
            actor=record.triggered_by,
            correlation_id=record.correlation_id,
            trace_id=record.trace_id,
        )
        # The claim pool does not filter on job status, so the cancelled
        # job's work item must be consumed: one worker pass completes it.
        self.run_pipeline()

    def run_pipeline(self) -> None:
        worker = ReportJobWorker(
            work_ledger=self.ledger,
            execution_service=ReportJobExecutionService(
                report_job_ledger=self.ledger,
                capture_service=self.capture,
                render_service=self.render,
            ),
            retry_policy=ReportJobWorkRetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
        )
        asyncio.run(
            worker.run_once(
                worker_id="integrated-proof-worker",
                max_items=100,
                lease_seconds=60,
            )
        )


def _archived_cycle(
    world: _World,
    *,
    tenant: str,
    suffix: str,
    restatement: str = "r1",
    content_tag: str | None = None,
):
    """Submit one job for the tenant, stage its source facts, run the real
    pipeline to archived, and return the (job record, snapshot record)."""

    job = world.submit(tenant=tenant, suffix=suffix)
    world.provider.stage(
        job.job_id,
        _source_payload(
            tenant=tenant, restatement=restatement, suffix=suffix, content_tag=content_tag
        ),
    )
    world.run_pipeline()
    record = world.ledger.get_job(job.job_id)
    snapshot = world.store.get_snapshot_by_job(job.job_id)
    return record, snapshot


# ---------------------------------------------------------------------------
# Assertion 1: same portfolio identifier/date under different tenants cannot
# share or cross-resolve report evidence.
# ---------------------------------------------------------------------------


def test_a1_tenants_sharing_portfolio_and_date_cannot_cross_resolve_evidence():
    suffix = uuid4().hex[:12]
    world = _World()
    # Both tenants receive IDENTICAL source facts for the shared portfolio
    # and date - so nothing below can pass by content coincidence: tenant
    # identity alone must carry the fence.
    record_a, snapshot_a = _archived_cycle(
        world, tenant=TENANT_A, suffix=f"a1a-{suffix}", content_tag=f"shared-{suffix}"
    )
    record_b, snapshot_b = _archived_cycle(
        world, tenant=TENANT_B, suffix=f"a1b-{suffix}", content_tag=f"shared-{suffix}"
    )

    assert record_a.job_id != record_b.job_id
    assert snapshot_a.snapshot_id != snapshot_b.snapshot_id
    # The captured FACTS are identical - the factual digest agrees...
    assert snapshot_a.factual_content_digest == snapshot_b.factual_content_digest
    # ...and the revision identity still differs, because the admitted
    # tenant is part of the series identity: one tenant's evidence can
    # never resolve as the other's revision.
    assert snapshot_a.report_revision_id != snapshot_b.report_revision_id

    # Cross-resolution is fenced at the search boundary: tenant A's admitted
    # scope never returns tenant B's job.
    from app.reporting_jobs.models import ReportJobListFilters

    listed_for_a = world.ledger.list_jobs(
        filters=ReportJobListFilters(tenant_id=TENANT_A, correlation_id=f"corr-proof-a1b-{suffix}")
    )
    assert listed_for_a == []
    listed_for_b = world.ledger.list_jobs(
        filters=ReportJobListFilters(tenant_id=TENANT_B, correlation_id=f"corr-proof-a1b-{suffix}")
    )
    assert [item.job_id for item in listed_for_b] == [record_b.job_id]


# ---------------------------------------------------------------------------
# Assertion 2: unknown identity fields fail validation; nested mutations
# cannot alter admitted identity.
# ---------------------------------------------------------------------------


def test_a2_unknown_identity_fields_fail_and_nested_mutation_cannot_switch_tenant():
    suffix = uuid4().hex[:12]
    world = _World()

    # Identity models are fail-closed (extra=forbid, the #290 posture): an
    # unknown field on a governed identity is refused, never absorbed.
    from pydantic import ValidationError

    from app.idea_evidence_intake.models import IdeaEvidenceReportPackageIdentity

    with pytest.raises(ValidationError):
        IdeaEvidenceReportPackageIdentity.model_validate(
            {
                "report_evidence_pack_id": "pack-1",
                "conversion_intent_id": "intent-1",
                "candidate_id": "cand-1",
                "tenant_override": TENANT_B,
            }
        )

    # Admitted identity comes from the caller context alone - request options
    # carrying a tenant-shaped value change nothing about admission.
    smuggling = PortfolioReviewJobRequest.model_validate(
        {
            "portfolio_scope": {"portfolio_ids": [SHARED_PORTFOLIO]},
            "as_of_date": SHARED_AS_OF,
            "requested_output_formats": ["pdf"],
            "reporting_currency": "USD",
            "options": {"sections": ["OVERVIEW"], "tenant_id": TENANT_B},
        }
    )
    job = world.ledger.submit_portfolio_review_job(
        request=smuggling,
        caller_context=_caller(tenant=TENANT_A, suffix=f"a2-{suffix}"),
        idempotency_key=f"proof-a2-{suffix}",
    )
    stored = world.ledger.get_job(job.job_id)
    assert stored.tenant_id == TENANT_A
    world.park(job.job_id)


# ---------------------------------------------------------------------------
# Assertion 3: semantically equivalent declared sets canonicalize
# identically; ordered semantic sequences retain order.
# ---------------------------------------------------------------------------


def test_a3_canonicalization_is_set_stable_and_order_preserving():
    caller = _caller(tenant=TENANT_A, suffix="a3")

    # requested_output_formats is a declared SET: pdf+json in either
    # declaration order is the same request identity.
    hash_pdf_json = compute_request_hash(
        report_type="portfolio_review",
        request=_request(formats=["pdf", "json"]),
        caller_context=caller,
    )
    hash_json_pdf = compute_request_hash(
        report_type="portfolio_review",
        request=_request(formats=["json", "pdf"]),
        caller_context=caller,
    )
    assert hash_pdf_json == hash_json_pdf

    # sections is a declared SET across the whole contract - the series
    # key sorts it and composition consumes it as a set - so a reordered
    # retry is the SAME client intent and converges.
    def _with_options(options: dict):
        return PortfolioReviewJobRequest.model_validate(
            {
                "portfolio_scope": {"portfolio_ids": [SHARED_PORTFOLIO]},
                "as_of_date": SHARED_AS_OF,
                "requested_output_formats": ["pdf"],
                "reporting_currency": "USD",
                "options": options,
            }
        )

    hash_sections = compute_request_hash(
        report_type="portfolio_review",
        request=_with_options({"sections": ["OVERVIEW", "PERFORMANCE"]}),
        caller_context=caller,
    )
    hash_sections_reordered = compute_request_hash(
        report_type="portfolio_review",
        request=_with_options({"sections": ["PERFORMANCE", "OVERVIEW"]}),
        caller_context=caller,
    )
    assert hash_sections == hash_sections_reordered

    # A list the contract does NOT declare a set keeps its order - it may
    # carry output-affecting semantics, so reordering IS a new request.
    hash_ordered = compute_request_hash(
        report_type="portfolio_review",
        request=_with_options({"sections": ["OVERVIEW"], "ranking": ["twr", "fees"]}),
        caller_context=caller,
    )
    hash_reordered = compute_request_hash(
        report_type="portfolio_review",
        request=_with_options({"sections": ["OVERVIEW"], "ranking": ["fees", "twr"]}),
        caller_context=caller,
    )
    assert hash_ordered != hash_reordered


# ---------------------------------------------------------------------------
# Assertion 4: persisted snapshot, job, revision, evidence reference and
# agreed downstream metadata resolve to the same facts.
# ---------------------------------------------------------------------------


def test_a4_job_snapshot_revision_and_downstream_metadata_state_the_same_facts():
    suffix = uuid4().hex[:12]
    world = _World()
    record, snapshot = _archived_cycle(world, tenant=TENANT_A, suffix=f"a4-{suffix}")

    assert record.status == "archived"
    assert snapshot.report_revision_id is not None
    assert snapshot.report_revision_id.startswith("rrv3_")

    # The recorded downstream package carries the SAME identity facts -
    # read from what the render client actually received, not from intent.
    package = world.render_client.payloads[-1]
    render_context = package["render_context"]
    assert render_context["report_revision_id"] == snapshot.report_revision_id
    assert package["snapshot_id"] == snapshot.snapshot_id
    # The custody block Archive stores states the SAME identity and tenant
    # facts the ledger and snapshot hold - read from the recorded handoff.
    custody = render_context["archive"]
    assert custody["report_revision_id"] == snapshot.report_revision_id
    assert custody["tenant_id"] == record.tenant_id
    assert custody["report_request_id"] == record.request_id
    assert record.archive_document_id == world.render_client.documents[record.render_job_id]


# ---------------------------------------------------------------------------
# Assertion 5: a source restatement produces a new revision without changing
# or overwriting the earlier evidence.
# ---------------------------------------------------------------------------


def test_a5_restatement_mints_new_revision_and_leaves_earlier_evidence_untouched():
    suffix = uuid4().hex[:12]
    world = _World()
    first_record, first_snapshot = _archived_cycle(
        world, tenant=TENANT_A, suffix=f"a5a-{suffix}", restatement="r1"
    )
    second_record, second_snapshot = _archived_cycle(
        world, tenant=TENANT_A, suffix=f"a5b-{suffix}", restatement="r2"
    )

    assert second_snapshot.report_revision_id != first_snapshot.report_revision_id

    # The earlier evidence is byte-for-byte what it was: re-read the first
    # snapshot after the restated cycle and compare persisted facts.
    reread = world.store.get_snapshot_by_job(first_record.job_id)
    assert reread.snapshot_hash == first_snapshot.snapshot_hash
    assert reread.factual_content_digest == first_snapshot.factual_content_digest
    assert reread.report_revision_id == first_snapshot.report_revision_id
    assert reread.source_revision_vector == first_snapshot.source_revision_vector

    # And the restatement is VISIBLE as source evidence, not inferred:
    revisions = {
        entry["source_service"]: entry
        for entry in second_snapshot.source_revision_vector["revisions"]
    }
    assert revisions["lotus-core"]["restatement_version"] == "r2"


# ---------------------------------------------------------------------------
# Assertion 6: every source digest entry comes from actual source evidence;
# missing/conflicting evidence retains its declared posture.
# ---------------------------------------------------------------------------


def test_a6_digest_entries_are_source_stated_and_absence_stays_declared():
    suffix = uuid4().hex[:12]
    world = _World()
    job = world.submit(tenant=TENANT_A, suffix=f"a6-{suffix}")
    payload = _source_payload(tenant=TENANT_A, restatement="r1", suffix=f"a6-{suffix}")
    world.provider.stage(job.job_id, payload)
    world.run_pipeline()

    snapshot = world.store.get_snapshot_by_job(job.job_id)
    vector = snapshot.source_revision_vector
    revisions = {entry["source_service"]: entry for entry in vector["revisions"]}

    # Stated entries are the source's own facts, verbatim.
    core = revisions["lotus-core"]
    assert core["content_hash"] == f"sha256:holdings-{TENANT_A}-r1"
    assert core["source_snapshot_id"] == f"core-snap-{TENANT_A}-r1"
    assert core["restatement_version"] == "r1"
    # The evidence-less participant stays DECLARED absent - present in the
    # vector because it took part (its upstream call is recorded), with no
    # manufactured evidence fields, and the coverage claim honestly degrades
    # instead of asserting completeness.
    perf = revisions["lotus-performance"]
    assert "content_hash" not in perf
    assert "source_snapshot_id" not in perf
    assert vector["coverage"] == "partial"


# ---------------------------------------------------------------------------
# Assertion 7: ephemeral composition cannot masquerade as a durable report
# revision.
# ---------------------------------------------------------------------------


def test_a7_ephemeral_composition_mints_no_durable_revision():
    suffix = uuid4().hex[:12]
    world = _World()
    # A composition that never completes capture: the provider fails, the
    # real pipeline records FAILURE EVIDENCE - which must not masquerade as
    # a durable report revision. Revisions mint at exactly one choke point,
    # successful capture completion.
    job = world.submit(tenant=TENANT_A, suffix=f"a7-{suffix}")
    world.run_pipeline()

    record = world.ledger.get_job(job.job_id)
    assert record.status == "failed"
    snapshot = world.store.get_snapshot_by_job(job.job_id)
    # Failure evidence exists durably, but it carries NO revision identity,
    # NO factual digests, and its lifecycle claims no reproduction - nothing
    # downstream can cite this capture as a revision of the report.
    assert snapshot.report_revision_id is None
    assert snapshot.factual_content_digest is None
    assert snapshot.source_revision_vector is None
    assert snapshot.lifecycle is not None
    assert snapshot.lifecycle["reproduction_availability"] == "none"


# ---------------------------------------------------------------------------
# Assertion 8: a deployment between acceptance, capture and package
# construction cannot change the accepted contract.
# ---------------------------------------------------------------------------


def test_a8_deployment_between_acceptance_and_package_cannot_change_the_contract(monkeypatch):
    suffix = uuid4().hex[:12]
    world = _World()
    job = world.submit(tenant=TENANT_A, suffix=f"a8-{suffix}")
    accepted = world.ledger.get_job(job.job_id).accepted_document_contract
    assert accepted is not None

    # The deployment moves AFTER acceptance: every current-resolution symbol
    # the package stage could consult is poisoned, so any re-resolution
    # would surface as the poison value in the recorded package.
    from app.report_ordering_catalogue import template_resolution
    from app.reporting_render import package_builder

    poisoned_family = dataclasses.replace(
        template_resolution.resolve_report_family("portfolio_review"),
        template_version="v99-poison",
        standard_disclosure_ref="poison.disclosures.v99",
    )
    monkeypatch.setattr(
        package_builder, "resolve_report_family", lambda report_type: poisoned_family
    )

    world.provider.stage(
        job.job_id, _source_payload(tenant=TENANT_A, restatement="r1", suffix=f"a8-{suffix}")
    )
    world.run_pipeline()

    record = world.ledger.get_job(job.job_id)
    assert record.status == "archived"
    package = world.render_client.payloads[-1]
    assert package["template_version"] == accepted["template_version"]
    assert package["template_version"] != "v99-poison"
    assert "poison.disclosures.v99" not in package["disclosure_refs"]


def test_a8b_shape_binding_axis_fails_closed_instead_of_relabelling(monkeypatch):
    suffix = uuid4().hex[:12]
    world = _World()
    job = world.submit(tenant=TENANT_A, suffix=f"a8b-{suffix}")

    # A deployment now composing a DIFFERENT report-data shape must refuse
    # the accepted job rather than mislabel the payload - regenerate is the
    # governed remedy, silent relabelling never is.
    from app.reporting_render import package_builder

    monkeypatch.setattr(
        package_builder,
        "resolve_report_data_contract",
        lambda report_type: "portfolio_review.v9",
    )
    world.provider.stage(
        job.job_id, _source_payload(tenant=TENANT_A, restatement="r1", suffix=f"a8b-{suffix}")
    )
    world.run_pipeline()

    record = world.ledger.get_job(job.job_id)
    assert record.status != "archived"
    handed_off = [
        payload
        for payload in world.render_client.payloads
        if payload["report_job_id"] == job.job_id
    ]
    assert handed_off == []


# ---------------------------------------------------------------------------
# Assertion 9: failed-work replay preserves the source job's accepted
# contract, including across a family-default change.
# ---------------------------------------------------------------------------


def test_a9_replay_preserves_the_accepted_contract_across_a_default_change(monkeypatch):
    suffix = uuid4().hex[:12]
    world = _World()
    source = world.submit(tenant=TENANT_A, suffix=f"a9-{suffix}")
    source = world.ledger.mark_failed(
        job_id=source.job_id,
        actor=source.triggered_by,
        correlation_id=source.correlation_id,
        trace_id=source.trace_id,
        failure_category="upstream_data_failed",
        failure_message="Upstream timeout.",
        retry_eligible=True,
    )
    accepted = source.accepted_document_contract
    assert accepted is not None

    # The family default moves between the source's acceptance and the
    # replay: current resolution now yields the poison pair, so ONLY
    # verbatim inheritance can reproduce the source's contract.
    from app.report_ordering_catalogue import template_resolution

    monkeypatch.setattr(
        template_resolution,
        "accepted_template_identity",
        lambda report_type, output_formats: ("portfolio-review", "v99-poison"),
    )

    # Poison is live: a job accepted DURING the window resolves it.
    poisoned_job = world.submit(tenant=TENANT_B, suffix=f"a9p-{suffix}")
    assert poisoned_job.render_template_version == "v99-poison"
    world.park(poisoned_job.job_id)

    from app.reporting_render.replay_service import PortfolioReviewReplayService

    world.provider.default_payload = _source_payload(
        tenant=TENANT_A, restatement="r1", suffix=f"a9-{suffix}"
    )
    replay_service = PortfolioReviewReplayService(
        ledger=world.ledger,
        capture_service=world.capture,
        render_service=world.render,
    )
    result = asyncio.run(
        replay_service.replay_job(
            job_id=source.job_id,
            command=ReportJobReplayRequest(reason="Integrated proof assertion 9."),
            caller_context=_caller(tenant=TENANT_A, suffix=f"a9r-{suffix}"),
            idempotency_key=f"proof-a9-replay-{suffix}",
        )
    )

    replayed = world.ledger.get_job(result.replayed_job.job_id)
    assert replayed.render_template_version == accepted["template_version"]
    assert replayed.render_template_version != "v99-poison"
    assert replayed.accepted_document_contract == accepted


# ---------------------------------------------------------------------------
# Assertion 10: pure rerender retains the same snapshot/revision and
# contract while recording distinct execution/artifact identity.
# ---------------------------------------------------------------------------


def test_a10_rerender_reuses_snapshot_revision_and_contract_with_new_execution_identity():
    suffix = uuid4().hex[:12]
    world = _World()
    record, snapshot = _archived_cycle(world, tenant=TENANT_A, suffix=f"a10-{suffix}")
    original_package = world.render_client.payloads[-1]

    from app.reporting_render.rerender_service import PortfolioReviewRerenderService

    rerender_service = PortfolioReviewRerenderService(
        render_client=world.render_client,
        archive_client=_CustodyArchiveClient(),
        snapshot_store=world.store,
        ledger=world.ledger,
    )
    attempt = asyncio.run(
        rerender_service.rerender_job(
            job_id=record.job_id,
            command=ReportJobRerenderRequest(reason="Integrated proof assertion 10."),
            caller_context=_caller(tenant=TENANT_A, suffix=f"a10r-{suffix}"),
            idempotency_key=f"proof-a10-rerender-{suffix}",
        )
    )

    rerender_package = world.render_client.payloads[-1]
    # Same facts: snapshot, revision, template, document reference.
    assert rerender_package["snapshot_id"] == snapshot.snapshot_id
    assert rerender_package["render_context"]["report_revision_id"] == snapshot.report_revision_id
    assert rerender_package["template_version"] == original_package["template_version"]
    assert (
        rerender_package["render_context"]["document_reference"]
        == original_package["render_context"]["document_reference"]
    )
    # Distinct execution identity: a new render job for the correction.
    assert rerender_package["render_job_id"] != original_package["render_job_id"]
    assert attempt.render_job_id == rerender_package["render_job_id"]


# ---------------------------------------------------------------------------
# Assertion 11: regenerate creates a fresh capture with explicit
# predecessor/replacement lineage and the chosen approved contract.
# ---------------------------------------------------------------------------


def test_a11_regenerate_captures_fresh_with_explicit_replacement_lineage():
    suffix = uuid4().hex[:12]
    world = _World()
    record, snapshot = _archived_cycle(
        world, tenant=TENANT_A, suffix=f"a11-{suffix}", restatement="r1"
    )
    archive_client = _CustodyArchiveClient()

    from app.reporting_render.regenerate_service import PortfolioReviewRegenerateService

    world.provider.default_payload = _source_payload(
        tenant=TENANT_A, restatement="r2", suffix=f"a11n-{suffix}"
    )
    regenerate_service = PortfolioReviewRegenerateService(
        ledger=world.ledger,
        snapshot_store=world.store,
        capture_service=world.capture,
        render_service=world.render,
        archive_lineage_client=archive_client,
    )
    result = asyncio.run(
        regenerate_service.regenerate_job(
            job_id=record.job_id,
            command=ReportJobRegenerateRequest(reason="Integrated proof assertion 11."),
            caller_context=_caller(tenant=TENANT_A, suffix=f"a11r-{suffix}"),
            idempotency_key=f"proof-a11-regenerate-{suffix}",
        )
    )

    new_record = world.ledger.get_job(result.regenerated_job.job_id)
    new_snapshot = world.store.get_snapshot_by_job(new_record.job_id)
    # Fresh capture: new snapshot, new revision minted from the restated
    # sources - never a clone of the predecessor's facts.
    assert new_snapshot.snapshot_id != snapshot.snapshot_id
    assert new_snapshot.report_revision_id != snapshot.report_revision_id
    revisions = {
        entry["source_service"]: entry for entry in new_snapshot.source_revision_vector["revisions"]
    }
    assert revisions["lotus-core"]["restatement_version"] == "r2"
    # Explicit predecessor/replacement lineage, recorded durably.
    relationships = world.ledger.list_job_relationships(record.job_id)
    assert any(
        rel.relationship_type == "regenerate_replacement"
        and rel.derived_report_job_id == new_record.job_id
        for rel in relationships
    )
    assert new_record.archive_document_id is not None
    assert new_record.archive_document_id != record.archive_document_id


# ---------------------------------------------------------------------------
# Assertion 12: repeated or concurrent scheduled-cycle execution creates only
# the intended logical work across a default-version change.
# ---------------------------------------------------------------------------


def _schedule_config(schedule_id: str) -> "BatchSchedulerConfig":
    return BatchSchedulerConfig(
        scheduler_id=f"scheduler-{schedule_id}",
        interval_seconds=60.0,
        tenant_id=TENANT_A,
        region="APAC",
        booking_center_code="SG",
        role="system",
        schedules=(
            BatchScheduleDefinition(
                schedule_id=schedule_id,
                enabled=True,
                selector_mode="explicit_portfolio_list",
                frequency="monthly",
                as_of_date=date(2026, 4, 22),
                portfolio_ids=[SHARED_PORTFOLIO],
                requested_output_formats=["pdf"],
                reporting_currency="USD",
                options={"sections": ["OVERVIEW"]},
            ),
        ),
    )


class _SchedulerPortfolioSource:
    async def get_portfolio_detail(self, portfolio_id, correlation_id=None):
        # report#177: Core projects the owning tenant and the scheduler refuses a
        # candidate it cannot attribute. TENANT_A rather than a literal, so the
        # fake cannot drift away from the tenant these schedules run under.
        return 200, {"portfolio_id": portfolio_id, "tenant_id": TENANT_A, "status": "active"}

    async def list_portfolios(self, correlation_id=None):
        return 200, {"portfolios": [{"portfolio_id": SHARED_PORTFOLIO, "status": "active"}]}


def test_a12_repeated_scheduled_cycles_converge_across_a_default_change(monkeypatch):
    suffix = uuid4().hex[:12]
    schedule_id = f"proof-cycle-{suffix}"
    batch_ledger = own_postgres_adapter(PostgresReportBatchLedger(_database_url()))
    scheduler = ReportBatchScheduler(
        batch_ledger=batch_ledger,
        portfolio_source=_SchedulerPortfolioSource(),
    )
    caller = _caller(tenant=TENANT_A, suffix=f"a12-{suffix}")

    first = asyncio.run(
        scheduler.run_due_schedules(
            config=_schedule_config(schedule_id),
            caller_context=caller,
            evaluation_date=date(2026, 4, 22),
        )
    )
    assert len(first.materialized) == 1

    # The family default moves between the first execution and the rerun -
    # cycle recognition is business-cycle identity ONLY, so the rerun still
    # converges on the existing batch instead of materializing a duplicate.
    from app.report_ordering_catalogue import template_resolution

    monkeypatch.setattr(
        template_resolution,
        "accepted_template_identity",
        lambda report_type, output_formats: ("portfolio-review", "v99-poison"),
    )
    second = asyncio.run(
        scheduler.run_due_schedules(
            config=_schedule_config(schedule_id),
            caller_context=caller,
            evaluation_date=date(2026, 4, 22),
        )
    )
    assert second.materialized == ()
    assert schedule_id in second.skipped_schedule_ids

    # Exactly one batch exists for the cycle's durable facts.
    assert batch_ledger.has_batch_for_schedule_cycle(
        tenant_id=TENANT_A,
        region="APAC",
        schedule_id=schedule_id,
        period_start="2026-04-01",
        period_end="2026-04-22",
        as_of_date="2026-04-22",
    )


# ---------------------------------------------------------------------------
# Assertion 13: scheduled requested policy and each accepted job's resolved
# contract agree and remain distinguishable.
# ---------------------------------------------------------------------------


def test_a13_requested_policy_and_resolved_contract_agree_and_stay_distinct():
    suffix = uuid4().hex[:12]
    schedule_id = f"proof-policy-{suffix}"
    world = _World()
    batch_ledger = own_postgres_adapter(PostgresReportBatchLedger(_database_url()))
    scheduler = ReportBatchScheduler(
        batch_ledger=batch_ledger,
        portfolio_source=_SchedulerPortfolioSource(),
    )
    caller = _caller(tenant=TENANT_A, suffix=f"a13-{suffix}")
    run = asyncio.run(
        scheduler.run_due_schedules(
            config=_schedule_config(schedule_id),
            caller_context=caller,
            evaluation_date=date(2026, 4, 22),
        )
    )
    batch = batch_ledger.get_batch(run.materialized[0].batch_id)

    # The schedule states REQUESTED policy only - output formats, currency,
    # composition options - plus the durable cycle-identity facts. It has no
    # template or contract axes to state.
    assert batch.requested_output_formats == ["pdf"]
    assert batch.reporting_currency == "USD"
    assert batch.options["sections"] == ["OVERVIEW"]
    assert batch.options["batch_schedule_id"] == schedule_id
    assert "template_version" not in batch.options

    dispatcher = ReportBatchDispatcher(
        batch_ledger=batch_ledger,
        report_job_ledger=world.ledger,
    )
    dispatched = dispatcher.dispatch_batch(
        batch_id=batch.batch_id,
        caller_context=caller,
        worker_id=f"proof-dispatcher-{suffix}",
    )
    assert len(dispatched.report_job_ids) == 1
    job = world.ledger.get_job(dispatched.report_job_ids[0])

    # The job's request AGREES with the schedule's requested policy...
    assert job.requested_output_formats == batch.requested_output_formats
    assert job.reporting_currency == batch.reporting_currency
    assert job.options["sections"] == ["OVERVIEW"]
    # ...while the resolved document contract is the ACCEPTANCE's own fact,
    # stamped from the governed definitions - present on the job, absent
    # from the schedule, so the two authorities remain distinguishable.
    contract = job.accepted_document_contract
    assert contract is not None
    assert contract["template_version"] == job.render_template_version
    assert contract["report_data_contract_version"] == "portfolio_review.v1"
    world.park(job.job_id)


# ---------------------------------------------------------------------------
# Assertion 14: a commit followed by a lost response and process restart
# converges without duplicate client artifacts or guessed custody.
# ---------------------------------------------------------------------------


class _LostResponseRenderClient(_CustodyRenderClient):
    """The owner COMMITS the render and archives the document, but the
    response never reaches Report - then answers the restart's status
    lookup with the committed outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.lost_once = False
        self.committed: dict | None = None
        self.status_lookups: list[str] = []

    async def submit_render_package(self, payload, **kwargs):
        status, response = await super().submit_render_package(payload, **kwargs)
        if not self.lost_once:
            self.lost_once = True
            self.committed = response
            raise RuntimeError("connection_lost_after_owner_commit")
        return status, response

    async def get_render_status(self, render_job_id, **kwargs):
        self.status_lookups.append(render_job_id)
        assert self.committed is not None
        committed = dict(self.committed)
        committed.pop("artifact_base64", None)
        return 200, committed


def test_a14_lost_response_and_restart_converge_without_duplicate_artifacts():
    suffix = uuid4().hex[:12]
    world = _World()
    world.render_client = _LostResponseRenderClient()
    world.render = PortfolioReviewRenderOrchestrationService(
        render_client=world.render_client,
        snapshot_store=world.store,
        job_ledger=world.ledger,
    )
    job = world.submit(tenant=TENANT_A, suffix=f"a14-{suffix}")
    world.provider.stage(
        job.job_id, _source_payload(tenant=TENANT_A, restatement="r1", suffix=f"a14-{suffix}")
    )

    # First attempt: the process CRASHES mid-execution - the owner has
    # committed, the response is lost, no failure bookkeeping ever runs,
    # and the work lease simply expires (lease_seconds=0).
    claimed = world.ledger.claim_work_items(
        worker_id=f"crashing-worker-{suffix}", limit=10, lease_seconds=0
    )
    assert len(claimed) == 1
    execution = ReportJobExecutionService(
        report_job_ledger=world.ledger,
        capture_service=world.capture,
        render_service=world.render,
    )
    with pytest.raises(RuntimeError, match="connection_lost_after_owner_commit"):
        asyncio.run(execution.execute_job(job_id=job.job_id))
    interrupted = world.ledger.get_job(job.job_id)
    assert interrupted.status != "archived"
    assert interrupted.render_job_id is not None

    # Process restart: a fresh worker resumes the SAME persisted render -
    # resolution before recomposition adopts the owner's committed outcome.
    world.run_pipeline()

    record = world.ledger.get_job(job.job_id)
    assert record.status == "archived"
    assert record.render_job_id == interrupted.render_job_id
    # Exactly one client artifact exists, and custody is the owner's
    # recorded fact, never a guess: one submitted package, one document,
    # and the adoption went through the status lookup.
    assert len(world.render_client.payloads) == 1
    assert len(world.render_client.documents) == 1
    assert record.archive_document_id == world.render_client.documents[record.render_job_id]
    assert world.render_client.status_lookups != []


# ---------------------------------------------------------------------------
# Assertion 15: transient supersession failure recovers through bounded
# reconciliation without a new client correction request.
# ---------------------------------------------------------------------------


class _FlakyLifecycleArchiveClient(_CustodyArchiveClient):
    """Archive's lifecycle boundary is down for the first transition
    attempt, healthy afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def record_lifecycle_transition(self, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            return 503, {"detail": "archive lifecycle temporarily unavailable"}
        return await super().record_lifecycle_transition(**kwargs)


def test_a15_transient_supersession_failure_self_heals_without_new_commands():
    suffix = uuid4().hex[:12]
    world = _World()
    record, _snapshot = _archived_cycle(world, tenant=TENANT_A, suffix=f"a15-{suffix}")
    archive_client = _FlakyLifecycleArchiveClient()

    from app.reporting_render.archive_lineage import reconcile_pending_archive_lineage
    from app.reporting_render.regenerate_service import PortfolioReviewRegenerateService

    world.provider.default_payload = _source_payload(
        tenant=TENANT_A, restatement="r2", suffix=f"a15n-{suffix}"
    )
    regenerate_service = PortfolioReviewRegenerateService(
        ledger=world.ledger,
        snapshot_store=world.store,
        capture_service=world.capture,
        render_service=world.render,
        archive_lineage_client=archive_client,
    )
    result = asyncio.run(
        regenerate_service.regenerate_job(
            job_id=record.job_id,
            command=ReportJobRegenerateRequest(reason="Integrated proof assertion 15."),
            caller_context=_caller(tenant=TENANT_A, suffix=f"a15r-{suffix}"),
            idempotency_key=f"proof-a15-regenerate-{suffix}",
        )
    )
    assert archive_client.attempts == 1
    assert archive_client.lifecycle_transitions == []

    # The replacement document exists; only the lifecycle pair is pending.
    pending = [
        entry
        for entry in world.ledger.list_pending_archive_lineage(limit=200)
        if entry.job_id in {record.job_id, result.regenerated_job.job_id}
    ]
    assert len(pending) == 1

    # One bounded reconciliation pass after Archive recovers settles the
    # pair - nobody orders another correction or regenerate.
    outcome = asyncio.run(
        reconcile_pending_archive_lineage(
            archive_client=archive_client,
            ledger=world.ledger,
            limit=200,
        )
    )
    assert outcome["attempted_jobs"] >= 1
    assert archive_client.attempts == 2
    assert len(archive_client.lifecycle_transitions) == 1
    still_pending = [
        entry
        for entry in world.ledger.list_pending_archive_lineage(limit=200)
        if entry.job_id in {record.job_id, result.regenerated_job.job_id}
    ]
    assert still_pending == []


# ---------------------------------------------------------------------------
# Assertion 16: publication evidence and custody are independently recorded;
# development-template availability does not imply client distribution
# authority.
# ---------------------------------------------------------------------------


def test_a16_custody_and_publication_evidence_are_independent_facts():
    suffix = uuid4().hex[:12]
    world = _World()
    world.render_client.template_publication = "development"
    record, _snapshot = _archived_cycle(world, tenant=TENANT_A, suffix=f"a16-{suffix}")

    # Custody IS recorded - the document exists durably in Archive...
    assert record.status == "archived"
    assert record.archive_document_id is not None
    # ...while the publication evidence persists the owner's stated posture
    # VERBATIM: development availability is an evidence fact on the job,
    # never an implied distribution authority.
    assert record.render_template_publication == "development"


# ---------------------------------------------------------------------------
# Assertion 17: legacy incomplete identities/contracts, historical profile
# limitations and snapshot reproduction availability remain explicit; no
# historical record is rewritten to manufacture certainty.
# ---------------------------------------------------------------------------


def test_a17_legacy_rows_stay_explicit_and_are_never_rewritten():
    suffix = uuid4().hex[:12]
    world = _World()
    # A pre-identity, policy-1.0.0 snapshot: no revision binding, the
    # command-shaped lifecycle spelling - exactly what history holds.
    legacy_job = world.submit(tenant=TENANT_A, suffix=f"a17-{suffix}")
    from app.reporting_lineage.models import ReportInputSnapshotCreateRequest

    created = world.store.create_snapshot(
        ReportInputSnapshotCreateRequest(
            report_job_id=legacy_job.job_id,
            report_type="portfolio_review",
            report_data_contract_version="v1",
            portfolio_scope={"portfolio_ids": [SHARED_PORTFOLIO]},
            as_of_date=SHARED_AS_OF,
            snapshot_payload={"report_id": f"legacy-{suffix}"},
            supportability_status="complete",
            completeness_status="complete",
            lineage_summary={"source_services": ["lotus-core"]},
            lifecycle={
                "policy_ref": "report-input-snapshot-standard",
                "policy_version": "1.0.0",
                "reproduction_availability": "rerender_from_snapshot",
                "lifecycle_authority": "report-input-snapshot",
            },
            captured_at=datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC),
            correlation_id=f"corr-proof-a17-{suffix}",
            trace_id=f"trace-proof-a17-{suffix}",
        )
    )

    loaded = world.store.get_snapshot_by_job(legacy_job.job_id)
    # Incomplete identity stays EXPLICITLY absent - never backfilled.
    assert loaded.report_revision_id is None
    assert loaded.factual_content_digest is None
    assert loaded.source_revision_vector is None
    # The capability claim reads in current vocabulary...
    assert loaded.lifecycle is not None
    assert loaded.lifecycle["reproduction_availability"] == "snapshot_recomposition"
    assert loaded.lifecycle["policy_version"] == "1.0.0"
    # ...while the stored row keeps its historical bytes verbatim.
    stored = world.store.get_stored_lifecycle(created.snapshot_id)
    assert stored is not None
    assert stored["reproduction_availability"] == "rerender_from_snapshot"
    # And the command surface stays truthful for the legacy job: never
    # archived, so no rerender path is advertised or accepted.
    from app.reporting_render.rerender_service import rerender_eligible

    assert rerender_eligible(world.ledger.get_job(legacy_job.job_id)) is False
    world.park(legacy_job.job_id)
