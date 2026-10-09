"""S family (short_trend_perp) live evaluator — USDT-M perp, short only.

Matches the analyst's engine (rd_short_range engine_sr.bt + families.sema/sdon; rd_p118v2 for
2h/6h/12h) rule by rule, with next-bar-open fills:
  * Entry (decided on the closed bar i, market order fills at bar i+1 open):
      sema: st = EMA_fast < EMA_slow AND filter; enter on the rising edge of st (st[i] and not st[i-1]).
      sdon: br = Close < lowest Low of the prior n bars; enter on the rising edge of br AND filter[i].
    ATR(14) (SMA of True Range) on the signal bar must be > 0.
  * Filters (daily info uses the PREVIOUS day's close on intraday bars):
      none | fpos (mean of the last 9 funding events with ts <= bar open > 0) | coin50 (coin daily
      close < SMA50) | btc50 (BTC daily close < EMA50) | btc200 (BTC daily close < SMA200) | btc50_fpos.
  * Stop: initial = entry fill + stop_m x ATR(signal bar); checked intrabar from the fill bar on
    (exchange stop, fills at max(open, stop)); then each bar close trails it DOWN to
    close + trail_m x ATR (never up).
  * Close-based exits (only if the stop was not hit on that bar), filled at the next open:
      sema: EMA_fast > EMA_slow;  sdon: Close > highest High of the prior max(n//2, 5) bars;
      max_hold: bars since the fill bar >= max_hold (2h=120, 4h=60, 6h=40, 12h=20, 1h=240, 1d=40).
  * Conflict rule: if a position in the opposite direction on the same coin is open
    (slot_meta["_opposite_open"]), no new short is opened (one-way mode, no hedge mode).
  * Stop basis: stop0 = fill x (1 - 0.002) + stop_m x ATR(signal bar) — the engine's cost-adjusted
    entry price (position["stop_basis"]); exchange stop is a reduce-only BUY STOP_MARKET triggered on
    the LAST/contract price (workingType=CONTRACT_PRICE), like the backtest's kline High.
  * Leverage: locked at 1x (資金控管 review 2026-10-09; the analyst spec trades 1x).
  * Re-entry: no cooldown, but a NEW entry edge is required. A stop-out on bar i still allows an entry
    edge at bar i's close (engine order: stop check before the entry check). An edge on a bar EARLIER
    than the exit bar never enters (the slot was in position then; the engine ignores it) — this
    matters for hourly runs, where an exchange stop can fill inside the bar after the signal bar.
Inputs: slot_meta["_funding"] (pd.Series rate by event time), ["_coin_daily"] / ["_btc_daily"]
(DataFrames of daily klines, closed days)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from indicators import bar_ts_iso, sma_atr, split_closed

FAMILY = "short_trend_perp"
MAXHOLD_BARS = {"1h": 240, "2h": 120, "4h": 60, "6h": 40, "12h": 20, "1d": 40}
S_LEVERAGE = 1.0             # 資金控管 2026-10-09: S locked at 1x
STOP_COST = 0.002            # engine per-side cost; stop basis = fill x (1 - STOP_COST)
STOP_WORKING_TYPE = "CONTRACT_PRICE"
STOP_REASONS = ("stop", "stop_loss", "stop_initial", "stop_trail")


def leverage_cap(maxdd_pct=None) -> float:
    """S leverage is locked at 1x regardless of MaxDD."""
    return S_LEVERAGE


def stop_basis_from_fill(fill_px: float) -> float:
    return float(fill_px) * (1.0 - STOP_COST)
FILTERS = ("none", "fpos", "coin50", "btc50", "btc200", "btc50_fpos")


def _now_iso() -> str:
    from strategy import now_iso_taipei
    return now_iso_taipei()


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _daily_flag_on_bars(flag: pd.Series, idx: pd.DatetimeIndex, tf: str) -> np.ndarray:
    """engine_sr.daily_to_bars: daily flag labelled by day open; intraday bars see the previous day."""
    s = flag.astype(float)
    if tf != "1d":
        s = s.shift(1)
    return s.reindex(idx, method="ffill").fillna(0).values.astype(bool)


def funding_avg_on_bars(funding: pd.Series | None, idx: pd.DatetimeIndex) -> np.ndarray:
    """Mean of the last 9 funding events with event time <= bar open (engine_sr.Data.favg)."""
    if funding is None or len(funding) == 0:
        return np.zeros(len(idx))
    f = funding.copy()
    f.index = pd.DatetimeIndex([_ts(t) for t in f.index]).floor("min")
    f = f.sort_index()
    f = f[~f.index.duplicated(keep="last")].astype(float)
    favg = f.rolling(9).mean()
    return favg.reindex(idx.union(favg.index)).ffill().reindex(idx).fillna(0).values


def filter_on_bars(name: str, idx: pd.DatetimeIndex, tf: str, slot_meta: dict) -> np.ndarray:
    name = (name or "none").lower()
    if name not in FILTERS:
        raise ValueError(f"unknown S filter {name!r}")
    n = len(idx)
    if name == "none":
        return np.ones(n, bool)
    out = np.ones(n, bool)
    if name in ("fpos", "btc50_fpos"):
        out &= funding_avg_on_bars(slot_meta.get("_funding"), idx) > 0
    if name in ("btc50", "btc50_fpos", "btc200"):
        b = slot_meta.get("_btc_daily")
        if b is None or len(b) == 0:
            return np.zeros(n, bool)  # missing data -> never enter
        c = b["Close"].astype(float)
        flag = c < (c.ewm(span=50, adjust=False).mean() if name != "btc200" else c.rolling(200).mean())
        out &= _daily_flag_on_bars(flag, idx, tf)
    if name == "coin50":
        d = slot_meta.get("_coin_daily")
        if d is None or len(d) == 0:
            return np.zeros(n, bool)
        c = d["Close"].astype(float)
        out &= _daily_flag_on_bars(c < c.rolling(50).mean(), idx, tf)
    return out


def _signals(ind: pd.DataFrame, slot: dict, F: np.ndarray):
    """Returns (entry_edge[last], exit_cond array) on the closed-bar frame."""
    C = ind["Close"].values.astype(float)
    kind = str(slot.get("signal") or "sema").lower()
    with np.errstate(invalid="ignore"):
        if kind == "sema":
            ef = ind["Close"].ewm(span=int(slot["ema_fast"]), adjust=False).mean().values
            es = ind["Close"].ewm(span=int(slot["ema_slow"]), adjust=False).mean().values
            st = (ef < es) & F
            edge = bool(len(st) >= 2 and st[-1] and not st[-2])
            return edge, ef > es, {"ema_fast": float(ef[-1]), "ema_slow": float(es[-1]), "bear": bool(ef[-1] < es[-1])}
        if kind == "sdon":
            n = int(slot["donch_n"])
            lo = ind["Low"].rolling(n).min().shift(1).values
            hi = ind["High"].rolling(max(n // 2, 5)).max().shift(1).values
            br = C < lo
            edge = bool(len(br) >= 2 and br[-1] and not br[-2] and F[-1])
            return edge, C > hi, {"donch_lo": float(lo[-1]), "exit_hi": float(hi[-1])}
    raise ValueError(f"unknown S signal {kind!r}")


def replay_short(ind: pd.DataFrame, atr: np.ndarray, entry: float, entry_bar_ts, stop_m: float, trail_m: float,
                 exit_cond: np.ndarray, max_hold: int) -> dict[str, Any]:
    """Walk bars after the signal bar (fill bar on): stop -> trail -> close-based exit (first event wins)."""
    idx = ind.index
    ets = _ts(entry_bar_ts)
    sig_pos = int(idx.searchsorted(ets))
    if sig_pos >= len(idx) or idx[sig_pos] != ets:
        sig_pos = max(0, int(idx.searchsorted(ets, side="right")) - 1)
    stop = init = float(entry) + stop_m * float(atr[sig_pos])
    O, H, C = (ind[k].values.astype(float) for k in ("Open", "High", "Close"))
    fill = sig_pos + 1
    for j in range(fill, len(idx)):
        if H[j] >= stop:
            why = "stop_trail" if stop < init - 1e-12 * max(1.0, abs(init)) else "stop_initial"
            return {"stop": stop, "init_stop": init, "exit": {"bar_ts": bar_ts_iso(idx[j]), "exit_ref": float(max(O[j], stop)), "reason": why}}
        if 0 < trail_m < 50:
            t = C[j] + trail_m * float(atr[j])
            if t < stop:
                stop = t
        if bool(exit_cond[j]):
            return {"stop": stop, "init_stop": init, "exit": {"bar_ts": bar_ts_iso(idx[j]), "exit_ref": float(C[j]), "reason": "exit_signal"}}
        if (j - fill) >= max_hold:
            return {"stop": stop, "init_stop": init, "exit": {"bar_ts": bar_ts_iso(idx[j]), "exit_ref": float(C[j]), "reason": "max_hold"}}
    return {"stop": stop, "init_stop": init, "exit": None}


def evaluate_short_trend_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    if len(closed) < 3:
        return {"slot": slot["id"], "symbol": slot["symbol"], "family": FAMILY, "error": "no_closed_bars"}
    ind = closed[["Open", "High", "Low", "Close"]].astype(float)
    atr = sma_atr(ind, 14).fillna(0).values
    F = filter_on_bars(slot.get("filter", "none"), ind.index, tf, slot_meta)
    edge, exit_cond, extra = _signals(ind, slot, F)
    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(ind.index[-1])
    mark = float(forming["Close"]) if forming is not None else float(bar["Close"])
    stop_m = float(slot["stop_atr_mult"]); trail_m = float(slot["trail_atr_mult"])
    max_hold = int(slot.get("max_hold_bars") or MAXHOLD_BARS[tf])
    res: dict[str, Any] = {
        "slot": slot["id"], "symbol": slot["symbol"], "tf": tf, "family": FAMILY, "venue": "futures",
        "side": "SHORT", "signal_kind": slot.get("signal"), "filter": slot.get("filter", "none"),
        "filter_on": bool(F[-1]), "bar_ts": bar_ts, "close": float(bar["Close"]), "mark": mark,
        "atr": float(atr[-1]), "checked_at": _now_iso(), "action": "hold", "reason": None,
        "leverage": S_LEVERAGE, "leverage_cap": S_LEVERAGE,
        "stop_atr_mult": stop_m, "stop_working_type": STOP_WORKING_TYPE, **extra,
    }
    if position and position.get("status") == "FILLED":
        basis = position.get("stop_basis")
        basis = float(basis) if basis is not None else stop_basis_from_fill(float(position["entry"]))
        rp = replay_short(ind, atr, basis, position.get("entry_bar_ts"), stop_m, trail_m, exit_cond, max_hold)
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
                # Runner-side stop close (exchange stop missing / not filled): the engine checks the
                # stop before the entry edge, so the flat decision on this same closed bar is attached
                # and applied by the runner right after the close executes.
                res["after_stop"] = evaluate_short_trend_slot(
                    slot, klines, None, {**slot_meta, "last_acted_bar_ts": ex["bar_ts"], "last_exit_bar_ts": ex["bar_ts"],
                                         "last_exit_reason": ex["reason"]})
        return res
    if not slot.get("armed", True):
        res.update(action="skip", reason="not_armed"); return res
    # Idempotency: never two entries off one signal bar; a close-based exit decided on this bar also
    # blocks it (engine: no entry while an exit is pending). An exchange STOP fill on this bar does not
    # (engine checks the stop before the entry edge, so a stop-out bar can still signal a new short).
    if slot_meta.get("last_entry_bar_ts") == bar_ts or (
            slot_meta.get("last_acted_bar_ts") == bar_ts
            and str(slot_meta.get("last_exit_reason") or "") not in STOP_REASONS):
        res.update(action="skip", reason="idempotent_same_bar"); return res
    lx = slot_meta.get("last_exit_bar_ts")
    if edge and lx and _ts(ind.index[-1]) < _ts(lx):
        # stale: the signal bar is earlier than the exit bar (position was open at the signal bar)
        res.update(action="skip", reason="stale_signal_before_exit"); return res
    if not edge:
        res.update(action="armed", reason="waiting_short_entry"); return res
    if not (atr[-1] > 0):
        res.update(action="armed", reason="atr_not_ready"); return res
    if slot_meta.get("_opposite_open"):
        res.update(action="skip", reason="opposite_position_open"); return res
    from slots import MAX_NOTIONAL_USDT
    quote = min(float(slot.get("quote_usdt") or 0.0), MAX_NOTIONAL_USDT)
    res.update(
        action="enter", reason="short_entry_edge", quote_usdt=quote,
        stop_atr=float(atr[-1]),  # initial stop = fill + stop_m x this ATR
        suggested_stop=round(float(bar["Close"]) + stop_m * float(atr[-1]), 8),
        client_order_id=f"{slot['id']}-{bar_ts[:16].replace(':', '').replace('+', '')}"[:36],
    )
    return res


def slot_from_catalog_params(code: str, params: dict, quote_usdt: float = 750.0) -> dict:
    """Map an analyst catalog row's params onto a runner slot dict."""
    fam = params["family_engine"]
    s = {"id": f"short_{params['symbol'].lower()}_{params['tf']}", "code": code, "symbol": f"{params['symbol']}USDT",
         "tf": params["tf"], "family": FAMILY, "signal": fam, "filter": params.get("filt", "none"),
         "stop_atr_mult": float(params["stop_m"]), "trail_atr_mult": float(params["trail_m"]),
         "max_hold_bars": int(params.get("max_hold") or MAXHOLD_BARS[params["tf"]]),
         "leverage": S_LEVERAGE,
         "maxdd_pct": params.get("maxdd_pct"), "quote_usdt": quote_usdt, "armed": True}
    if fam == "sema":
        from_pairs = [(9, 21), (12, 26), (20, 50), (30, 80), (50, 100), (50, 200)][int(params["pair"])]
        s["ema_fast"], s["ema_slow"] = from_pairs
    else:
        s["donch_n"] = int(params["n"])
    return s


