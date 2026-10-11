"""Y1 (U1+V1 equal risk tv75 cap5) and X1 (V1 standalone) paper parity — trade by trade and bar by bar against
the analyst fixtures in /workspace/strategy-unified-3y/combo_u1v1_spec/{v1,combo}/, replaying the rd_regime 1h
panel through the runner's own engine functions (open_bar / close_bar / advance_day / decide)."""
import gzip
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper_u1 as U  # noqa: E402
import paper_y1 as Y  # noqa: E402

SPEC = Path("/workspace/strategy-unified-3y/combo_u1v1_spec")
PANEL = Path("/workspace/strategy-unified-3y/rd_regime/data")
U1FIX = Path(__file__).resolve().parent / "fixtures" / "u1"
need = pytest.mark.skipif(not (SPEC / "combo" / "trades.csv").is_file() or not (PANEL / "P_C.npy").is_file(),
                          reason="analyst panel / fixtures not on this machine")
RT = 1e-8          # fixtures are printed with 10 significant digits


@pytest.fixture(scope="module")
def rep():
    pan = Y.load_panel(PANEL)
    _, spot = U.load_fixture(U1FIX)
    cd = pd.read_csv(SPEC / "combo" / "daily.csv")
    ao = {r.date_utc: r.a_U1 for r in cd.itertuples() if not np.isfinite(r.sigma60_U1_1x_raw) or not np.isfinite(r.sigma60_V1_1x_raw)}
    hourly = {b: [] for b in Y.BOOKS}
    daily_units = {b: [] for b in Y.BOOKS}
    targets = []

    def rec(t, ts, st):
        for b in Y.BOOKS:
            hourly[b].append(st["books"][b]["eq"])
        if ts.hour == 23:
            d = str(ts.date())
            for b in Y.BOOKS:
                bk = st["books"][b]
                for j in np.where(bk["u"] != 0)[0]:
                    daily_units[b].append((d, st["syms"][j], bk["u"][j]))
            p = st["pending"] if False else None
    st_holder = {}

    def rec2(t, ts, st):
        rec(t, ts, st)
        if ts.hour == 23 and st.get("_last_pending_id") != id(st["decisions"][-1]):
            pass
    # capture targets by wrapping decide
    orig = Y.decide

    def spy(*a, **k):
        out = orig(*a, **k)
        targets.append((a[1], {b: {a[0]["syms"][j]: w[j] for j in np.where(w != 0)[0]} for b, w in out["targets"].items()}, out["meta"]))
        return out
    Y.decide = spy
    try:
        st = Y.replay_panel(pan, spot, a_override=ao, u1_combo_cast=np.float32, record=rec2)
    finally:
        Y.decide = orig
    return {"st": st, "hourly": {b: np.array(v) for b, v in hourly.items()}, "units": daily_units, "targets": targets, "pan": pan,
            "warmup_days": sorted(ao)}


def _fills(st, b):
    return pd.DataFrame(st["fills"][b])


def _cmp_trades(mine: pd.DataFrame, ref: pd.DataFrame, extra=()):
    assert len(mine) == len(ref), (len(mine), len(ref))
    for c in ("fill_time_utc", "symbol", "side", "reason"):
        bad = np.where(mine[c].astype(str).to_numpy() != ref[c].astype(str).to_numpy())[0]
        assert len(bad) == 0, (c, mine.iloc[bad[0]].to_dict(), ref.iloc[bad[0]].to_dict())
    for c in ("target_weight", "price", "qty_delta", "units_after", "notional", "fee", "equity_before", *extra):
        a, r = mine[c].to_numpy(float), ref[c].to_numpy(float)
        np.testing.assert_allclose(a, r, rtol=RT, atol=1e-9, err_msg=c)
    d = ref.decision_bar_open_utc.fillna("").astype(str).to_numpy()
    assert (mine.decision_bar_open_utc.fillna("").astype(str).to_numpy() == d).all()


@need
def test_v1_levered_trades_trade_by_trade(rep):
    """X1 = V1 levered book: v1/trades.csv (9,642 fills)."""
    _cmp_trades(_fills(rep["st"], "V1_lev"), pd.read_csv(SPEC / "v1" / "trades.csv"))


@need
def test_v1_1x_shadow_trades(rep):
    _cmp_trades(_fills(rep["st"], "V1_1x"), pd.read_csv(SPEC / "v1" / "trades_1x_shadow.csv"))
    _cmp_trades(_fills(rep["st"], "V1_1x"), pd.read_csv(SPEC / "combo" / "trades_V1_1x_shadow.csv"))


