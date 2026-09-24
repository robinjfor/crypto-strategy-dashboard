#!/usr/bin/env python3
"""Shared indicators & pricing for paper automation (stdlib + pandas/numpy)."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TZ = ZoneInfo("Asia/Taipei")
UA = {"User-Agent": "Mozilla/5.0 crypto-strategy-dashboard-paper/1.0"}
VISION = "https://data-api.binance.vision"
VISION_FALLBACKS = (
    "https://data-api.binance.vision",
    "https://data.binance.vision",
    "https://api1.binance.com",
    "https://api.binance.com",
)

SLIP_BPS = 10.0
FEE_BPS = 10.0
ONE_WAY = (SLIP_BPS + FEE_BPS) / 10_000.0  # 20 bps

STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDE", "USD1", "USDP", "RLUSD", "USD1", "USDD", "EURC", "XUSD", "EUR", "AEUR",
}
LEVERAGE_HINTS = ("UPUSDT", "DOWNUSDT", "BULL", "BEAR", "3L", "3S", "4L", "4S", "5L", "5S")

BLACKLIST = {
    "AVAX", "INJ", "HYPE", "UNI", "ARB", "APT", "RAY", "WLD", "AAVE", "ONDO",
    "STRK", "AR", "ENA", "FIL", "SUI", "DOGE",
}
OBSERVE_ONLY = {"PEPE"}


def now_taipei() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S CST")


def now_iso_taipei() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def http_json(url: str, retries: int = 4, timeout: int = 60) -> Any:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"HTTP failed {url}: {last}")


def http_json_multi(path_qs: str, retries: int = 3, timeout: int = 60) -> Any:
    """Try VISION_FALLBACKS until one host works (handles HTTP 451 / regional blocks)."""
    last: Exception | None = None
    for base in VISION_FALLBACKS:
        url = f"{base}{path_qs}"
        try:
            return http_json(url, retries=retries, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            continue
    raise RuntimeError(f"HTTP multi-host failed {path_qs}: {last}")


def interval_ms(interval: str) -> int:
    return {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]


def fetch_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    raw = http_json_multi(
        f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    )
    cols = [
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("open_time").sort_index()


def fetch_klines_deep(symbol: str, interval: str, target: int = 3000) -> pd.DataFrame:
    """Paginate backwards for scanner backtests."""
    all_rows: list = []
    end = None
    while len(all_rows) < target:
        qs = f"/api/v3/klines?symbol={symbol}&interval={interval}&limit=1000"
        if end is not None:
            qs += f"&endTime={end}"
        batch = http_json_multi(qs)
        if not batch:
            break
        all_rows = batch + all_rows
        end = batch[0][0] - 1
        if len(batch) < 1000:
            break
        time.sleep(0.08)
    if not all_rows:
        return pd.DataFrame()
    seen: set = set()
    out = []
    for row in all_rows:
        if row[0] not in seen:
            seen.add(row[0])
            out.append(row)
    out.sort(key=lambda x: x[0])
    cols = [
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(out, columns=cols)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("open_time").sort_index()


def split_closed(df: pd.DataFrame, interval: str, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if df.empty:
        return df.copy(), None
    ims = interval_ms(interval)
    last = df.iloc[-1]
    open_ts = last.name.to_pydatetime()
    if open_ts.tzinfo is None:
        open_ts = open_ts.replace(tzinfo=timezone.utc)
    closes_at = open_ts + timedelta(milliseconds=ims)
    if now < closes_at:
        return df.iloc[:-1].copy(), df.iloc[-1]
    return df.copy(), None


def wilder_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ATR(n): SMA seed then RMA (alpha=1/n). Same as trading-view/Wilder."""
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


def add_donch_atr(df: pd.DataFrame, donch_n: int = 20) -> pd.DataFrame:
    o = df.copy()
    c, h, l = o["Close"], o["High"], o["Low"]
    o["donch_hi"] = h.rolling(donch_n).max().shift(1)
    o["donch_lo"] = l.rolling(donch_n).min().shift(1)
    o["atr"] = wilder_atr(o, 14)
    return o


def signal_donchian(df: pd.DataFrame) -> pd.Series:
    hold = pd.Series(0, index=df.index, dtype=int)
    state = 0
    closes = df["Close"].values
    his = df["donch_hi"].values
    los = df["donch_lo"].values
    for i in range(len(df)):
        if np.isnan(his[i]) or np.isnan(los[i]):
            hold.iloc[i] = state
            continue
        if state == 0 and closes[i] > his[i]:
            state = 1
        elif state == 1 and closes[i] < los[i]:
            state = 0
        hold.iloc[i] = state
    return hold


def buy_px(ref: float) -> float:
    return ref * (1.0 + ONE_WAY)


def sell_px(ref: float) -> float:
    return ref * (1.0 - ONE_WAY)


