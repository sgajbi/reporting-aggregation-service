from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

ROUNDING_MODE = ROUND_HALF_EVEN
PRICE_SCALE = Decimal("0.000001")
FX_RATE_SCALE = Decimal("0.00000001")
QUANTITY_SCALE = Decimal("0.000001")
MONEY_SCALE = Decimal("0.01")
PERFORMANCE_SCALE = Decimal("0.000001")
RISK_SCALE = Decimal("0.000001")


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc


def quantize_money(value: Any) -> Decimal:
    return to_decimal(value).quantize(MONEY_SCALE, rounding=ROUNDING_MODE)


def quantize_quantity(value: Any) -> Decimal:
    return to_decimal(value).quantize(QUANTITY_SCALE, rounding=ROUNDING_MODE)


def quantize_price(value: Any) -> Decimal:
    return to_decimal(value).quantize(PRICE_SCALE, rounding=ROUNDING_MODE)


def quantize_fx_rate(value: Any) -> Decimal:
    return to_decimal(value).quantize(FX_RATE_SCALE, rounding=ROUNDING_MODE)


def quantize_performance(value: Any) -> Decimal:
    return to_decimal(value).quantize(PERFORMANCE_SCALE, rounding=ROUNDING_MODE)


def quantize_risk(value: Any) -> Decimal:
    return to_decimal(value).quantize(RISK_SCALE, rounding=ROUNDING_MODE)
