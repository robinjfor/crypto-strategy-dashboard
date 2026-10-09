"""S family (short_trend_perp) — live evaluator vs the analyst's walk-forward spec, trade by trade.

Only the 4 rows that pass the analyst's walk-forward re-check (s_family_spec wf PASS: S2 ARB 4h,
S3 DOT 4h, S5 APT 4h, S8 GALA 6h) are built, with the spec.json WF params (not the catalog's).
Fixtures (fixtures/short_trend/spec/) are the analyst's own parity files: bars_<code>.csv.gz (perp
OHLC + indicator columns) and trades_<code>.csv (every trade). The live evaluator
short_trend.evaluate_short_trend_slot is replayed bar by bar from 2023-09-24 on a sliding
1500-bar window (the /fapi/v1/klines max), next-bar-open fills, exchange stop at max(open, stop),
runner meta exactly as main.apply_signal / reconcile set it, and must reproduce every trade:
signal bar, entry time/price, initial stop, exit time/price, reason, stop at exit, bars held,
funding events and ret_pct."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import reference_short_trend as R
from indicators import sma_atr
from short_trend import (STOP_COST, evaluate_short_trend_slot, filter_on_bars, funding_avg_on_bars,
                         leverage_cap, slot_from_spec, stop_basis_from_fill)

SPEC_DIR = Path(__file__).resolve().parent / "fixtures" / "short_trend" / "spec"
SPEC = {e["code"]: e for e in json.loads((SPEC_DIR / "spec.json").read_text(encoding="utf-8"))}
CODES = ["S2", "S3", "S5", "S8"]
WINDOW = 1500
COST = STOP_COST


def _bars(code):
    b = pd.read_csv(SPEC_DIR / f"bars_{code}.csv.gz")
    b.index = pd.to_datetime(b.pop("open_time"), utc=True)
    return b.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})


def _trades(code):
    t = pd.read_csv(SPEC_DIR / f"trades_{code}.csv")
    return t[t["exit_reason"] != "end_of_data"].reset_index(drop=True)


def _meta(slot):
    coin = slot["symbol"].replace("USDT", "")
    return {"_funding": R.load_funding(coin), "_coin_daily": R.load(coin, "1d"), "_btc_daily": R.load("BTC", "1d")}


def test_spec_rows_are_exactly_the_wf_passers():
    assert sorted(SPEC) == CODES
    for c in CODES:
        assert SPEC[c]["walk_forward"]["second_half_cagr"] > 0 and SPEC[c]["status"] == "PENDING_EMILY"


@pytest.mark.parametrize("code", CODES)
def test_slot_from_spec(code):
    s = slot_from_spec(SPEC[code])
    want = {"S2": ("ARBUSDT", "4h", 9, 21, 2.0, 5.0, "fpos", 60), "S3": ("DOTUSDT", "4h", 9, 21, 2.0, 4.0, "none", 60),
            "S5": ("APTUSDT", "4h", 12, 26, 2.0, 2.0, "coin50", 60), "S8": ("GALAUSDT", "6h", 12, 26, 3.0, 5.0, "none", 40)}[code]
    assert (s["symbol"], s["tf"], s["ema_fast"], s["ema_slow"], s["stop_atr_mult"], s["trail_atr_mult"], s["filter"],
            s["max_hold_bars"]) == want
    assert s["leverage"] == 1.0 and s["quote_usdt"] == 750.0


@pytest.mark.parametrize("code", CODES)
def test_bar_columns_match_spec(code):
    """Indicators and filters bar by bar vs the analyst's bars file (full history, as the engine)."""
    slot, b = slot_from_spec(SPEC[code]), _bars(code)
    ind = b[["Open", "High", "Low", "Close"]]
    np.testing.assert_allclose(sma_atr(ind, 14).fillna(0).values, b["atr14_sma"].values, rtol=1e-7, atol=1e-12)
    for n in (slot["ema_fast"], slot["ema_slow"]):
        np.testing.assert_allclose(ind["Close"].ewm(span=n, adjust=False).mean().values, b[f"ema{n}"].values, rtol=1e-7)
    meta = _meta(slot)
    np.testing.assert_allclose(funding_avg_on_bars(meta["_funding"], ind.index), b["funding_avg9_asof_open"].values,
                               rtol=1e-6, atol=1e-12)
    F = filter_on_bars(slot["filter"], ind.index, slot["tf"], meta)
    assert (F == b["filter_ok"].astype(bool).values).all()
    if slot["filter"] == "coin50":
        assert (F == b["coin_below_sma50_prevday"].astype(bool).values).all()


