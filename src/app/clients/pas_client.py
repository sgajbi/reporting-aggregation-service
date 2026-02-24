from typing import Any

import httpx


class PasClient:
    def __init__(self, base_url: str, timeout_seconds: float):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def get_core_snapshot(
        self,
        portfolio_id: str,
        as_of_date: str,
        include_sections: list[str],
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/integration/portfolios/{portfolio_id}/core-snapshot"
        payload = {
            "asOfDate": as_of_date,
            "includeSections": include_sections,
            "consumerSystem": "REPORTING",
        }
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(url, json=payload)
            return response.status_code, self._parse_payload(response)

    async def get_portfolio_summary(
        self,
        portfolio_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/portfolios/{portfolio_id}/summary"
        headers = self._headers(correlation_id)
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            return response.status_code, self._parse_payload(response)

    async def get_portfolio_review(
        self,
        portfolio_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/portfolios/{portfolio_id}/review"
        headers = self._headers(correlation_id)
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            return response.status_code, self._parse_payload(response)

    def _headers(self, correlation_id: str | None) -> dict[str, str]:
        if not correlation_id:
            return {}
        return {"X-Correlation-ID": correlation_id}

    def _parse_payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}
        if isinstance(payload, dict):
            return payload
        return {"detail": payload}
