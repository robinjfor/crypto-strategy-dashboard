"""Armed slots. NEAR intentionally not armed."""
from __future__ import annotations

MAX_NOTIONAL_USDT = 1500.0

# Allocation vs ~5000 USDT book (ALLOCATION_5000.json / Demo sizing)
# FET 25% · OP 20% · DOT 20% · SOL 30% · cash buffer ~5%
SLOTS: list[dict] = [
    {
        "id": "sat_fet_1h",
        "symbol": "FETUSDT",
        "tf": "1h",
        "donch_n": 55,
        "stop_atr_mult": 2.0,
        "trail_atr_mult": 3.0,
        "max_hold_bars": 192,
        "quote_usdt": 1250.0,
        "target_pct": 25.0,
        "variant": "donchian55_s2.0_t3.0",
        "require_reset_below_hi": False,
        "armed": True,
        "stop_reference": None,
    },
    {
        "id": "sat_op_4h",
        "symbol": "OPUSDT",
        "tf": "4h",
        "donch_n": 20,
        "stop_atr_mult": 1.5,
        "trail_atr_mult": 1.5,
        "max_hold_bars": 36,
        "quote_usdt": 1000.0,
        "target_pct": 20.0,
        "variant": "donchian20_s1.5_t1.5",
        "require_reset_below_hi": True,  # OP_REARM
        "armed": True,
        "stop_reference": None,
    },
    {
        "id": "sat_dot_4h",
        "symbol": "DOTUSDT",
        "tf": "4h",
        "donch_n": 20,
        "stop_atr_mult": 1.5,
        "trail_atr_mult": 1.5,
        "max_hold_bars": 36,
        "quote_usdt": 1000.0,
        "target_pct": 20.0,
        "variant": "donchian20_s1.5_t1.5",
        "require_reset_below_hi": False,
        "armed": True,
        "stop_reference": 1.073,
    },
]

SOL_SLOT = {
    "id": "core_sol",
    "symbol": "SOLUSDT",
    "tf": "1d",
    "donch_n": 20,
    "quote_usdt": 1500.0,
    "target_pct": 30.0,
    "variant": "donchian20_atr_btcRegime",
    "require_reset_below_hi": True,
    "armed": True,
}
