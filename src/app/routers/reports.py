from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query

from app.models.contracts import ReportRequest, ReportResponse
from app.services.report_service import ReportService
from app.services.reporting_read_service import ReportingReadService

router = APIRouter(prefix="/reports", tags=["Reports"])


def get_reporting_read_service() -> ReportingReadService:
    return ReportingReadService()


def _apply_section_limit(payload: dict[str, Any], section_limit: int) -> dict[str, Any]:
    limited_payload = dict(payload)
    sections = limited_payload.get("sections")
    if isinstance(sections, list) and len(sections) > section_limit:
        limited_payload["sections"] = sections[:section_limit]
    return limited_payload


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
    section_limit: Annotated[
        int, Query(alias="sectionLimit", ge=1, le=20, description="pagination")
    ] = 10,
    service: ReportingReadService = Depends(get_reporting_read_service),
    correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> dict[str, Any]:
    return await service.get_portfolio_summary(
        portfolio_id=portfolio_id,
        request_payload=_apply_section_limit(request, section_limit),
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
    section_limit: Annotated[
        int, Query(alias="sectionLimit", ge=1, le=20, description="pagination")
    ] = 10,
    service: ReportingReadService = Depends(get_reporting_read_service),
    correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> dict[str, Any]:
    return await service.get_portfolio_review(
        portfolio_id=portfolio_id,
        request_payload=_apply_section_limit(request, section_limit),
        correlation_id=correlation_id,
    )
