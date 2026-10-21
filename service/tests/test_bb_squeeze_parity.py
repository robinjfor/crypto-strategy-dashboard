"""Z family (bb_squeeze_breakout_perp) — LIVE evaluator vs the analyst's backtest, trade by trade.

Scope: Z1 WLD 4h, Z2 SUI 1d, Z3 ARB 4h, Z4 DOT 4h, Z5 ARB 1h (Z6 LDO 4h excluded: MaxDD −57.8% > 45%
limit and negative 3y return). Fixtures (fixtures/bb_squeeze/) are the analyst's own parity files
from rd_daily/2026-10-21/fixtures: bars_<code>.csv.gz (perp OHLC + indicator columns + funding rate
applied at each bar open) and trades_<code>.csv (every trade of the eng.bt backtest).

bb_squeeze.evaluate_bb_squeeze_slot is replayed bar by bar from the first backtest bar on a sliding
1500-bar window (the /fapi/v1/klines max the live runner fetches), i.e. using only the bars closed
at each decision; next-bar-open fills, exchange stop at min(open, stop), runner meta exactly as
main.apply_signal / reconcile set it. Every trade must match: signal bar, entry time/price, initial
stop, exit signal bar, exit time/price, reason, stop at exit, bars held, funding events/PnL, ret_pct."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bb_squeeze import (FAMILY, STOP_COST, bb_indicators, bb_state, evaluate_bb_squeeze_slot, initial_stop_from_fill,
                        leverage_cap, slot_from_spec, stop_basis_from_fill)
from indicators import sma_atr

FIX = Path(__file__).resolve().parent / "fixtures" / "bb_squeeze"
SPEC = {e["code"]: e for e in json.loads((FIX / "spec.json").read_text(encoding="utf-8"))}
SUMMARY = {e["code"]: e for e in json.loads((FIX / "parity_summary.json").read_text(encoding="utf-8"))}
CODES = ["Z1", "Z2", "Z3", "Z4", "Z5"]
WINDOW = 1500
COST = STOP_COST
_BARS: dict = {}


def _bars(code):
    if code not in _BARS:
        b = pd.read_csv(FIX / f"bars_{code}.csv.gz")
        b.index = pd.to_datetime(b.pop("open_time"), utc=True)
        _BARS[code] = b.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    return _BARS[code]


def _trades(code):
    return pd.read_csv(FIX / f"trades_{code}.csv")


def test_scope_is_z1_to_z5_only():
    assert sorted(SPEC) == CODES and "Z6" not in SPEC
    for c in CODES:
        assert SPEC[c]["status"] == "PENDING_EMILY" and SPEC[c]["side"] == "long_only"
        assert SPEC[c]["venue"] == "binance_usdtm_perp"


@pytest.mark.parametrize("code", CODES)
def test_slot_from_spec(code):
    s = slot_from_spec(SPEC[code])
    want = {"Z1": ("WLDUSDT", "4h", 20, 2.5, 0.2, 1.5, 3.5, 36), "Z2": ("SUIUSDT", "1d", 30, 1.5, 0.5, 2.0, 1.5, 40)}
    if code in want:
        assert (s["symbol"], s["tf"], s["bb_n"], s["bb_k"], s["sq_q"], s["stop_atr_mult"], s["trail_atr_mult"],
                s["max_hold_bars"]) == want[code]
    assert s["family"] == FAMILY and s["leverage"] == 1.0 and s["sq_lookback"] == 120 and s["sq_recent"] == 5
    assert s["max_hold_bars"] == {"1h": 192, "4h": 36, "1d": 40}[s["tf"]]


@pytest.mark.parametrize("code", CODES)
def test_indicator_columns_match_backtest(code):
    """ATR14(SMA), BB mid/up/bw, squeeze threshold/recent flag and state bar by bar (full history)."""
    s, b = slot_from_spec(SPEC[code]), _bars(code)
    ind = b[["Open", "High", "Low", "Close"]]
    np.testing.assert_allclose(sma_atr(ind, 14).fillna(0).values, b["atr14_sma"].fillna(0).values, rtol=1e-7, atol=1e-12)
    mid, up, sq, bw, thr = bb_indicators(ind["Close"], s["bb_n"], s["bb_k"], s["sq_q"])
    for got, col in ((mid, "bb_mid"), (up, "bb_up"), (bw, "bb_bw"), (thr, "sq_thr")):
        w = b[col].values.astype(float)
        assert (np.isnan(got) == np.isnan(w)).all(), col
        m = ~np.isnan(w)
        np.testing.assert_allclose(got[m], w[m], rtol=1e-9, atol=1e-12, err_msg=col)
    assert (sq == b["squeeze_recent"].astype(bool).values).all()
    st = bb_state(ind["Close"].values, mid, up, sq)
    assert (st == b["signal_state"].values).all()
    edge = np.r_[False, (st[1:] == 1) & (st[:-1] == 0)]
    w = b["in_backtest_window"].astype(bool).values  # analyst column only flags edges inside the 3y window
    assert (edge[w] == b["entry_edge"].astype(bool).values[w]).all()


def _funding(b, fill_i, exit_i, O, qty):
    """eng.bt: at each bar open with sh>0 and FR!=0, cash -= sh*open*FR (held: fill_i < k <= exit_i)."""
    fr = b["funding_applied_at_open"].values.astype(float)
    k = np.arange(fill_i + 1, exit_i + 1)
    k = k[fr[k] != 0.0]
    return int(len(k)), float(-np.sum(qty * O[k] * fr[k]))


def _live(code, runner_side_stop=False):
    slot, b = slot_from_spec(SPEC[code]), _bars(code)
    df = b[["Open", "High", "Low", "Close"]]
    idx, O = df.index, df["Open"].values
    s0 = int(np.argmax(b["in_backtest_window"].values))
    meta, pos, out, eq = {}, None, [], 10000.0

    def run(win, p):
        return evaluate_bb_squeeze_slot(slot, win, p, dict(meta))

    for t in range(s0 + 1, len(df)):  # decision on closed bar t-1, fill at bar t open
        win = df.iloc[max(0, t - WINDOW):t]
        r = run(win, pos)
        if pos is not None and r["action"] == "exit":
            stop_exit = r["reason"].startswith("stop")
            e_i = int(idx.get_loc(pd.Timestamp(r["exit_bar_ts"]))) if stop_exit else t
            raw = r["exit_ref"] if stop_exit else float(O[t])
            fill_px = raw * (1 - COST)
            n_f, f_pnl = _funding(b, pos["fill_i"], e_i, O, pos["qty"])
            pnl = pos["qty"] * (fill_px - pos["stop_basis"]) + f_pnl
            out.append(dict(signal_bar=pos["signal_bar"], entry_time=idx[pos["fill_i"]], entry_raw_price=pos["raw"],
                            entry_fill_price=pos["stop_basis"], initial_stop=r["initial_stop"],
                            exit_signal_bar=(pd.NaT if stop_exit else pd.Timestamp(r["exit_bar_ts"])),
                            exit_time=idx[e_i], exit_raw_price=raw, exit_fill_price=fill_px, exit_reason=r["reason"],
                            stop_at_exit=r["stop"], n_funding_events=n_f,
                            # analyst build.py bookkeeping: a stop on bar e counts bar e as held (e+1-fill);
                            # an open-filled exit at bar e does not (e-fill)
                            bars_held=e_i - pos["fill_i"] + (1 if stop_exit else 0),
                            funding_pnl=f_pnl, pnl=pnl, ret_pct=pnl / eq * 100.0, equity_after=eq + pnl))
            eq += pnl
            pos = None
            if runner_side_stop and stop_exit:
                # runner closes on the stop itself; main applies the attached same-bar flat decision
                meta.update(last_acted_bar_ts=r["exit_bar_ts"], last_exit_bar_ts=r["exit_bar_ts"], last_exit_reason=r["reason"])
                r = r["after_stop"]
            else:
                meta.update(last_acted_bar_ts=r["exit_bar_ts"], last_exit_bar_ts=r["exit_bar_ts"],
                            last_exit_reason=("stop" if stop_exit else r["reason"]))
                if stop_exit:  # exchange stop already filled -> reconcile, then runner evaluates this bar flat
                    r = run(win, None)
        if pos is None and r["action"] == "enter":
            raw = float(O[t])
            basis = stop_basis_from_fill(raw)
            assert initial_stop_from_fill(raw, slot["stop_atr_mult"], r["stop_atr"]) == pytest.approx(
                basis - slot["stop_atr_mult"] * r["stop_atr"])
            pos = {"status": "FILLED", "entry": raw, "stop_basis": basis, "entry_bar_ts": r["bar_ts"], "fill_i": t,
                   "raw": raw, "signal_bar": pd.Timestamp(r["bar_ts"]), "qty": eq / basis}
            meta.update(last_acted_bar_ts=r["bar_ts"], last_entry_bar_ts=r["bar_ts"])
    assert pos is None, f"{code}: live still in position at end of data"
    return out


def _cmp_frame(rows):
    d = pd.DataFrame(rows)
    for c in ("signal_bar", "entry_time", "exit_signal_bar", "exit_time"):
        d[c] = pd.to_datetime(d[c], utc=True)
    return d


def assert_trades_match(code, got):
    want = _trades(code)
    assert len(got) == len(want), (code, len(got), len(want))
    w = want.copy()
    for c in ("signal_bar", "entry_time", "exit_signal_bar", "exit_time"):
        w[c] = pd.to_datetime(w[c], utc=True)
    for c in ("signal_bar", "entry_time", "exit_time", "exit_reason", "n_funding_events", "bars_held"):
        bad = got[c].values != w[c].values
        assert not bad.any(), f"{code} {c}: first diff trade {int(np.argmax(bad)) + 1}: live {got[c].values[bad][0]} backtest {w[c].values[bad][0]}"
    assert (got["exit_signal_bar"].isna().values == w["exit_signal_bar"].isna().values).all()
    m = w["exit_signal_bar"].notna().values
    assert (got["exit_signal_bar"].values[m] == w["exit_signal_bar"].values[m]).all()
    for c, tol in (("entry_raw_price", 1e-12), ("entry_fill_price", 1e-10), ("initial_stop", 1e-9),
                   ("exit_raw_price", 1e-9), ("exit_fill_price", 1e-9), ("stop_at_exit", 1e-9),
                   ("funding_pnl", 1e-6), ("pnl", 1e-6), ("ret_pct", 1e-6), ("equity_after", 1e-8)):
        np.testing.assert_allclose(got[c].values, w[c].values.astype(float), rtol=tol, atol=1e-9, err_msg=f"{code} {c}")


@pytest.mark.parametrize("code", CODES)
def test_live_matches_backtest_trades(code):
    got = _cmp_frame(_live(code))
    assert_trades_match(code, got)
    assert len(got) == SUMMARY[code]["n_trades"]
    assert round(float(got["equity_after"].iloc[-1]), 2) == SUMMARY[code]["final"]
    assert got["exit_reason"].value_counts().to_dict() == SUMMARY[code]["exit_reasons"]


@pytest.mark.parametrize("code", ["Z2", "Z3"])
def test_live_runner_side_stop_same_trades(code):
    """Stops closed by the runner (exchange stop missing) give the same trades, incl. same-bar re-entry."""
    assert_trades_match(code, _cmp_frame(_live(code, runner_side_stop=True)))


def _live_hourly(code):
    """Hourly runner: the exchange stop fills INSIDE bar t; the runner reconciles and evaluates mid-bar
    (last closed bar still t-1). An edge on t-1 (position open then) must not enter."""
    slot, b = slot_from_spec(SPEC[code]), _bars(code)
    df = b[["Open", "High", "Low", "Close"]]
    idx, O, Lo = df.index, df["Open"].values, df["Low"].values
    s0 = int(np.argmax(b["in_backtest_window"].values))
    meta, pos, out, entered = {}, None, [], []

    def run(win, p):
        return evaluate_bb_squeeze_slot(slot, win, p, dict(meta))

    for t in range(s0 + 1, len(df)):
        win = df.iloc[max(0, t - WINDOW):t]
        r = run(win, pos)
        if pos is not None and r["action"] == "exit":
            assert not r["reason"].startswith("stop")
            out.append((idx[pos["fill_i"]], idx[t], r["reason"], round(float(O[t]) * (1 - COST), 10)))
            meta.update(last_acted_bar_ts=r["bar_ts"], last_exit_bar_ts=r["bar_ts"], last_exit_reason=r["reason"])
            pos = None
        elif pos is not None:
            pos["stop"] = r["stop"]
        if pos is None and r["action"] == "enter":
            raw = float(O[t])
            pos = {"status": "FILLED", "entry": raw, "stop_basis": stop_basis_from_fill(raw), "entry_bar_ts": r["bar_ts"],
                   "fill_i": t, "stop": initial_stop_from_fill(raw, slot["stop_atr_mult"], r["stop_atr"])}
            meta.update(last_acted_bar_ts=r["bar_ts"], last_entry_bar_ts=r["bar_ts"])
        if pos is not None and Lo[t] <= pos["stop"]:
            out.append((idx[pos["fill_i"]], idx[t], "stop", round(min(float(O[t]), pos["stop"]) * (1 - COST), 10)))
            pos = None
            bar = idx[t].isoformat()
            meta.update(last_acted_bar_ts=bar, last_exit_bar_ts=bar, last_exit_reason="stop")
            mid = run(win, None)
            if mid["action"] == "enter":
                entered.append(mid["bar_ts"])
    return out, entered


@pytest.mark.parametrize("code", ["Z1", "Z4"])
def test_hourly_runner_exchange_stop_no_stale_reentry(code):
    out, entered = _live_hourly(code)
    assert entered == [], f"{code}: stale re-entries {entered}"
    want = _trades(code)
    exp = [(pd.Timestamp(a), pd.Timestamp(x), ("stop" if r.startswith("stop") else r), round(xf, 10))
           for a, x, r, xf in zip(want["entry_time"], want["exit_time"], want["exit_reason"], want["exit_fill_price"])]
    assert len(out) == len(exp)
    for g, w in zip(out, exp):
        assert g[:3] == w[:3], (code, g, w)
        assert g[3] == pytest.approx(w[3], rel=1e-8)


def test_conflict_rule_blocks_long_when_short_open():
    slot, b = slot_from_spec(SPEC["Z4"]), _bars("Z4")
    first = _trades("Z4").iloc[0]
    t = int(b.index.get_loc(pd.Timestamp(first["entry_time"])))
    win = b[["Open", "High", "Low", "Close"]].iloc[t - WINDOW:t]
    r = evaluate_bb_squeeze_slot(slot, win, None, {})
    assert (r["action"], r["side"], r["venue"]) == ("enter", "LONG", "futures")
    r = evaluate_bb_squeeze_slot(slot, win, None, {"_opposite_open": True})
    assert (r["action"], r["reason"]) == ("skip", "opposite_position_open")


def test_leverage_locked_1x():
    assert leverage_cap() == 1.0 and leverage_cap(-80) == 1.0
    slot, b = slot_from_spec(SPEC["Z1"]), _bars("Z1")
    r = evaluate_bb_squeeze_slot({**slot, "leverage": 5}, b[["Open", "High", "Low", "Close"]].iloc[-WINDOW:], None, {})
    assert r["leverage"] == 1.0 and r["leverage_cap"] == 1.0
