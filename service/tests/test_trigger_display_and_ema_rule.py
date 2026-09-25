"""EMA entry rule parity with backtest (edge of ema_bull & BTC regime, per bar)
and per-family trigger fields (EMA values, LS bands, allowed direction)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategy import evaluate_ema_slot, evaluate_long_short_slot  # noqa: E402
from indicators import add_ema  # noqa: E402

EMA_SLOT = {"id": "sat_sol_ema_1d", "symbol": "SOLUSDT", "tf": "1d", "family": "ema_cross_atr",
            "ema_fast": 12, "ema_slow": 26, "stop_atr_mult": 2.0, "trail_atr_mult": 3.0,
            "atr_mode": "sma", "btc_regime": True, "quote_usdt": 750.0, "armed": True}
LS_SLOT = {"id": "ls_arb_4h", "symbol": "ARBUSDT", "tf": "4h", "family": "ls_donch_btc_regime_perp",
           "donch_n": 20, "stop_atr_mult": 2.0, "trail_atr_mult": 3.0, "atr_mode": "sma",
           "btc_regime": True, "leverage": 1.5, "quote_usdt": 750.0, "armed": True}


def _df(close, freq="1D", start="2025-01-01"):
    idx = pd.date_range(start, periods=len(close), freq=freq, tz="UTC")
    c = pd.Series(np.asarray(close, dtype=float), index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1.0}, index=idx)


def _btc_series(index_daily_open, bull):
    """Regime indexed by daily CLOSE time, as btc_daily_regime_series returns."""
    return pd.Series(np.asarray(bull, dtype=int), index=index_daily_open + pd.Timedelta(days=1))


def _backtest_entries(df, bull):
    """Reference: engine.signal_ema & apply_btc_regime → 0→1 edges (run_donchian_engine)."""
    ind = add_ema(df, (12, 26))
    sig = (ind["ema_12"] > ind["ema_26"]).astype(int)
    sig = (sig & pd.Series(bull, index=df.index).astype(int)).astype(int)
    return set(sig.index[(sig.diff() == 1) & (sig == 1)])


def _runner_entries(df, bull):
    series = _btc_series(df.index, bull)
    out = set()
    for i in range(40, len(df)):
        sub = df.iloc[: i + 1]
        slot = {**EMA_SLOT, "_btc_series": series}
        r = evaluate_ema_slot(slot, sub, None, {"_btc_regime_on": bool(bull[i])})
        if r.get("action") == "enter":
            out.add(sub.index[-1])
    return out


def _path(n=260, seed=3):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))


def test_ema_entry_matches_backtest_edges_with_regime_flips():
    close = _path()
    df = _df(close)
    bull = np.array([1 if (i // 30) % 2 == 0 else 0 for i in range(len(df))])
    bt = {t for t in _backtest_entries(df, bull) if t >= df.index[40]}
    rn = _runner_entries(df, bull)
    assert bt, "fixture should contain entries"
    assert rn == bt


def test_already_bullish_does_not_enter_and_labels_wait_next_cross():
    close = list(np.linspace(100, 80, 60)) + list(np.linspace(80, 130, 40))
    df = _df(close)
    bull = np.ones(len(df), dtype=int)
    series = _btc_series(df.index, bull)
    r = evaluate_ema_slot({**EMA_SLOT, "_btc_series": series}, df, None, {"_btc_regime_on": True})
    assert r["ema_bull"] is True and r["ema_fast"] > r["ema_slow"]
    assert r["action"] == "armed" and r["reason"] == "ema_bull_wait_next_cross"
    assert r["ema_gap_pct"] > 0 and r["btc_regime_on"] is True
    assert r["last_cross_bar_ts"] is not None and r["entry_rule"] == "edge_only"


def test_regime_turning_on_while_bullish_is_an_entry_edge():
    close = list(np.linspace(100, 80, 60)) + list(np.linspace(80, 130, 40))
    df = _df(close)
    bull = np.zeros(len(df), dtype=int)
    bull[-1] = 1  # BTC regime flips on at the last bar while EMA already bullish
    series = _btc_series(df.index, bull)
    r = evaluate_ema_slot({**EMA_SLOT, "_btc_series": series}, df, None, {"_btc_regime_on": True})
    assert r["action"] == "enter"  # backtest edge of combined signal


def test_ls_bands_and_allowed_direction():
    pat = [100, 101, 102, 101, 100, 99, 98, 99]
    close = [pat[i % 8] for i in range(120)]
    df = _df(close, freq="4h")
    days = pd.date_range("2024-12-01", periods=80, freq="1D", tz="UTC")
    for bull, want in ((1, "long_only"), (0, "short_only")):
        series = _btc_series(days, np.full(len(days), bull))
        r = evaluate_long_short_slot({**LS_SLOT, "_btc_series": series}, df, None, {"_btc_regime_on": bool(bull)})
        assert r["allowed_direction"] == want
        assert r["donch_hi"] > r["mark"] > r["donch_lo"]
        assert r["dist_hi_pct"] > 0 > r["dist_lo_pct"]
        assert r["reason"] == "waiting_ls_breakout"


def test_status_trigger_fields_per_family():
    import api
    ema = api._trigger_fields({"family": "ema_cross_atr", "btc_regime": True, "ema_fast": 12, "ema_slow": 26},
                              {"ema_fast": 110.9, "ema_slow": 105.4, "ema_gap_pct": 5.2182, "ema_bull": True,
                               "btc_regime_on": True, "mark": 120})
    assert ema["trigger_kind"] == "ema_cross" and ema["ema_bull"] is True and ema["trigger"] is None
    ls = api._trigger_fields({"family": "ls_donch_btc_regime_perp"},
                             {"donch_hi": 0.2555, "donch_lo": 0.2104, "mark": 0.2261, "allowed_direction": "long_only"})
    assert ls["trigger_kind"] == "donchian_ls" and ls["allowed_direction"] == "long_only"
    assert round(ls["dist_hi_pct"], 1) == 13.0 and ls["dist_lo_pct"] < 0
    dc = api._trigger_fields({"family": "donchian_atr"}, {"donch_hi": 1.065, "donch_lo": 0.9, "mark": 0.977})
    assert dc["trigger_kind"] == "donchian_hi" and dc["dist_hi_pct"] > 0
