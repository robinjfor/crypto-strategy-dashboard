"""CAGR70 portfolio rebalance + futures liquidation monitoring."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("trader.portfolio")

MAX_LEVERAGE = 3.0
MAX_BOOK_USDT = 5000.0
DEFAULT_UNIVERSE = ["SOL", "ETH", "AVAX", "LINK", "ARB", "FET", "DOT", "OP", "NEAR", "INJ"]


def clamp_book(book: float) -> float:
    return max(0.0, min(float(book), MAX_BOOK_USDT))


def clamp_lev(lev: float | int | None) -> float:
    return max(1.0, min(float(lev or 1.0), MAX_LEVERAGE))


def target_weights(
    mode: str,
    atr_pcts: dict[str, float] | None = None,
    symbols: list[str] | None = None,
) -> dict[str, float]:
    """Equal-weight or inverse-vol weights (sum=1)."""
    syms = list(symbols or (list(atr_pcts.keys()) if atr_pcts else DEFAULT_UNIVERSE))
    if not syms:
        return {}
    m = (mode or "ew").lower()
    if m in ("inv_vol", "invvol", "inverse_vol") and atr_pcts:
        inv = {}
        for s in syms:
            v = float(atr_pcts.get(s) or 0.0)
            inv[s] = 1.0 / v if v > 1e-9 else 0.0
        tot = sum(inv.values())
        if tot <= 0:
            return {s: 1.0 / len(syms) for s in syms}
        return {s: inv[s] / tot for s in syms}
    w = 1.0 / len(syms)
    return {s: w for s in syms}


def target_notionals(book_usdt: float, leverage: float, weights: dict[str, float]) -> dict[str, float]:
    """Per-symbol target notional. Leverage is exchange setting; book is hard notional cap."""
    book = clamp_book(book_usdt)
    _ = clamp_lev(leverage)  # validated/capped for callers
    out = {s: round(book * float(w), 4) for s, w in weights.items()}
    ssum = sum(out.values())
    if ssum > book and ssum > 0:
        scale = book / ssum
        out = {k: round(v * scale, 4) for k, v in out.items()}
    return out


def rebalance_orders(
    current: dict[str, dict],
    targets: dict[str, float],
    marks: dict[str, float],
    *,
    drift_pct: float = 0.15,
) -> list[dict]:
    """Emit adjust/enter/exit intents when drift exceeds threshold."""
    intents: list[dict] = []
    syms = sorted(set(current) | set(targets))
    for sym in syms:
        tgt = float(targets.get(sym) or 0.0)
        cur = current.get(sym) or {}
        side = str(cur.get("side") or "FLAT").upper()
        qty = float(cur.get("qty") or 0.0)
        mark = float(marks.get(sym) or cur.get("mark") or 0.0)
        cur_notional = abs(qty) * mark if mark else float(cur.get("notional") or 0.0)
        if tgt <= 0 and cur_notional > 0:
            intents.append({
                "symbol": sym, "action": "exit", "reason": "rebalance_zero_weight",
                "side": side, "qty": qty,
            })
            continue
        if tgt <= 0:
            continue
        if cur_notional <= 0:
            intents.append({
                "symbol": sym, "action": "enter", "reason": "rebalance_open",
                "side": "LONG", "quote_usdt": tgt,
            })
            continue
        drift = abs(cur_notional - tgt) / tgt
        if drift >= drift_pct:
            intents.append({
                "symbol": sym, "action": "adjust", "reason": "rebalance_drift",
                "side": side, "from_notional": round(cur_notional, 4),
                "to_notional": tgt, "drift": round(drift, 4),
            })
    return intents


def liquidation_risk(position: dict, mark: float, *, mmr: float = 0.004) -> dict[str, Any]:
    """Isolated-margin style liq proximity check."""
    side = str(position.get("side") or "LONG").upper()
    entry = float(position.get("entry") or 0.0)
    qty = abs(float(position.get("qty") or 0.0))
    lev = max(1.0, float(position.get("leverage") or 1.0))
    if not entry or not qty or mark <= 0:
        return {"at_risk": False, "distance_pct": None, "liq_price_est": None}
    notional = qty * entry
    margin = notional / lev
    if side == "SHORT":
        liq = entry * (1.0 + (1.0 / lev) - mmr)
        dist = (liq - mark) / mark if mark else None
        at_risk = mark >= liq * 0.98
    else:
        liq = entry * (1.0 - (1.0 / lev) + mmr)
        dist = (mark - liq) / mark if mark else None
        at_risk = mark <= liq * 1.02
    return {
        "at_risk": bool(at_risk),
        "distance_pct": None if dist is None else round(dist * 100.0, 4),
        "liq_price_est": round(liq, 8),
        "margin_usdt": round(margin, 4),
        "mmr": mmr,
    }
