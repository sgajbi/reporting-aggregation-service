import pytest
from fastapi import HTTPException

from app.services.reporting_read_service import ReportingReadService


class _PasClientSuccess:
    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 200, {
            "snapshot": {
                "overview": {
                    "total_market_value": 1_000_000.0,
                    "total_cash": 50_000.0,
                    "pnl_summary": {"total_pnl": 1_200.0},
                },
                "allocation": {"byAssetClass": [{"group": "Equity", "weight": 0.6}]},
                "incomeAndActivity": {
                    "income_summary_ytd": {"total_dividends": 100.0},
                    "activity_summary_ytd": {"total_deposits": 1_000.0},
                },
                "holdings": {"holdingsByAssetClass": {"Equity": []}},
                "transactions": {"transactionsByAssetClass": {"Equity": []}},
            }
        }


class _PaClientSuccess:
    async def get_pas_input_twr(self, portfolio_id: str, as_of_date: str, periods: list[str]):
        return 200, {
            "resultsByPeriod": {
                "YTD": {
                    "net_cumulative_return": 4.1,
                    "net_annualized_return": 4.1,
                    "gross_cumulative_return": 4.3,
                    "gross_annualized_return": 4.3,
                }
            }
        }


class _PasClientNotFound:
    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 404, {"detail": "Portfolio not found"}


class _PasClientFailure:
    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 503, {"detail": "upstream unavailable"}


class _PaClientFailure:
    async def get_pas_input_twr(self, portfolio_id: str, as_of_date: str, periods: list[str]):
        return 503, {"detail": "upstream unavailable"}


@pytest.mark.asyncio
async def test_summary_composed_from_pas_core_snapshot():
    service = ReportingReadService(pas_client=_PasClientSuccess(), pa_client=_PaClientSuccess())
    response = await service.get_portfolio_summary(
        "P1",
        {"as_of_date": "2026-02-24", "sections": ["WEALTH", "ALLOCATION", "PNL"]},
        "CID-1",
    )
    assert response["scope"]["portfolio_id"] == "P1"
    assert response["wealth"]["total_market_value"] == 1_000_000.0
    assert response["allocation"]["byAssetClass"][0]["group"] == "Equity"
    assert response["pnlSummary"]["total_pnl"] == 1_200.0


@pytest.mark.asyncio
async def test_review_composes_pas_and_pa_performance():
    service = ReportingReadService(pas_client=_PasClientSuccess(), pa_client=_PaClientSuccess())
    response = await service.get_portfolio_review(
        "P1",
        {"as_of_date": "2026-02-24", "sections": ["OVERVIEW", "PERFORMANCE", "HOLDINGS"]},
        "CID-1",
    )
    assert response["portfolio_id"] == "P1"
    assert response["overview"]["total_market_value"] == 1_000_000.0
    assert "YTD" in response["performance"]["summary"]
    assert response["holdings"]["holdingsByAssetClass"] is not None


@pytest.mark.asyncio
async def test_review_sets_performance_none_when_pa_unavailable():
    service = ReportingReadService(pas_client=_PasClientSuccess(), pa_client=_PaClientFailure())
    response = await service.get_portfolio_review(
        "P1",
        {"as_of_date": "2026-02-24", "sections": ["PERFORMANCE"]},
        None,
    )
    assert response["performance"] is None


@pytest.mark.asyncio
async def test_pas_not_found_maps_to_404():
    service = ReportingReadService(pas_client=_PasClientNotFound(), pa_client=_PaClientSuccess())
    with pytest.raises(HTTPException) as exc:
        await service.get_portfolio_summary("P404", {"as_of_date": "2026-02-24"}, None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_pas_failure_maps_to_502():
    service = ReportingReadService(pas_client=_PasClientFailure(), pa_client=_PaClientSuccess())
    with pytest.raises(HTTPException) as exc:
        await service.get_portfolio_review("P1", {"as_of_date": "2026-02-24"}, None)
    assert exc.value.status_code == 502
