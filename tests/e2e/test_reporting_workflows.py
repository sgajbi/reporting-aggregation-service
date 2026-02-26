from fastapi.testclient import TestClient

from app.main import app
from app.routers.reports import get_reporting_read_service

client = TestClient(app)


class _WorkflowReportingReadService:
    async def get_portfolio_summary(
        self, portfolio_id: str, request_payload: dict, correlation_id: str | None
    ) -> dict:
        return {
            "scope": {
                "portfolio_id": portfolio_id,
                "as_of_date": request_payload.get("as_of_date"),
            },
            "wealth": {"total_market_value": 250000.0, "total_cash": 12000.0},
            "allocation": {"byAssetClass": [{"group": "EQUITY", "weight": 0.55}]},
        }

    async def get_portfolio_review(
        self, portfolio_id: str, request_payload: dict, correlation_id: str | None
    ) -> dict:
        return {
            "portfolio_id": portfolio_id,
            "as_of_date": request_payload.get("as_of_date"),
            "overview": {"total_market_value": 250000.0},
            "performance": {"summary": {"YTD": {"net_cumulative_return": 3.2}}},
        }


def test_e2e_reporting_summary_flow():
    app.dependency_overrides[get_reporting_read_service] = lambda: _WorkflowReportingReadService()
    response = client.post(
        "/reports/portfolios/DEMO_CA_USD_001/summary",
        headers={"X-Correlation-ID": "cid-e2e-1"},
        json={"as_of_date": "2026-02-24", "sections": ["WEALTH", "ALLOCATION"]},
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 200
    assert response.json()["scope"]["portfolio_id"] == "DEMO_CA_USD_001"
    assert response.json()["wealth"]["total_market_value"] == 250000.0
    assert response.headers["X-Correlation-Id"] == "cid-e2e-1"


def test_e2e_reporting_review_flow():
    app.dependency_overrides[get_reporting_read_service] = lambda: _WorkflowReportingReadService()
    response = client.post(
        "/reports/portfolios/DEMO_CA_USD_001/review",
        headers={"X-Correlation-ID": "cid-e2e-2"},
        json={"as_of_date": "2026-02-24", "sections": ["OVERVIEW", "PERFORMANCE"]},
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 200
    body = response.json()
    assert body["portfolio_id"] == "DEMO_CA_USD_001"
    assert body["performance"]["summary"]["YTD"]["net_cumulative_return"] == 3.2


def test_e2e_aggregation_non_live_flow():
    response = client.get("/aggregations/portfolios/DEMO_CA_USD_001?asOfDate=2026-02-24&live=false")
    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["portfolioId"] == "DEMO_CA_USD_001"
    row_metrics = {row["metric"] for row in body["rows"]}
    assert "market_value_base" in row_metrics


def test_e2e_service_observability_contract_headers():
    response = client.get("/health/ready", headers={"X-Correlation-Id": "cid-e2e-obs"})
    assert response.status_code == 200
    assert response.headers["X-Correlation-Id"] == "cid-e2e-obs"
    assert response.headers.get("X-Request-Id")
    assert response.headers.get("X-Trace-Id")


def test_e2e_health_live_contract():
    response = client.get("/health/live", headers={"X-Correlation-Id": "cid-e2e-live"})
    assert response.status_code == 200
    assert response.json()["status"] == "live"
    assert response.headers["X-Correlation-Id"] == "cid-e2e-live"


def test_e2e_summary_section_limit_rejects_out_of_range():
    app.dependency_overrides[get_reporting_read_service] = lambda: _WorkflowReportingReadService()
    response = client.post(
        "/reports/portfolios/DEMO_CA_USD_001/summary?sectionLimit=21",
        headers={"X-Correlation-ID": "cid-e2e-limit"},
        json={"as_of_date": "2026-02-24", "sections": ["WEALTH"]},
    )
    app.dependency_overrides.pop(get_reporting_read_service, None)

    assert response.status_code == 422