@need
def test_combo_levered_trades_trade_by_trade(rep):
    """Y1 = combo levered book: combo/trades.csv (10,217 fills)."""
    _cmp_trades(_fills(rep["st"], "C_lev"), pd.read_csv(SPEC / "combo" / "trades.csv"))


@need
def test_combo_1x_shadow_trades(rep):
    _cmp_trades(_fills(rep["st"], "C_1x"), pd.read_csv(SPEC / "combo" / "trades_1x_shadow.csv"))


@need
def test_trade_annotations_w1x_a_leverage(rep):
    """Per-fill w1x / a / L of the decision that produced it (combo trades.csv extra columns)."""
    ref = pd.read_csv(SPEC / "combo" / "trades.csv")
    ref = ref[ref.reason != "delist_close"]
    by_day = {d: (tg, m) for d, tg, m in rep["targets"]}
    aU = np.array([by_day[str(pd.Timestamp(x).date())][1]["a_U"] for x in ref.decision_bar_open_utc])
    L = np.array([by_day[str(pd.Timestamp(x).date())][1]["L_C"] for x in ref.decision_bar_open_utc])
    wc = np.array([by_day[str(pd.Timestamp(x).date())][0]["C_1x"].get(s, 0.0) for x, s in zip(ref.decision_bar_open_utc, ref.symbol)])
    np.testing.assert_allclose(aU, ref.a_U1, rtol=RT)
    np.testing.assert_allclose(L, ref.leverage, rtol=RT)
    np.testing.assert_allclose(wc, ref.w_combo_1x, rtol=RT, atol=1e-12)


@need
def test_v1_bars_bar_by_bar(rep):
    b = pd.read_csv(SPEC / "v1" / "bars.csv.gz")
    assert len(b) == len(rep["hourly"]["V1_lev"])
    np.testing.assert_allclose(rep["hourly"]["V1_1x"], b.equity_1x_shadow, rtol=RT)
    np.testing.assert_allclose(rep["hourly"]["V1_lev"], b.equity, rtol=RT)


@need
def test_combo_bars_bar_by_bar(rep):
    b = pd.read_csv(SPEC / "combo" / "bars.csv.gz")
    h = rep["hourly"]
    assert len(b) == len(h["C_lev"])
    np.testing.assert_allclose(h["U1_1x"], b.equity_U1_1x_shadow, rtol=1e-7)   # fixture: U1 W stored float32
    np.testing.assert_allclose(h["V1_1x"], b.equity_V1_1x_shadow, rtol=RT)
    np.testing.assert_allclose(h["C_1x"], b.equity_combo_1x_shadow, rtol=RT)
    np.testing.assert_allclose(h["C_lev"], b.equity, rtol=RT)


@need
def test_daily_a_sigma_leverage(rep):
    cd = pd.read_csv(SPEC / "combo" / "daily.csv"); vd = pd.read_csv(SPEC / "v1" / "daily.csv")
    m = pd.DataFrame([dict(day=d, **{k: mm[k] for k in ("a_U", "L_C", "L_V", "sigma_C", "n_eligible")}) for d, _, mm in rep["targets"]])
    assert list(m.day) == list(cd.date_utc)
    np.testing.assert_allclose(m.a_U, cd.a_U1, rtol=RT)
    np.testing.assert_allclose(m.L_C, cd.L, rtol=RT)
    np.testing.assert_allclose(m.L_V, vd.L, rtol=RT)
    ok = cd.sigma60_combo_1x.notna().to_numpy()
    np.testing.assert_allclose(m.sigma_C.to_numpy()[ok], cd.sigma60_combo_1x.to_numpy()[ok], rtol=1e-7, atol=1e-9)
    assert (m.n_eligible.to_numpy() == vd.n_eligible.to_numpy()).all()
    assert m.L_C.max() == 5.0 and (np.isclose(m.L_C, 5.0)).sum() == 628        # cap 5x binds 628 days (spec)


