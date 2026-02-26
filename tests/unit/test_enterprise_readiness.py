import json

import pytest
from fastapi import Request

from src.app.enterprise_readiness import (
    authorize_write_request,
    build_enterprise_audit_middleware,
    is_feature_enabled,
    redact_sensitive,
    validate_enterprise_runtime_config,
)


def test_feature_flags_resolution(monkeypatch):
    monkeypatch.setenv(
        "ENTERPRISE_FEATURE_FLAGS_JSON",
        json.dumps({"reports.export": {"tenant-r": {"ops": True, "*": False}}}),
    )
    assert is_feature_enabled("reports.export", "tenant-r", "ops") is True
    assert is_feature_enabled("reports.export", "tenant-r", "advisor") is False


def test_redaction_masks_sensitive_values():
    payload = {"token": "x", "nested": {"account_number": "1", "safe": "ok"}}
    redacted = redact_sensitive(payload)
    assert redacted["token"] == "***REDACTED***"
    assert redacted["nested"]["account_number"] == "***REDACTED***"
    assert redacted["nested"]["safe"] == "ok"


def test_authorize_write_request_enforces_required_headers_when_enabled(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ENFORCE_AUTHZ", "true")
    allowed, reason = authorize_write_request("POST", "/reports", {})
    assert allowed is False
    assert reason.startswith("missing_headers:")


def test_authorize_write_request_enforces_capability_rules(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ENFORCE_AUTHZ", "true")
    monkeypatch.setenv(
        "ENTERPRISE_CAPABILITY_RULES_JSON",
        json.dumps({"POST /reports": "reports.write"}),
    )
    headers = {
        "X-Actor-Id": "a1",
        "X-Tenant-Id": "t1",
        "X-Role": "ops",
        "X-Correlation-Id": "c1",
        "X-Service-Identity": "ras",
        "X-Capabilities": "reports.read",
    }
    denied, denied_reason = authorize_write_request("POST", "/reports/export", headers)
    assert denied is False
    assert denied_reason == "missing_capability:reports.write"

    headers["X-Capabilities"] = "reports.read,reports.write"
    allowed, allowed_reason = authorize_write_request("POST", "/reports/export", headers)
    assert allowed is True
    assert allowed_reason is None


def test_validate_enterprise_runtime_config_reports_rotation_issue(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_SECRET_ROTATION_DAYS", "120")
    issues = validate_enterprise_runtime_config()
    assert "secret_rotation_days_out_of_range" in issues


def test_invalid_json_and_invalid_int_env_defaults(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_FEATURE_FLAGS_JSON", "{bad")
    monkeypatch.setenv("ENTERPRISE_SECRET_ROTATION_DAYS", "not-a-number")
    assert is_feature_enabled("reports.export", "tenant-r", "ops") is False
    issues = validate_enterprise_runtime_config()
    assert "secret_rotation_days_out_of_range" not in issues


def test_validate_runtime_config_flags_missing_policy_and_key(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_POLICY_VERSION", " ")
    monkeypatch.setenv("ENTERPRISE_ENFORCE_AUTHZ", "true")
    monkeypatch.delenv("ENTERPRISE_PRIMARY_KEY_ID", raising=False)
    issues = validate_enterprise_runtime_config()
    assert "missing_policy_version" in issues
    assert "missing_primary_key_id" in issues


@pytest.mark.asyncio
async def test_middleware_blocks_oversized_payload(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ENFORCE_AUTHZ", "false")
    monkeypatch.setenv("ENTERPRISE_MAX_WRITE_PAYLOAD_BYTES", "1")
    middleware = build_enterprise_audit_middleware()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/reports",
        "headers": [(b"content-length", b"2")],
    }
    request = Request(scope)
    response = await middleware(request, lambda req: None)  # pragma: no cover
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_middleware_denies_missing_service_identity(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ENFORCE_AUTHZ", "true")
    middleware = build_enterprise_audit_middleware()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/reports",
        "headers": [
            (b"x-actor-id", b"a1"),
            (b"x-tenant-id", b"t1"),
            (b"x-role", b"ops"),
            (b"x-correlation-id", b"c1"),
            (b"x-capabilities", b"reports.write"),
        ],
    }
    request = Request(scope)
    response = await middleware(request, lambda req: None)  # pragma: no cover
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_middleware_accepts_invalid_content_length_and_sets_policy_header(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ENFORCE_AUTHZ", "false")
    monkeypatch.setenv("ENTERPRISE_POLICY_VERSION", "2.0.0")
    middleware = build_enterprise_audit_middleware()

    async def _call_next(_request):
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True}, status_code=200)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/reports",
        "headers": [(b"content-length", b"abc")],
    }
    request = Request(scope)
    response = await middleware(request, _call_next)
    assert response.status_code == 200
    assert response.headers["X-Enterprise-Policy-Version"] == "2.0.0"


def test_validate_runtime_config_raises_when_enforced(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_POLICY_VERSION", " ")
    monkeypatch.setenv("ENTERPRISE_ENFORCE_RUNTIME_CONFIG", "true")
    with pytest.raises(RuntimeError, match="enterprise_runtime_config_invalid"):
        validate_enterprise_runtime_config()


def test_authorize_write_request_allows_when_rule_not_matching_path(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ENFORCE_AUTHZ", "true")
    monkeypatch.setenv(
        "ENTERPRISE_CAPABILITY_RULES_JSON", json.dumps({"POST /other": "reports.write"})
    )
    headers = {
        "X-Actor-Id": "a1",
        "X-Tenant-Id": "t1",
        "X-Role": "ops",
        "X-Correlation-Id": "c1",
        "X-Service-Identity": "lotus-report",
    }
    allowed, reason = authorize_write_request("POST", "/reports/export", headers)
    assert allowed is True
    assert reason is None


def test_redaction_handles_list_payloads():
    redacted = redact_sensitive([{"token": "x"}, {"safe": "ok"}])
    assert redacted[0]["token"] == "***REDACTED***"
    assert redacted[1]["safe"] == "ok"