def _funding_events(slot, idx, fill_i, exit_i, O):
    """Spec funding rule: settlement -> first bar with open >= t; charged if held at that bar's open."""
    f = _meta(slot)["_funding"]
    t = pd.DatetimeIndex(f.index).floor("min")
    k = idx.searchsorted(t, side="left")
    m = (k > fill_i) & (k <= exit_i)
    return int(m.sum()), float(np.sum(O[k[m]] * f.values[m]))


def _live(code, runner_side_stop=False):
    slot, b = slot_from_spec(SPEC[code]), _bars(code)
    df = b[["Open", "High", "Low", "Close"]]
    idx, O = df.index, df["Open"].values
    s0 = int(np.argmax(b["in_backtest_window"].values))
    base, meta, pos, out = _meta(slot), {}, None, []

    def run(win, p):
        return evaluate_short_trend_slot(slot, win, p, {**meta, **base})

    for t in range(s0 + 1, len(df)):  # decision on closed bar t-1, fill at bar t open
        win = df.iloc[max(0, t - WINDOW):t]
        r = run(win, pos)
        if pos is not None and r["action"] == "exit":
            stop_exit = r["reason"].startswith("stop")
            e_i = int(idx.get_loc(pd.Timestamp(r["exit_bar_ts"]))) if stop_exit else t
            raw = r["exit_ref"] if stop_exit else float(O[t])
            n_f, f_sum = _funding_events(slot, idx, pos["fill_i"], e_i, O)
            fill_px = raw * (1 + COST)
            out.append(dict(signal_bar=pos["signal_bar"], entry_time=idx[pos["fill_i"]], entry_raw_price=pos["raw"],
                            entry_fill_price=pos["stop_basis"], initial_stop=r["initial_stop"],
                            exit_signal_bar=(pd.NaT if stop_exit else pd.Timestamp(r["exit_bar_ts"])),
                            exit_time=idx[e_i], exit_raw_price=raw, exit_fill_price=fill_px, exit_reason=r["reason"],
                            stop_at_exit=r["stop"], bars_held=e_i - pos["fill_i"],
                            n_funding_events=n_f,
                            ret_pct=((pos["stop_basis"] - fill_px) + f_sum) / pos["stop_basis"] * 100.0))
            pos = None
            # runner bookkeeping: reconcile records an exchange stop as reason "stop" on its bar;
            # apply_signal records a close-based exit on the decision bar
            if runner_side_stop and stop_exit:
                # runner closes on the stop itself: apply_signal records the evaluator's reason, then
                # main applies the attached same-bar flat decision (after_stop)
                meta.update(last_acted_bar_ts=r["exit_bar_ts"], last_exit_reason=r["reason"])
                r = r["after_stop"]
            else:
                meta.update(last_acted_bar_ts=r["exit_bar_ts"], last_exit_reason=("stop" if stop_exit else r["reason"]))
                if stop_exit:  # exchange stop already filled -> reconcile, then runner evaluates this bar flat
                    r = run(win, None)
        if pos is None and r["action"] == "enter":
            raw = float(O[t])
            pos = {"status": "FILLED", "entry": raw, "stop_basis": stop_basis_from_fill(raw),
                   "entry_bar_ts": r["bar_ts"], "fill_i": t, "raw": raw, "signal_bar": pd.Timestamp(r["bar_ts"])}
            meta.update(last_acted_bar_ts=r["bar_ts"], last_entry_bar_ts=r["bar_ts"])
    return out


def _cmp_frame(rows):
    d = pd.DataFrame(rows)
    for c in ("signal_bar", "entry_time", "exit_signal_bar", "exit_time"):
        d[c] = pd.to_datetime(d[c], utc=True)
    return d


