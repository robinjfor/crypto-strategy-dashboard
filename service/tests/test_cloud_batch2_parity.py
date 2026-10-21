"""Cloud batch 2 (2026-10-21, strict-WF H1-selected params) — LIVE evaluators vs analyst backtest, trade by trade.

C23 WLD 4h, C25 ARB 1h, C29 FIL 1h, C31 APT 4h, C32 DOT 1h, C35 SEI 4h, C36 GALA 4h, C39 OP 1h
  -> donchian_perp.evaluate_donchian_perp_slot (family donchian_atr_perp; engine rd_catalog_refresh/code/eng.bt)
Z9 ARB 1h -> bb_squeeze.evaluate_bb_squeeze_slot (bb_squeeze_breakout_perp).
Fixtures (fixtures/cloud_batch2/): C23..C32 = rd_catalog_refresh/fixtures; C35/C36/C39/Z9 =
rd_daily/2026-10-21/fixtures_batch2 (code/build_batch2_fixtures.py: pure-python sim == numba engine, final ==
catalog). Replay: sliding 1500-bar window of closed bars (the /fapi klines max), next-bar-open fills,
exchange stop at min(open, stop), 20bps/side, funding at each settlement bar open on held qty."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import bb_squeeze
import donchian_perp

FIX = Path(__file__).resolve().parent / "fixtures" / "cloud_batch2"
SPEC = {e["code"]: e for e in json.loads((FIX / "spec.json").read_text(encoding="utf-8"))}
SUMMARY = {e["code"]: e for e in json.loads((FIX / "parity_summary.json").read_text(encoding="utf-8"))}
C_CODES = ["C23", "C25", "C29", "C31", "C32", "C35", "C36", "C39"]
CODES = C_CODES + ["Z9"]
WINDOW = 1500
COST = 0.002
_BARS: dict = {}


def _mod(code):
    return donchian_perp if code.startswith("C") else bb_squeeze


def _eval(code):
    return donchian_perp.evaluate_donchian_perp_slot if code.startswith("C") else bb_squeeze.evaluate_bb_squeeze_slot


def _bars(code):
    if code not in _BARS:
        b = pd.read_csv(FIX / f"bars_{code}.csv.gz")
        b.index = pd.to_datetime(b.pop("open_time"), utc=True)
        _BARS[code] = b.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    return _BARS[code]


EXPECTED = {  # code: (symbol, tf, n, stop_m, trail_m) — strict-WF H1 picks (STRICT_WF_AUDIT.md)
    "C23": ("WLDUSDT", "4h", 70, 2.0, 1.5), "C25": ("ARBUSDT", "1h", 70, 2.5, 3.0), "C29": ("FILUSDT", "1h", 70, 1.5, 2.5),
    "C31": ("APTUSDT", "4h", 30, 1.5, 3.5), "C32": ("DOTUSDT", "1h", 70, 2.0, 3.5), "C35": ("SEIUSDT", "4h", 40, 1.5, 2.5),
    "C36": ("GALAUSDT", "4h", 20, 2.0, 2.5), "C39": ("OPUSDT", "1h", 70, 1.5, 3.0), "Z9": ("ARBUSDT", "1h", 40, 1.5, 3.5),
}


@pytest.mark.parametrize("code", CODES)
def test_slot_params_are_h1_pick(code):
    s = _mod(code).slot_from_spec(SPEC[code])
    n = s["donch_n"] if code.startswith("C") else s["bb_n"]
    assert (s["symbol"], s["tf"], n, s["stop_atr_mult"], s["trail_atr_mult"]) == EXPECTED[code]
    assert s["max_hold_bars"] == {"1h": 192, "4h": 36}[s["tf"]] and s["leverage"] == 1.0
    if code == "Z9":
        assert (s["bb_k"], s["sq_q"]) == (2.5, 0.35)
    else:
        assert s["require_reset_below_hi"] is True and s["family"] == "donchian_atr_perp"


@pytest.mark.parametrize("code", C_CODES)
def test_donchian_indicators_match_backtest(code):
    s, b = donchian_perp.slot_from_spec(SPEC[code]), _bars(code)
    ind = b[["Open", "High", "Low", "Close"]]
    from indicators import sma_atr
    np.testing.assert_allclose(sma_atr(ind, 14).fillna(0).values, b["atr14_sma"].fillna(0).values, rtol=1e-7, atol=1e-12)
    hi, lo = donchian_perp.donch_bands(ind, s["donch_n"])
    for got, col in ((hi, "donch_hi"), (lo, "donch_lo")):
        w = b[col].values.astype(float)
        assert (np.isnan(got) == np.isnan(w)).all(), col
        m = ~np.isnan(w)
        np.testing.assert_allclose(got[m], w[m], rtol=0, atol=0, err_msg=col)
    assert (donchian_perp.donch_state(ind["Close"].values, hi, lo) == b["signal_state"].values).all()


def _funding(b, fill_i, exit_i, O, qty):
    fr = b["funding_applied_at_open"].values.astype(float)
    k = np.arange(fill_i + 1, exit_i + 1)
    k = k[fr[k] != 0.0]
    return int(len(k)), float(-np.sum(qty * O[k] * fr[k]))


def _live(code, runner_side_stop=False):
    mod, ev = _mod(code), _eval(code)
    slot, b = mod.slot_from_spec(SPEC[code]), _bars(code)
    df = b[["Open", "High", "Low", "Close"]]
    idx, O = df.index, df["Open"].values
    s0 = int(np.argmax(b["in_backtest_window"].values))
    meta, pos, out, eq = {}, None, [], 10000.0

    def run(win, p):
        return ev(slot, win, p, dict(meta))

    for t in range(s0 + 1, len(df)):
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
                            bars_held=e_i - pos["fill_i"] + (1 if stop_exit else 0),
                            funding_pnl=f_pnl, pnl=pnl, ret_pct=pnl / eq * 100.0, equity_after=eq + pnl))
            eq += pnl
            pos = None
            if runner_side_stop and stop_exit:
                meta.update(last_acted_bar_ts=r["exit_bar_ts"], last_exit_bar_ts=r["exit_bar_ts"], last_exit_reason=r["reason"])
                r = r["after_stop"]
            else:
                meta.update(last_acted_bar_ts=r["exit_bar_ts"], last_exit_bar_ts=r["exit_bar_ts"],
                            last_exit_reason=("stop" if stop_exit else r["reason"]))
                if stop_exit:
                    r = run(win, None)
        if pos is None and r["action"] == "enter":
            raw = float(O[t])
            basis = mod.stop_basis_from_fill(raw)
            assert mod.initial_stop_from_fill(raw, slot["stop_atr_mult"], r["stop_atr"]) == pytest.approx(
                basis - slot["stop_atr_mult"] * r["stop_atr"])
            pos = {"status": "FILLED", "entry": raw, "stop_basis": basis, "entry_bar_ts": r["bar_ts"], "fill_i": t,
                   "raw": raw, "signal_bar": pd.Timestamp(r["bar_ts"]), "qty": eq / basis}
            meta.update(last_acted_bar_ts=r["bar_ts"], last_entry_bar_ts=r["bar_ts"])
    assert pos is None, f"{code}: live still in position at end of data"
    return out


def _cmp(code, rows):
    got = pd.DataFrame(rows)
    for c in ("signal_bar", "entry_time", "exit_signal_bar", "exit_time"):
        got[c] = pd.to_datetime(got[c], utc=True)
    want = pd.read_csv(FIX / f"trades_{code}.csv")
    assert len(got) == len(want), (code, len(got), len(want))
    for c in ("signal_bar", "entry_time", "exit_signal_bar", "exit_time"):
        want[c] = pd.to_datetime(want[c], utc=True)
    for c in ("signal_bar", "entry_time", "exit_time", "exit_reason", "n_funding_events", "bars_held"):
        bad = got[c].values != want[c].values
        assert not bad.any(), f"{code} {c}: first diff trade {int(np.argmax(bad)) + 1}: live {got[c].values[bad][0]} backtest {want[c].values[bad][0]}"
    m = want["exit_signal_bar"].notna().values
    assert (got["exit_signal_bar"].isna().values == ~m).all()
    assert (got["exit_signal_bar"].values[m] == want["exit_signal_bar"].values[m]).all()
    for c, tol in (("entry_raw_price", 1e-12), ("entry_fill_price", 1e-10), ("initial_stop", 1e-9),
                   ("exit_raw_price", 1e-9), ("exit_fill_price", 1e-9), ("stop_at_exit", 1e-9),
                   ("funding_pnl", 1e-6), ("pnl", 1e-6), ("ret_pct", 1e-6), ("equity_after", 1e-8)):
        np.testing.assert_allclose(got[c].values, want[c].values.astype(float), rtol=tol, atol=1e-9, err_msg=f"{code} {c}")
    return got


@pytest.mark.parametrize("code", CODES)
def test_live_matches_backtest_trades(code):
    got = _cmp(code, _live(code))
    assert len(got) == SUMMARY[code]["n_trades"]
    assert round(float(got["equity_after"].iloc[-1]), 2) == SUMMARY[code]["final"]
    assert got["exit_reason"].value_counts().to_dict() == SUMMARY[code]["exit_reasons"]


@pytest.mark.parametrize("code", ["C31"])
def test_live_runner_side_stop_same_trades(code):
    _cmp(code, _live(code, runner_side_stop=True))


def test_donchian_perp_blocks_long_when_short_open_and_1x():
    slot, b = donchian_perp.slot_from_spec(SPEC["C31"]), _bars("C31")
    first = pd.read_csv(FIX / "trades_C31.csv").iloc[0]
    t = int(b.index.get_loc(pd.Timestamp(first["entry_time"])))
    win = b[["Open", "High", "Low", "Close"]].iloc[t - WINDOW:t]
    r = donchian_perp.evaluate_donchian_perp_slot(slot, win, None, {})
    assert (r["action"], r["side"], r["venue"], r["leverage"]) == ("enter", "LONG", "futures", 1.0)
    r = donchian_perp.evaluate_donchian_perp_slot(slot, win, None, {"_opposite_open": True})
    assert (r["action"], r["reason"]) == ("skip", "opposite_position_open")


def test_runner_wiring_dispatch_and_order_path():
    import main
    from strategy import evaluate_slot_dispatch
    slot, b = donchian_perp.slot_from_spec(SPEC["C23"]), _bars("C23")
    win = b[["Open", "High", "Low", "Close"]].iloc[-WINDOW:]
    r = evaluate_slot_dispatch(slot, win, None, {})
    assert r["family"] == "donchian_atr_perp" and r["venue"] == "futures"
    assert main._next_open_perp_module("donchian_atr_perp") is donchian_perp
    assert "donchian_atr_perp" in main.NEXT_OPEN_PERP_FAMILIES
    # not allocatable / not in 待審核 until 資金控管 accepts it
    import allocation
    assert "donchian_atr_perp" not in allocation.RUNNER_FAMILIES
