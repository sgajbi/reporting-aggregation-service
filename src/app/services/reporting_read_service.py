from typing import Any

from fastapi import HTTPException, status

from app.clients.pas_client import PasClient
from app.config import settings


class ReportingReadService:
    def __init__(self, pas_client: PasClient | None = None):
        self._pas_client = pas_client or PasClient(
            base_url=settings.pas_base_url,
            timeout_seconds=settings.upstream_timeout_seconds,
        )

    async def get_portfolio_summary(
        self,
        portfolio_id: str,
        request_payload: dict[str, Any],
        correlation_id: str | None,
    ) -> dict[str, Any]:
        status_code, payload = await self._pas_client.get_portfolio_summary(
            portfolio_id=portfolio_id,
            payload=request_payload,
            correlation_id=correlation_id,
        )
        return self._map_upstream_response(status_code=status_code, payload=payload)

    async def get_portfolio_review(
        self,
        portfolio_id: str,
        request_payload: dict[str, Any],
        correlation_id: str | None,
    ) -> dict[str, Any]:
        status_code, payload = await self._pas_client.get_portfolio_review(
            portfolio_id=portfolio_id,
            payload=request_payload,
            correlation_id=correlation_id,
        )
        return self._map_upstream_response(status_code=status_code, payload=payload)

    def _map_upstream_response(self, status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
        if status_code < status.HTTP_400_BAD_REQUEST:
            return payload
        if status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=payload.get("detail"))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"PAS reporting upstream failure: {payload}",
        )
