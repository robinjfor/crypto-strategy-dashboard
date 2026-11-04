"""Demo slot VBO-1h: 55-day breakout with 1h entry, top-50 by 30d volume, long/short, 7d hold, max 10 equal slots,
0.5x gross on 1,000 USDT, isolated 2x per symbol, Binance futures DEMO (no real money).
Spec + backtest: strategy-unified-3y/spec_2026-11/research_lowcorr_r2_1104/ (r2.py vbo_h, selected.json key
('vbo_1h', 55, 5, 7, 0.01), scale 0.25 of the 2x gross = 0.5x, REPORT.md). Approved for DEMO by 自動交易專家 (fund control,
Robin's delegate since 10/23) on 2026-11-04. Not part of the orchestrator weights. Review date: 2026-12-02 (4 weeks).

Rules (identical to r2.vbo_h(lb=55, n=5, kd=7, conf=0.01) + Sim.run, re-implemented here):
  universe  : universe.txt (98 USDT perps, TradFi/stables excluded); tradable at bar t iff age >= 720 1h bars and
              30d ADV (mean 1h quote volume, 720 bars, min 240) ranks top 50 among symbols with data at t
  levels    : hh/ll = max high / min low of the previous 55 complete UTC days (daily bars from the 1h bars)
  signal    : +1 if close_t > hh*1.01 and close_{t-1} <= hh*1.01 (and in universe at t); -1 mirror below ll*0.99
  hold      : position sign = sign(sum of signals over the last 168 bars incl. t)   (7d)
  sizing    : w = sign / max(#nonzero, 10) (cap_norm, max 10 equal slots); notional = w * 0.25 * 2x * equity = 0.5x gross
  min notional: a leg whose notional is below the symbol's Binance MIN_NOTIONAL is not held (Sim.run mask)
  execution : decided at the close of 1h bar t, traded at the open of t+1 (first reconcile tick after the hour)."""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

SLOT = "demo_vbo1h_top50_1k"
STATE_KEY = "demo_vbo1h_exec"
CAPITAL = 1000.0
LEVERAGE = 2            # isolated, per symbol
SCALE = 0.25            # selected.json scale (fraction of the 2x gross)
GROSS = SCALE * LEVERAGE   # 0.5x
LB_DAYS, CONF, HOLD_BARS, MAXPOS, TOPN = 55, 0.01, 7 * 24, 10, 50
ADV_BARS, ADV_MIN, AGE_MIN = 24 * 30, 24 * 10, 24 * 30
FETCH_DAYS = LB_DAYS + 7 + 3          # 55d levels + 7d hold window + margin (complete UTC days)
REVIEW_DATE = "2026-12-02"
SPEC = "research_lowcorr_r2_1104/r2.py vbo_h(55,5,7,0.01) scale0.25"
_HERE = os.path.dirname(os.path.abspath(__file__))


def universe_symbols() -> list[str]:
    return [s for s in open(os.path.join(_HERE, "vbo1h_universe.txt")).read().split() if s]


def _daily_to_hourly(x: pd.DataFrame, idx: pd.DatetimeIndex) -> pd.DataFrame:
    return x.reindex(idx.floor("D")).set_axis(idx)


def signal_panel(H: pd.DataFrame, L: pd.DataFrame, C: pd.DataFrame, QV: pd.DataFrame) -> dict:
    """H/L/C/QV: contiguous hourly UTC index (bar open time), NaN where a symbol has no bar. First row must be 00:00 UTC."""
    adv = QV.rolling(ADV_BARS, min_periods=ADV_MIN).mean(); age = C.notna().cumsum()
    uni = adv.where(age >= AGE_MIN).rank(axis=1, ascending=False) <= TOPN
    Dh = H.resample("D").max(); Dl = L.resample("D").min()
    hh = _daily_to_hourly(Dh.rolling(LB_DAYS).max().shift(1), C.index) * (1 + CONF)
    ll = _daily_to_hourly(Dl.rolling(LB_DAYS).min().shift(1), C.index) * (1 - CONF)
    up = (C > hh) & (C.shift(1) <= hh) & uni
    dn = (C < ll) & (C.shift(1) >= ll) & uni
    s = up.astype(float) - dn.astype(float)
    pos = np.sign(s.rolling(HOLD_BARS, min_periods=1).sum())
    W = pos.div(pos.abs().sum(axis=1).clip(lower=MAXPOS), axis=0)
    return {"W": W, "signal": s, "uni": uni}


def notional_frac(W: pd.DataFrame | pd.Series):
    """signed notional / equity actually held (0.5x gross)."""
    return W * GROSS


def apply_min(wn: dict, equity: float, mins: dict) -> tuple[dict, list]:
    """Sim.run min-notional mask on the live book: drop legs with |w|*equity < MIN_NOTIONAL. Returns (kept, dropped)."""
    keep, drop = {}, []
    for s, w in wn.items():
        if w == 0:
            continue
        if abs(w) * equity >= float(mins.get(s, 5.0)):
            keep[s] = w
        else:
            drop.append(s)
    return keep, drop


def decide(prev_t: float, t: float, held_qty: float, price: float, equity: float, step: float, mn: float) -> tuple[float, str]:
    """One symbol at one bar. prev_t/t: last/new target notional fraction (signed); held_qty: ledger qty.
    Trades exactly when the backtest weight changes (entry, exit, flip, cap_norm re-weight); otherwise holds.
    -> (new_qty, kind) kind in hold|open|close|flip|resize|skip_min."""
    if t == prev_t and (t == 0) == (held_qty == 0) and np.sign(held_qty) in (0, np.sign(t)):
        return held_qty, "hold"
    if t == 0:
        return (0.0, "close") if held_qty else (0.0, "hold")
    q = t * equity / price
    if step > 0:
        q = math.copysign(math.floor(abs(q) / step + 1e-9) * step, q)
    if q == 0 or abs(q) * price < mn:
        return (0.0, "close") if held_qty else (held_qty, "skip_min")
    if held_qty == 0:
        return q, "open"
    if np.sign(q) != np.sign(held_qty):
        return q, "flip"
    if abs(q - held_qty) * price < mn and abs(q) > abs(held_qty):
        return held_qty, "skip_min"     # increase below exchange min: keep (reductions are reduceOnly, no min)
    return q, "resize"


def last_targets(H, L, C, QV) -> tuple[pd.Timestamp, dict]:
    sp = signal_panel(H, L, C, QV); w = sp["W"].iloc[-1]
    return C.index[-1], {s: float(x) for s, x in w.items() if x != 0 and not np.isnan(x)}


def trim_to_day(df: pd.DataFrame) -> pd.DataFrame:
    i0 = df.index[0]
    if i0.hour or i0.minute:
        df = df.loc[i0.floor("D") + pd.Timedelta(days=1):]
    return df


# ---------------- gates (same three as every Demo slot)
def active(state: dict) -> tuple[bool, str]:
    from demo_candidates import accepted
    g = (state.get("demo_candidates") or {}).get(SLOT) or {}
    for k, why in (("enabled", "未啟用"), ("approved", "未批准"), ("parity_pass", "對帳未通過"), ("cloud_ordering_ok", "下單測試未通過")):
        if not g.get(k):
            return False, why
    if not accepted(SLOT):
        return False, "未驗收"
    return True, ""


def checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
