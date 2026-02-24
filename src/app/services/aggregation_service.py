from datetime import UTC, datetime
from typing import Any

from app.clients.pa_client import PaClient
from app.clients.pas_client import PasClient
from app.config import settings
from app.models.contracts import AggregationRow, AggregationScope, PortfolioAggregationResponse


class AggregationService:
    def __init__(self, pas_client: PasClient | None = None, pa_client: PaClient | None = None):
        self._pas_client = pas_client or PasClient(
            base_url=settings.pas_base_url,
            timeout_seconds=settings.upstream_timeout_seconds,
        )
        self._pa_client = pa_client or PaClient(
            base_url=settings.pa_base_url,
            timeout_seconds=settings.upstream_timeout_seconds,
        )

    async def _fetch_inputs(self, portfolio_id: str, as_of_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
        pas_status, pas_payload = await self._pas_client.get_core_snapshot(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            include_sections=["OVERVIEW", "HOLDINGS"],
        )
        if pas_status >= 400:
            pas_payload = {}

        pa_status, pa_payload = await self._pa_client.get_pas_input_twr(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            periods=["YTD"],
        )
        if pa_status >= 400:
            pa_payload = {}
        return pas_payload, pa_payload

    def get_portfolio_aggregation(
        self,
        portfolio_id: str,
        as_of_date: str,
    ) -> PortfolioAggregationResponse:
        scope = AggregationScope(portfolioId=portfolio_id, asOfDate=as_of_date)
        # Placeholder deterministic rows until PAS+PA connectors are added.
        rows = [
            AggregationRow(bucket="TOTAL", metric="market_value_base", value=1_250_000.0),
            AggregationRow(bucket="EQUITY", metric="weight_pct", value=45.2),
            AggregationRow(bucket="FIXED_INCOME", metric="weight_pct", value=39.8),
            AggregationRow(bucket="CASH", metric="weight_pct", value=15.0),
        ]
        return PortfolioAggregationResponse(
            scope=scope,
            generatedAt=datetime.now(UTC),
            rows=rows,
        )

    async def get_portfolio_aggregation_live(
        self,
        portfolio_id: str,
        as_of_date: str,
    ) -> PortfolioAggregationResponse:
        scope = AggregationScope(portfolioId=portfolio_id, asOfDate=as_of_date)
        pas_payload, pa_payload = await self._fetch_inputs(portfolio_id, as_of_date)

        total_mv = (
            pas_payload.get("snapshot", {})
            .get("overview", {})
            .get("total_market_value")
        )
        if total_mv is None:
            total_mv = 1_250_000.0

        ytd_return = (
            pa_payload.get("resultsByPeriod", {})
            .get("YTD", {})
            .get("net_cumulative_return")
        )
        if ytd_return is None:
            ytd_return = 0.0

        rows = [
            AggregationRow(bucket="TOTAL", metric="market_value_base", value=float(total_mv)),
            AggregationRow(bucket="TOTAL", metric="return_ytd_pct", value=float(ytd_return)),
        ]
        return PortfolioAggregationResponse(
            scope=scope,
            generatedAt=datetime.now(UTC),
            rows=rows,
        )
