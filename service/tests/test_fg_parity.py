"""Fear/greed signal parity vs unified-3y high_return ext_engine semantics."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indicators import add_donch_atr, signal_donchian_state  # noqa: E402
from fear_greed import apply_fg_filter, _CACHE  # noqa: E402


def _synth(n=160, seed=3):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.02, n)
    close = 100 * np.cumprod(1 + rets)
    high = close * (1 + rng.uniform(0, 0.01, n))
    low = close * (1 - rng.uniform(0, 0.01, n))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def test_fg_filter_matches_ext_engine_semantics():
    df = _synth()
    ind = add_donch_atr(df, 20, atr_mode="wilder").dropna(subset=["donch_hi", "donch_lo"])
    raw = signal_donchian_state(ind)
    days = pd.DatetimeIndex(ind.index.tz_convert("UTC")).normalize().unique()
    rng = np.random.default_rng(1)
    fg = pd.Series(rng.integers(5, 95, size=len(days)).astype(float), index=days)
    for mode in ("gt25", "lt75", "mid25_75"):
        dates = pd.DatetimeIndex(raw.index.tz_convert("UTC")).normalize()
        vals = fg.reindex(dates).ffill().fillna(50).values
        ok = np.ones(len(raw), dtype=bool)
        if mode == "gt25":
            ok = vals > 25
        elif mode == "lt75":
            ok = vals < 75
        else:
            ok = (vals > 25) & (vals < 75)
        want = raw.astype(int).values.copy()
        want[~ok] = 0
        got = apply_fg_filter(raw, fg, mode).astype(int).values
        assert (want == got).mean() > 0.99, mode


def test_fg_unavailable_blocks_entries():
    df = _synth(40)
    sig = pd.Series(1, index=df.index)
    assert int(apply_fg_filter(sig, None, "gt25").sum()) == 0


def test_fear_greed_slot_sets_futures_venue_when_levered():
    from strategy import evaluate_fear_greed_slot

    df = _synth(200)
    slot = {
        "id": "fg_t",
        "symbol": "OPUSDT",
        "tf": "4h",
        "donch_n": 20,
        "stop_atr_mult": 1.5,
        "trail_atr_mult": 1.5,
        "quote_usdt": 1000,
        "fg_mode": "gt25",
        "leverage": 1.5,
        "armed": True,
        "atr_mode": "wilder",
    }
    days = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
    _CACHE["series"] = pd.Series(50.0, index=days)
    _CACHE["ts"] = 1e18
    r = evaluate_fear_greed_slot(slot, df, None, {})
    assert r.get("venue") == "futures"
    assert float(r.get("leverage") or 0) == 1.5
    assert r.get("family") == "donchian_fear_greed"
