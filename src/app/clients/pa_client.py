from typing import Any

import httpx


class PaClient:
    def __init__(self, base_url: str, timeout_seconds: float):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

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
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(url, json=payload)
            return response.status_code, self._parse_payload(response)

    def _parse_payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}
        if isinstance(payload, dict):
            return payload
        return {"detail": payload}
