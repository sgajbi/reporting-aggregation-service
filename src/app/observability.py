import json
import logging
import os
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from prometheus_fastapi_instrumentator import Instrumentator

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": os.getenv("SERVICE_NAME", "lotus-report"),
            "environment": os.getenv("ENVIRONMENT", "local"),
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get() or None,
            "request_id": request_id_var.get() or None,
            "trace_id": trace_id_var.get() or None,
        }
        if hasattr(record, "extra_fields") and isinstance(record.extra_fields, dict):
            payload.update(record.extra_fields)
        return json.dumps({k: v for k, v in payload.items() if v is not None})


def setup_logging() -> None:
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
    root_logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root_logger.addHandler(handler)


def resolve_correlation_id(request: Request) -> str:
    incoming = request.headers.get("X-Correlation-Id") or request.headers.get("X-Correlation-ID")
    return incoming if incoming else f"corr_{uuid4().hex[:12]}"


def resolve_request_id(request: Request) -> str:
    incoming = request.headers.get("X-Request-Id")
    return incoming if incoming else f"req_{uuid4().hex[:12]}"


def resolve_trace_id(request: Request) -> str:
    traceparent = request.headers.get("traceparent")
    if isinstance(traceparent, str) and traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 4 and len(parts[1]) == 32:
            return parts[1]
    incoming = request.headers.get("X-Trace-Id")
    if isinstance(incoming, str) and incoming:
        return incoming
    return uuid4().hex


def propagation_headers(correlation_id: str | None = None) -> dict[str, str]:
    resolved_trace = trace_id_var.get() or uuid4().hex
    resolved_correlation_id = (
        correlation_id or correlation_id_var.get() or f"corr_{uuid4().hex[:12]}"
    )
    return {
        "X-Correlation-Id": resolved_correlation_id,
        "X-Request-Id": request_id_var.get() or f"req_{uuid4().hex[:12]}",
        "X-Trace-Id": resolved_trace,
        "traceparent": f"00-{resolved_trace}-0000000000000001-01",
    }


def setup_observability(app: FastAPI) -> None:
    setup_logging()
    Instrumentator().instrument(app).expose(app)

    @app.middleware("http")
    async def _request_observability_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        logger = logging.getLogger("http.access")
        started = time.perf_counter()

        correlation_id = resolve_correlation_id(request)
        request_id = resolve_request_id(request)
        trace_id = resolve_trace_id(request)

        corr_token = correlation_id_var.set(correlation_id)
        req_token = request_id_var.set(request_id)
        trace_token = trace_id_var.set(trace_id)
        try:
            response = await call_next(request)
        finally:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "request.completed",
                extra={
                    "extra_fields": {
                        "http_method": request.method,
                        "endpoint": request.url.path,
                        "latency_ms": latency_ms,
                    }
                },
            )
            correlation_id_var.reset(corr_token)
            request_id_var.reset(req_token)
            trace_id_var.reset(trace_token)

        response.headers["X-Correlation-Id"] = correlation_id
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        response.headers["traceparent"] = f"00-{trace_id}-0000000000000001-01"
        return response

