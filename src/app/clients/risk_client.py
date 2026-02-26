from typing import Any

from app.clients.http_resilience import post_with_retry
from app.observability import propagation_headers


class RiskClient:
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

    async def calculate_risk(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/analytics/risk/calculate"
        headers = propagation_headers()
        return await post_with_retry(
            url=url,
            timeout_seconds=self._timeout_seconds,
            json_body=payload,
            headers=headers,
            max_retries=self._max_retries,
            backoff_seconds=self._retry_backoff_seconds,
        )
