"""Donchian + ATR14 (Wilder or SMA of TR)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def interval_ms(interval: str) -> int:
    return INTERVAL_MS[interval]


def split_closed(df: pd.DataFrame, interval: str, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if df.empty:
        return df.copy(), None
    ims = interval_ms(interval)
    open_ts = df.iloc[-1].name.to_pydatetime()
    if open_ts.tzinfo is None:
        open_ts = open_ts.replace(tzinfo=timezone.utc)
    if now < open_ts + timedelta(milliseconds=ims):
        return df.iloc[:-1].copy(), df.iloc[-1]
    return df.copy(), None


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


def sma_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """SMA of True Range over n bars — same formula as SOL core atr14_sma."""
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def resolve_atr(df: pd.DataFrame, n: int = 14, atr_mode: str = "wilder") -> pd.Series:
    mode = (atr_mode or "wilder").strip().lower()
    if mode == "sma":
        return sma_atr(df, n)
    if mode == "wilder":
        return wilder_atr(df, n)
    raise ValueError(f"atr_mode 必須是 sma 或 wilder，收到：{atr_mode!r}")


def add_donch_atr(
    df: pd.DataFrame,
    donch_n: int = 20,
    *,
    atr_n: int = 14,
    atr_mode: str = "wilder",
) -> pd.DataFrame:
    o = df.copy()
    o["donch_hi"] = o["High"].rolling(donch_n).max().shift(1)
    o["donch_lo"] = o["Low"].rolling(donch_n).min().shift(1)
    o["atr"] = resolve_atr(o, atr_n, atr_mode)
    o["atr_mode"] = (atr_mode or "wilder").strip().lower()
    return o


def bar_ts_iso(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC").isoformat()


def replay_stop_path(
    closed: pd.DataFrame,
    entry: float,
    entry_bar_ts: str | None,
    stop_atr_mult: float,
    trail_atr_mult: float,
    initial_stop: float | None = None,
) -> dict[str, Any]:
    ind = closed.dropna(subset=["atr", "donch_hi", "donch_lo"])
    nan = float("nan")
    if ind.empty:
        s0 = float(initial_stop) if initial_stop is not None else float(entry)
        return {"stop": s0, "donch_lo": nan, "atr": nan, "exit": None, "bars_replayed": 0}
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
            stop = float(initial_stop) if initial_stop is not None else (entry - stop_atr_mult * atr)
        if i == 0:
            continue
        if lo <= stop:
            exit_info = {
                "bar_ts": bar_ts_iso(ts),
                "stop": float(stop),
                "exit_ref": float(min(o, stop)),
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


def needs_reset_below_hi(closed_ind: pd.DataFrame, exit_bar_ts: str | None) -> bool:
    if not exit_bar_ts or closed_ind.empty:
        return False
    ets = pd.Timestamp(exit_bar_ts)
    if ets.tzinfo is None:
        ets = ets.tz_localize("UTC")
    post = closed_ind.loc[closed_ind.index > ets]
    if post.empty:
        return True
    for _, row in post.iterrows():
        if float(row["Close"]) < float(row["donch_hi"]):
            return False
    return True
