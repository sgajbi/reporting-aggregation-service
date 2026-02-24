import pytest

from app.services.aggregation_service import AggregationService


class _StubPasClient:
    async def get_core_snapshot(self, portfolio_id: str, as_of_date: str, include_sections: list[str]):
        return (
            200,
            {"snapshot": {"overview": {"total_market_value": 999_999.0}}},
        )


class _StubPaClient:
    async def get_pas_input_twr(self, portfolio_id: str, as_of_date: str, periods: list[str]):
        return (
            200,
            {"resultsByPeriod": {"YTD": {"net_cumulative_return": 4.2}}},
        )


@pytest.mark.asyncio
async def test_live_aggregation_uses_upstream_payloads():
    service = AggregationService(pas_client=_StubPasClient(), pa_client=_StubPaClient())
    response = await service.get_portfolio_aggregation_live(
        portfolio_id="P1",
        as_of_date="2026-02-24",
    )
    metric_map = {row.metric: row.value for row in response.rows}
    assert metric_map["market_value_base"] == 999_999.0
    assert metric_map["return_ytd_pct"] == 4.2
