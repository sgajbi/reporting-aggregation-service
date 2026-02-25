from decimal import Decimal

import pytest

from app.precision_policy import (
    ROUNDING_POLICY_VERSION,
    _decimal_scale,
    normalize_input,
    quantize_fx_rate,
    quantize_money,
    quantize_performance,
    quantize_price,
    quantize_quantity,
    quantize_risk,
    to_decimal,
)


def test_to_decimal_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        to_decimal("bad-number")


def test_to_decimal_handles_none_and_decimal_passthrough() -> None:
    assert to_decimal(None) == Decimal("0")
    original = Decimal("1.23")
    assert to_decimal(original) is original


def test_money_quantization_half_even() -> None:
    assert quantize_money("1.005") == Decimal("1.00")
    assert quantize_money("1.015") == Decimal("1.02")


def test_precision_scales() -> None:
    assert quantize_price("10.1234567") == Decimal("10.123457")
    assert quantize_fx_rate("1.234567895") == Decimal("1.23456790")
    assert quantize_quantity("100.1234567") == Decimal("100.123457")
    assert quantize_performance("0.123456789") == Decimal("0.123457")
    assert quantize_risk("0.22222229") == Decimal("0.222222")


def test_rounding_policy_version_exposed() -> None:
    assert ROUNDING_POLICY_VERSION == "1.1.0"


def test_normalize_input_rejects_over_scale() -> None:
    with pytest.raises(ValueError, match="money scale 9 exceeds max 8"):
        normalize_input("12.123456789", "money")


def test_intermediate_precision_preserved_before_final_quantize() -> None:
    value = normalize_input("0.123456789012", "performance")
    assert value == Decimal("0.123456789012")
    assert quantize_performance(value) == Decimal("0.123457")


def test_normalize_input_rejects_unsupported_semantic_type() -> None:
    with pytest.raises(ValueError, match="Unsupported semantic type: unknown"):
        normalize_input("1.23", "unknown")


def test_decimal_scale_handles_special_decimal_exponent() -> None:
    assert _decimal_scale(Decimal("NaN")) == 0
