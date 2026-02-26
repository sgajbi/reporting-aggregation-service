from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class AggregationScope(BaseModel):
    portfolio_id: str = Field(..., alias="portfolioId")
    as_of_date: date = Field(..., alias="asOfDate")

    model_config = {"populate_by_name": True}


class AggregationRow(BaseModel):
    bucket: str
    metric: str
    value: float


class PortfolioAggregationResponse(BaseModel):
    source_service: str = Field("lotus-report", alias="sourceService")
    scope: AggregationScope
    generated_at: datetime = Field(..., alias="generatedAt")
    rows: list[AggregationRow]

    model_config = {"populate_by_name": True}


class ReportRequest(BaseModel):
    portfolio_id: str = Field(..., alias="portfolioId")
    as_of_date: date = Field(..., alias="asOfDate")
    report_type: Literal["PORTFOLIO_SNAPSHOT", "PERFORMANCE_SUMMARY"] = Field(
        ..., alias="reportType"
    )
    output_format: Literal["JSON", "PDF"] = Field("JSON", alias="outputFormat")

    model_config = {"populate_by_name": True}


class ReportResponse(BaseModel):
    report_id: str = Field(..., alias="reportId")
    status: Literal["READY"] = "READY"
    report_type: str = Field(..., alias="reportType")
    output_format: str = Field(..., alias="outputFormat")
    generated_at: datetime = Field(..., alias="generatedAt")
    download_url: str | None = Field(default=None, alias="downloadUrl")

    model_config = {"populate_by_name": True}


class IntegrationCapabilitiesResponse(BaseModel):
    source_service: str = Field("lotus-report", alias="sourceService")
    contract_version: str = Field(..., alias="contractVersion")
    policy_version: str = Field("ras-default-v1", alias="policyVersion")
    features: list[dict[str, str | bool]]
    workflows: list[dict[str, str | bool]]
    supported_input_modes: list[str] = Field(alias="supportedInputModes")

    model_config = {"populate_by_name": True}

