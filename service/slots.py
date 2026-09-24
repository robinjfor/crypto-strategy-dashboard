"""Armed slots. NEAR intentionally not armed."""
from __future__ import annotations

MAX_NOTIONAL_USDT = 1500.0

# Allocation vs ~5000 USDT book · FET 25% · OP 20% · DOT 20% · SOL 30%
SLOTS: list[dict] = [
    {
        "id": "sat_fet_1h",
        "strategy_id": "donchian55_s2.0_t3.0__FET__1h",
        "family": "donchian_atr",
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
        "satellite_slot": "satellite_A",
        "role": "incumbent",
        "mode": "signal_only",
    },
    # Satellite A candidates (pending Emily) — monitored signal-only until approved
    {
        "id": "sat_fet_4h",
        "strategy_id": "donchian55_s2.0_t3.0__FET__4h",
        "family": "donchian_atr",
        "symbol": "FETUSDT",
        "tf": "4h",
        "donch_n": 55,
        "stop_atr_mult": 2.0,
        "trail_atr_mult": 3.0,
        "max_hold_bars": 96,
        "quote_usdt": 1250.0,
        "target_pct": 25.0,
        "variant": "donchian55_s2.0_t3.0",
        "require_reset_below_hi": True,
        "armed": True,
        "stop_reference": None,
        "btc_regime": False,
        "satellite_slot": "satellite_A",
        "role": "primary_candidate",
        "mode": "signal_only",
        "review": "PENDING_EMILY",
    },
    {
        "id": "sat_fet_4h_btc",
        "strategy_id": "donchian55_s2.0_t3.0_btcRegimeD__FET__4h",
        "family": "donchian_atr",
        "symbol": "FETUSDT",
        "tf": "4h",
        "donch_n": 55,
        "stop_atr_mult": 2.0,
        "trail_atr_mult": 3.0,
        "max_hold_bars": 96,
        "quote_usdt": 1250.0,
        "target_pct": 25.0,
        "variant": "donchian55_s2.0_t3.0_btcRegimeD",
        "require_reset_below_hi": True,
        "armed": True,
        "stop_reference": None,
        "btc_regime": True,
        "satellite_slot": "satellite_A",
        "role": "alternate_candidate",
        "mode": "signal_only",
        "review": "PENDING_EMILY",
    },
    {
        "id": "sat_op_4h",
        "strategy_id": "donchian20_s1.5_t1.5__OP__4h",
        "family": "donchian_atr",
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
        "family": "donchian_atr",
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
SUPPORTED_FAMILIES = frozenset({"donchian_atr", "donchian_btc_regime", "ema_cross_atr", "donchian_lev_vol", "donchian_long_short_btc_regime"})

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
        "satellite_slot": "satellite_A",
        "gate_fail_reasons": ["maxdd worse than -45% (3y unified scores)"],
    },
    "donchian55_s2.0_t3.0__FET__4h": {
        "slot": "sat_fet_4h",
        "notional_usdt": 1250.0,
        "seeded": True,
        "mode": "signal_only",
        "approved": False,
        "label_zh": "只算訊號（未核准）",
        "gate_pass": True,
        "satellite_slot": "satellite_A",
        "role": "primary_candidate",
        "review": "PENDING_EMILY",
    },
    "donchian55_s2.0_t3.0_btcRegimeD__FET__4h": {
        "slot": "sat_fet_4h_btc",
        "notional_usdt": 1250.0,
        "seeded": True,
        "mode": "signal_only",
        "approved": False,
        "label_zh": "只算訊號（未核准）",
        "gate_pass": True,
        "satellite_slot": "satellite_A",
        "role": "alternate_candidate",
        "review": "PENDING_EMILY",
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
    """Return live | signal_only | none.

    Family-level model: live only if strategy's family is in approved_families
    AND (when allocation is present) the matching slot is enabled.
    """
    if not strategy_id:
        return "none"
    slot = slot_by_strategy_id(strategy_id)
    family = (slot or {}).get("family") or ""
    if family == "donchian":
        family = "donchian_atr"
    # Prefer family approval list
    try:
        fams = ensure_approved_families(state or {})
    except Exception:
        fams = list(DEFAULT_APPROVED_FAMILIES)
    if family and family not in fams:
        # still monitored as signal_only if known
        if strategy_id in DEFAULT_SIGNAL_ONLY or strategy_id in DEFAULT_APPROVED or slot:
            return "signal_only"
        return "none"
    # Family approved — check per-strategy overrides / allocation enablement
    approved = (state or {}).get("approved") if state else None
    if isinstance(approved, dict) and strategy_id in approved:
        meta = approved[strategy_id] or {}
        mode = str(meta.get("mode") or "live")
        if mode == "signal_only" or meta.get("approved") is False:
            return "signal_only"
    # Allocation gate (optional): if allocation lists this strategy disabled → signal_only
    alloc = (state or {}).get("allocation") if state else None
    if isinstance(alloc, dict):
        for s in alloc.get("slots") or []:
            if s.get("strategy_id") == strategy_id:
                if not s.get("enabled"):
                    return "signal_only"
                return "live"
    if strategy_id in DEFAULT_APPROVED:
        return "live"
    if strategy_id in DEFAULT_SIGNAL_ONLY:
        return "signal_only"
    if slot and family in fams:
        return "live"
    return "none"


def is_approved_live(state: dict | None, strategy_id: str) -> bool:
    return approval_mode(state, strategy_id) == "live"


def strategies_in_satellite_slot(slot_name: str) -> list[dict]:
    out = []
    for s in SLOTS:
        if s.get("satellite_slot") == slot_name:
            out.append(s)
    return out


def apply_satellite_slot_approval(
    state: dict,
    strategy_id: str,
    *,
    notional: float,
    approved_at: str,
) -> dict:
    """Approve one strategy into its satellite_slot; demote siblings to signal_only.

    Mutually exclusive: at most one live strategy per satellite_slot.
    Returns a summary dict for API responses / unit tests.
    """
    slot = slot_by_strategy_id(strategy_id)
    if not slot:
        # Allow approving a scores-only id by deriving config from strategy_id
        raise KeyError(f"unknown strategy_id: {strategy_id}")
    sat = slot.get("satellite_slot")
    approved = state.setdefault("approved", {})
    monitored = state.setdefault("signal_only", {})
    demoted: list[str] = []
    if sat:
        for sib in strategies_in_satellite_slot(sat):
            sid = sib["strategy_id"]
            if sid == strategy_id:
                continue
            if sid in approved:
                approved.pop(sid, None)
            monitored[sid] = {
                **(DEFAULT_SIGNAL_ONLY.get(sid) or {}),
                "slot": sib["id"],
                "mode": "signal_only",
                "approved": False,
                "label_zh": LABEL_SIGNAL_ONLY,
                "satellite_slot": sat,
                "demoted_by": strategy_id,
                "updated_at": approved_at,
            }
            demoted.append(sid)
    # Promote chosen
    monitored.pop(strategy_id, None)
    approved[strategy_id] = {
        "slot": slot["id"],
        "notional_usdt": float(notional),
        "approved_at": approved_at,
        "approved": True,
        "mode": "live",
        "status": "live_standby",
        "family": slot.get("family"),
        "satellite_slot": sat,
        "label_zh": "已核准 · 上線待命",
    }
    state["approved"] = approved
    state["signal_only"] = monitored
    return {
        "approved_id": strategy_id,
        "slot": slot["id"],
        "satellite_slot": sat,
        "demoted": demoted,
        "notional_usdt": float(notional),
    }


# --- Family-level approval (Emily approves families; analyst picks symbols) ---
DEFAULT_APPROVED_FAMILIES = ["donchian_atr"]


def ensure_approved_families(state: dict) -> list[str]:
    """Migrate / seed approved_families. Returns normalized list."""
    fams = state.get("approved_families")
    if not isinstance(fams, list):
        # Migrate from per-strategy approved if present
        fams = []
        approved = state.get("approved")
        if isinstance(approved, dict) and approved:
            for sid, meta in approved.items():
                if not isinstance(meta, dict):
                    continue
                if meta.get("approved") is False:
                    continue
                if str(meta.get("mode") or "live") == "signal_only":
                    continue
                fam = (meta.get("family") or "").strip()
                if fam == "donchian":
                    fam = "donchian_atr"
                if fam == "donchian_btc_regime":
                    fam = "donchian_btc_regime"
                if not fam:
                    # infer from strategy id
                    if "btcRegime" in sid or "btc_regime" in sid:
                        fam = "donchian_btc_regime"
                    else:
                        fam = "donchian_atr"
                if fam and fam not in fams:
                    fams.append(fam)
        if not fams:
            fams = list(DEFAULT_APPROVED_FAMILIES)
        state["approved_families"] = fams
    # Always ensure donchian_atr stays if OP/DOT were live historically and list empty
    if not fams:
        fams = list(DEFAULT_APPROVED_FAMILIES)
        state["approved_families"] = fams
    # Normalize aliases
    norm = []
    for f in fams:
        if f == "donchian":
            f = "donchian_atr"
        if f == "donchian_btc_regime":
            f = "donchian_btc_regime"
        if f and f not in norm:
            norm.append(f)
    state["approved_families"] = norm
    return norm


def is_family_approved(state: dict | None, family: str) -> bool:
    fams = ensure_approved_families(state or {})
    f = family
    if f == "donchian":
        f = "donchian_atr"
    return f in fams


def approve_family(state: dict, family: str, *, at: str, by: str = "api") -> dict:
    fams = ensure_approved_families(state)
    f = "donchian_atr" if family == "donchian" else family
    if f == "donchian_btc_regime":
        f = "donchian_btc_regime"
    if f not in SUPPORTED_FAMILIES:
        raise ValueError(f"雲端尚未支援此策略類型：{f}")
    if f not in fams:
        fams.append(f)
    state["approved_families"] = fams
    state.setdefault("meta", {})["last_family_approve"] = {"family": f, "at": at, "by": by}
    return {"approved_families": fams, "family": f}


def revoke_family(state: dict, family: str, *, at: str, by: str = "api") -> dict:
    fams = ensure_approved_families(state)
    f = "donchian_atr" if family == "donchian" else family
    if f == "donchian_btc_regime":
        f = "donchian_btc_regime"
    fams = [x for x in fams if x != f]
    state["approved_families"] = fams
    state.setdefault("meta", {})["last_family_revoke"] = {"family": f, "at": at, "by": by}
    return {"approved_families": fams, "family": f}