@need
def test_weights_daily(rep):
    for folder, b, extra in (("v1", "V1_lev", ()), ("combo", "C_lev", ())):
        w = pd.read_csv(SPEC / folder / "weights_daily.csv.gz")
        mine = pd.DataFrame([(d, s, v) for d, tg, _ in rep["targets"] for s, v in tg[b].items()], columns=["decision_day_utc", "symbol", "t"])
        j = w.merge(mine, on=["decision_day_utc", "symbol"], how="outer", indicator=True)
        assert not (j._merge == "right_only").any()
        # fixture rows are pre `W[~ok]=0`: extra rows only where the coin has no 1h bar at that 23:00 decision bar
        lo = j[j._merge == "left_only"]
        pan = rep["pan"]
        for d, s in zip(lo.decision_day_utc, lo.symbol):
            t = int(pan["IDX"].searchsorted(pd.Timestamp(d + " 23:00", tz="UTC")))
            assert not np.isfinite(pan["C"][t, pan["syms"].index(s)]), (d, s)
        rep.setdefault("pre_zero_rows", {})[folder] = len(lo)
        j = j[j._merge == "both"]
        np.testing.assert_allclose(j.t, j.target_w, rtol=RT, atol=1e-12)
    w = pd.read_csv(SPEC / "combo" / "weights_daily.csv.gz")
    mine = {(d, s): v for d, tg, _ in rep["targets"] for s, v in tg["V1_1x"].items()}
    pan = rep["pan"]
    keep = np.array([np.isfinite(pan["C"][int(pan["IDX"].searchsorted(pd.Timestamp(d + " 23:00", tz="UTC"))), pan["syms"].index(s)])
                     for d, s in zip(w.decision_day_utc, w.symbol)])
    assert (~keep).sum() == rep["pre_zero_rows"]["combo"]
    w = w[keep]
    np.testing.assert_allclose([mine.get((d, s), 0.0) for d, s in zip(w.decision_day_utc, w.symbol)], w.w_V1_1x, rtol=RT, atol=1e-12)


@need
def test_units_daily(rep):
    for f, b in (("v1/units_daily.csv.gz", "V1_lev"), ("v1/units_daily_1x_shadow.csv.gz", "V1_1x"),
                 ("combo/units_daily.csv.gz", "C_lev"), ("combo/units_daily_1x_shadow.csv.gz", "C_1x")):
        ref = pd.read_csv(SPEC / f)
        mine = pd.DataFrame(rep["units"][b], columns=["date_utc", "symbol", "u"])
        j = ref.merge(mine, on=["date_utc", "symbol"], how="outer", indicator=True)
        assert (j._merge == "both").all(), (f, j[j._merge != "both"].head())
        np.testing.assert_allclose(j.u, j.units, rtol=RT, err_msg=f)


@need
def test_final_equities_match_fixture_meta(rep):
    meta = json.load(open(SPEC / "fixture_meta.json"))
    for b, k in (("V1_1x", "v1_1x"), ("V1_lev", "v1_lev"), ("C_1x", "combo_1x"), ("C_lev", "combo_lev")):
        assert rep["st"]["books"][b]["eq"] == pytest.approx(meta[k]["final_equity"], rel=1e-10)
        assert len(rep["st"]["fills"][b]) == meta[k]["n_fills"]
        assert pd.DataFrame(rep["st"]["fills"][b]).reason.value_counts().to_dict() == meta[k]["reasons"]
        assert rep["st"]["books"][b]["n_liq"] == 0


@need
def test_v1_indicators_incremental_equal_fixture(rep):
    """The incremental Pine machine (coin_day) == v1/daily_coin_indicators.csv.gz per coin per day."""
    pan = rep["pan"]
    o, h, l, c, _ = Y.daily_bars_from_hourly(pan["O"], pan["H"], pan["L"], pan["C"], pan["V"])
    days = pd.date_range("2020-01-01", periods=c.shape[0], freq="1D", tz="UTC").strftime("%Y-%m-%d")
    ref = pd.read_csv(SPEC / "v1" / "daily_coin_indicators.csv.gz")
    rows = []
    for j, s in enumerate(pan["syms"]):
        if s not in set(ref.symbol):
            continue
        cs = Y.new_coin()
        alive = np.where(np.isfinite(c[:, j]))[0]
        for di in range(c.shape[0]):
            if di > alive[-1]:
                break
            has = np.isfinite(c[di, j])
            r = Y.coin_day(cs, days[di], *(map(float, (o[di, j], h[di, j], l[di, j], c[di, j])) if has else (math.nan,) * 4), bool(has))
            if cs["started"] and days[di] >= "2020-06-01":
                rows.append((days[di], s, r.get("wma_tr14"), r.get("rsi25"), r.get("rsi100"), r["pos"]))
    mine = pd.DataFrame(rows, columns=["date_utc", "symbol", "w", "r1", "r2", "p"])
    j = ref.merge(mine, on=["date_utc", "symbol"], how="inner")
    assert len(j) == len(ref), (len(j), len(ref))
    for a, b in (("w", "wma_tr14"), ("r1", "rsi25"), ("r2", "rsi100")):
        x, y = j[a].to_numpy(float), j[b].to_numpy(float)
        assert (np.isnan(x) == np.isnan(y)).all(), a
        np.testing.assert_allclose(x[~np.isnan(x)], y[~np.isnan(y)], rtol=1e-8, atol=1e-10, err_msg=a)
    assert (j.p.to_numpy() == j.pos.to_numpy()).all()


