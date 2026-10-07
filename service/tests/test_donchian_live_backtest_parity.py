"""Live Donchian evaluator must reproduce the backtest trade-by-trade on real klines.

Replays evaluate_donchian_slot bar by bar on real Binance klines (fixtures from
strategy-unified-3y/data, closed bars) with a 1000-bar window like live, and
compares entries/exits with the reference backtest engine (vendored verbatim)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import reference_engine as ref  # noqa: E402
from strategy import evaluate_donchian_slot  # noqa: E402

WINDOW = 1000

CASES = {
    # slot params == config/allocation.json + catalog backtest params
    "C2_FIL_1h": ("FIL_1h.csv", "1h", dict(donch_n=55, stop=1.5, trail=2.5, mh=192, atr="wilder", reset=True)),
    "C5_DOT_4h": ("DOT_4h.csv", "4h", dict(donch_n=20, stop=1.5, trail=1.5, mh=36, atr="wilder", reset=True)),
    "C1_ARB_4h": ("ARB_4h.csv", "4h", dict(donch_n=20, stop=2.0, trail=3.0, mh=36, atr="sma", reset=False)),
    "C8_APT_1h": ("APT_1h.csv", "1h", dict(donch_n=55, stop=2.0, trail=3.0, mh=192, atr="sma", reset=False)),
}


def _load(name):
    df = pd.read_csv(HERE / "fixtures" / "klines" / name)
    df.index = pd.to_datetime(df.pop("ts"), utc=True)
    return df.astype(float)


def _backtest(df, p):
    ind = ref.add_donch(df, p["donch_n"], atr_mode=p["atr"])
    sig = ref.signal_donchian(ind)
    res = ref.run_donchian_engine(ind, sig, p["stop"], p["trail"], p["mh"], reset_below_hi=p["reset"])
    return [(pd.Timestamp(t["entry"]), pd.Timestamp(t["exit"]), t["reason"]) for t in res.trades if t["reason"] != "eod_flat"]


def _live(df, p, tf):
    slot = {"id": "t", "symbol": "X", "tf": tf, "donch_n": p["donch_n"], "atr_mode": p["atr"],
            "stop_atr_mult": p["stop"], "trail_atr_mult": p["trail"], "max_hold_bars": p["mh"],
            "require_reset_below_hi": p["reset"], "quote_usdt": 750.0, "armed": True}
    meta: dict = {}
    pos = None
    trades = []
    for t in range(WINDOW, len(df) + 1):
        win = df.iloc[t - WINDOW:t]
        r = evaluate_donchian_slot(slot, win, pos, meta)
        if pos is None and r.get("action") == "enter":
            pos = {"status": "FILLED", "entry": r["close"] * (1 + ref.COST), "entry_bar_ts": r["bar_ts"],
                   "fill_bar_ts": r["bar_ts"]}  # fill-at-close model: fill bar == signal bar
            meta["last_acted_bar_ts"] = r["bar_ts"]
        elif pos is not None and r.get("action") == "exit":
            reason = {"stop": "stop_loss", "donch_lo": "signal_exit", "max_hold": "max_hold"}[r["reason"]]
            trades.append((pd.Timestamp(pos["entry_bar_ts"]), pd.Timestamp(r["exit_bar_ts"]), reason))
            meta["last_exit_bar_ts"] = r["exit_bar_ts"]
            meta["last_acted_bar_ts"] = r["exit_bar_ts"]
            pos = None
    return trades


@pytest.mark.parametrize("case", list(CASES))
def test_live_matches_backtest_trade_by_trade(case):
    name, tf, p = CASES[case]
    df = _load(name)
    # Live starts flat at bar WINDOW; compare backtest trades entered after the
    # live sim's first flat-state sync (first backtest trade whose entry ≥ start).
    start = df.index[WINDOW - 1]
    bt = [x for x in _backtest(df, p) if x[0] > start]
    lv = _live(df, p, tf)
    # drop any live trade opened before the first backtest trade (warm-up sync)
    if bt:
        lv = [x for x in lv if x[0] >= bt[0][0]]
    assert len(bt) >= 3, f"{case}: too few trades to be meaningful ({len(bt)})"
    assert lv == bt, f"{case}: live {len(lv)} vs backtest {len(bt)}\nfirst diff: " + str(
        next(((a, b) for a, b in zip(lv, bt) if a != b), (lv[len(bt):len(bt)+1], bt[len(lv):len(lv)+1])))
