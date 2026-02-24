from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path

from app.models.contracts import ReportRequest, ReportResponse
from app.services.report_service import ReportService
from app.services.reporting_read_service import ReportingReadService

router = APIRouter(prefix="/reports", tags=["Reports"])


def get_reporting_read_service() -> ReportingReadService:
    return ReportingReadService()


@router.post(
    "",
    response_model=ReportResponse,
    summary="Generate report",
    description=(
        "Generates a report metadata record from aggregated PAS+PA backed views. "
        "Current slice supports JSON metadata and PDF placeholder download URL."
    ),
)
def generate_report(request: ReportRequest) -> ReportResponse:
    return ReportService().generate_report(request)


@router.post(
    "/portfolios/{portfolio_id}/summary",
    response_model=dict[str, Any],
    summary="Get portfolio summary (RAS-owned)",
    description=(
        "RAS-owned reporting endpoint for consolidated portfolio summary. "
        "Phase-1 source is PAS upstream while ownership moves to RAS."
    ),
)
async def get_portfolio_summary(
    portfolio_id: Annotated[str, Path(description="Canonical portfolio identifier.")],
    request: dict[str, Any],
    service: ReportingReadService = Depends(get_reporting_read_service),
    correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> dict[str, Any]:
    return await service.get_portfolio_summary(
        portfolio_id=portfolio_id,
        request_payload=request,
        correlation_id=correlation_id,
    )


@router.post(
    "/portfolios/{portfolio_id}/review",
    response_model=dict[str, Any],
    summary="Get portfolio review report (RAS-owned)",
    description=(
        "RAS-owned reporting endpoint for portfolio review report payload. "
        "Phase-1 source is PAS upstream while ownership moves to RAS."
    ),
)
async def get_portfolio_review(
    portfolio_id: Annotated[str, Path(description="Canonical portfolio identifier.")],
    request: dict[str, Any],
    service: ReportingReadService = Depends(get_reporting_read_service),
    correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> dict[str, Any]:
    return await service.get_portfolio_review(
        portfolio_id=portfolio_id,
        request_payload=request,
        correlation_id=correlation_id,
    )
