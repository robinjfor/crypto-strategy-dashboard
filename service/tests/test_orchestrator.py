from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import orchestrator as O


def _px(n=900, seed=1, drift=0.0):
    r = np.random.default_rng(seed).normal(drift, 0.03, n)
    return pd.Series(30000 * np.exp(np.cumsum(r)), index=pd.date_range("2022-06-01", periods=n, freq="D", tz="UTC"))


def test_weights_match_backtest_rules():
    w, x = O.weights(False, False)
    assert w == {"E": .6, "U": .25, "R": .15, "F": 0.0} and x == 1.0
    w, x = O.weights(True, True)
    assert w["E"] == pytest.approx(.3) and w["U"] == pytest.approx(.125)
    assert w["R"] == pytest.approx(.15 + .2125) and w["F"] == pytest.approx(.2125) and x == .7
    assert sum(w.values()) == pytest.approx(1.0)


def test_regime_equals_backtest_formula():
    px = _px()
    rg = O.regime(px)
    ma = px.rolling(200).mean(); vol = np.log(px).diff().rolling(30).std() * np.sqrt(365)
    thr = vol.expanding(365).quantile(2 / 3)
    assert rg["bear"] == bool(px.iloc[-1] < ma.iloc[-1])
    assert rg["hivol"] == bool(vol.iloc[-1] > thr.iloc[-1])
    assert rg["bar"] == str(px.index[-1].date())


def test_regime_short_history_is_not_hivol():
    rg = O.regime(_px(250))
    assert rg["vol_thr"] is None and rg["hivol"] is False


def test_plan_targets_peak_dd_alert_no_breaker(monkeypatch):
    px = _px(900, drift=0.002)  # strong uptrend: not bear
    monkeypatch.setattr(O, "regime", lambda c: {"bar": "2026-10-24", "bear": False, "hivol": False})
    now = datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc)
    eq = {"E": 3000.0, "U": 600.0, "R": 1800.0, "F": 600.0}  # P = 6000
    d = O.plan(px, eq, {"peak": 10000.0, "targets": {}}, now)
    assert d["portfolio_equity"] == 6000 and d["peak"] == 10000 and d["drawdown"] == pytest.approx(0.4)
    assert d["dd_alert"] is True and any("ALERT" in a for a in d["actions"])
    # alert only: targets are still plain weights x P (no halving)
    assert d["targets"] == {"demo_e5_min5_x175_t05top20": 3600.0, "u1_live": 1500.0, "demo_r2s_top10_3k": 900.0, "demo_funding_arb_1x": 0.0}
    monkeypatch.setattr(O, "regime", lambda c: {"bar": "x", "bear": True, "hivol": True})
    d = O.plan(px, {"E": 5000.0, "U": 1000.0, "R": 3000.0, "F": 1000.0}, None, now)
    assert d["peak"] == 10000 and not d["dd_alert"]
    assert d["targets"]["demo_e5_min5_x175_t05top20"] == pytest.approx(10000 * .3 * .7)
    assert sum(d["targets"].values()) == pytest.approx(7000)


def test_sizing_capital_fallbacks(monkeypatch):
    now = datetime(2026, 10, 25, 0, 8, tzinfo=timezone.utc)
    p = {"generated_at": (now - timedelta(minutes=3)).isoformat(), "targets": {"demo_e5_min5_x175": 6000.0}}
    assert O.sizing_capital("demo_e5_min5_x175", 5000.0, now=now, plan_doc=p) == 5000.0  # capped at ledger equity
    assert O.sizing_capital("demo_r2s_top10_3k", 3000.0, now=now, plan_doc=p) == 3000.0
    stale = {**p, "generated_at": (now - timedelta(hours=31)).isoformat()}
    assert O.sizing_capital("demo_e5_min5_x175", 5000.0, now=now, plan_doc=stale) == 5000.0
    O.load({})
    assert O.sizing_capital("demo_e5_min5_x175", 5000.0, now=now) == 5000.0
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "0")
    assert O.sizing_capital("demo_e5_min5_x175", 5000.0, now=now, plan_doc=p) == 5000.0


def test_u1_plan_orders_scales_with_size_equity():
    import u1_live
    sl = {"cash": 1000.0, "units": {k: 0.0 for k in u1_live.SYMS}, "quote": "USDC"}
    tg = {k: 0.5 for k in u1_live.SYMS}; op = {k: 100.0 for k in u1_live.SYMS}
    a, _ = u1_live.plan_orders(sl, tg, op, cap_gross=1e9, cap_pos=1e9)
    b, _ = u1_live.plan_orders(sl, tg, op, cap_gross=1e9, cap_pos=1e9, size_equity=500.0)  # down only; never above sleeve equity
    qa = {o["symbol"]: abs(o["qty_delta"]) for o in a}; qb = {o["symbol"]: abs(o["qty_delta"]) for o in b}
    assert qa and all(qb[s] == pytest.approx(0.5 * qa[s], rel=0.02) for s in qa)


def test_fa_equity_from_positions():
    class F:
        def mark_price(self, s): return 110.0
    st = {"demo_fa_positions": {"XUSDT": {"spot_qty": 2, "spot_px": 100, "perp_qty": 2, "perp_px": 101}}}
    eq = O.slot_equities(st, F())
    assert eq["F"] == pytest.approx(1000 + 2 * 10 - 2 * 9) and eq["E"] == 5000 and eq["U"] == 1000


def test_sizing_capped_at_ledger_equity():
    from datetime import datetime, timezone
    import orchestrator as O
    now = datetime(2026, 10, 26, 1, tzinfo=timezone.utc)
    doc = {"generated_at": now.isoformat(), "targets": {"e5": 5939.0, "r2s": 1485.0, "u1_live": 2488.0}}
    assert O.sizing_capital("e5", 4939.0, now=now, plan_doc=doc) == 4939.0
    assert O.sizing_capital("r2s", 2991.0, now=now, plan_doc=doc) == 1485.0
    assert O.sizing_capital("u1_live", None, now=now, plan_doc=doc) == 2488.0


def test_u1_plan_orders_caps_size_equity_at_sleeve_equity():
    import u1_live as U
    sleeve = {"cash": 968.0, "units": {k: 0.0 for k in U.SYMS}}
    opens = {k: 100.0 for k in U.SYMS}
    tg = {k: 0.88 for k in U.SYMS}
    orders, _ = U.plan_orders(sleeve, tg, opens, cap_gross=1e9, cap_pos=1e9, size_equity=2488.0)
    gross = sum(o["qty_delta"] * opens[o["coin"]] for o in orders)
    assert gross <= 1.76 * 968.0 + 1e-6
