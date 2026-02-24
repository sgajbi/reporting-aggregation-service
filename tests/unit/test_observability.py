import json
import logging

from fastapi import Request

from app.observability import (
    JsonFormatter,
    correlation_id_var,
    propagation_headers,
    request_id_var,
    resolve_correlation_id,
    resolve_request_id,
    resolve_trace_id,
    trace_id_var,
)


def _request_with_headers(headers: dict[str, str]) -> Request:
    asgi_headers = [(k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in headers.items()]
    scope = {"type": "http", "headers": asgi_headers}
    return Request(scope)


def test_resolve_correlation_id_prefers_primary_header():
    request = _request_with_headers({"X-Correlation-Id": "corr-primary"})
    assert resolve_correlation_id(request) == "corr-primary"


def test_resolve_correlation_id_accepts_alias_header():
    request = _request_with_headers({"X-Correlation-ID": "corr-alias"})
    assert resolve_correlation_id(request) == "corr-alias"


def test_resolve_request_id_generates_when_missing():
    request = _request_with_headers({})
    value = resolve_request_id(request)
    assert value.startswith("req_")
    assert len(value) > 8


def test_resolve_trace_id_prefers_traceparent():
    request = _request_with_headers(
        {"traceparent": "00-0123456789abcdef0123456789abcdef-0000000000000001-01"}
    )
    assert resolve_trace_id(request) == "0123456789abcdef0123456789abcdef"


def test_resolve_trace_id_falls_back_to_x_trace_id():
    request = _request_with_headers({"X-Trace-Id": "trace-x"})
    assert resolve_trace_id(request) == "trace-x"


def test_propagation_headers_include_context_values():
    correlation_id_var.set("corr-ctx")
    request_id_var.set("req-ctx")
    trace_id_var.set("0123456789abcdef0123456789abcdef")
    headers = propagation_headers()
    assert headers["X-Correlation-Id"] == "corr-ctx"
    assert headers["X-Request-Id"] == "req-ctx"
    assert headers["X-Trace-Id"] == "0123456789abcdef0123456789abcdef"
    assert headers["traceparent"] == "00-0123456789abcdef0123456789abcdef-0000000000000001-01"


def test_json_formatter_emits_structured_payload_with_extra_fields(monkeypatch):
    monkeypatch.setenv("SERVICE_NAME", "ras-test")
    monkeypatch.setenv("ENVIRONMENT", "test")
    correlation_id_var.set("corr-log")
    request_id_var.set("req-log")
    trace_id_var.set("trace-log")
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="unit.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="log-message",
        args=(),
        exc_info=None,
    )
    record.extra_fields = {"endpoint": "/health", "latency_ms": 12.5}
    payload = json.loads(formatter.format(record))
    assert payload["service"] == "ras-test"
    assert payload["environment"] == "test"
    assert payload["correlation_id"] == "corr-log"
    assert payload["message"] == "log-message"
    assert payload["endpoint"] == "/health"
    assert payload["latency_ms"] == 12.5
