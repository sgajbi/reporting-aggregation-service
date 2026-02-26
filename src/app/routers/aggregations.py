from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.models.contracts import PortfolioAggregationResponse
from app.services.aggregation_service import AggregationService

router = APIRouter(prefix="/aggregations", tags=["Aggregations"])


@router.get(
    "/portfolios/{portfolio_id}",
    response_model=PortfolioAggregationResponse,
    summary="Get portfolio aggregation",
    description=(
        "Returns reporting-ready aggregated rows for a portfolio by as-of date. "
        "Current slice uses deterministic placeholder rows while "
        "lotus-core/lotus-performance connectors are integrated."
    ),
)
async def get_portfolio_aggregation(
    portfolio_id: Annotated[str, Path(description="Canonical portfolio identifier.")],
    as_of_date: Annotated[
        str, Query(alias="asOfDate", description="Business as-of date (YYYY-MM-DD).")
    ],
    live: Annotated[
        bool,
        Query(
            description=(
                "If true, fetches lotus-core and "
                "lotus-performance upstream contracts "
                "before aggregation."
            ),
            examples=[True],
        ),
    ] = True,
) -> PortfolioAggregationResponse:
    service = AggregationService()
    if live:
        return await service.get_portfolio_aggregation_live(
            portfolio_id=portfolio_id, as_of_date=as_of_date
        )
    return service.get_portfolio_aggregation(portfolio_id=portfolio_id, as_of_date=as_of_date)
