"""Allocation validation for newly supported families."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation import RUNNER_FAMILIES, validate_allocation, validate_params


def _base_slot(**over):
    s = {
        "slot": "t1",
        "family": "ema_cross_atr",
        "strategy_id": "ema12_26_atr_btcRegime__SOL__1d",
        "symbol": "SOLUSDT",
        "timeframe": "1d",
        "notional_usdt": 1000,
        "enabled": False,
        "params": {
            "fast": 12,
            "slow": 26,
            "stop_m": 2.0,
            "trail_m": 3.0,
            "atr_mode": "sma",
            "btc_regime": True,
        },
    }
    s.update(over)
    return s


def test_ema_in_runner():
    assert "ema_cross_atr" in RUNNER_FAMILIES
    assert "donchian_fear_greed" not in RUNNER_FAMILIES
    assert "donchian_lev_vol" in RUNNER_FAMILIES
    assert "donchian_long_short_btc_regime" in RUNNER_FAMILIES


def test_ema_params_ok():
    errs: list[str] = []
    validate_params("ema_cross_atr", _base_slot()["params"], errs, 0)
    assert errs == []


def test_fg_lev_rejected():
    errs: list[str] = []
    validate_params(
        "donchian_fear_greed",
        {
            "donch_n": 20,
            "stop_m": 1.5,
            "trail_m": 1.5,
            "fg_mode": "gt25",
            "leverage": 1.5,
        },
        errs,
        0,
    )
    assert any("leverage" in e for e in errs)


def test_allocation_accepts_ema_candidate():
    doc = {
        "book_usdt": 5000,
        "max_notional_per_order_usdt": 1500,
        "slots": [_base_slot()],
    }
    errs = validate_allocation(doc, check_binance=False)
    assert errs == []


def test_allocation_accepts_lev_vol():
    doc = {
        "book_usdt": 5000,
        "max_notional_per_order_usdt": 1500,
        "slots": [
            _base_slot(
                family="donchian_lev_vol",
                strategy_id="lev_vol_x",
                params={
                    "donch_n": 20,
                    "stop_m": 1.5,
                    "trail_m": 1.5,
                    "leverage": 1.5,
                    "atr_mode": "wilder",
                },
            )
        ],
    }
    errs = validate_allocation(doc, check_binance=False)
    assert errs == [], errs


def test_allocation_rejects_donchian_lev_no_pass_family():
    doc = {
        "book_usdt": 5000,
        "max_notional_per_order_usdt": 1500,
        "slots": [
            _base_slot(
                family="donchian_lev",
                strategy_id="lev_x",
                params={"donch_n": 20, "stop_m": 1.5, "trail_m": 1.5, "leverage": 1.5},
            )
        ],
    }
    errs = validate_allocation(doc, check_binance=False)
    assert errs, "donchian_lev must stay blocked (no gate passers)"
