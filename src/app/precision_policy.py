from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

ROUNDING_POLICY_VERSION = "1.1.0"
ROUNDING_MODE = ROUND_HALF_EVEN
PRICE_SCALE = Decimal("0.000001")
FX_RATE_SCALE = Decimal("0.00000001")
QUANTITY_SCALE = Decimal("0.000001")
MONEY_SCALE = Decimal("0.01")
PERFORMANCE_SCALE = Decimal("0.000001")
RISK_SCALE = Decimal("0.000001")
INPUT_MAX_SCALE = {
    "money": 8,
    "quantity": 12,
    "price": 12,
    "fx_rate": 12,
    "performance": 12,
    "risk": 12,
}


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc


def _decimal_scale(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return abs(exponent) if exponent < 0 else 0


def normalize_input(value: Any, semantic_type: str) -> Decimal:
    if semantic_type not in INPUT_MAX_SCALE:
        raise ValueError(f"Unsupported semantic type: {semantic_type}")
    decimal_value = to_decimal(value)
    max_scale = INPUT_MAX_SCALE[semantic_type]
    actual_scale = _decimal_scale(decimal_value)
    if actual_scale > max_scale:
        raise ValueError(
            f"{semantic_type} scale {actual_scale} exceeds max {max_scale}; value={value!r}"
        )
    return decimal_value


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