@pytest.mark.parametrize("code,runner_side_stop", [(c, False) for c in CODES] + [("S2", True), ("S5", True)])
def test_live_matches_spec_trades(code, runner_side_stop):
    """runner_side_stop: stops closed by the runner (not the exchange) must give the same trades,
    including a re-entry on the stop bar itself (S2/S5 have the most stop exits)."""
    want = _trades(code)
    got = _cmp_frame(_live(code, runner_side_stop))
    assert len(got) == len(want), (code, len(got), len(want))
    w = want.copy()
    for c in ("signal_bar", "entry_time", "exit_signal_bar", "exit_time"):
        w[c] = pd.to_datetime(w[c], utc=True)
    for c in ("signal_bar", "entry_time", "exit_time", "exit_reason", "n_funding_events"):
        bad = got[c].values != w[c].values
        assert not bad.any(), f"{code} {c}: first diff trade {int(np.argmax(bad)) + 1}: live {got[c].values[bad][0]} spec {w[c].values[bad][0]}"
    sig_ok = got["exit_signal_bar"].isna().values == w["exit_signal_bar"].isna().values
    assert sig_ok.all()
    m = w["exit_signal_bar"].notna().values
    assert (got["exit_signal_bar"].values[m] == w["exit_signal_bar"].values[m]).all()
    assert (got["bars_held"].values == w["bars_held"].values).all()
    for c, tol in (("entry_raw_price", 1e-9), ("entry_fill_price", 1e-8), ("initial_stop", 1e-8),
                   ("exit_raw_price", 1e-8), ("exit_fill_price", 1e-8), ("stop_at_exit", 1e-8), ("ret_pct", 1e-6)):
        np.testing.assert_allclose(got[c].values, w[c].values.astype(float), rtol=tol, atol=1e-12, err_msg=f"{code} {c}")


@pytest.mark.parametrize("code", CODES)
def test_reference_engine_reproduces_spec(code):
    """Independent cross-check: the verbatim engine port with the WF params gives the same trades."""
    s = slot_from_spec(SPEC[code])
    pair = [(9, 21), (12, 26), (20, 50), (30, 80), (50, 100), (50, 200)].index((s["ema_fast"], s["ema_slow"]))
    params = {"family_engine": "sema", "symbol": s["symbol"].replace("USDT", ""), "tf": s["tf"], "pair": pair,
              "filt": s["filter"], "stop_m": s["stop_atr_mult"], "trail_m": s["trail_atr_mult"]}
    _, (tr, final) = R.run_row(params)
    tr = [t for t in tr if t["reason"] != "eod_flat"]
    want = _trades(code)
    assert len(tr) == len(want)
    assert [t["entry"] for t in tr] == list(pd.to_datetime(want["entry_time"], utc=True))
    assert [t["exit"] for t in tr] == list(pd.to_datetime(want["exit_time"], utc=True))
    np.testing.assert_allclose([t["exit_px"] for t in tr], want["exit_fill_price"].values, rtol=1e-8)
    full = pd.read_csv(SPEC_DIR / f"trades_{code}.csv")
    assert round(float(full["equity_after"].iloc[-1]), 2) == round(final, 2) == SPEC[code]["backtest_full"]["final_from_10000"]


def test_conflict_rule_blocks_short_when_long_open():
    slot, b = slot_from_spec(SPEC["S3"]), _bars("S3")
    first = _trades("S3").iloc[0]
    t = int(b.index.get_loc(pd.Timestamp(first["entry_time"])))
    win = b[["Open", "High", "Low", "Close"]].iloc[t - WINDOW:t]
    assert evaluate_short_trend_slot(slot, win, None, {})["action"] == "enter"
    r = evaluate_short_trend_slot(slot, win, None, {"_opposite_open": True})
    assert (r["action"], r["reason"]) == ("skip", "opposite_position_open")


def test_leverage_locked_1x():
    assert leverage_cap(-36.858) == 1.0 and leverage_cap(-48.7) == 1.0 and leverage_cap(None) == 1.0
    assert slot_from_spec({**SPEC["S8"], "sizing": {"leverage": 9}})["leverage"] == 1.0
    slot, b = slot_from_spec(SPEC["S3"]), _bars("S3")
    win = b[["Open", "High", "Low", "Close"]].iloc[-WINDOW:]
    r = evaluate_short_trend_slot({**slot, "leverage": 5}, win, None, {})
    assert r["leverage"] == 1.0 and r["leverage_cap"] == 1.0


def test_missing_filter_data_never_enters():
    idx = pd.date_range("2024-01-01", periods=5, freq="4h", tz="UTC")
    assert not filter_on_bars("coin50", idx, "4h", {}).any()
    assert not filter_on_bars("fpos", idx, "4h", {}).any()


