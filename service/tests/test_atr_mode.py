"""Unit: atr_mode sma vs wilder on ARB 4h Vision klines."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance_client import BinanceClient  # noqa: E402
from indicators import add_donch_atr, resolve_atr, sma_atr, wilder_atr  # noqa: E402


@pytest.fixture(scope="module")
def arb_4h():
    client = BinanceClient()
    df = client.fetch_klines("ARBUSDT", "4h", limit=200, use_vision=True)
    assert len(df) >= 50, f"need enough bars, got {len(df)}"
    return df


def test_sma_vs_wilder_differ_on_arb(arb_4h):
    sma = resolve_atr(arb_4h, 14, "sma")
    wild = resolve_atr(arb_4h, 14, "wilder")
    # last finite values should exist and differ (SMA ≠ Wilder in general)
    s = float(sma.dropna().iloc[-1])
    w = float(wild.dropna().iloc[-1])
    assert s > 0 and w > 0
    assert s != pytest.approx(w, rel=1e-9), f"expected sma!=wilder, got {s} vs {w}"
    # SMA helper matches resolve_atr(..., sma)
    assert float(sma_atr(arb_4h, 14).dropna().iloc[-1]) == pytest.approx(s)


def test_add_donch_atr_mode_propagates(arb_4h):
    a = add_donch_atr(arb_4h, 20, atr_mode="sma").dropna(subset=["atr"])
    b = add_donch_atr(arb_4h, 20, atr_mode="wilder").dropna(subset=["atr"])
    assert a["atr_mode"].iloc[-1] == "sma"
    assert b["atr_mode"].iloc[-1] == "wilder"
    assert float(a["atr"].iloc[-1]) != pytest.approx(float(b["atr"].iloc[-1]), rel=1e-9)


def test_invalid_atr_mode_raises(arb_4h):
    with pytest.raises(ValueError, match="atr_mode"):
        resolve_atr(arb_4h, 14, "ema")


def test_default_is_wilder(arb_4h):
    default = resolve_atr(arb_4h, 14)
    wild = wilder_atr(arb_4h, 14)
    assert float(default.dropna().iloc[-1]) == pytest.approx(float(wild.dropna().iloc[-1]))
