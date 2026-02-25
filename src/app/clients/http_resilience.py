import asyncio
from typing import Any

import httpx


def response_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text}
    if isinstance(payload, dict):
        return payload
    return {"detail": payload}


async def post_with_retry(
    *,
    url: str,
    timeout_seconds: float,
    json_body: dict[str, Any],
    headers: dict[str, str],
    max_retries: int = 2,
    backoff_seconds: float = 0.2,
) -> tuple[int, dict[str, Any]]:
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(url, json=json_body, headers=headers)
            return response.status_code, response_payload(response)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt >= max_retries:
                return 503, {"detail": f"upstream communication failure: {exc.__class__.__name__}"}
            await asyncio.sleep(backoff_seconds * (2**attempt))
    return 503, {"detail": "upstream communication failure: exhausted retries"}
