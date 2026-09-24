"""Parity: runner signals match strategy-unified-3y backtest engine."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ENG = Path("/workspace/strategy-unified-3y/code")
sys.path.insert(0, str(ENG))

from indicators import (  # noqa: E402
    add_donch_atr,
    add_ema,
    resolve_atr,
    signal_donchian_state,
    signal_donchian_ls,
    signal_ema,
)
from fear_greed import apply_fg_filter  # noqa: E402


def _synth(n=120, seed=7):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.02, n)
    close = 100 * np.cumprod(1 + rets)
    high = close * (1 + rng.uniform(0, 0.01, n))
    low = close * (1 - rng.uniform(0, 0.01, n))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def test_ema_signal_matches_engine():
    import engine as eng
    df = _synth()
    df_e = eng.add_donch(df, 20, atr_mode="sma")
    want = eng.signal_ema(df_e, 12, 26)
    got_df = add_ema(df, (12, 26))
    got_df["atr"] = resolve_atr(got_df, 14, "sma")
    got = signal_ema(got_df, 12, 26)
    # align on overlapping index after warmup
    a = want.dropna()
    b = got.reindex(a.index)
    assert (a.astype(int).values == b.astype(int).values).mean() > 0.99


def test_donchian_state_matches_engine():
    import engine as eng
    df = _synth()
    df_e = eng.add_donch(df, 20, atr_mode="wilder")
    want = eng.signal_donchian(df_e)
    got_df = add_donch_atr(df, 20, atr_mode="wilder")
    got = signal_donchian_state(got_df.dropna(subset=["donch_hi", "donch_lo"]))
    a = want.reindex(got.index).dropna()
    b = got.reindex(a.index)
    assert (a.astype(int).values == b.astype(int).values).mean() > 0.99


def test_donchian_ls_matches_reference_impl():
    """Inline reference matching high_return/ext_engine.signal_donchian_ls (avoid matplotlib import)."""
    df = _synth(200)
    got_df = add_donch_atr(df, 20, atr_mode="wilder").dropna(subset=["donch_hi", "donch_lo"])
    c = got_df["Close"].astype(float).values
    hi = got_df["donch_hi"].astype(float).values
    lo = got_df["donch_lo"].astype(float).values
    out = np.zeros(len(got_df), dtype=int)
    pos = 0
    for i in range(len(got_df)):
        if np.isnan(hi[i]) or np.isnan(lo[i]):
            out[i] = 0
            continue
        if c[i] > hi[i]:
            pos = 1
        elif c[i] < lo[i]:
            pos = -1
        out[i] = pos
    want = pd.Series(out, index=got_df.index)
    got = signal_donchian_ls(got_df)
    assert (want.values == got.astype(int).values).all()


def test_fg_filter_blocks_when_unavailable():
    df = _synth(40)
    sig = pd.Series(1, index=df.index)
    out = apply_fg_filter(sig, None, "gt25")
    assert int(out.sum()) == 0


def test_fg_filter_modes():
    idx = pd.date_range("2024-01-01", periods=5, freq="1d", tz="UTC")
    sig = pd.Series(1, index=idx)
    fg = pd.Series([10, 30, 50, 80, 90], index=idx)
    assert apply_fg_filter(sig, fg, "gt25").tolist() == [0, 1, 1, 1, 1]
    assert apply_fg_filter(sig, fg, "lt75").tolist() == [1, 1, 1, 0, 0]
    assert apply_fg_filter(sig, fg, "mid25_75").tolist() == [0, 1, 1, 0, 0]
