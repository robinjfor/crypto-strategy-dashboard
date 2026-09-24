"""Armed slots. NEAR intentionally not armed."""
from __future__ import annotations

MAX_NOTIONAL_USDT = 1500.0

# Allocation vs ~5000 USDT book · FET 25% · OP 20% · DOT 20% · SOL 30%
SLOTS: list[dict] = [
    {
        "id": "sat_fet_1h",
        "strategy_id": "donchian55_s2.0_t3.0__FET__1h",
        "family": "donchian",
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
        "strategy_id": "donchian20_s1.5_t1.5__OP__4h",
        "family": "donchian",
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
        "strategy_id": "donchian20_s1.5_t1.5__DOT__4h",
        "family": "donchian",
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
    "strategy_id": "donchian20_atr_btcRegime__SOL__1d",
    "family": "donchian_btc_regime",
    "symbol": "SOLUSDT",
    "tf": "1d",
    "donch_n": 20,
    "quote_usdt": 1500.0,
    "target_pct": 30.0,
    "variant": "donchian20_atr_btcRegime",
    "require_reset_below_hi": True,
    "armed": True,
    "mode": "signal_only",
}

# Families the Cloud Run job can actually execute today
SUPPORTED_FAMILIES = frozenset({"donchian", "donchian_btc_regime"})

DEFAULT_APPROVED: dict[str, dict] = {
    "donchian55_s2.0_t3.0__FET__1h": {
        "slot": "sat_fet_1h",
        "notional_usdt": 1250.0,
        "seeded": True,
        "mode": "live",
        "gate_pass": False,
        "gate_fail_reasons": ["maxdd worse than -45% (3y unified scores)"],
        "label_zh": "已核准（門檻未過 · 現役維持）",
    },
    "donchian20_s1.5_t1.5__OP__4h": {
        "slot": "sat_op_4h",
        "notional_usdt": 1000.0,
        "seeded": True,
    },
    "donchian20_s1.5_t1.5__DOT__4h": {
        "slot": "sat_dot_4h",
        "notional_usdt": 1000.0,
        "seeded": True,
    },
    "donchian20_atr_btcRegime__SOL__1d": {
        "slot": "core_sol",
        "notional_usdt": 1500.0,
        "seeded": True,
        # 資金控管：SOL 僅訊號監看，禁止 Demo 實單，直到另行核准
        "mode": "signal_only",
        "label_zh": "訊號監看（未核准下單）",
        "gate_pass": False,
        "gate_fail_reasons": ["ret_3y <= bh_ret_3y (3y unified scores)"],
    },
}


def slot_by_strategy_id(strategy_id: str) -> dict | None:
    for s in SLOTS:
        if s.get("strategy_id") == strategy_id:
            return s
    if SOL_SLOT.get("strategy_id") == strategy_id:
        return SOL_SLOT
    return None


def strategy_id_for_slot(slot_id: str) -> str | None:
    for s in SLOTS:
        if s["id"] == slot_id:
            return s.get("strategy_id")
    if SOL_SLOT["id"] == slot_id:
        return SOL_SLOT.get("strategy_id")
    return None


def approval_mode(state: dict | None, strategy_id: str) -> str:
    """Return live | signal_only | none for a strategy_id."""
    approved = (state or {}).get("approved") if state else None
    if not isinstance(approved, dict) or not approved:
        approved = DEFAULT_APPROVED
    meta = approved.get(strategy_id) or {}
    if not meta:
        return "none"
    return str(meta.get("mode") or "live")
