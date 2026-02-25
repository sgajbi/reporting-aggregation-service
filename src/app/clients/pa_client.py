from typing import Any

import httpx

from app.clients.http_resilience import post_with_retry, response_payload
from app.observability import propagation_headers


class PaClient:
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

    async def get_pas_input_twr(
        self,
        portfolio_id: str,
        as_of_date: str,
        periods: list[str],
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/performance/twr/pas-input"
        payload = {
            "portfolioId": portfolio_id,
            "asOfDate": as_of_date,
            "periods": periods,
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

    def _parse_payload(self, response: httpx.Response) -> dict[str, Any]:
        return response_payload(response)
