"""allocation validate_params accepts atr_mode sma|wilder and require_reset false."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from allocation import slot_to_runtime, validate_params  # noqa: E402


def _base():
    return {"donch_n": 20, "stop_atr_mult": 2.0, "trail_atr_mult": 3.0}


def test_accept_sma_and_no_reset():
    errs = []
    validate_params("donchian_atr", {**_base(), "atr_mode": "sma", "require_reset_below_hi": False}, errs, 0)
    assert errs == []
    rt = slot_to_runtime(
        {
            "slot": "sat_arb_4h",
            "family": "donchian_atr",
            "strategy_id": "donchian_breakout_atr__ARB__4h",
            "symbol": "ARBUSDT",
            "timeframe": "4h",
            "notional_usdt": 1000,
            "enabled": False,
            "params": {**_base(), "atr_mode": "sma", "require_reset_below_hi": False, "max_hold_bars": 36},
        }
    )
    assert rt["atr_mode"] == "sma"
    assert rt["require_reset_below_hi"] is False
    assert rt["max_hold_bars"] == 36


def test_reject_bad_atr_mode():
    errs = []
    validate_params("donchian_atr", {**_base(), "atr_mode": "ema"}, errs, 0)
    assert any("atr_mode" in e for e in errs)


def test_default_wilder_when_omitted():
    rt = slot_to_runtime(
        {
            "slot": "x",
            "family": "donchian_atr",
            "symbol": "OPUSDT",
            "timeframe": "4h",
            "notional_usdt": 1000,
            "enabled": True,
            "params": _base(),
        }
    )
    assert rt["atr_mode"] == "wilder"
