#!/usr/bin/env python3
"""Unified 3y backtest engine — fixed params, comparable costs/init."""
from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TZ = ZoneInfo("Asia/Taipei")

INIT = 10_000.0
COST = 20.0 / 10_000.0  # 10bps fee + 10bps slip
FUNDING_ANN = 0.10
MAINTENANCE = 0.005

PERIOD_START = pd.Timestamp("2023-09-24", tz="UTC")
PERIOD_END = pd.Timestamp("2026-09-24", tz="UTC")  # inclusive calendar end; use last closed bar
RET_1Y_START = pd.Timestamp("2025-09-24", tz="UTC")

UA = {"User-Agent": "Mozilla/5.0 strategy-unified-3y/1.0"}
VISION = "https://data-api.binance.vision"

BARS_PER_YEAR = {"1d": 365.25, "4h": 365.25 * 6, "1h": 365.25 * 24}


def now_taipei() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def http_json(url: str, retries: int = 5) -> Any:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(0.35 * (i + 1))
    raise RuntimeError(f"HTTP failed {url}: {last}")


def fetch_klines(symbol: str, interval: str, target: int) -> pd.DataFrame:
    all_rows: list = []
    end = None
    bn = f"{symbol}USDT" if not symbol.endswith("USDT") else symbol
    while len(all_rows) < target:
        url = f"{VISION}/api/v3/klines?symbol={bn}&interval={interval}&limit=1000"
        if end is not None:
            url += f"&endTime={end}"
        batch = http_json(url)
        if not batch:
            break
        all_rows = batch + all_rows
        end = batch[0][0] - 1
        if len(batch) < 1000:
            break
        time.sleep(0.06)
    seen: set = set()
    out = []
    for row in all_rows:
        if row[0] not in seen:
            seen.add(row[0])
            out.append(row)
    out.sort(key=lambda x: x[0])
    df = pd.DataFrame(
        out,
        columns=["ot", "Open", "High", "Low", "Close", "Volume", "ct", "qv", "n", "tb", "tq", "i"],
    )
    df["ts"] = pd.to_datetime(df["ot"].astype(np.int64), unit="ms", utc=True)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    return df.set_index("ts")[["Open", "High", "Low", "Close", "Volume"]].sort_index()