@need
def test_eligibility_rule_equals_panel(rep):
    """eligible(): >45 of the previous 90 days with perp qv >= $50M and >= 30 data days == elig_daily.pkl."""
    pan = rep["pan"]
    C = np.asarray(pan["C"])
    days = pd.date_range("2020-01-01", periods=C.shape[0] // 24, freq="1D", tz="UTC")
    has = np.isfinite(C[: len(days) * 24].reshape(len(days), 24, -1)).any(1)
    qv = pan["qv"].reindex(days).fillna(0.0).to_numpy()
    ref = pan["elig_ref"].reindex(days).fillna(False).to_numpy(bool)
    ref = ref[:, [list(pan["elig_ref"].columns).index(s[:-4]) for s in pan["syms"]]]
    age = np.cumsum(has, 0)
    mine = np.zeros_like(ref)
    for di in range(len(days)):
        lo = max(0, di - 90)
        for j in range(len(pan["syms"])):
            mine[di, j] = Y.eligible(list(qv[lo:di, j]), int(age[di, j]))
    assert (mine == ref).all(), np.argwhere(mine != ref)[:5]


def test_seed_file_equals_analyst_seed():
    """Shipped seed (full-precision replay end state) == analyst seed_state_2026-10-10.json."""
    A = json.load(open(Path(__file__).resolve().parent / "fixtures" / "y1" / "seed_state_2026-10-10.json"))
    S = Y.load_seed()
    assert S["last_closed_bar"] == "2026-10-10T23:00:00+00:00" and S["v1_day"] == "2026-10-10"
    assert S["pending"]["meta"]["fill_bar"] == "2026-10-11T00:00:00+00:00"
    m = {"U1_1x": "U1_1x_shadow", "V1_1x": "V1_1x_shadow", "V1_lev": "V1_lev", "C_1x": "combo_1x_shadow", "C_lev": "combo_lev"}
    for b, a in m.items():
        bk, ref = S["books"][b], A["books"][a]
        assert bk["eq"] == pytest.approx(ref["equity_close"], rel=1e-9)
        assert bk["cash"] == pytest.approx(ref["cash"], rel=1e-8)
        u = {S["syms"][j]: bk["u"][j] for j in np.where(bk["u"] != 0)[0]}
        assert set(u) == set(ref["units"])
        for s, v in ref["units"].items():
            assert u[s] == pytest.approx(v, rel=1e-8)
    for b, a in (("V1_lev", "V1_lev"), ("C_lev", "combo_lev")):
        w = S["pending"]["targets"][b]
        mine = {S["syms"][j]: w[j] for j in np.where(w != 0)[0]}
        ref = A["pending_targets_fill_2026-10-11T00:00Z"][a]
        assert set(mine) == set(ref)
        for s, v in ref.items():
            assert mine[s] == pytest.approx(v, abs=1e-9)
    for s, r in A["v1_coin_state"].items():
        assert S["coins"][s]["pos"] == r["pos"], s
    d = S["decisions"][-1]
    assert d["n_eligible"] == 27 and d["a_U"] == pytest.approx(0.6143, abs=1e-4) and d["L_C"] == pytest.approx(1.7282, abs=1e-4)
    for k, a in (("U1_1x", "U1_1x"), ("V1_1x", "V1_1x"), ("C_1x", "combo_1x")):
        e = A["daily_equity_last60"][a]
        r = [e[i] / e[i - 1] - 1 for i in range(1, len(e))]
        np.testing.assert_allclose(S["rets"][k], r[-60:], rtol=1e-6, atol=1e-9)
