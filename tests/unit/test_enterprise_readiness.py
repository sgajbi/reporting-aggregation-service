import json

from src.app.enterprise_readiness import (
    authorize_write_request,
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
