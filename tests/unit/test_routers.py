import pytest

from app.routers.aggregations import get_portfolio_aggregation
from app.routers.reports import _apply_section_limit, get_reporting_read_service
from app.services.reporting_read_service import ReportingReadService


class _LiveAggregationServiceStub:
    async def get_portfolio_aggregation_live(self, portfolio_id: str, as_of_date: str):
        return {"mode": "live", "portfolio_id": portfolio_id, "as_of_date": as_of_date}

    def get_portfolio_aggregation(self, portfolio_id: str, as_of_date: str):
        return {"mode": "static", "portfolio_id": portfolio_id, "as_of_date": as_of_date}


@pytest.mark.asyncio
async def test_aggregation_router_live_branch(monkeypatch):
    monkeypatch.setattr(
        "app.routers.aggregations.AggregationService",
        lambda: _LiveAggregationServiceStub(),
    )
    response = await get_portfolio_aggregation(
        portfolio_id="P1",
        as_of_date="2026-02-24",
        live=True,
    )
    assert response["mode"] == "live"


def test_reporting_router_dependency_factory():
    service = get_reporting_read_service()
    assert isinstance(service, ReportingReadService)


def test_apply_section_limit_trims_oversized_sections():
    payload = {"sections": ["A", "B", "C"], "as_of_date": "2026-02-25"}
    limited = _apply_section_limit(payload, section_limit=2)
    assert limited["sections"] == ["A", "B"]


def test_apply_section_limit_keeps_non_list_sections():
    payload = {"sections": "ALL"}
    limited = _apply_section_limit(payload, section_limit=2)
    assert limited["sections"] == "ALL"
