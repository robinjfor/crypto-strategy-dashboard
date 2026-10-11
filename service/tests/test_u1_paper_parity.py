"""U1 (btc_eth_trend_voltarget_perp) paper engine parity vs the analyst fixture (d4_trend_spec; spec.json authoritative).

Replays every 1h bar 2020-06-24..2026-10-10 23:00 UTC through paper_u1.open_phase/close_phase (the exact
functions the live paper runner uses) and checks trade-by-trade vs trades.csv / trades_1x_shadow.csv and
bar-by-bar vs bars.csv.gz (units, equity, shadow equity, funding, targets, L, indicators) and daily_indicators.csv.
Fixture floats are printed with 10 significant digits, hence rtol 1e-8 (observed max ~5e-10)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import paper_u1 as P  # noqa: E402

F = Path(__file__).resolve().parent / "fixtures" / "u1"
RT = 1e-8


@pytest.fixture(scope="module")
def run():
    bars, spot = P.load_fixture(F)
    n = len(bars)
    rec = {"E": np.zeros(n), "E1": np.zeros(n), "U": np.zeros((n, 2)), "U1": np.zeros((n, 2)), "FP": np.zeros((n, 2)),
           "dec": {}}

    def r(i, ts, st):
        rec["E"][i] = P.book_equity(st["books"]["lev"]); rec["E1"][i] = P.book_equity(st["books"]["x1"])
        rec["U"][i] = [st["books"]["lev"]["units"][k] for k in P.SYMS]
        rec["U1"][i] = [st["books"]["x1"]["units"][k] for k in P.SYMS]
        if ts.hour == P.DECISION_HOUR:
            rec["dec"][i] = st["pending"]

    fp_prev = {"lev": {k: 0.0 for k in P.SYMS}}
    # per-bar funding of the levered book: wrap open_phase to capture
    orig = P.open_phase
    fps = []

    def op(st, ts, opens, rates, **kw):
        out = orig(st, ts, opens, rates, **kw)
        fps.append([st["open_funding_used"]["funding"]["lev"].get(k, 0.0) for k in P.SYMS])
        return out

    P.open_phase = op
    try:
        st = P.replay_bars(bars, spot, record=r)
    finally:
        P.open_phase = orig
    rec["FP"] = np.asarray(fps)
    return bars, spot, st, rec


def test_spec_constants():
    sp = json.loads((F / "spec.json").read_text())
    assert sp["code"] == P.CODE and sp["strategy_family_id"] == P.FAMILY and sp["strategy_id"] == P.STRATEGY_ID
    assert sp["signal"]["N"] == P.N_SMA and sp["account_leverage"]["cap"] == P.LEV_CAP == 3.0
    assert sp["account_leverage"]["target_vol_ann"] == P.ACCT_TV and sp["rebalance"]["band"] == P.BAND
    assert sp["costs"]["fee_bps_per_side"] == P.COST * 1e4 and sp["margin"]["maintenance"] == P.MAINT
    assert sp["initial_capital"] == P.INITIAL and sp["mode"] == "paper_only"


def test_bar_level_books(run):
    bars, _, _, rec = run
    np.testing.assert_allclose(rec["E"], bars["equity"], rtol=RT)
    np.testing.assert_allclose(rec["E1"], bars["equity_1x_shadow"], rtol=RT)
    for j, k in enumerate(P.SYMS):
        np.testing.assert_allclose(rec["U"][:, j], bars[f"{k}_units"], rtol=RT, atol=1e-9)
        np.testing.assert_allclose(rec["U1"][:, j], bars[f"{k}_units_1x_shadow"], rtol=RT, atol=1e-9)
        np.testing.assert_allclose(rec["FP"][:, j], bars[f"{k}_funding_pnl"], rtol=1e-7, atol=1e-6)


def test_decision_bars_targets_L_indicators(run):
    bars, _, _, rec = run
    dec_rows = np.where(bars["decision_bar"].to_numpy() == 1)[0]
    assert len(dec_rows) == len(rec["dec"]) > 2000
    for i in dec_rows:
        d = rec["dec"][i]; b = bars.iloc[i]
        assert d["indicator_day"] == b["indicator_day_utc"]
        assert d["L"] == pytest.approx(b["L_in_force"], rel=RT)
        for k in P.SYMS:
            assert d["targets_lev"][k] == pytest.approx(b[f"{k}_target_w"], rel=RT, abs=1e-12)
            assert d["w1x"][k] == pytest.approx(b[f"{k}_w1x_state"], rel=RT, abs=1e-12)
            ind = d["indicators"][k]
            assert ind["signal"] == int(b[f"{k}_signal_used"])
            for c in ("spot_close", "sma200", "sigma20"):
                assert ind[c] == pytest.approx(b[f"{k}_{c}_used"], rel=RT), (b["bar_open_utc"], k, c)


def test_daily_indicators_and_shadow_leverage(run):
    _, _, _, rec = run
    di = pd.read_csv(F / "daily_indicators.csv")
    decs = [rec["dec"][i] for i in sorted(rec["dec"])]
    by_day = {d["indicator_day"]: d for d in decs}
    for _, r in di.iterrows():
        d = by_day[str(pd.Timestamp(r["date_utc"]).date())]
        assert d["e1x_close"] == pytest.approx(r["eq_1x_close"], rel=RT)
        assert d["equity_close"] == pytest.approx(r["eq_lev_close"], rel=RT)
        assert d["L"] == pytest.approx(r["L"], rel=RT)
        if not pd.isna(r["sigma60_1x"]):
            assert d["sigma60_1x"] == pytest.approx(r["sigma60_1x"], rel=1e-7, abs=1e-6)  # pandas rolling noise on all-zero windows (both floored to 0.05)
        for k in P.SYMS:
            assert d["w1x"][k] == pytest.approx(r[f"{k}_w1x"], rel=RT, abs=1e-12)


def _cmp_trades(got: list[dict], csv: Path):
    exp = pd.read_csv(csv)
    assert len(got) == len(exp)
    for g, (_, e) in zip(got, exp.iterrows()):
        assert _ts(g["fill_time_utc"]) == _ts(e["fill_time_utc"])
        assert _ts(g["decision_bar_open_utc"]) == _ts(e["decision_bar_open_utc"])
        assert g["indicator_day_utc"] == e["indicator_day_utc"]
        assert (g["symbol"], g["side"], g["reason"]) == (e["symbol"], e["side"], e["reason"])
        for c in ("target_weight", "weight_before", "w1x", "leverage", "price", "qty_delta", "units_after", "notional",
                  "fee", "equity_before"):
            assert g[c] == pytest.approx(e[c], rel=RT, abs=1e-9), (e["fill_time_utc"], e["symbol"], c)


def _ts(x):
    return pd.Timestamp(x).tz_convert("UTC") if pd.Timestamp(x).tzinfo else pd.Timestamp(x, tz="UTC")


def test_trades_levered_trade_by_trade(run):
    _, _, st, _ = run
    _cmp_trades(st["fills"], F / "trades.csv")
    reasons = pd.Series([f["reason"] for f in st["fills"]]).value_counts().to_dict()
    assert reasons == {"rebalance_band": 100, "entry": 44, "exit_to_cash": 42}


def test_trades_shadow_1x_trade_by_trade(run):
    _, _, st, _ = run
    _cmp_trades(st["fills_1x"], F / "trades_1x_shadow.csv")


def test_final_state_and_no_liquidation(run):
    _, _, st, _ = run
    meta = {"final_equity": 342930.6738671098}
    assert P.book_equity(st["books"]["lev"]) == pytest.approx(meta["final_equity"], rel=1e-12)
    assert st["books"]["lev"]["n_liq"] == 0 and st["books"]["x1"]["n_liq"] == 0
    p = st["pending"]  # spec §9: state at data end
    assert p["indicator_day"] == "2026-10-10" and p["fill_bar"].startswith("2026-10-11T00:00")
    assert p["L"] == pytest.approx(1.596, abs=5e-4) and p["sigma60_1x"] == pytest.approx(0.3759, abs=1e-4)
    for k in P.SYMS:
        assert p["w1x"][k] == 0.5 and p["targets_lev"][k] == pytest.approx(0.798, abs=5e-4)


def test_committed_seed_is_replay_end_state(run):
    _, _, st, _ = run
    seed = json.loads((Path(P.__file__).parent / "paper" / "u1_seed.json").read_text())["engine"]
    for b in ("x1", "lev"):
        assert seed["books"][b]["cash"] == pytest.approx(st["books"][b]["cash"], rel=1e-12)
        for k in P.SYMS:
            assert seed["books"][b]["units"][k] == pytest.approx(st["books"][b]["units"][k], rel=1e-12)
    assert seed["last_closed_bar"] == st["last_closed_bar"]
    assert seed["pending"]["targets_lev"] == pytest.approx(st["pending"]["targets_lev"], rel=1e-12)
    assert seed["returns_1x"] == pytest.approx(st["returns_1x"], rel=1e-12)
    assert seed["e1x_prev"] == pytest.approx(st["e1x_prev"], rel=1e-12)
