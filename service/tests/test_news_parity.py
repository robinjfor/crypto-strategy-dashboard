"""Parity: runner news_lib matches strategy-unified-3y news_driven news_lib on historical GDELT."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_lib import (  # noqa: E402
    parse_gdelt_timeline,
    gdelt_z,
    shift_daily_to_next_bar,
    news_burst_confirm_signal,
    apply_entry_mask,
    z_series_from_vol_tone,
)
from indicators import add_donch_atr, signal_donchian_state  # noqa: E402

SEED = ROOT / "news_seed"


def _load_research():
    """Load research news_lib with temporary stubs; restore sys.modules after."""
    import types

    spec = importlib.util.spec_from_file_location(
        "research_news_lib",
        "/workspace/strategy-unified-3y/news_driven/code/news_lib.py",
    )
    saved = {k: sys.modules.get(k) for k in ("engine", "run_unified", "research_news_lib")}

    eng = types.ModuleType("engine")
    eng.INIT = 10000
    eng.COST = 0
    eng.PERIOD_START = None
    eng.PERIOD_END = None
    eng.RET_1Y_START = None
    eng.BARS_PER_YEAR = {}

    def _noop(*a, **k):
        raise RuntimeError("stub")

    for name in (
        "load_or_fetch",
        "drop_incomplete",
        "add_donch",
        "run_donchian_engine",
        "metrics_from_equity",
        "oos_fixed_params",
        "ret_between",
        "now_taipei",
    ):
        setattr(eng, name, _noop)
    eng.signal_donchian = signal_donchian_state
    sys.modules["engine"] = eng

    ru = types.ModuleType("run_unified")
    ru.normalize_equity_from = _noop
    ru.bh_for_df = _noop
    ru.TARGETS = {}
    sys.modules["run_unified"] = ru

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod


def _synth(n=180, seed=5):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-06-01", periods=n, freq="4h", tz="UTC")
    close = 100 * np.cumprod(1 + rng.normal(0, 0.015, n))
    high = close * (1 + rng.uniform(0, 0.01, n))
    low = close * (1 - rng.uniform(0, 0.01, n))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def test_parse_and_z_match_research_btc_vol():
    rlib = _load_research()
    path = SEED / "gdelt_BTC_timelinevol.json"
    ours = parse_gdelt_timeline(path)
    theirs = rlib.parse_gdelt_timeline(path)
    assert len(ours) > 100
    assert (ours.index == theirs.index).all()
    assert np.allclose(ours.values, theirs.values, equal_nan=True)
    oz, tz = gdelt_z(ours), rlib.gdelt_z(theirs)
    common = oz.dropna().index.intersection(tz.dropna().index)
    assert len(common) > 50
    assert np.allclose(oz.loc[common].values, tz.loc[common].values, equal_nan=True, atol=1e-9)


def test_shift_daily_no_lookahead():
    daily = pd.Series(
        [1.0, 2.0, 3.0],
        index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
    )
    bars = pd.date_range("2024-01-01", periods=6, freq="12h", tz="UTC")
    al = shift_daily_to_next_bar(daily, bars)
    # value of Jan1 appears starting Jan2
    assert float(al.loc[pd.Timestamp("2024-01-02", tz="UTC")]) == 1.0
    # Jan1 00:00 should not have Jan1 daily yet (NaN or 0 from empty fill)
    v0 = al.loc[bars[0]]
    assert pd.isna(v0) or float(v0) == 0.0


def test_burst_ema_parity_and_filter_mask():
    rlib = _load_research()
    df = add_donch_atr(_synth(), 20, atr_mode="wilder").dropna(subset=["donch_hi", "donch_lo", "atr"])
    vol = parse_gdelt_timeline(SEED / "gdelt_BTC_timelinevol.json")
    z_al = shift_daily_to_next_bar(gdelt_z(vol), df.index).fillna(0)
    ours = news_burst_confirm_signal(df, z_al, 1.5, "ema")
    theirs = rlib.news_burst_confirm_signal(df, z_al, 1.5, "ema")
    agree = float((ours.values == theirs.values).mean())
    assert agree >= 0.99, agree

    raw = signal_donchian_state(df)
    entry_ok = z_al > 1.5
    masked = apply_entry_mask(raw, entry_ok)
    for i in range(1, len(masked)):
        if int(masked.iloc[i]) == 1 and int(masked.iloc[i - 1]) == 0:
            assert bool(entry_ok.iloc[i])


def test_news_pipeline_fail_closed(tmp_path):
    from news_ingest import NewsStore, news_pipeline_ok, ensure_seeded, load_meta

    store = NewsStore(bucket="", local_dir=tmp_path / "news")
    meta = ensure_seeded(store)
    assert meta.get("ok") is True
    assert news_pipeline_ok(store) is True
    meta = load_meta(store)
    meta["last_ok_at"] = "2020-01-01T00:00:00Z"
    meta["ok"] = True
    store.save_json("trader/news/meta.json", meta)
    assert news_pipeline_ok(store) is False


def test_fil_seed_z():
    s = parse_gdelt_timeline(SEED / "gdelt_FIL_timelinevol.json")
    assert len(s) > 100
    z = z_series_from_vol_tone(s, pd.Series(dtype=float), "vol")
    assert float(z.dropna().abs().max()) > 0
