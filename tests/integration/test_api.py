from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers.reports import get_reporting_read_service

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("X-Correlation-Id")
    assert response.headers.get("X-Request-Id")
    assert response.headers.get("X-Trace-Id")


def test_health_live_and_ready():
    live = client.get("/health/live")
    ready = client.get("/health/ready")
    assert live.status_code == 200
    assert ready.status_code == 200
    assert live.json() == {"status": "live"}
    assert ready.json() == {"status": "ready"}


def test_health_ready_returns_503_when_draining():
    app.state.is_draining = True
    response = client.get("/health/ready")
    app.state.is_draining = False

    assert response.status_code == 503
    assert response.json() == {"status": "draining"}


def test_lifespan_sets_drain_flag_on_shutdown():
    with TestClient(app) as local_client:
        assert app.state.is_draining is False
        response = local_client.get("/health/ready")
        assert response.status_code == 200

    assert app.state.is_draining is True
    app.state.is_draining = False


def test_metrics_endpoint_available():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text or "http_request_duration" in response.text


def test_load_concurrency_health_live_requests():
    def call_live() -> int:
        return client.get("/health/live").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: call_live(), range(32)))

    assert all(status == 200 for status in statuses)


def test_load_concurrency_health_ready_requests():
    def call_ready() -> int:
        return client.get("/health/ready").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: call_ready(), range(32)))

    assert all(status == 200 for status in statuses)


def test_load_concurrency_metrics_requests():
    def call_metrics() -> int:
        return client.get("/metrics").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: call_metrics(), range(24)))

    assert all(status == 200 for status in statuses)


def test_integration_capabilities():
    response = client.get("/integration/capabilities?consumerSystem=lotus-gateway&tenantId=default")
    assert response.status_code == 200
    body = response.json()
    assert body["sourceService"] == "lotus-report"
    assert body["contractVersion"] == "v1"
    assert body["policyVersion"] == "ras-default-v1"
    assert body["supportedInputModes"] == ["pas_ref"]
    assert len(body["features"]) >= 3
    assert len(body["workflows"]) >= 1


def test_aggregation_endpoint():
    response = client.get(
        "/aggregations/portfolios/DEMO_DPM_EUR_001?asOfDate=2026-02-24&live=false"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["portfolioId"] == "DEMO_DPM_EUR_001"
    assert len(body["rows"]) >= 1


def test_generate_report():
    response = client.post(
        "/reports",
        json={
            "portfolioId": "DEMO_DPM_EUR_001",
            "asOfDate": "2026-02-24",
            "reportType": "PORTFOLIO_SNAPSHOT",
            "outputFormat": "PDF",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY"
    assert body["downloadUrl"] is not None


def test_generate_report_non_pdf_has_no_download_url():
    response = client.post(
        "/reports",
        json={
            "portfolioId": "DEMO_DPM_EUR_001",
            "asOfDate": "2026-02-24",
            "reportType": "PORTFOLIO_SNAPSHOT",
            "outputFormat": "JSON",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY"
    assert body["downloadUrl"] is None


class _StubReportingReadService:
    async def get_portfolio_summary(
        self, portfolio_id: str, request_payload: dict, correlation_id: str | None
    ) -> dict:
        scope = {
            "portfolio_id": portfolio_id,
            "as_of_date": request_payload.get("as_of_date"),
        }
        return {
            "scope": scope,
            "wealth": {"total_market_value": 1_000_000.0, "total_cash": 50_000.0},
        }

    async def get_portfolio_review(
        self, portfolio_id: str, request_payload: dict, correlation_id: str | None
    ) -> dict:
        return {
            "portfolio_id": portfolio_id,
            "as_of_date": request_payload.get("as_of_date"),
            "overview": {"total_market_value": 1_000_000.0, "total_cash": 50_000.0},
        }


class _StubReportingReadServiceFailure:
    async def get_portfolio_summary(
        self, portfolio_id: str, request_payload: dict, correlation_id: str | None
    ) -> dict:
        raise HTTPException(status_code=422, detail="Missing required request field: as_of_date")

    async def get_portfolio_review(
        self, portfolio_id: str, request_payload: dict, correlation_id: str | None
    ) -> dict:
        raise HTTPException(status_code=502, detail="lotus-core core snapshot upstream failure")


def test_ras_portfolio_summary_endpoint():
    app.dependency_overrides[get_reporting_read_service] = lambda: _StubReportingReadService()
    response = client.post(
        "/reports/portfolios/DEMO_DPM_EUR_001/summary",
        json={
            "as_of_date": "2026-02-24",
            "period": {"type": "YTD"},
            "sections": ["WEALTH", "ALLOCATION"],
            "allocation_dimensions": ["ASSET_CLASS"],
        },
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["portfolio_id"] == "DEMO_DPM_EUR_001"
    assert body["wealth"]["total_market_value"] == 1_000_000.0


def test_ras_portfolio_summary_propagates_validation_error():
    app.dependency_overrides[get_reporting_read_service] = lambda: (
        _StubReportingReadServiceFailure()
    )
    response = client.post(
        "/reports/portfolios/DEMO_DPM_EUR_001/summary",
        json={},
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 422
    assert "Missing required request field" in response.json()["detail"]


def test_ras_portfolio_review_endpoint():
    app.dependency_overrides[get_reporting_read_service] = lambda: _StubReportingReadService()
    response = client.post(
        "/reports/portfolios/DEMO_DPM_EUR_001/review",
        json={
            "as_of_date": "2026-02-24",
            "sections": ["OVERVIEW", "ALLOCATION", "HOLDINGS"],
        },
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 200
    body = response.json()
    assert body["portfolio_id"] == "DEMO_DPM_EUR_001"
    assert body["overview"]["total_market_value"] == 1_000_000.0


def test_ras_portfolio_review_propagates_upstream_error():
    app.dependency_overrides[get_reporting_read_service] = lambda: (
        _StubReportingReadServiceFailure()
    )
    response = client.post(
        "/reports/portfolios/DEMO_DPM_EUR_001/review",
        json={"as_of_date": "2026-02-24"},
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 502
    assert "upstream failure" in response.json()["detail"]


def test_ras_portfolio_summary_includes_correlation_headers():
    app.dependency_overrides[get_reporting_read_service] = lambda: _StubReportingReadService()
    response = client.post(
        "/reports/portfolios/DEMO_DPM_EUR_001/summary",
        json={
            "as_of_date": "2026-02-24",
            "period": {"type": "YTD"},
        },
        headers={"X-Correlation-Id": "corr-ras-it-001"},
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 200
    assert response.headers.get("X-Correlation-Id") == "corr-ras-it-001"
    assert response.headers.get("X-Request-Id")
    assert response.headers.get("X-Trace-Id")


def test_ras_portfolio_summary_rejects_invalid_section_limit():
    app.dependency_overrides[get_reporting_read_service] = lambda: _StubReportingReadService()
    response = client.post(
        "/reports/portfolios/DEMO_DPM_EUR_001/summary?sectionLimit=0",
        json={"as_of_date": "2026-02-24", "sections": ["WEALTH"]},
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 422