def buy_hold_return(df: pd.DataFrame) -> float:
    c = df["Close"].dropna()
    if len(c) < 2:
        return 0.0
    return float((c.iloc[-1] * (1 - ONE_WAY) / (c.iloc[0] * (1 + ONE_WAY)) - 1.0) * 100.0)


def run_long_only(
    df: pd.DataFrame,
    signal: pd.Series,
    atr_stop_mult: float = 2.0,
    atr_trail_mult: float = 3.0,
    max_hold_bars: int = 96,
    init: float = 100_000.0,
) -> tuple[pd.Series, list[dict]]:
    data = df.dropna(subset=["atr"]).copy()
    signal = signal.reindex(data.index).fillna(0).astype(int)
    cash = init
    shares = 0.0
    entry_px = 0.0
    entry_i = 0
    stop = 0.0
    entry_ts = None
    equity = []
    trades: list[dict] = []
    idx = data.index
    opens = data["Open"].values
    closes = data["Close"].values
    lows = data["Low"].values
    atrs = data["atr"].values
    sigs = signal.values

    for i in range(len(data)):
        ts = idx[i]
        o, c, lo, atr = opens[i], closes[i], lows[i], atrs[i]
        if shares > 0:
            hit_stop = lo <= stop
            hold_bars = i - entry_i
            exit_sig = sigs[i] == 0
            reason = None
            exit_ref = None
            if hit_stop:
                reason = "stop_loss"
                exit_ref = min(o, stop)
            else:
                trail = c - atr_trail_mult * atr
                if trail > stop:
                    stop = trail
            if reason is None and (exit_sig or hold_bars >= max_hold_bars):
                reason = "signal_exit" if exit_sig else "max_hold"
                exit_ref = c
            if reason is not None:
                px = sell_px(exit_ref)
                pnl = shares * (px - entry_px)
                pnl_pct = (px / entry_px - 1.0) * 100.0
                cash += shares * px
                trades.append({
                    "entry_date": str(entry_ts),
                    "exit_date": str(ts),
                    "entry_price": round(entry_px, 8),
                    "exit_price": round(px, 8),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": reason,
                })
                shares = 0.0
                entry_px = 0.0
                stop = 0.0

        if shares == 0 and i > 0 and sigs[i] == 1 and sigs[i - 1] == 0 and atr > 0 and not np.isnan(atr):
            px = buy_px(c)
            alloc = cash * 0.98
            if alloc > 0 and px > 0:
                shares = alloc / px
                cash -= shares * px
                entry_px = px
                entry_i = i
                entry_ts = ts
                stop = px - atr_stop_mult * atr

        equity.append((ts, cash + shares * c))

    if shares > 0:
        ts = idx[-1]
        px = sell_px(closes[-1])
        pnl = shares * (px - entry_px)
        cash += shares * px
        trades.append({
            "entry_date": str(entry_ts),
            "exit_date": str(ts),
            "entry_price": round(entry_px, 8),
            "exit_price": round(px, 8),
            "pnl": round(pnl, 2),
            "pnl_pct": round((px / entry_px - 1.0) * 100.0, 4),
            "reason": "eod_flat",
        })
        equity[-1] = (ts, cash)

    eq = pd.Series({t: v for t, v in equity})
    return eq, trades


def metrics(equity: pd.Series, trades: list[dict], bh_pct: float, bars_per_year: float) -> dict:
    eq = equity.dropna()
    if len(eq) < 2:
        return {}
    total_ret = (eq.iloc[-1] / eq.iloc[0] - 1.0) * 100.0
    years = max((eq.index[-1] - eq.index[0]).total_seconds() / (365.25 * 86400), 1e-9)
    peak = eq.cummax()
    max_dd = float((eq / peak - 1.0).min() * 100.0)
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) if rets.std() > 0 else 0.0
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "total_return_pct": round(total_ret, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "n_trades": n,
        "win_rate_pct": round(len(wins) / n * 100.0, 4) if n else 0.0,
        "buy_hold_return_pct": round(bh_pct, 4),
        "excess_vs_bh_pct": round(total_ret - bh_pct, 4),
        "beats_bh": bool(total_ret > bh_pct and total_ret > 0),
        "years": round(years, 4),
    }


