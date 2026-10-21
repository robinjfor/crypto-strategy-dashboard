"""Donchian ATR LONG on Binance USDT-M PERP, next-bar-open fills (family donchian_atr_perp).

For the catalog donchian_atr rows built on perp data with the canonical engine
(strategy-unified-3y/rd_catalog_refresh/code/eng.bt + sim.py): C22..C40 (2026-10-20 refresh and
2026-10-21 strict-WF replacements). Rule by rule identical to eng.bt:
  * ATR14 = SMA of True Range (first TR = H-L); donch_hi/lo = rolling N max(High)/min(Low) shifted 1.
  * State machine (position independent): 0->1 on Close > donch_hi, 1->0 on Close < donch_lo. Entry only
    on the 0->1 edge at the close of bar i (ATR > 0), market BUY at bar i+1 open.
  * Initial stop = fill x (1 + 0.002) - stop_m x ATR(signal bar) (engine cost-adjusted entry, position
    "stop_basis"); active from the fill bar, fills at min(open, stop) (reduce-only STOP_MARKET,
    CONTRACT_PRICE). Each close: trail = max(stop, Close - trail_m x ATR); then state == 0 (exit_signal)
    > bars since fill >= max_hold (1h 192 / 4h 36 / 1d 40), both filled at the next open.
  * reset_below_hi: after any exit an entry needs a close < donch_hi on some bar from the exit bar (stop
    bar, or the next-open exit bar) up to the signal bar.
  * One direction per coin (an open SHORT on the coin blocks); leverage locked 1x. Funding is real (perp).
This is a separate family from the spot donchian_atr runner (C1..C21: spot klines, Wilder default, entry
stop basis = raw fill), which is unchanged."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from bb_squeeze import (MAXHOLD_BARS, STOP_COST, STOP_REASONS, STOP_WORKING_TYPE, _now_iso, _ts, initial_stop_from_fill,
                        replay_long, stop_basis_from_fill)
from indicators import bar_ts_iso, sma_atr, split_closed

FAMILY = "donchian_atr_perp"
LEVERAGE = 1.0
TIMEFRAMES = frozenset(MAXHOLD_BARS)
__all__ = ["FAMILY", "STOP_COST", "STOP_WORKING_TYPE", "stop_basis_from_fill", "initial_stop_from_fill",
           "evaluate_donchian_perp_slot", "donch_bands", "donch_state", "slot_from_spec", "leverage_cap"]


def leverage_cap(maxdd_pct=None) -> float:
    return LEVERAGE


def donch_bands(ind: pd.DataFrame, n: int):
    hi = ind["High"].astype(float).rolling(n).max().shift(1).values
    lo = ind["Low"].astype(float).rolling(n).min().shift(1).values
    return hi, lo


def donch_state(C, HI, LO) -> np.ndarray:
    """eng.donch_state verbatim."""
    c, hi, lo = (np.asarray(x, float).tolist() for x in (C, HI, LO))
    out = [0] * len(c)
    st = 0
    for i in range(len(c)):
        if not (hi[i] != hi[i] or lo[i] != lo[i]):
            if st == 0 and c[i] > hi[i]:
                st = 1
            elif st == 1 and c[i] < lo[i]:
                st = 0
        out[i] = st
    return np.array(out, np.int64)


def _reset_pending(ind: pd.DataFrame, HI: np.ndarray, slot_meta: dict) -> bool:
    """eng.bt reset_below_hi: need=True at the exit bar; cleared by any bar (exit bar .. now) with C < HI."""
    lx = slot_meta.get("last_exit_bar_ts")
    if not lx:
        return False
    idx = ind.index
    t = _ts(lx)
    p = int(idx.searchsorted(t))
    if str(slot_meta.get("last_exit_reason") or "") not in STOP_REASONS:
        # close-based exit decided on bar e (last_exit_bar_ts) fills at bar e+1 open: need starts at e+1
        p = int(idx.searchsorted(t, side="right"))
    if p >= len(idx):
        return True
    C = ind["Close"].values.astype(float)[p:]
    h = HI[p:]
    return not bool(np.any(C < h))


def evaluate_donchian_perp_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    if len(closed) < 3:
        return {"slot": slot["id"], "symbol": slot["symbol"], "family": FAMILY, "error": "no_closed_bars"}
    ind = closed[["Open", "High", "Low", "Close"]].astype(float)
    atr = sma_atr(ind, 14).fillna(0).values
    n = int(slot["donch_n"])
    HI, LO = donch_bands(ind, n)
    C = ind["Close"].values
    st = donch_state(C, HI, LO)
    edge = bool(len(st) >= 2 and st[-1] == 1 and st[-2] == 0)
    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(ind.index[-1])
    mark = float(forming["Close"]) if forming is not None else float(bar["Close"])
    stop_m = float(slot["stop_atr_mult"]); trail_m = float(slot["trail_atr_mult"])
    max_hold = int(slot.get("max_hold_bars") or MAXHOLD_BARS[tf])
    reset = bool(slot.get("require_reset_below_hi", True))
    _fl = (lambda v: None if v is None or not np.isfinite(v) else float(v))
    res: dict[str, Any] = {
        "slot": slot["id"], "symbol": slot["symbol"], "tf": tf, "family": FAMILY, "venue": "futures",
        "side": "LONG", "bar_ts": bar_ts, "close": float(bar["Close"]), "mark": mark,
        "atr": float(atr[-1]), "atr_mode": "sma", "checked_at": _now_iso(), "action": "hold", "reason": None,
        "leverage": LEVERAGE, "leverage_cap": LEVERAGE,
        "stop_atr_mult": stop_m, "trail_atr_mult": trail_m, "max_hold_bars": max_hold,
        "stop_working_type": STOP_WORKING_TYPE, "donch_n": n, "require_reset_below_hi": reset,
        "donch_hi": _fl(HI[-1]), "donch_lo": _fl(LO[-1]), "donch_state": int(st[-1]), "entry_edge": edge,
        "dist_to_breakout_atr": (round((float(bar["Close"]) - float(HI[-1])) / float(atr[-1]), 4)
                                 if atr[-1] > 0 and np.isfinite(HI[-1]) else None),
    }
    if position and position.get("status") == "FILLED":
        basis = position.get("stop_basis")
        basis = float(basis) if basis is not None else stop_basis_from_fill(float(position["entry"]))
        rp = replay_long(ind, atr, basis, position.get("entry_bar_ts"), stop_m, trail_m, st, max_hold)
        res["stop"] = float(rp["stop"])
        res["initial_stop"] = float(rp["init_stop"])
        res["entry"] = position.get("entry")
        res["qty"] = position.get("qty")
        ex = rp["exit"]
        if ex is None:
            res.update(action="manage", reason="trail_update")
        else:
            res.update(action="exit", reason=ex["reason"], exit_ref=ex["exit_ref"], exit_bar_ts=ex["bar_ts"])
            if ex["reason"].startswith("stop"):
                res["after_stop"] = evaluate_donchian_perp_slot(
                    slot, klines, None, {**slot_meta, "last_acted_bar_ts": ex["bar_ts"], "last_exit_bar_ts": ex["bar_ts"],
                                         "last_exit_reason": ex["reason"]})
        return res
    if not slot.get("armed", True):
        res.update(action="skip", reason="not_armed"); return res
    if slot_meta.get("last_entry_bar_ts") == bar_ts or (
            slot_meta.get("last_acted_bar_ts") == bar_ts
            and str(slot_meta.get("last_exit_reason") or "") not in STOP_REASONS):
        res.update(action="skip", reason="idempotent_same_bar"); return res
    lx = slot_meta.get("last_exit_bar_ts")
    if edge and lx and _ts(ind.index[-1]) < _ts(lx):
        res.update(action="skip", reason="stale_signal_before_exit"); return res
    if not edge:
        res.update(action="armed", reason=("state_on_wait_close_below_donch_lo" if st[-1] == 1 else "waiting_breakout"))
        return res
    if not (atr[-1] > 0):
        res.update(action="armed", reason="atr_not_ready"); return res
    if reset and _reset_pending(ind, HI, slot_meta):
        res.update(action="wait_reset", reason="need_close_below_donch_hi"); return res
    if slot_meta.get("_opposite_open"):
        res.update(action="skip", reason="opposite_position_open"); return res
    from slots import MAX_NOTIONAL_USDT
    quote = min(float(slot.get("quote_usdt") or 0.0), MAX_NOTIONAL_USDT)
    res.update(
        action="enter", reason="donchian_breakout", quote_usdt=quote,
        stop_atr=float(atr[-1]),  # initial stop = fill x 1.002 - stop_m x this ATR
        suggested_stop=round(float(bar["Close"]) - stop_m * float(atr[-1]), 8),
        client_order_id=f"{slot['id']}-{bar_ts[:16].replace(':', '').replace('+', '')}"[:36],
    )
    return res


def slot_from_spec(entry: dict, quote_usdt: float = 750.0) -> dict:
    """Runner slot from an analyst perp donchian spec entry (rd_catalog_refresh / strict-WF fixtures)."""
    p = entry["params"]
    tf = entry["timeframe"]
    coin = str(p["symbol"]).upper()
    assert entry["symbol"] == f"{coin}USDT" and p["tf"] == tf and entry.get("side") == "long_only", entry["code"]
    assert entry.get("venue") == "binance_usdtm_perp"
    assert str(p.get("atr_mode") or "sma") == "sma" and not p.get("btc_regime")
    return {"id": f"dperp_{coin.lower()}_{tf}", "code": entry["code"], "strategy_id": entry["spec_strategy_id"],
            "symbol": entry["symbol"], "tf": tf, "family": FAMILY, "donch_n": int(p["donch_n"]),
            "stop_atr_mult": float(p["stop_m"]), "trail_atr_mult": float(p["trail_m"]),
            "max_hold_bars": int(p.get("max_hold") or MAXHOLD_BARS[tf]),
            "require_reset_below_hi": bool(p.get("reset_below_hi", True)), "atr_mode": "sma",
            "leverage": LEVERAGE, "quote_usdt": quote_usdt, "armed": True}
