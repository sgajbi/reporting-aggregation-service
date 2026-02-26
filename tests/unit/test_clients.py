import pytest

from app.clients.pa_client import PaClient
from app.clients.pas_client import PasClient
from app.clients.risk_client import RiskClient
from app.observability import correlation_id_var, request_id_var, trace_id_var


class _FakeResponse:
    def __init__(self, status_code: int, payload, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _RecordingAsyncClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, json: dict, headers: dict):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response


@pytest.mark.parametrize(
    ("payload", "text", "expected"),
    [
        ({"ok": True}, "", {"ok": True}),
        (["not", "dict"], "", {"detail": ["not", "dict"]}),
        (ValueError("bad json"), "raw-text", {"detail": "raw-text"}),
    ],
)
def test_pa_client_parse_payload(payload, text, expected):
    client = PaClient(base_url="http://pa", timeout_seconds=2.0)
    response = _FakeResponse(status_code=200, payload=payload, text=text)
    assert client._parse_payload(response) == expected


@pytest.mark.asyncio
async def test_pa_client_get_pas_input_twr_posts_expected_contract(monkeypatch):
    correlation_id_var.set("corr-1")
    request_id_var.set("req-1")
    trace_id_var.set("0123456789abcdef0123456789abcdef")

    response = _FakeResponse(status_code=200, payload={"resultsByPeriod": {}})
    recorder = _RecordingAsyncClient(response=response)
    monkeypatch.setattr(
        "app.clients.pa_client.httpx.AsyncClient",
        lambda timeout: recorder,
    )

    client = PaClient(base_url="http://pa/", timeout_seconds=3.0)
    status_code, payload = await client.get_pas_input_twr(
        portfolio_id="P1",
        as_of_date="2026-02-24",
        periods=["YTD"],
    )
    assert status_code == 200
    assert payload == {"resultsByPeriod": {}}
    assert recorder.calls[0]["url"] == "http://pa/performance/twr/pas-input"
    assert recorder.calls[0]["json"]["consumerSystem"] == "REPORTING"
    assert recorder.calls[0]["headers"]["X-Correlation-Id"] == "corr-1"


@pytest.mark.parametrize(
    ("payload", "text", "expected"),
    [
        ({"snapshot": {}}, "", {"snapshot": {}}),
        ("non-dict", "", {"detail": "non-dict"}),
        (ValueError("bad json"), "raw-payload", {"detail": "raw-payload"}),
    ],
)
def test_pas_client_parse_payload(payload, text, expected):
    client = PasClient(base_url="http://pas", timeout_seconds=2.0)
    response = _FakeResponse(status_code=200, payload=payload, text=text)
    assert client._parse_payload(response) == expected


def test_pas_client_headers_empty_without_correlation_id():
    client = PasClient(base_url="http://pas", timeout_seconds=2.0)
    assert client._headers(None) == {}


def test_pas_client_headers_with_correlation_id_uses_propagation_context():
    request_id_var.set("req-2")
    trace_id_var.set("abcdef0123456789abcdef0123456789")
    client = PasClient(base_url="http://pas", timeout_seconds=2.0)
    headers = client._headers("corr-2")
    assert headers["X-Correlation-Id"] == "corr-2"
    assert headers["X-Request-Id"] == "req-2"


@pytest.mark.asyncio
async def test_pas_client_get_core_snapshot_posts_expected_contract(monkeypatch):
    response = _FakeResponse(status_code=200, payload={"snapshot": {"overview": {}}})
    recorder = _RecordingAsyncClient(response=response)
    monkeypatch.setattr(
        "app.clients.pas_client.httpx.AsyncClient",
        lambda timeout: recorder,
    )
    client = PasClient(base_url="http://pas/", timeout_seconds=3.0)

    status_code, payload = await client.get_core_snapshot(
        portfolio_id="P2",
        as_of_date="2026-02-24",
        include_sections=["OVERVIEW"],
    )
    assert status_code == 200
    assert payload["snapshot"] == {"overview": {}}
    assert recorder.calls[0]["url"] == "http://pas/integration/portfolios/P2/core-snapshot"
    assert recorder.calls[0]["json"]["includeSections"] == ["OVERVIEW"]


