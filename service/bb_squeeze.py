"""Z family (bb_squeeze_breakout_perp) live evaluator — Binance USDT-M perp, LONG only.

Rule by rule identical to the analyst's backtest (rd_daily/2026-10-21/code/bbsq.py signal on the
canonical engine rd_catalog_refresh/code/eng.bt; PREREG §B), next-bar-open fills:
  * BB(n,k) on closed bars: mid = SMA(n) of Close, up = mid + k·std(n, ddof=0), lo = mid − k·std,
    bw = (up − lo) / mid.
  * Squeeze on bar i: bw[i−1] <= rolling(L=120).quantile(q) of bw shifted by 1 (i.e. over
    bw[i−120..i−1]); squeeze_recent[i] = squeeze true on any of the last 5 bars (i−4..i).
  * State machine (position independent): 0→1 when Close > up AND squeeze_recent; 1→0 when
    Close < mid. Entry only on the 0→1 edge (state[i]==1 and state[i−1]==0) decided at the close of
    bar i, market BUY fills at bar i+1 open; ATR14 (SMA of True Range) on the signal bar must be > 0.
  * Initial stop = fill × (1 + 0.002) − stop_m × ATR(signal bar) (engine cost-adjusted entry price,
    position["stop_basis"]); active from the fill bar, fills at min(open, stop) (exchange reduce-only
    SELL STOP_MARKET on the last/contract price, like the backtest's kline Low).
  * Each bar close (stop not hit): trail stop = max(stop, Close − trail_m × ATR) (never down), then
    close-based exits filled at the next open: state == 0 (exit_signal) > bars since the fill bar >=
    max_hold (1h 192 / 4h 36 / 1d 40).
  * Re-entry: a stop-out on bar i still allows an entry edge at bar i's close (engine checks the stop
    before the entry); a close-based exit pending on bar i blocks an entry on bar i; an edge on a bar
    EARLIER than the exit bar never enters (stale; hourly runs).
  * One direction per coin (one-way mode, no hedge): an open SHORT on the same coin
    (slot_meta["_opposite_open"]) blocks the entry. Leverage locked at 1x (spec trades 1x)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from indicators import bar_ts_iso, sma_atr, split_closed

FAMILY = "bb_squeeze_breakout_perp"
MAXHOLD_BARS = {"1h": 192, "4h": 36, "1d": 40}
TIMEFRAMES = frozenset(MAXHOLD_BARS)
SQ_LOOKBACK = 120
SQ_RECENT = 5
Z_LEVERAGE = 1.0
STOP_COST = 0.002            # engine per-side cost; stop basis = fill x (1 + STOP_COST)
STOP_WORKING_TYPE = "CONTRACT_PRICE"
STOP_REASONS = ("stop", "stop_loss", "stop_initial", "stop_trail")
# Analyst grid (PREREG §B); live slots must use one of these values.
GRID = {"bb_n": (20, 30, 40), "bb_k": (1.5, 2.0, 2.5), "sq_q": (0.2, 0.35, 0.5)}


def leverage_cap(maxdd_pct=None) -> float:
    return Z_LEVERAGE


def stop_basis_from_fill(fill_px: float) -> float:
    return float(fill_px) * (1.0 + STOP_COST)


def initial_stop_from_fill(fill_px: float, stop_m: float, atr_signal: float) -> float:
    return stop_basis_from_fill(fill_px) - float(stop_m) * float(atr_signal)


def _now_iso() -> str:
    from strategy import now_iso_taipei
    return now_iso_taipei()


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def bb_indicators(close: pd.Series, n: int, k: float, q: float, L: int = SQ_LOOKBACK, recent: int = SQ_RECENT):
    """bbsq.indicators verbatim: (mid, up, squeeze_recent) arrays."""
    c = close.astype(float)
    mid = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    up = mid + k * sd
    lo = mid - k * sd
    bw = (up - lo) / mid
    b1 = bw.shift(1)
    thr = b1.rolling(L).quantile(q)
    cond = (b1 <= thr).astype(float)
    sq = cond.rolling(recent).max().fillna(0).values.astype(bool)
    return mid.values, up.values, sq, bw.values, thr.values


def bb_state(C: np.ndarray, MID: np.ndarray, UP: np.ndarray, SQ: np.ndarray) -> np.ndarray:
    """bbsq.state verbatim (0/1 per bar)."""
    c, mid, up, sq = (np.asarray(C, float).tolist(), np.asarray(MID, float).tolist(),
                      np.asarray(UP, float).tolist(), np.asarray(SQ, bool).tolist())
    out = [0] * len(c)
    st = 0
    for i in range(len(c)):
        m, u = mid[i], up[i]
        if not (m != m or u != u):  # NaN warmup: state unchanged
            if st == 0 and c[i] > u and sq[i]:
                st = 1
            elif st == 1 and c[i] < m:
                st = 0
        out[i] = st
    return np.array(out, np.int64)


def replay_long(ind: pd.DataFrame, atr: np.ndarray, basis: float, entry_bar_ts, stop_m: float, trail_m: float,
                state: np.ndarray, max_hold: int) -> dict[str, Any]:
    """Walk bars from the fill bar (signal bar + 1): stop -> trail -> state==0 -> max_hold (eng.bt order)."""
    idx = ind.index
    ets = _ts(entry_bar_ts)
    sig_pos = int(idx.searchsorted(ets))
    if sig_pos >= len(idx) or idx[sig_pos] != ets:
        sig_pos = max(0, int(idx.searchsorted(ets, side="right")) - 1)
    stop = init = float(basis) - stop_m * float(atr[sig_pos])
    O, Lo, C = (ind[k].values.astype(float) for k in ("Open", "Low", "Close"))
    fill = sig_pos + 1
    for j in range(fill, len(idx)):
        if stop_m > 0 and Lo[j] <= stop:
            why = "stop_trail" if stop > init + 1e-12 * max(1.0, abs(init)) else "stop_initial"
            return {"stop": stop, "init_stop": init, "exit": {"bar_ts": bar_ts_iso(idx[j]), "exit_ref": float(min(O[j], stop)), "reason": why}}
        if trail_m > 0:
            t = C[j] - trail_m * float(atr[j])
            if t > stop:
                stop = t
        if state[j] == 0:
            return {"stop": stop, "init_stop": init, "exit": {"bar_ts": bar_ts_iso(idx[j]), "exit_ref": float(C[j]), "reason": "exit_signal"}}
        if (j - fill) >= max_hold:
            return {"stop": stop, "init_stop": init, "exit": {"bar_ts": bar_ts_iso(idx[j]), "exit_ref": float(C[j]), "reason": "max_hold"}}
    return {"stop": stop, "init_stop": init, "exit": None}


def evaluate_bb_squeeze_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    if len(closed) < 3:
        return {"slot": slot["id"], "symbol": slot["symbol"], "family": FAMILY, "error": "no_closed_bars"}
    ind = closed[["Open", "High", "Low", "Close"]].astype(float)
    atr = sma_atr(ind, 14).fillna(0).values
    n, k, q = int(slot["bb_n"]), float(slot["bb_k"]), float(slot["sq_q"])
    mid, up, sq, bw, thr = bb_indicators(ind["Close"], n, k, q,
                                         int(slot.get("sq_lookback") or SQ_LOOKBACK), int(slot.get("sq_recent") or SQ_RECENT))
    C = ind["Close"].values
    st = bb_state(C, mid, up, sq)
    edge = bool(len(st) >= 2 and st[-1] == 1 and st[-2] == 0)
    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(ind.index[-1])
    mark = float(forming["Close"]) if forming is not None else float(bar["Close"])
    stop_m = float(slot["stop_atr_mult"]); trail_m = float(slot["trail_atr_mult"])
    max_hold = int(slot.get("max_hold_bars") or MAXHOLD_BARS[tf])
    _fl = (lambda v: None if v is None or not np.isfinite(v) else float(v))
    res: dict[str, Any] = {
        "slot": slot["id"], "symbol": slot["symbol"], "tf": tf, "family": FAMILY, "venue": "futures",
        "side": "LONG", "bar_ts": bar_ts, "close": float(bar["Close"]), "mark": mark,
        "atr": float(atr[-1]), "checked_at": _now_iso(), "action": "hold", "reason": None,
        "leverage": Z_LEVERAGE, "leverage_cap": Z_LEVERAGE,
        "stop_atr_mult": stop_m, "trail_atr_mult": trail_m, "max_hold_bars": max_hold,
        "stop_working_type": STOP_WORKING_TYPE,
        "bb_n": n, "bb_k": k, "sq_q": q,
        "bb_mid": _fl(mid[-1]), "bb_up": _fl(up[-1]), "bb_bw": _fl(bw[-1]), "sq_thr": _fl(thr[-1]),
        "squeeze_recent": bool(sq[-1]), "bb_state": int(st[-1]), "entry_edge": edge,
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
                # Runner-side stop close: engine checks the stop before the entry edge, so the flat
                # decision on this same closed bar is attached (main applies it after the close).
                res["after_stop"] = evaluate_bb_squeeze_slot(
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
        res.update(action="armed", reason="waiting_bb_squeeze_breakout"); return res
    if not (atr[-1] > 0):
        res.update(action="armed", reason="atr_not_ready"); return res
    if slot_meta.get("_opposite_open"):
        res.update(action="skip", reason="opposite_position_open"); return res
    from slots import MAX_NOTIONAL_USDT
    quote = min(float(slot.get("quote_usdt") or 0.0), MAX_NOTIONAL_USDT)
    res.update(
        action="enter", reason="bb_squeeze_breakout_edge", quote_usdt=quote,
        stop_atr=float(atr[-1]),  # initial stop = fill x 1.002 - stop_m x this ATR
        suggested_stop=round(float(bar["Close"]) - stop_m * float(atr[-1]), 8),
        client_order_id=f"{slot['id']}-{bar_ts[:16].replace(':', '').replace('+', '')}"[:36],
    )
    return res


def slot_from_spec(entry: dict, quote_usdt: float = 750.0) -> dict:
    """Runner slot from an analyst rd_daily/2026-10-21 fixtures/spec.json entry."""
    p = entry["params"]
    tf = entry["timeframe"]
    coin = str(p["symbol"]).upper()
    assert entry["symbol"] == f"{coin}USDT" and p["tf"] == tf and entry.get("side") == "long_only", entry["code"]
    assert str(p.get("atr_mode") or "sma") == "sma" and not p.get("btc_regime")
    return {"id": f"bbsq_{coin.lower()}_{tf}", "code": entry["code"], "strategy_id": entry["spec_strategy_id"],
            "symbol": entry["symbol"], "tf": tf, "family": FAMILY,
            "bb_n": int(p["bb_n"]), "bb_k": float(p["bb_k"]), "sq_q": float(p["sq_q"]),
            "sq_lookback": int(p.get("sq_lookback") or SQ_LOOKBACK), "sq_recent": int(p.get("sq_recent") or SQ_RECENT),
            "stop_atr_mult": float(p["stop_m"]), "trail_atr_mult": float(p["trail_m"]),
            "max_hold_bars": int(p.get("max_hold") or MAXHOLD_BARS[tf]),
            "leverage": Z_LEVERAGE, "quote_usdt": quote_usdt, "armed": True}