def walk_forward_oos(
    raw: pd.DataFrame,
    donch_n: int,
    stop_m: float,
    trail_m: float,
    tf: str,
    train_m: int = 15,
    test_m: int = 5,
    step_m: int = 5,
    max_folds: int = 6,
) -> dict:
    d = add_donch_atr(raw, donch_n).dropna(subset=["atr", "donch_hi", "donch_lo"])
    if len(d) < 200:
        return {"status": "thin", "oos_wins": 0, "oos_folds": 0}
    bpy = (24 * 365.25) if tf == "1h" else (6 * 365.25)
    max_hold = 192 if (tf == "1h" and donch_n >= 55) else (96 if tf == "1h" else 36)
    start, end = d.index[0], d.index[-1]
    folds = []
    train_start = start
    while len(folds) < max_folds:
        train_end = train_start + pd.DateOffset(months=train_m)
        test_start = train_end
        test_end = test_start + pd.DateOffset(months=test_m)
        if test_start >= end:
            break
        if test_end > end:
            test_end = end
        if (test_end - test_start).days < 40:
            break
        sl = d.loc[(d.index >= test_start) & (d.index <= test_end)]
        if len(sl) < 40:
            train_start = train_start + pd.DateOffset(months=step_m)
            continue
        sig = signal_donchian(sl)
        eq, tr = run_long_only(sl, sig, stop_m, trail_m, max_hold)
        bh = buy_hold_return(sl)
        m = metrics(eq, tr, bh, bpy)
        folds.append({
            "fold": len(folds) + 1,
            "beats_bh": m.get("beats_bh", False),
            "total_return_pct": m.get("total_return_pct"),
            "bh_return_pct": bh,
            "max_drawdown_pct": m.get("max_drawdown_pct"),
        })
        train_start = train_start + pd.DateOffset(months=step_m)
    wins = sum(1 for f in folds if f.get("beats_bh"))
    return {
        "status": "ok",
        "oos_wins": wins,
        "oos_folds": len(folds),
        "folds": folds,
    }


def is_leveraged_or_stable(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return True
    base = symbol[:-4]
    if base in STABLE_BASES or base == "USDT":
        return True
    up = symbol.upper()
    return any(h in up for h in LEVERAGE_HINTS)


def base_of(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def compute_live_stop(
    closed: pd.DataFrame,
    entry: float,
    entry_bar_ts: str | None,
    stop_atr_mult: float,
    trail_atr_mult: float,
    initial_stop: float | None = None,
) -> tuple[float, float, float]:
    """Replay close-ratchet trail; return (stop, donch_lo, atr) on last closed bar.

    Path-correct replay (see replay_stop_path): initial stop from entry bar ATR
    (seed ignored if it would skip the path), no ratchet on entry bar, then
    check low vs stop before ratcheting on later bars.
    """
    res = replay_stop_path(
        closed, entry, entry_bar_ts, stop_atr_mult, trail_atr_mult, initial_stop=None
    )
    return res["stop"], res["donch_lo"], res["atr"]


def replay_stop_path(
    closed: pd.DataFrame,
    entry: float,
    entry_bar_ts: str | None,
    stop_atr_mult: float,
    trail_atr_mult: float,
    initial_stop: float | None = None,
) -> dict:
    """Bar-by-bar Wilder trail with low-hit exits.

    Rules (capital-control aligned):
    - ATR = Wilder ATR14 (via add_donch_atr)
    - Entry bar: set initial stop = entry - stop_mult*ATR (or initial_stop);
      do NOT exit; do NOT ratchet from entry close
    - Later bars: if Low <= stop → exit at min(Open, stop); else
      stop = max(stop, Close - trail_mult*ATR)
    """
    ind = closed.dropna(subset=["atr", "donch_hi", "donch_lo"])
    nan = float("nan")
    if ind.empty:
        s0 = float(initial_stop) if initial_stop is not None else float(entry)
        return {
            "stop": s0, "donch_lo": nan, "atr": nan,
            "exit": None, "bars_replayed": 0,
        }
    if entry_bar_ts:
        ets = pd.Timestamp(entry_bar_ts)
        if ets.tzinfo is None:
            ets = ets.tz_localize("UTC")
        post = ind.loc[ind.index >= ets].copy()
        if post.empty:
            post = ind.iloc[-20:].copy()
    else:
        post = ind.iloc[-50:].copy()

    stop: float | None = None
    exit_info = None
    for i, (ts, row) in enumerate(post.iterrows()):
        o = float(row["Open"])
        c = float(row["Close"])
        lo = float(row["Low"])
        atr = float(row["atr"])
        if stop is None:
            stop = float(initial_stop) if initial_stop is not None else (
                float(entry) - stop_atr_mult * atr
            )
        if i == 0:
            # entry bar: establish stop only
            continue
        if lo <= stop:
            exit_ref = min(o, stop)
            exit_info = {
                "bar_ts": bar_ts_iso(ts),
                "stop": float(stop),
                "exit_ref": float(exit_ref),
                "open": o,
                "low": lo,
                "close": c,
                "atr": atr,
                "reason": "stop",
            }
            break
        trail = c - trail_atr_mult * atr
        if trail > stop:
            stop = trail

    last = ind.iloc[-1]
    return {
        "stop": float(stop if stop is not None else entry),
        "donch_lo": float(last["donch_lo"]),
        "atr": float(last["atr"]),
        "exit": exit_info,
        "bars_replayed": int(len(post)),
    }


def bar_ts_iso(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC").isoformat()
