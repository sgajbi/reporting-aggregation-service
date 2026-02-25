import json
from pathlib import Path

from app.precision_policy import (
    ROUNDING_POLICY_VERSION,
    quantize_fx_rate,
    quantize_money,
    quantize_performance,
    quantize_price,
    quantize_quantity,
    quantize_risk,
)


def test_rounding_golden_vectors() -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "rounding-golden-vectors.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert ROUNDING_POLICY_VERSION == payload["policy_version"]

    assert [str(quantize_money(v)) for v in payload["vectors"]["money"]] == payload["expected"]["money"]
    assert [str(quantize_price(v)) for v in payload["vectors"]["price"]] == payload["expected"]["price"]
    assert [str(quantize_fx_rate(v)) for v in payload["vectors"]["fx_rate"]] == payload["expected"]["fx_rate"]
    assert [str(quantize_quantity(v)) for v in payload["vectors"]["quantity"]] == payload["expected"]["quantity"]
    assert [str(quantize_performance(v)) for v in payload["vectors"]["performance"]] == payload["expected"]["performance"]
    assert [str(quantize_risk(v)) for v in payload["vectors"]["risk"]] == payload["expected"]["risk"]
