from fastapi import HTTPException, status

from app.clients.pa_client import PaClient
from app.clients.pas_client import PasClient
from app.clients.risk_client import RiskClient
from app.config import settings


class ReportingReadService:
    def __init__(
        self,
        pas_client: PasClient | None = None,
        pa_client: PaClient | None = None,
        risk_client: RiskClient | None = None,
    ):
        self._pas_client = pas_client or PasClient(
            base_url=settings.pas_base_url,
            timeout_seconds=settings.upstream_timeout_seconds,
            max_retries=settings.upstream_max_retries,
            retry_backoff_seconds=settings.upstream_retry_backoff_seconds,
        )
        self._pa_client = pa_client or PaClient(
            base_url=settings.pa_base_url,
            timeout_seconds=settings.upstream_timeout_seconds,
            max_retries=settings.upstream_max_retries,
            retry_backoff_seconds=settings.upstream_retry_backoff_seconds,
        )
        self._risk_client = risk_client or RiskClient(
            base_url=settings.risk_base_url,
            timeout_seconds=settings.upstream_timeout_seconds,
            max_retries=settings.upstream_max_retries,
            retry_backoff_seconds=settings.upstream_retry_backoff_seconds,
        )

    async def get_portfolio_summary(
        self,
        portfolio_id: str,
        request_payload: dict[str, object],
        correlation_id: str | None,
    ) -> dict[str, object]:
        as_of_date = self._required_string(request_payload, "as_of_date", "asOfDate")
        requested_sections = self._requested_sections(
            request_payload=request_payload,
            default_sections=["WEALTH", "ALLOCATION", "PNL", "INCOME", "ACTIVITY"],
        )

        status_code, payload = await self._pas_client.get_core_snapshot(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            include_sections=["OVERVIEW", "ALLOCATION", "INCOME_AND_ACTIVITY"],
        )
        snapshot = self._unwrap_pas_snapshot(status_code=status_code, payload=payload)

        overview = self._as_dict(snapshot.get("overview"))
        allocation = self._as_dict(snapshot.get("allocation"))
        income_activity = self._as_dict(snapshot.get("incomeAndActivity"))

        ytd_start = f"{as_of_date[:4]}-01-01"
        response: dict[str, object] = {
            "scope": {
                "portfolio_id": portfolio_id,
                "as_of_date": as_of_date,
                "period_start_date": ytd_start,
                "period_end_date": as_of_date,
            }
        }
        if "WEALTH" in requested_sections:
            response["wealth"] = {
                "total_market_value": self._to_float(overview.get("total_market_value")),
                "total_cash": self._to_float(overview.get("total_cash")),
            }
        if "PNL" in requested_sections and "pnl_summary" in overview:
            response["pnlSummary"] = overview.get("pnl_summary")
        if "INCOME" in requested_sections:
            response["incomeSummary"] = income_activity.get("income_summary_ytd")
        if "ACTIVITY" in requested_sections:
            response["activitySummary"] = income_activity.get("activity_summary_ytd")
        if "ALLOCATION" in requested_sections:
            response["allocation"] = allocation if allocation else None
        return response

    async def get_portfolio_review(
        self,
        portfolio_id: str,
        request_payload: dict[str, object],
        correlation_id: str | None,
    ) -> dict[str, object]:
        as_of_date = self._required_string(request_payload, "as_of_date", "asOfDate")
        requested_sections = self._requested_sections(
            request_payload=request_payload,
            default_sections=[
                "OVERVIEW",
                "ALLOCATION",
                "PERFORMANCE",
                "RISK_ANALYTICS",
                "INCOME_AND_ACTIVITY",
                "HOLDINGS",
                "TRANSACTIONS",
            ],
        )

        status_code, payload = await self._pas_client.get_core_snapshot(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            include_sections=[
                "OVERVIEW",
                "ALLOCATION",
                "INCOME_AND_ACTIVITY",
                "HOLDINGS",
                "TRANSACTIONS",
            ],
        )
        snapshot = self._unwrap_pas_snapshot(status_code=status_code, payload=payload)
        response: dict[str, object] = {"portfolio_id": portfolio_id, "as_of_date": as_of_date}

        if "OVERVIEW" in requested_sections:
            response["overview"] = snapshot.get("overview")
        if "ALLOCATION" in requested_sections:
            response["allocation"] = snapshot.get("allocation")
        if "INCOME_AND_ACTIVITY" in requested_sections:
            response["incomeAndActivity"] = snapshot.get("incomeAndActivity")
        if "HOLDINGS" in requested_sections:
            response["holdings"] = snapshot.get("holdings")
        if "TRANSACTIONS" in requested_sections:
            response["transactions"] = snapshot.get("transactions")

        if "PERFORMANCE" in requested_sections:
            pa_status, pa_payload = await self._pa_client.get_pas_input_twr(
                portfolio_id=portfolio_id,
                as_of_date=as_of_date,
                periods=["MTD", "QTD", "YTD", "THREE_YEAR", "SI"],
            )
            if pa_status < status.HTTP_400_BAD_REQUEST:
                response["performance"] = self._map_pa_performance(pa_payload)
            else:
                response["performance"] = None

        if "RISK_ANALYTICS" in requested_sections:
            response["riskAnalytics"] = await self._build_risk_analytics(
                portfolio_id=portfolio_id,
                as_of_date=as_of_date,
            )

        return response

    async def _build_risk_analytics(
        self,
        portfolio_id: str,
        as_of_date: str,
    ) -> dict[str, object] | None:
        perf_status, perf_payload = await self._pas_client.get_performance_input(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            lookback_days=1200,
        )
        if perf_status >= status.HTTP_400_BAD_REQUEST:
            return None

        valuation_points = perf_payload.get("valuationPoints")
        performance_start_date = perf_payload.get("performanceStartDate")
        if not isinstance(valuation_points, list) or not valuation_points:
            return None
        if not isinstance(performance_start_date, str):
            return None

        twr_payload = {
            "portfolio_id": portfolio_id,
            "performance_start_date": performance_start_date,
            "metric_basis": "NET",
            "report_start_date": performance_start_date,
            "report_end_date": as_of_date,
            "analyses": [{"period": "EXPLICIT", "frequencies": ["daily"]}],
            "valuation_points": valuation_points,
            "currency": perf_payload.get("baseCurrency", "USD"),
            "output": {"include_cumulative": True, "include_timeseries": True},
        }
        twr_status, twr_response = await self._pa_client.calculate_twr(twr_payload)
        if twr_status >= status.HTTP_400_BAD_REQUEST:
            return None

        returns = self._extract_daily_returns_from_twr(twr_response)
        if not returns:
            return None

        risk_payload = {
            "scope": {"asOfDate": as_of_date, "netOrGross": "NET"},
            "periods": [{"type": "YTD"}, {"type": "THREE_YEAR"}],
            "metrics": ["VOLATILITY", "SHARPE", "DRAWDOWN", "VAR"],
            "portfolioOpenDate": performance_start_date,
            "returns": returns,
            "benchmarkReturns": [],
        }
        risk_status, risk_response = await self._risk_client.calculate_risk(risk_payload)
        if risk_status >= status.HTTP_400_BAD_REQUEST:
            return None

        results = self._as_dict(risk_response.get("results"))
        return {"results": results}

    def _extract_daily_returns_from_twr(
        self,
        twr_payload: dict[str, object],
    ) -> list[dict[str, object]]:
        results_by_period = self._as_dict(twr_payload.get("results_by_period"))
        period_payload = next(iter(results_by_period.values()), None)
        if not isinstance(period_payload, dict):
            return []

        breakdowns = self._as_dict(period_payload.get("breakdowns"))
        daily_items = breakdowns.get("daily")
        if not isinstance(daily_items, list):
            return []

        returns: list[dict[str, object]] = []
        for item in daily_items:
            if not isinstance(item, dict):
                continue
            period = item.get("period")
            summary = self._as_dict(item.get("summary"))
            value = summary.get("period_return_pct")
            if not isinstance(period, str) or not isinstance(value, (int, float)):
                continue
            returns.append({"date": period[:10], "value": float(value)})
        return returns

    def _unwrap_pas_snapshot(
        self, status_code: int, payload: dict[str, object]
    ) -> dict[str, object]:
        if status_code < status.HTTP_400_BAD_REQUEST:
            snapshot = self._as_dict(payload.get("snapshot"))
            if snapshot:
                return snapshot
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="lotus-core core snapshot payload missing snapshot section.",
            )
        if status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=payload.get("detail"))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"lotus-core core snapshot upstream failure: {payload}",
        )

    def _map_pa_performance(self, payload: dict[str, object]) -> dict[str, object]:
        results_by_period = self._as_dict(payload.get("resultsByPeriod"))
        summary: dict[str, object] = {}
        for period, row in results_by_period.items():
            row_dict = self._as_dict(row)
            summary[period] = {
                "start_date": row_dict.get("start_date"),
                "end_date": row_dict.get("end_date"),
                "net_cumulative_return": row_dict.get("net_cumulative_return"),
                "net_annualized_return": row_dict.get("net_annualized_return"),
                "gross_cumulative_return": row_dict.get("gross_cumulative_return"),
                "gross_annualized_return": row_dict.get("gross_annualized_return"),
            }
        return {"summary": summary}

    def _requested_sections(
        self,
        request_payload: dict[str, object],
        default_sections: list[str],
    ) -> set[str]:
        raw_sections = request_payload.get("sections")
        if not isinstance(raw_sections, list):
            return set(default_sections)
        sections: set[str] = set()
        for item in raw_sections:
            if isinstance(item, str):
                sections.add(item.upper())
        return sections or set(default_sections)

    def _required_string(self, payload: dict[str, object], *keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing required request field: {keys[0]}",
        )

    @staticmethod
    def _as_dict(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return value
        return {}

    @staticmethod
    def _to_float(value: object) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0
