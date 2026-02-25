from decimal import Decimal

import pytest

from app.precision_policy import (
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


def test_money_quantization_half_even() -> None:
    assert quantize_money("1.005") == Decimal("1.00")
    assert quantize_money("1.015") == Decimal("1.02")


def test_precision_scales() -> None:
    assert quantize_price("10.1234567") == Decimal("10.123457")
    assert quantize_fx_rate("1.234567895") == Decimal("1.23456790")
    assert quantize_quantity("100.1234567") == Decimal("100.123457")
    assert quantize_performance("0.123456789") == Decimal("0.123457")
    assert quantize_risk("0.22222229") == Decimal("0.222222")
