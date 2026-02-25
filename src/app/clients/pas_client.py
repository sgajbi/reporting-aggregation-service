from typing import Any

import httpx

from app.clients.http_resilience import post_with_retry, response_payload
from app.observability import propagation_headers


class PasClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds

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
        headers = propagation_headers()
        return await post_with_retry(
            url=url,
            timeout_seconds=self._timeout_seconds,
            json_body=payload,
            headers=headers,
            max_retries=self._max_retries,
            backoff_seconds=self._retry_backoff_seconds,
        )

    async def get_portfolio_summary(
        self,
        portfolio_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/portfolios/{portfolio_id}/summary"
        headers = self._headers(correlation_id)
        return await post_with_retry(
            url=url,
            timeout_seconds=self._timeout_seconds,
            json_body=payload,
            headers=headers,
            max_retries=self._max_retries,
            backoff_seconds=self._retry_backoff_seconds,
        )

    async def get_portfolio_review(
        self,
        portfolio_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/portfolios/{portfolio_id}/review"
        headers = self._headers(correlation_id)
        return await post_with_retry(
            url=url,
            timeout_seconds=self._timeout_seconds,
            json_body=payload,
            headers=headers,
            max_retries=self._max_retries,
            backoff_seconds=self._retry_backoff_seconds,
        )

    def _headers(self, correlation_id: str | None) -> dict[str, str]:
        if not correlation_id:
            return {}
        return propagation_headers(correlation_id)

    def _parse_payload(self, response: httpx.Response) -> dict[str, Any]:
        return response_payload(response)
