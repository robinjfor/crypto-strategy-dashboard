"""News signal math — parity with strategy-unified-3y/news_driven/code/news_lib.py."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

NEG_WORDS = re.compile(
    r"\b(hack|exploit|delist|sec\b|lawsuit|regulation|ban\b|fraud|investigat|breach|stolen|pause trading)\b",
    re.I,
)

GDELT_QUERY = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "OP": "Optimism",
    "DOT": "Polkadot",
    "FET": '"Fetch.ai"',
    "FIL": "Filecoin",
}


def parse_gdelt_timeline(path_or_obj: Path | str | dict | None) -> pd.Series:
    if path_or_obj is None:
        return pd.Series(dtype=float)
    if isinstance(path_or_obj, dict):
        j: Any = path_or_obj
    else:
        p = Path(path_or_obj)
        if not p.exists() or p.stat().st_size < 50:
            return pd.Series(dtype=float)
        try:
            j = json.loads(p.read_text())
        except Exception:
            return pd.Series(dtype=float)
    rows = []
    for s in j.get("timeline", []) or []:
        for d in s.get("data", []) or []:
            ds = d.get("date")
            if not ds:
                continue
            ts = pd.to_datetime(ds.replace("Z", "+00:00"), utc=True, format="%Y%m%dT%H%M%S%z")
            rows.append((ts.normalize(), float(d.get("value", 0))))
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({t: v for t, v in rows}).sort_index()
    return s[~s.index.duplicated(keep="last")]


def gdelt_z(series: pd.Series, win: int = 30) -> pd.Series:
    mu = series.rolling(win, min_periods=max(5, win // 3)).mean()
    sd = series.rolling(win, min_periods=max(5, win // 3)).std()
    return (series - mu) / sd.replace(0, np.nan)


def shift_daily_to_next_bar(daily: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """No lookahead: daily value available only from next calendar day, ffilled onto bars."""
    if daily.empty:
        return pd.Series(0.0, index=index)
    d = daily.copy().sort_index()
    d.index = d.index + pd.Timedelta(days=1)
    return d.reindex(index, method="ffill")


def news_burst_confirm_signal(df: pd.DataFrame, z_aligned: pd.Series, k: float, mode: str) -> pd.Series:
    """mode: donchian | ema. Entry when z>k and price confirm. Matches research news_lib."""
    burst = z_aligned > k
    if mode == "ema":
        ema = df["Close"].ewm(span=20, adjust=False).mean()
        confirm = df["Close"] > ema
    else:
        from indicators import signal_donchian_state

        confirm = signal_donchian_state(df).astype(bool)
    raw = (burst & confirm).astype(int)
    out = np.zeros(len(df), dtype=int)
    pos = 0
    rv = raw.values
    cv = confirm.fillna(False).astype(bool).values
    for i in range(len(df)):
        if pos == 0:
            if rv[i] == 1:
                pos = 1
        else:
            if not cv[i]:
                pos = 0
        out[i] = pos
    return pd.Series(out, index=df.index)


def apply_entry_mask(base_sig: pd.Series, entry_ok: pd.Series) -> pd.Series:
    """AND entry edges with entry_ok — matches research run_donch_with_entry_mask simple loop."""
    vals = base_sig.astype(int).values
    ok = entry_ok.reindex(base_sig.index).fillna(False).astype(bool).values
    simple = vals.copy()
    in_pos = False
    for i in range(1, len(vals)):
        if not in_pos:
            if vals[i] == 1 and vals[i - 1] == 0 and ok[i]:
                in_pos = True
                simple[i] = 1
            else:
                simple[i] = 0
        else:
            if vals[i] == 0:
                in_pos = False
                simple[i] = 0
            else:
                simple[i] = 1
    return pd.Series(simple, index=base_sig.index)


def z_series_from_vol_tone(vol: pd.Series, tone: pd.Series, field: str) -> pd.Series:
    field = (field or "vol").strip().lower()
    if field == "vol":
        return gdelt_z(vol) if len(vol) else pd.Series(dtype=float)
    if field == "tone":
        return gdelt_z(tone) if len(tone) else pd.Series(dtype=float)
    zv = gdelt_z(vol) if len(vol) else None
    zt = gdelt_z(tone) if len(tone) else None
    if zv is not None and zt is not None and len(zv) and len(zt):
        return zv.combine(zt, max, fill_value=np.nan)
    if zv is not None and len(zv):
        return zv
    if zt is not None and len(zt):
        return zt
    return pd.Series(dtype=float)
