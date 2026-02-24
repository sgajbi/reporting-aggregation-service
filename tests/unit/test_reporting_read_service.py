import pytest
from fastapi import HTTPException

from app.services.reporting_read_service import ReportingReadService


class _SuccessPasClient:
    async def get_portfolio_summary(
        self,
        portfolio_id: str,
        payload: dict,
        correlation_id: str | None,
    ):
        return 200, {
            "scope": {"portfolio_id": portfolio_id},
            "wealth": {"total_market_value": 100.0},
        }

    async def get_portfolio_review(
        self,
        portfolio_id: str,
        payload: dict,
        correlation_id: str | None,
    ):
        return 200, {"portfolio_id": portfolio_id, "overview": {"total_market_value": 100.0}}


class _NotFoundPasClient:
    async def get_portfolio_summary(
        self,
        portfolio_id: str,
        payload: dict,
        correlation_id: str | None,
    ):
        return 404, {"detail": "Portfolio not found"}

    async def get_portfolio_review(
        self,
        portfolio_id: str,
        payload: dict,
        correlation_id: str | None,
    ):
        return 404, {"detail": "Portfolio not found"}


class _FailingPasClient:
    async def get_portfolio_summary(
        self,
        portfolio_id: str,
        payload: dict,
        correlation_id: str | None,
    ):
        return 503, {"detail": "upstream unavailable"}

    async def get_portfolio_review(
        self,
        portfolio_id: str,
        payload: dict,
        correlation_id: str | None,
    ):
        return 500, {"detail": "internal error"}


@pytest.mark.asyncio
async def test_summary_success_passthrough():
    service = ReportingReadService(pas_client=_SuccessPasClient())
    response = await service.get_portfolio_summary("P1", {"as_of_date": "2026-02-24"}, "CID-1")
    assert response["scope"]["portfolio_id"] == "P1"


@pytest.mark.asyncio
async def test_review_success_passthrough():
    service = ReportingReadService(pas_client=_SuccessPasClient())
    response = await service.get_portfolio_review("P1", {"as_of_date": "2026-02-24"}, "CID-1")
    assert response["portfolio_id"] == "P1"


@pytest.mark.asyncio
async def test_not_found_maps_to_404():
    service = ReportingReadService(pas_client=_NotFoundPasClient())
    with pytest.raises(HTTPException) as exc:
        await service.get_portfolio_summary("P404", {"as_of_date": "2026-02-24"}, None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_upstream_failure_maps_to_502():
    service = ReportingReadService(pas_client=_FailingPasClient())
    with pytest.raises(HTTPException) as exc:
        await service.get_portfolio_review("P1", {"as_of_date": "2026-02-24"}, None)
    assert exc.value.status_code == 502
