"""Next-bar-open parity (re-audit item 8).

Live decides on the closed bar and sends a market order, so the realistic fill is
the NEXT bar's open. Replays the real live evaluator (evaluate_donchian_slot) with
fills at next open, and compares trade-by-trade with the analyst's next-open
reference (strategy-unified-3y/lookahead_fix/live_reval/reval_fill.run_next_open,
vendored in reference_engine.run_next_open)."""
from __future__ import annotations

import pandas as pd
import pytest

import reference_engine as ref
from strategy import evaluate_donchian_slot
from test_donchian_live_backtest_parity import CASES, WINDOW, _load


def _backtest_next_open(df, p):
    ind = ref.add_donch(df, p["donch_n"], atr_mode=p["atr"])
    sig = ref.signal_donchian(ind)
    res = ref.run_next_open(ind, sig, p["stop"], p["trail"], p["mh"], reset_below_hi=p["reset"])
    return [(pd.Timestamp(t["entry"]), pd.Timestamp(t["exit"]), t["reason"]) for t in res.trades if t["reason"] != "eod_flat"]


def _live_next_open(df, p, tf):
    slot = {"id": "t", "symbol": "X", "tf": tf, "donch_n": p["donch_n"], "atr_mode": p["atr"],
            "stop_atr_mult": p["stop"], "trail_atr_mult": p["trail"], "max_hold_bars": p["mh"],
            "require_reset_below_hi": p["reset"], "quote_usdt": 750.0, "armed": True}
    meta: dict = {}
    pos = None
    trades = []
    idx = df.index
    for t in range(WINDOW, len(df)):  # need bar t (next) to fill
        win = df.iloc[t - WINDOW:t]
        nxt_ts, nxt_open = idx[t], float(df["Open"].iloc[t])
        r = evaluate_donchian_slot(slot, win, pos, meta)
        if pos is None and r.get("action") == "enter":
            # live records the signal bar as entry_bar_ts; fill price = next open
            pos = {"status": "FILLED", "entry": nxt_open * (1 + ref.COST), "entry_bar_ts": r["bar_ts"],
                   "fill_ts": nxt_ts}
            meta["last_acted_bar_ts"] = r["bar_ts"]
        elif pos is not None and r.get("action") == "exit":
            reason = {"stop": "stop_loss", "donch_lo": "signal_exit", "max_hold": "max_hold"}[r["reason"]]
            # exchange stop fills intrabar on its bar; close-based exits fill next open
            ex_ts = pd.Timestamp(r["exit_bar_ts"]) if reason == "stop_loss" else nxt_ts
            trades.append((pd.Timestamp(pos["fill_ts"]), ex_ts, reason))
            meta["last_exit_bar_ts"] = r["exit_bar_ts"]
            meta["last_acted_bar_ts"] = r["exit_bar_ts"]
            pos = None
    return trades


# Known gap (re-audit 2026-10-08, NOT changed in live — needs Emily's call):
# live counts max_hold from the signal bar (entry_bar_ts), the next-open backtest
# from the fill bar, so under next-open fills live's max_hold exit is 1 bar early.
# Only C1 hits max_hold in the fixture window.
KNOWN_MAX_HOLD_GAP = {"C1_ARB_4h"}


@pytest.mark.parametrize("case", [
    pytest.param(c, marks=pytest.mark.xfail(strict=True, reason="live max_hold counts from signal bar (1 bar early vs next-open)"))
    if c in KNOWN_MAX_HOLD_GAP else c for c in CASES])
def test_live_matches_next_open_backtest(case):
    name, tf, p = CASES[case]
    df = _load(name)
    start = df.index[WINDOW]
    bt = [x for x in _backtest_next_open(df, p) if x[0] > start]
    lv = _live_next_open(df, p, tf)
    if bt:
        lv = [x for x in lv if x[0] >= bt[0][0]]
    assert len(bt) >= 3, f"{case}: too few trades ({len(bt)})"
    diffs = [(a, b) for a, b in zip(lv, bt) if a != b]
    assert lv == bt, f"{case}: live {len(lv)} vs next-open backtest {len(bt)}; {len(diffs)} diffs; first: " + str(
        diffs[0] if diffs else (lv[len(bt):len(bt)+1], bt[len(lv):len(lv)+1]))