def _live_hourly(code):
    """Hourly runner: the exchange stop fills INSIDE bar t; the runner then reconciles and evaluates
    mid-bar, when the last closed bar is t-1. An entry edge on t-1 (while the position was still open)
    must not enter (stale signal); the bar-t close is evaluated normally at the next run."""
    slot, b = slot_from_spec(SPEC[code]), _bars(code)
    df = b[["Open", "High", "Low", "Close"]]
    idx, O, H = df.index, df["Open"].values, df["High"].values
    s0 = int(np.argmax(b["in_backtest_window"].values))
    base, meta, pos, out, stale_blocked, stale_entered = _meta(slot), {}, None, [], [], []

    def run(win, p):
        return evaluate_short_trend_slot(slot, win, p, {**meta, **base})

    for t in range(s0 + 1, len(df)):
        win = df.iloc[max(0, t - WINDOW):t]
        r = run(win, pos)  # run right after bar t-1 closes
        if pos is not None and r["action"] == "exit":
            assert not r["reason"].startswith("stop")  # stops are filled intrabar by the exchange here
            n_f, f_sum = _funding_events(slot, idx, pos["fill_i"], t, O)
            px = float(O[t]) * (1 + COST)
            out.append((idx[pos["fill_i"]], idx[t], r["reason"], round(pos["stop_basis"], 10), round(px, 10)))
            meta.update(last_acted_bar_ts=r["bar_ts"], last_exit_bar_ts=r["bar_ts"], last_exit_reason=r["reason"])
            pos = None
        elif pos is not None:
            pos["stop"] = r["stop"]  # level effective for bar t
        if pos is None and r["action"] == "enter":
            raw = float(O[t])
            basis = stop_basis_from_fill(raw)
            pos = {"status": "FILLED", "entry": raw, "stop_basis": basis, "entry_bar_ts": r["bar_ts"], "fill_i": t,
                   "stop": basis + slot["stop_atr_mult"] * r["stop_atr"]}
            meta.update(last_acted_bar_ts=r["bar_ts"], last_entry_bar_ts=r["bar_ts"])
        if pos is not None and H[t] >= pos["stop"]:  # exchange STOP fills inside bar t
            px = max(float(O[t]), pos["stop"]) * (1 + COST)
            out.append((idx[pos["fill_i"]], idx[t], "stop", round(pos["stop_basis"], 10), round(px, 10)))
            pos = None
            bar = idx[t].isoformat()  # reconcile: _bar_floor_iso(fill time) = bar t
            meta.update(last_acted_bar_ts=bar, last_exit_bar_ts=bar, last_exit_reason="stop")
            mid = run(win, None)  # hourly run inside bar t: last closed bar is still t-1
            if mid["action"] == "enter":
                stale_entered.append(mid["bar_ts"])
            elif mid["reason"] == "stale_signal_before_exit":
                stale_blocked.append(mid["bar_ts"])
    return out, stale_blocked, stale_entered


@pytest.mark.parametrize("code", CODES)
def test_hourly_runner_no_stale_signal_reentry(code):
    out, blocked, entered = _live_hourly(code)
    assert entered == [], f"{code}: stale re-entries on signal bars {entered}"
    want = _trades(code)
    got = [(e, x, ("stop" if r.startswith("stop") else r), p_in, p_out) for e, x, r, p_in, p_out in out]
    exp = [(pd.Timestamp(a), pd.Timestamp(b), ("stop" if r.startswith("stop") else r), round(ein, 10), round(xf, 10))
           for a, b, r, ein, xf in zip(want["entry_time"], want["exit_time"], want["exit_reason"],
                                       want["entry_fill_price"], want["exit_fill_price"])]
    assert len(got) == len(exp), (code, len(got), len(exp))
    for g, w in zip(got, exp):
        assert g[:3] == w[:3], (code, g, w)
        assert g[3] == pytest.approx(w[3], rel=1e-8) and g[4] == pytest.approx(w[4], rel=1e-8), (code, g, w)
    # the cases 資金控管 found over 3 years (edge while in position, stop inside the next bar): S2 x2, S5 x1
    assert len(blocked) == {"S2": 2, "S3": 0, "S5": 1, "S8": 0}[code], (code, blocked)