@pytest.mark.asyncio
async def test_pas_client_get_portfolio_summary_posts_expected_contract(monkeypatch):
    request_id_var.set("req-3")
    trace_id_var.set("abcdef0123456789abcdef0123456789")
    response = _FakeResponse(status_code=200, payload={"scope": {"portfolio_id": "P3"}})
    recorder = _RecordingAsyncClient(response=response)
    monkeypatch.setattr(
        "app.clients.pas_client.httpx.AsyncClient",
        lambda timeout: recorder,
    )
    client = PasClient(base_url="http://pas/", timeout_seconds=3.0)
    body = {"as_of_date": "2026-02-24"}
    status_code, payload = await client.get_portfolio_summary(
        portfolio_id="P3",
        payload=body,
        correlation_id="corr-3",
    )
    assert status_code == 200
    assert payload["scope"]["portfolio_id"] == "P3"
    assert recorder.calls[0]["url"] == "http://pas/portfolios/P3/summary"
    assert recorder.calls[0]["headers"]["X-Correlation-Id"] == "corr-3"


@pytest.mark.asyncio
async def test_pas_client_get_portfolio_review_posts_expected_contract(monkeypatch):
    response = _FakeResponse(status_code=200, payload={"portfolio_id": "P4"})
    recorder = _RecordingAsyncClient(response=response)
    monkeypatch.setattr(
        "app.clients.pas_client.httpx.AsyncClient",
        lambda timeout: recorder,
    )
    client = PasClient(base_url="http://pas/", timeout_seconds=3.0)
    status_code, payload = await client.get_portfolio_review(
        portfolio_id="P4",
        payload={"as_of_date": "2026-02-24"},
        correlation_id=None,
    )
    assert status_code == 200
    assert payload["portfolio_id"] == "P4"
    assert recorder.calls[0]["url"] == "http://pas/portfolios/P4/review"
    assert recorder.calls[0]["headers"] == {}


@pytest.mark.asyncio
async def test_pa_client_calculate_twr_posts_expected_contract(monkeypatch):
    response = _FakeResponse(status_code=200, payload={"results_by_period": {}})
    recorder = _RecordingAsyncClient(response=response)
    monkeypatch.setattr("app.clients.pa_client.httpx.AsyncClient", lambda timeout: recorder)
    client = PaClient(base_url="http://pa/", timeout_seconds=3.0)

    status_code, payload = await client.calculate_twr({"portfolio_id": "P1"})
    assert status_code == 200
    assert payload == {"results_by_period": {}}
    assert recorder.calls[0]["url"] == "http://pa/performance/twr"


@pytest.mark.asyncio
async def test_pas_client_get_performance_input_posts_expected_contract(monkeypatch):
    response = _FakeResponse(status_code=200, payload={"valuationPoints": []})
    recorder = _RecordingAsyncClient(response=response)
    monkeypatch.setattr("app.clients.pas_client.httpx.AsyncClient", lambda timeout: recorder)
    client = PasClient(base_url="http://pas/", timeout_seconds=3.0)

    status_code, payload = await client.get_performance_input(
        portfolio_id="P2", as_of_date="2026-02-24", lookback_days=365
    )
    assert status_code == 200
    assert payload == {"valuationPoints": []}
    assert recorder.calls[0]["url"] == "http://pas/integration/portfolios/P2/performance-input"
    assert recorder.calls[0]["json"]["lookbackDays"] == 365


@pytest.mark.asyncio
async def test_risk_client_calculate_risk_posts_expected_contract(monkeypatch):
    async def _fake_post_with_retry(**kwargs):
        return 200, {"results": {}}, kwargs

    monkeypatch.setattr("app.clients.risk_client.post_with_retry", _fake_post_with_retry)
    client = RiskClient(base_url="http://risk/", timeout_seconds=3.0)

    status_code, payload, kwargs = await client.calculate_risk({"metrics": ["VAR"]})
    assert status_code == 200
    assert payload == {"results": {}}
    assert kwargs["url"] == "http://risk/analytics/risk/calculate"
