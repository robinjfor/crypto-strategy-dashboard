"""Fear & Greed index (alternative.me) for donchian_fear_greed filter."""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("trader.fear_greed")
FG_URL = "https://api.alternative.me/fng/?limit=0&format=json"
_CACHE: dict = {"ts": 0.0, "series": None}
_CACHE_TTL = 3600


def fetch_fear_greed(force: bool = False) -> pd.Series | None:
    """Return daily UTC-normalized FG series, or None if unavailable."""
    now = time.time()
    if not force and _CACHE["series"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["series"]
    try:
        with urllib.request.urlopen(FG_URL, timeout=20) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode())
        rows = []
        for d in raw.get("data") or []:
            ts = pd.to_datetime(int(d["timestamp"]), unit="s", utc=True).normalize()
            rows.append((ts, float(d["value"])))
        if not rows:
            return None
        s = pd.Series(dict(rows)).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        _CACHE["ts"] = now
        _CACHE["series"] = s
        return s
    except Exception as e:  # noqa: BLE001
        log.warning("fear_greed_fetch_fail err=%s", e)
        return _CACHE["series"]


def apply_fg_filter(sig: pd.Series, fg: pd.Series | None, mode: str) -> pd.Series:
    """mode: gt25 | lt75 | mid25_75 | none. If fg unavailable → all entries blocked (sig→0)."""
    mode = (mode or "none").strip().lower()
    if mode in ("", "none"):
        return sig.astype(int)
    if fg is None or fg.empty:
        # Emily: no new entries if FG unavailable
        return pd.Series(np.zeros(len(sig), dtype=int), index=sig.index)
    dates = pd.DatetimeIndex(sig.index.tz_convert("UTC") if sig.index.tz else sig.index).normalize()
    vals = fg.reindex(dates).ffill().fillna(50).values
    ok = np.ones(len(sig), dtype=bool)
    if mode in ("gt25", "fggt25"):
        ok = vals > 25
    elif mode in ("lt75", "fglt75"):
        ok = vals < 75
    elif mode in ("mid25_75", "fgmid25_75", "mid"):
        ok = (vals > 25) & (vals < 75)
    else:
        log.warning("unknown_fg_mode mode=%s — blocking entries", mode)
        ok = np.zeros(len(sig), dtype=bool)
    out = sig.astype(int).values.copy()
    out[~ok] = 0
    return pd.Series(out, index=sig.index)
