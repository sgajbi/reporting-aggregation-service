from datetime import UTC, datetime
from typing import Any

from app.clients.pa_client import PaClient
from app.clients.pas_client import PasClient
from app.config import settings
from app.models.contracts import AggregationRow, AggregationScope, PortfolioAggregationResponse
from app.precision_policy import quantize_money, quantize_performance, quantize_quantity, to_decimal


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

    async def _fetch_inputs(
        self, portfolio_id: str, as_of_date: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
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

    def _parse_market_value(self, position: dict[str, Any]) -> float | None:
        valuation = position.get("valuation")
        if isinstance(valuation, dict):
            for key in ("market_value_base", "market_value", "current_value_base", "current_value"):
                value = valuation.get(key)
                if value is None:
                    continue
                try:
                    return float(quantize_money(value))
                except (TypeError, ValueError):
                    continue
        for key in ("market_value_base", "market_value", "current_value_base", "current_value"):
            value = position.get(key)
            if value is None:
                continue
            try:
                return float(quantize_money(value))
            except (TypeError, ValueError):
                continue
        return None

    def _build_asset_class_rows(
        self, pas_payload: dict[str, Any], total_mv: float
    ) -> list[AggregationRow]:
        snapshot = pas_payload.get("snapshot", {})
        if not isinstance(snapshot, dict):
            return []
        holdings = snapshot.get("holdings", {})
        if not isinstance(holdings, dict):
            return []
        by_asset_class = holdings.get("holdingsByAssetClass", {})
        if not isinstance(by_asset_class, dict):
            return []

        rows: list[AggregationRow] = []
        for asset_class, positions in by_asset_class.items():
            if not isinstance(positions, list):
                continue
            asset_market_value = 0.0
            for position in positions:
                if not isinstance(position, dict):
                    continue
                parsed_mv = self._parse_market_value(position)
                if parsed_mv is not None:
                    asset_market_value += parsed_mv
            if asset_market_value <= 0 or total_mv <= 0:
                continue
            rows.append(
                AggregationRow(
                    bucket=str(asset_class).upper(),
                    metric="weight_pct",
                    value=float(
                        quantize_performance(
                            (to_decimal(asset_market_value) / to_decimal(total_mv)) * 100
                        )
                    ),
                )
            )

        rows.sort(key=lambda row: row.bucket)
        return rows

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

        total_mv = pas_payload.get("snapshot", {}).get("overview", {}).get("total_market_value")
        if total_mv is None:
            total_mv = 1_250_000.0

        ytd_return = (
            pa_payload.get("resultsByPeriod", {}).get("YTD", {}).get("net_cumulative_return")
        )
        if ytd_return is None:
            ytd_return = 0.0

        position_count = 0
        holdings = pas_payload.get("snapshot", {}).get("holdings", {})
        if isinstance(holdings, dict):
            by_asset_class = holdings.get("holdingsByAssetClass", {})
            if isinstance(by_asset_class, dict):
                for items in by_asset_class.values():
                    if isinstance(items, list):
                        position_count += len(items)

        rows = [
            AggregationRow(
                bucket="TOTAL",
                metric="market_value_base",
                value=float(quantize_money(total_mv)),
            ),
            AggregationRow(
                bucket="TOTAL",
                metric="position_count",
                value=float(quantize_quantity(position_count)),
            ),
            AggregationRow(
                bucket="TOTAL",
                metric="return_ytd_pct",
                value=float(quantize_performance(ytd_return)),
            ),
        ]
        rows.extend(
            self._build_asset_class_rows(
                pas_payload=pas_payload,
                total_mv=float(quantize_money(total_mv)),
            )
        )
        return PortfolioAggregationResponse(
            scope=scope,
            generatedAt=datetime.now(UTC),
            rows=rows,
        )
