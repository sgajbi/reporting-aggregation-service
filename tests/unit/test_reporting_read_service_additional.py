import pytest
from fastapi import HTTPException

from app.services.reporting_read_service import ReportingReadService


class _PasSnapshotMissing:
    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 200, {"unexpected": "shape"}


class _PasSuccessMinimal:
    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 200, {
            "snapshot": {
                "overview": {"total_market_value": 100.0, "total_cash": 10.0},
                "allocation": {"byAssetClass": []},
                "incomeAndActivity": {
                    "income_summary_ytd": {"total_dividends": 3.0},
                    "activity_summary_ytd": {"total_deposits": 5.0},
                },
                "holdings": {"holdingsByAssetClass": {"EQUITY": []}},
                "transactions": {"transactionsByAssetClass": {"EQUITY": []}},
            }
        }


class _PaSuccessEmpty:
    async def get_pas_input_twr(self, portfolio_id: str, as_of_date: str, periods: list[str]):
        return 200, {"resultsByPeriod": {"YTD": {"net_cumulative_return": 2.1}}}


@pytest.mark.asyncio
async def test_summary_requires_as_of_date():
    service = ReportingReadService(pas_client=_PasSuccessMinimal(), pa_client=_PaSuccessEmpty())
    with pytest.raises(HTTPException) as exc:
        await service.get_portfolio_summary("P1", {}, None)
    assert exc.value.status_code == 422
    assert "as_of_date" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_summary_includes_default_sections_when_sections_not_list():
    service = ReportingReadService(pas_client=_PasSuccessMinimal(), pa_client=_PaSuccessEmpty())
    response = await service.get_portfolio_summary(
        "P1",
        {"as_of_date": "2026-02-24", "sections": "WEALTH"},
        None,
    )
    assert "wealth" in response
    assert "allocation" in response
    assert "incomeSummary" in response
    assert "activitySummary" in response


@pytest.mark.asyncio
async def test_summary_snapshot_missing_raises_502():
    service = ReportingReadService(pas_client=_PasSnapshotMissing(), pa_client=_PaSuccessEmpty())
    with pytest.raises(HTTPException) as exc:
        await service.get_portfolio_summary("P1", {"as_of_date": "2026-02-24"}, None)
    assert exc.value.status_code == 502


def test_requested_sections_filters_non_string_values():
    service = ReportingReadService(pas_client=_PasSuccessMinimal(), pa_client=_PaSuccessEmpty())
    sections = service._requested_sections(
        request_payload={"sections": ["overview", 10, None, "performance"]},
        default_sections=["OVERVIEW"],
    )
    assert sections == {"OVERVIEW", "PERFORMANCE"}


def test_map_pa_performance_handles_non_dict_rows():
    service = ReportingReadService(pas_client=_PasSuccessMinimal(), pa_client=_PaSuccessEmpty())
    mapped = service._map_pa_performance({"resultsByPeriod": {"YTD": "bad-row"}})
    assert mapped["summary"]["YTD"]["net_cumulative_return"] is None


@pytest.mark.asyncio
async def test_review_default_sections_include_all_pas_payload_groups():
    service = ReportingReadService(pas_client=_PasSuccessMinimal(), pa_client=_PaSuccessEmpty())
    response = await service.get_portfolio_review(
        "P1",
        {"as_of_date": "2026-02-24"},
        None,
    )
    assert "overview" in response
    assert "allocation" in response
    assert "incomeAndActivity" in response
    assert "holdings" in response
    assert "transactions" in response


@pytest.mark.asyncio
async def test_review_without_performance_section_omits_performance_block():
    service = ReportingReadService(pas_client=_PasSuccessMinimal(), pa_client=_PaSuccessEmpty())
    response = await service.get_portfolio_review(
        "P1",
        {"as_of_date": "2026-02-24", "sections": ["overview", "holdings"]},
        None,
    )
    assert "overview" in response
    assert "holdings" in response
    assert "performance" not in response


@pytest.mark.asyncio
async def test_summary_with_explicit_sections_can_exclude_wealth_and_allocation():
    service = ReportingReadService(pas_client=_PasSuccessMinimal(), pa_client=_PaSuccessEmpty())
    response = await service.get_portfolio_summary(
        "P1",
        {"as_of_date": "2026-02-24", "sections": ["pnl"]},
        None,
    )
    assert "pnlSummary" not in response
    assert "wealth" not in response
    assert "allocation" not in response


def test_to_float_returns_zero_for_non_numeric_string():
    assert ReportingReadService._to_float("not-a-number") == 0.0