EMA_PAIRS = [(9, 21), (12, 26), (20, 50), (30, 80), (50, 100), (50, 200)]


def slot_from_spec(entry: dict, quote_usdt: float = 750.0) -> dict:
    """Runner slot from an analyst s_family_spec/spec.json entry (walk-forward params)."""
    import re
    sid = entry["spec_strategy_id"]  # e.g. wf_sema_ema9_21_s2.0_t5.0_fpos__ARB__4h
    m = re.match(r"wf_sema_ema(\d+)_(\d+)_s([\d.]+)_t([\d.]+)_([a-z0-9_]+)__([A-Z0-9]+)__(\w+)$", sid)
    if not m:
        raise ValueError(f"unsupported S spec id {sid!r}")
    fast, slow, sm, tm, filt, coin, tf = m.groups()
    assert entry["symbol"] == f"{coin}USDT" and entry["timeframe"] == tf, sid
    maxdd = (entry.get("backtest_full") or {}).get("maxdd_pct")
    return {"id": f"short_{coin.lower()}_{tf}", "code": entry["code"], "strategy_id": sid, "symbol": entry["symbol"],
            "tf": tf, "family": FAMILY, "signal": "sema", "filter": filt, "ema_fast": int(fast), "ema_slow": int(slow),
            "stop_atr_mult": float(sm), "trail_atr_mult": float(tm),
            "max_hold_bars": int(entry.get("max_hold_bars") or MAXHOLD_BARS[tf]),
            "leverage": S_LEVERAGE,
            "maxdd_pct": maxdd, "quote_usdt": quote_usdt, "armed": True}
