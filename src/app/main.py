from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.observability import setup_observability
from app.routers.aggregations import router as aggregations_router
from app.routers.health import router as health_router
from app.routers.integration import router as integration_router
from app.routers.reports import router as reports_router


@asynccontextmanager
async def _app_lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.is_draining = False
    yield
    application.state.is_draining = True


app = FastAPI(
    title="Reporting and Aggregation Service",
    version="0.1.0",
    description=(
        "Generates reporting-ready aggregated views from PAS core data and PA analytics outputs."
    ),
    openapi_tags=[
        {"name": "Health", "description": "Service health and readiness endpoints."},
        {"name": "Integration", "description": "Cross-service integration contracts."},
        {"name": "Aggregations", "description": "Aggregated portfolio and analytics read models."},
        {"name": "Reports", "description": "Report-generation APIs and report metadata."},
    ],
    lifespan=_app_lifespan,
)
setup_observability(app)

app.include_router(health_router)
app.include_router(integration_router)
app.include_router(aggregations_router)
app.include_router(reports_router)
