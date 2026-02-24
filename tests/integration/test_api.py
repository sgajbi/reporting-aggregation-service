from fastapi.testclient import TestClient

from app.main import app
from app.routers.reports import get_reporting_read_service

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
