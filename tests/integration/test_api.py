from fastapi.testclient import TestClient

from app.main import app

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
