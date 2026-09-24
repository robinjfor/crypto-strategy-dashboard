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

# Live-approved (may place Demo orders). OP + DOT only per 資金控管 ruling.
DEFAULT_APPROVED: dict[str, dict] = {
    "donchian20_s1.5_t1.5__OP__4h": {
        "slot": "sat_op_4h",
        "notional_usdt": 1000.0,
        "seeded": True,
        "mode": "live",
        "approved": True,
        "label_zh": "已核准 · 上線待命",
    },
    "donchian20_s1.5_t1.5__DOT__4h": {
        "slot": "sat_dot_4h",
        "notional_usdt": 1000.0,
        "seeded": True,
        "mode": "live",
        "approved": True,
        "label_zh": "已核准 · 上線待命",
    },
}

# Monitored but NOT approved — signals only, never place orders.
DEFAULT_SIGNAL_ONLY: dict[str, dict] = {
    "donchian55_s2.0_t3.0__FET__1h": {
        "slot": "sat_fet_1h",
        "notional_usdt": 1250.0,
        "seeded": True,
        "mode": "signal_only",
        "approved": False,
        "label_zh": "只算訊號（未核准）",
        "gate_pass": False,
        "gate_fail_reasons": ["maxdd worse than -45% (3y unified scores)"],
    },
    "donchian20_atr_btcRegime__SOL__1d": {
        "slot": "core_sol",
        "notional_usdt": 1500.0,
        "seeded": True,
        "mode": "signal_only",
        "approved": False,
        "label_zh": "只算訊號（未核准）",
        "gate_pass": False,
        "gate_fail_reasons": ["ret_3y <= bh_ret_3y (3y unified scores)"],
    },
}

LABEL_SIGNAL_ONLY = "只算訊號（未核准）"


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
    """Return live | signal_only | none."""
    if not strategy_id:
        return "none"
    approved = (state or {}).get("approved") if state else None
    if not isinstance(approved, dict):
        approved = {}
    if strategy_id in approved:
        meta = approved[strategy_id] or {}
        mode = str(meta.get("mode") or "live")
        if mode == "signal_only" or meta.get("approved") is False:
            return "signal_only"
        return "live"
    # Defaults
    if strategy_id in DEFAULT_APPROVED:
        return "live"
    if strategy_id in DEFAULT_SIGNAL_ONLY:
        return "signal_only"
    return "none"


def is_approved_live(state: dict | None, strategy_id: str) -> bool:
    return approval_mode(state, strategy_id) == "live"
