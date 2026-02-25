import pytest

from app.services.aggregation_service import AggregationService


class _PasOkClient:
    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 200, {"snapshot": {"overview": {"total_market_value": 1000.0}}}


class _PaOkClient:
    async def get_pas_input_twr(self, portfolio_id: str, as_of_date: str, periods: list[str]):
        return 200, {"resultsByPeriod": {"YTD": {"net_cumulative_return": 1.0}}}


class _PasFailClient:
    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 503, {"detail": "down"}


class _PaFailClient:
    async def get_pas_input_twr(self, portfolio_id: str, as_of_date: str, periods: list[str]):
        return 503, {"detail": "down"}


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ({"valuation": {"market_value_base": 10}}, 10.0),
        ({"valuation": {"market_value": 11}}, 11.0),
        ({"valuation": {"current_value_base": 12}}, 12.0),
        ({"valuation": {"current_value": 13}}, 13.0),
        ({"market_value_base": 14}, 14.0),
        ({"market_value": 15}, 15.0),
        ({"current_value_base": 16}, 16.0),
        ({"current_value": 17}, 17.0),
        ({"valuation": {"market_value_base": "bad"}, "market_value": 18}, 18.0),
        ({}, None),
    ],
)
def test_parse_market_value_variants(position, expected):
    service = AggregationService(pas_client=_PasOkClient(), pa_client=_PaOkClient())
    assert service._parse_market_value(position) == expected


def test_build_asset_class_rows_sorts_and_ignores_non_positive_values():
    service = AggregationService(pas_client=_PasOkClient(), pa_client=_PaOkClient())
    payload = {
        "snapshot": {
            "holdings": {
                "holdingsByAssetClass": {
                    "BOND": [{"valuation": {"market_value_base": 25}}],
                    "EQUITY": [{"valuation": {"market_value_base": 75}}],
                    "CASH": [{"valuation": {"market_value_base": -5}}],
                }
            }
        }
    }
    rows = service._build_asset_class_rows(pas_payload=payload, total_mv=100.0)
    assert [row.bucket for row in rows] == ["BOND", "EQUITY"]
    row_map = {row.bucket: row.value for row in rows}
    assert row_map["BOND"] == 25.0
    assert row_map["EQUITY"] == 75.0


@pytest.mark.parametrize(
    ("payload", "total_mv"),
    [
        ({"snapshot": []}, 100.0),
        ({"snapshot": {"holdings": []}}, 100.0),
        ({"snapshot": {"holdings": {"holdingsByAssetClass": []}}}, 100.0),
        ({"snapshot": {"holdings": {"holdingsByAssetClass": {"EQ": "bad"}}}}, 100.0),
        ({"snapshot": {"holdings": {"holdingsByAssetClass": {"EQ": []}}}}, 0.0),
    ],
)
def test_build_asset_class_rows_handles_non_conforming_payloads(payload, total_mv):
    service = AggregationService(pas_client=_PasOkClient(), pa_client=_PaOkClient())
    assert service._build_asset_class_rows(pas_payload=payload, total_mv=total_mv) == []


@pytest.mark.asyncio
async def test_fetch_inputs_drops_upstream_payloads_when_services_fail():
    service = AggregationService(pas_client=_PasFailClient(), pa_client=_PaFailClient())
    pas_payload, pa_payload = await service._fetch_inputs("P1", "2026-02-24")
    assert pas_payload == {}
    assert pa_payload == {}


def test_get_portfolio_aggregation_non_live_returns_deterministic_rows():
    service = AggregationService(pas_client=_PasOkClient(), pa_client=_PaOkClient())
    response = service.get_portfolio_aggregation("P1", "2026-02-24")
    assert response.scope.portfolio_id == "P1"
    assert str(response.scope.as_of_date) == "2026-02-24"
    assert len(response.rows) == 4
    assert response.rows[0].bucket == "TOTAL"


def test_parse_market_value_returns_none_when_non_numeric_position_key():
    service = AggregationService(pas_client=_PasOkClient(), pa_client=_PaOkClient())
    assert service._parse_market_value({"market_value_base": "n/a"}) is None


def test_build_asset_class_rows_returns_empty_when_total_market_value_non_positive():
    service = AggregationService(pas_client=_PasOkClient(), pa_client=_PaOkClient())
    payload = {
        "snapshot": {
            "holdings": {
                "holdingsByAssetClass": {"EQUITY": [{"valuation": {"market_value_base": 30}}]}
            }
        }
    }
    assert service._build_asset_class_rows(pas_payload=payload, total_mv=-1.0) == []


def test_build_asset_class_rows_ignores_non_dict_positions():
    service = AggregationService(pas_client=_PasOkClient(), pa_client=_PaOkClient())
    payload = {
        "snapshot": {
            "holdings": {"holdingsByAssetClass": {"EQUITY": ["bad", {"market_value_base": 20}]}}
        }
    }
    rows = service._build_asset_class_rows(pas_payload=payload, total_mv=100.0)
    assert len(rows) == 1
    assert rows[0].bucket == "EQUITY"
    assert rows[0].value == 20.0


class _PasMalformedHoldings:
    def __init__(self, holdings):
        self._holdings = holdings

    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ):
        return 200, {
            "snapshot": {
                "overview": {"total_market_value": 250.0},
                "holdings": self._holdings,
            }
        }


@pytest.mark.parametrize(
    "holdings",
    [
        "bad-holdings",
        {"holdingsByAssetClass": "bad-map"},
        {"holdingsByAssetClass": {"EQUITY": "bad-list"}},
    ],
)
@pytest.mark.asyncio
async def test_live_aggregation_handles_malformed_holdings_shapes(holdings):
    service = AggregationService(
        pas_client=_PasMalformedHoldings(holdings),
        pa_client=_PaOkClient(),
    )
    response = await service.get_portfolio_aggregation_live("P1", "2026-02-24")
    metric_map = {row.metric: row.value for row in response.rows}
    assert metric_map["market_value_base"] == 250.0
    assert metric_map["position_count"] == 0.0