def drop_incomplete(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    if df.empty:
        return df
    delta = {"1d": pd.Timedelta(days=1), "4h": pd.Timedelta(hours=4), "1h": pd.Timedelta(hours=1)}[interval]
    now = pd.Timestamp.now(tz="UTC")
    if now < df.index[-1] + delta:
        return df.iloc[:-1].copy()
    return df.copy()


def load_or_fetch(symbol: str, interval: str, target: int) -> pd.DataFrame:
    cache = DATA / interval / f"{symbol}.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Prefer local caches from s2/s3 if present and long enough
    alts = [
        Path(f"/workspace/strategy-crypto-s3-majors/data/{interval}/{symbol}.csv"),
        Path(f"/workspace/strategy-crypto-s2/data/{interval}/{symbol}.csv"),
    ]
    df = None
    if cache.is_file() and cache.stat().st_size > 500:
        df = pd.read_csv(cache, parse_dates=["ts"], index_col="ts")
    else:
        for a in alts:
            if a.is_file() and a.stat().st_size > 500:
                df = pd.read_csv(a, parse_dates=["ts"], index_col="ts")
                break
    if df is not None:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df = drop_incomplete(df, interval)
        # refresh if short or stale end
        need = target * 0.5
        if len(df) >= need and df.index[-1] >= PERIOD_END - pd.Timedelta(days=2):
            return df
    try:
        df_new = fetch_klines(symbol, interval, target)
        df_new = drop_incomplete(df_new, interval)
        df_new.to_csv(cache, index_label="ts")
        return df_new
    except Exception:
        # Keep local/alt cache if Binance rejects symbol (e.g. HYPE spot) or network fails
        if df is not None and len(df) > 50:
            return df
        raise


def sma_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def wilder_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = tr.copy().astype(float)
    atr[:] = np.nan
    if len(tr) < n:
        return atr
    atr.iloc[n - 1] = float(tr.iloc[:n].mean())
    for i in range(n, len(tr)):
        atr.iloc[i] = (float(atr.iloc[i - 1]) * (n - 1) + float(tr.iloc[i])) / n
    return atr


def add_donch(df: pd.DataFrame, n: int, atr_mode: str = "sma") -> pd.DataFrame:
    o = df.copy()
    h, l, c = o["High"], o["Low"], o["Close"]
    o["donch_hi"] = h.rolling(n).max().shift(1)
    o["donch_lo"] = l.rolling(n).min().shift(1)
    o["atr"] = sma_atr(o, 14) if atr_mode == "sma" else wilder_atr(o, 14)
    for span in (8, 9, 12, 20, 21, 26, 50, 200):
        o[f"ema_{span}"] = c.ewm(span=span, adjust=False).mean()
        o[f"sma_{span}"] = c.rolling(span).mean()
    return o


def signal_donchian(df: pd.DataFrame) -> pd.Series:
    hold = np.zeros(len(df), dtype=int)
    state = 0
    closes = df["Close"].values
    his, los = df["donch_hi"].values, df["donch_lo"].values
    for i in range(len(df)):
        if np.isnan(his[i]) or np.isnan(los[i]):
            hold[i] = state
            continue
        if state == 0 and closes[i] > his[i]:
            state = 1
        elif state == 1 and closes[i] < los[i]:
            state = 0
        hold[i] = state
    return pd.Series(hold, index=df.index)


def signal_ema(df: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    return (df[f"ema_{fast}"] > df[f"ema_{slow}"]).astype(int)


def signal_supertrend(df: pd.DataFrame, mult: float = 3.0) -> pd.Series:
    c = df["Close"].values
    h = df["High"].values
    l = df["Low"].values
    atr = df["atr"].values
    n = len(df)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    direction = np.ones(n)
    st = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        mid = (h[i] + l[i]) / 2.0
        upper[i] = mid + mult * atr[i]
        lower[i] = mid - mult * atr[i]
        if i == 0 or np.isnan(st[i - 1]):
            st[i] = upper[i]
            direction[i] = -1
            continue
        if not (lower[i] > lower[i - 1] or c[i - 1] < lower[i - 1]):
            lower[i] = lower[i - 1]
        if not (upper[i] < upper[i - 1] or c[i - 1] > upper[i - 1]):
            upper[i] = upper[i - 1]
        if direction[i - 1] == -1:
            if c[i] > upper[i - 1]:
                direction[i] = 1
                st[i] = lower[i]
            else:
                direction[i] = -1
                st[i] = upper[i]
        else:
            if c[i] < lower[i - 1]:
                direction[i] = -1
                st[i] = upper[i]
            else:
                direction[i] = 1
                st[i] = lower[i]
    return pd.Series((direction == 1).astype(int), index=df.index)


def apply_btc_regime(sig: pd.Series, btc: pd.DataFrame) -> pd.Series:
    ok = (btc["Close"] > btc["sma_200"]).astype(int)
    ok = ok.reindex(sig.index).ffill().fillna(0)
    return (sig.astype(int) & ok.astype(int)).astype(int)


@dataclass
class BTResult:
    equity: pd.Series
    trades: list
    n_liquidations: int = 0


def run_donchian_engine(
    df: pd.DataFrame,
    signal: pd.Series,
    stop_m: float,
    trail_m: float,
    max_hold: int,
    init: float = INIT,
    reset_below_hi: bool = False,
    leverage: float = 1.0,
    funding_ann: float = 0.0,
    check_liq: bool = False,
    risk_frac: float | pd.Series = 1.0,
) -> BTResult:
    """Long-only Donchian-style engine. Entry: edge 0→1; optional reset_below_hi."""
    data = df.dropna(subset=["atr", "donch_hi", "donch_lo"]).copy()
    signal = signal.reindex(data.index).fillna(0).astype(int)
    if isinstance(risk_frac, pd.Series):
        risk_frac_arr = risk_frac.reindex(data.index).fillna(1.0).clip(0.0, 1.0).values
    else:
        risk_frac_arr = np.full(len(data), float(risk_frac))
    cash = init
    shares = 0.0
    entry_px = 0.0
    entry_i = 0
    stop = 0.0
    entry_ts = None
    equity = []
    trades = []
    n_liq = 0
    need_reset = False
    idx = data.index
    opens, closes, highs, lows = data["Open"].values, data["Close"].values, data["High"].values, data["Low"].values
    atrs, sigs, his = data["atr"].values, signal.values, data["donch_hi"].values

    # bars/year approx for funding
    if len(idx) > 1:
        dt = (idx[1] - idx[0]).total_seconds()
        bpy = 365.25 * 86400 / max(dt, 1)
    else:
        bpy = 365.25
    fund_bar = funding_ann / bpy

    for i in range(len(data)):
        ts = idx[i]
        o, c, h, lo, atr = opens[i], closes[i], highs[i], lows[i], atrs[i]

        if shares != 0 and fund_bar and leverage > 1:
            cash -= abs(shares) * c * fund_bar

        if check_liq and shares > 0 and leverage > 1:
            mtm = cash + shares * c
            notional = shares * c
            if mtm <= notional * MAINTENANCE or mtm <= 0:
                px = lo * (1 - COST)
                cash += shares * px
                trades.append({"entry": str(entry_ts), "exit": str(ts), "pnl": shares * (px - entry_px), "reason": "liquidation"})
                shares = 0.0
                n_liq += 1
                cash = max(cash, 0.0)
                equity.append((ts, cash))
                need_reset = True if reset_below_hi else False
                continue

        if shares > 0:
            hit = (stop_m > 0) and (lo <= stop)
            hold_bars = i - entry_i
            exit_sig = sigs[i] == 0
            reason = None
            exit_ref = None
            if hit:
                reason = "stop_loss"
                exit_ref = min(o, stop)
            elif trail_m > 0:
                trail = c - trail_m * atr
                if trail > stop:
                    stop = trail
            if reason is None and (exit_sig or hold_bars >= max_hold):
                reason = "signal_exit" if exit_sig else "max_hold"
                exit_ref = c
            if reason is not None:
                px = exit_ref * (1 - COST)
                cash += shares * px
                trades.append({
                    "entry": str(entry_ts), "exit": str(ts),
                    "entry_px": entry_px, "exit_px": px,
                    "pnl": shares * (px - entry_px),
                    "pnl_pct": (px / entry_px - 1) * 100,
                    "reason": reason,
                })
                shares = 0.0
                entry_px = 0.0
                stop = 0.0
                if reset_below_hi:
                    need_reset = True

        can = shares == 0 and i > 0
        if reset_below_hi and need_reset:
            if c < his[i]:
                need_reset = False
            else:
                can = False
        edge = sigs[i] == 1 and sigs[i - 1] == 0
        trigger = edge  # both core (no reset) and reset_below use edge; reset gates via need_reset
        if can and trigger and atr > 0 and not np.isnan(atr):
            px = c * (1 + COST)
            equity_now = cash
            notional = equity_now * float(risk_frac_arr[i]) * leverage
            if notional > 0 and px > 0:
                shares = notional / px
                cash -= shares * px
                entry_px = px
                entry_i = i
                entry_ts = ts
                stop = (px - stop_m * atr) if stop_m > 0 else 0.0

        equity.append((ts, cash + shares * c))

    if shares > 0:
        px = closes[-1] * (1 - COST)
        cash += shares * px
        trades.append({"entry": str(entry_ts), "exit": str(idx[-1]), "pnl": shares * (px - entry_px), "reason": "eod_flat"})
        equity[-1] = (idx[-1], cash)

    return BTResult(equity=pd.Series({t: v for t, v in equity}), trades=trades, n_liquidations=n_liq)


def run_rotation(
    prices: pd.DataFrame,
    lookback: int,
    top_n: int,
    abs_mom: bool,
    init: float = INIT,
) -> BTResult:
    prices = prices.sort_index().ffill()
    mom = prices.pct_change(lookback)
    aligned_rb = []
    for d in prices.resample("W-FRI").last().index:
        prior = prices.index[prices.index <= d]
        if len(prior):
            aligned_rb.append(prior[-1])
    rb_set = set(aligned_rb)
    cash = init
    holdings: dict[str, float] = {}
    equity = []
    trades = []
    for dt in prices.index:
        if dt in rb_set:
            row = mom.loc[dt] if dt in mom.index else (mom.loc[:dt].iloc[-1] if len(mom.loc[:dt]) else None)
            picks = []
            if row is not None:
                s = row.dropna().sort_values(ascending=False)
                if abs_mom:
                    s = s[s > 0]
                picks = list(s.head(top_n).index)
            for s, sh in list(holdings.items()):
                px = float(prices.at[dt, s]) * (1 - COST)
                cash += sh * px
                trades.append({"entry": str(dt), "exit": str(dt), "pnl": 0, "reason": "rebalance_sell", "symbol": s})
            holdings = {}
            eq_now = cash
            if picks and eq_now > 0:
                w = 1.0 / len(picks)
                for s in picks:
                    px = float(prices.at[dt, s]) * (1 + COST)
                    sh = (eq_now * w) / px
                    cash -= sh * px
                    holdings[s] = sh
                    trades.append({"entry": str(dt), "exit": str(dt), "pnl": 0, "reason": "rebalance_buy", "symbol": s})
        mtm = cash + sum(holdings.get(s, 0) * float(prices.at[dt, s]) for s in holdings)
        equity.append((dt, mtm))
    return BTResult(equity=pd.Series({t: v for t, v in equity}), trades=trades)


def equal_weight_bh(prices: pd.DataFrame, init: float = INIT) -> pd.Series:
    rets = prices.pct_change().fillna(0)
    avail = prices.notna().astype(float)
    w = avail.div(avail.sum(axis=1), axis=0).fillna(0)
    port = (w * rets).sum(axis=1)
    eq = (1 + port).cumprod() * init
    eq.iloc[0] = init * (1 - COST)
    return eq


def buy_hold_equity(df: pd.DataFrame, init: float = INIT) -> pd.Series:
    c = df["Close"].dropna()
    if len(c) < 2:
        return pd.Series(dtype=float)
    # enter at first close with cost, mark to market, exit cost at end embedded in final
    units = (init * (1 - 0)) / (c.iloc[0] * (1 + COST))  # spend init at buy
    # simpler: equity path without continuous cost
    cash_spent = init
    shares = cash_spent / (c.iloc[0] * (1 + COST))
    eq = shares * c
    # apply sell cost only conceptually on final for return calc — mark path uses raw close for DD
    return eq


def slice_period(eq: pd.Series, start: pd.Timestamp, end: pd.Timestamp | None = None) -> pd.Series:
    s = eq[eq.index >= start]
    if end is not None:
        s = s[s.index <= end]
    return s


def metrics_from_equity(eq: pd.Series, trades: list, bh_ret: float, bpy: float) -> dict:
    eq = eq.dropna()
    if len(eq) < 2:
        return {}
    # renormalize to start at INIT for reporting consistency if needed
    total_ret = (eq.iloc[-1] / eq.iloc[0] - 1.0) * 100.0
    years = max((eq.index[-1] - eq.index[0]).total_seconds() / (365.25 * 86400), 1e-9)
    cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1.0) * 100.0 if eq.iloc[-1] > 0 else -100.0
    peak = eq.cummax()
    max_dd = float((eq / peak - 1.0).min() * 100.0)
    return {
        "final": round(float(eq.iloc[-1]), 2),
        "ret_3y": round(total_ret, 4),
        "cagr_3y": round(cagr, 4),
        "maxdd": round(max_dd, 4),
        "n_trades": len([t for t in trades if t.get("reason") not in ("rebalance_sell",)]),
        "bh_ret_3y": round(bh_ret, 4),
        "period_start": str(eq.index[0].date()),
        "period_end": str(eq.index[-1].date()),
        "years": round(years, 4),
    }


def oos_fixed_params(eq: pd.Series, bh_eq: pd.Series, n_folds: int = 6) -> dict:
    """Split evaluation window into n equal chronological folds; count strategy vs BH wins."""
    eq = eq.dropna()
    bh_eq = bh_eq.reindex(eq.index).ffill().dropna()
    common = eq.index.intersection(bh_eq.index)
    eq, bh_eq = eq.loc[common], bh_eq.loc[common]
    if len(eq) < n_folds * 10:
        return {"oos_pass": "0/0", "oos_wins": 0, "oos_total": 0, "folds": []}
    n = len(eq)
    fold_size = n // n_folds
    folds = []
    wins = 0
    for f in range(n_folds):
        a = f * fold_size
        b = (f + 1) * fold_size if f < n_folds - 1 else n
        se, sb = eq.iloc[a:b], bh_eq.iloc[a:b]
        if len(se) < 2:
            continue
        sr = (se.iloc[-1] / se.iloc[0] - 1) * 100
        br = (sb.iloc[-1] / sb.iloc[0] - 1) * 100
        beat = bool(sr > br)
        if beat:
            wins += 1
        folds.append({
            "fold": f + 1,
            "start": str(se.index[0].date()),
            "end": str(se.index[-1].date()),
            "strat_ret": round(float(sr), 4),
            "bh_ret": round(float(br), 4),
            "beats_bh": beat,
        })
    return {"oos_pass": f"{wins}/{len(folds)}", "oos_wins": wins, "oos_total": len(folds), "folds": folds}


def ret_between(eq: pd.Series, start: pd.Timestamp, end: pd.Timestamp | None = None) -> float | None:
    s = slice_period(eq, start, end)
    if len(s) < 2:
        return None
    return float((s.iloc[-1] / s.iloc[0] - 1.0) * 100.0)
