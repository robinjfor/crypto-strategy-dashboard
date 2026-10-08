"""F1 (ema12_26_atr_btcRegime SOL 1d) live evaluator vs next-open backtest (資金控管 REAUDIT2 E1).

Replays evaluate_ema_slot bar by bar on real SOL/BTC daily klines with next-bar-open
fills (stops fill intrabar at min(open, stop)) and compares entry/exit time,
reason and prices with reference_engine.run_next_open (analyst's reval_fill).
Key case: 2025-10-10 — stop and EMA death cross on the same bar; the backtest
exits at the stop (209.99), the old live order gave signal_exit at the next open."""
from __future__ import annotations

import pandas as pd

import reference_engine as ref
from strategy import evaluate_ema_slot
from test_donchian_live_backtest_parity import _load

WINDOW = 250  # live fetch limit for EMA slots: max(250, slow + 80)
P = dict(fast=12, slow=26, stop=2.0, trail=3.0, mh=10000)  # config/allocation.json sat_sol_ema_1d


def _btc_regime_series(btc: pd.DataFrame) -> pd.Series:
    """Same as strategy.btc_daily_regime_series: Close > SMA200, keyed by daily close time."""
    sma = btc["Close"].rolling(200).mean()
    bull = (btc["Close"] > sma).astype(int)[sma.notna()]
    bull.index = bull.index + pd.Timedelta(days=1)
    return bull


def _backtest(sol, btc):
    ind = ref.add_donch(sol, 20, atr_mode="sma")
    sig = ref.apply_btc_regime(ref.signal_ema(ind, P["fast"], P["slow"]), ref.add_donch(btc, 20, atr_mode="sma"))
    res = ref.run_next_open(ind, sig, P["stop"], P["trail"], P["mh"])
    return [(pd.Timestamp(t["entry"]), pd.Timestamp(t["exit"]), t["reason"], round(t["entry_px"], 5), round(t["exit_px"], 5))
            for t in res.trades if t["reason"] != "eod_flat"]


def _live(sol, btc):
    slot = {"id": "sat_sol_ema_1d", "symbol": "SOLUSDT", "tf": "1d", "family": "ema_cross_atr",
            "ema_fast": P["fast"], "ema_slow": P["slow"], "atr_mode": "sma", "btc_regime": True,
            "stop_atr_mult": P["stop"], "trail_atr_mult": P["trail"], "max_hold_bars": P["mh"],
            "quote_usdt": 750.0, "armed": True, "_btc_series": _btc_regime_series(btc)}
    meta: dict = {}
    pos = None
    trades = []
    for t in range(WINDOW, len(sol)):
        win = sol.iloc[t - WINDOW:t]
        nxt_ts, nxt_open = sol.index[t], float(sol["Open"].iloc[t])
        r = evaluate_ema_slot(slot, win, pos, meta)
        if pos is None and r.get("action") == "enter":
            pos = {"status": "FILLED", "entry": nxt_open * (1 + ref.COST), "entry_bar_ts": r["bar_ts"], "fill_ts": nxt_ts}
            meta["last_acted_bar_ts"] = r["bar_ts"]
        elif pos is not None and r.get("action") == "exit":
            reason = {"stop": "stop_loss", "signal_exit": "signal_exit", "max_hold": "max_hold"}[r["reason"]]
            if reason == "stop_loss":
                ex_ts, ex_px = pd.Timestamp(r["exit_bar_ts"]), float(r["exit_ref"]) * (1 - ref.COST)
            else:
                ex_ts, ex_px = nxt_ts, nxt_open * (1 - ref.COST)
            trades.append((pd.Timestamp(pos["fill_ts"]), ex_ts, reason, round(pos["entry"], 5), round(ex_px, 5)))
            meta["last_acted_bar_ts"] = r["exit_bar_ts"]
            pos = None
    return trades


def _run():
    sol, btc = _load("SOL_1d.csv"), _load("BTC_1d.csv")
    start = sol.index[WINDOW]
    bt = [x for x in _backtest(sol, btc) if x[0] > start]
    lv = _live(sol, btc)
    if bt:
        lv = [x for x in lv if x[0] >= bt[0][0]]
    return bt, lv


def test_f1_live_matches_next_open_backtest():
    bt, lv = _run()
    assert len(bt) >= 3, f"too few trades ({len(bt)})"
    diffs = [(a, b) for a, b in zip(lv, bt) if a != b]
    assert lv == bt, f"live {len(lv)} vs backtest {len(bt)}; first diff: {diffs[:1] or (lv[len(bt):], bt[len(lv):])}"


def test_f1_2025_10_10_stop_beats_death_cross():
    bt, lv = _run()
    want = [x for x in bt if x[1] == pd.Timestamp("2025-10-10", tz="UTC")]
    assert want and want[0][2] == "stop_loss", want
    assert abs(want[0][4] - 209.99009) < 0.01, want  # REAUDIT2: backtest stop 209.99
    got = [x for x in lv if x[0] == want[0][0]]
    assert got == want, f"live {got} vs backtest {want}"
