"""R2S ceiling stays 1464.08. On the 11/11 pool the 15% weight is under that ceiling."""
from datetime import datetime, timezone

import pytest

import demo_r2s
import orchestrator as O

NOW = datetime(2026, 11, 12, 0, 5, tzinfo=timezone.utc)
# The 11/11 books that produced eligible.usdt 7863.91 and an uncapped R2S target of 2359.17.
EQ = {"E": 5504.74, "R": 2359.17, "U": 0.0, "F": 1000.0}


def test_nov12_targets_on_the_nov11_pool_leave_the_cap_idle(monkeypatch):
    monkeypatch.setattr(O, "regime", lambda c: {"bar": "2026-11-11", "bear": False, "hivol": False})
    d = O.plan([1.0], EQ, None, NOW)
    assert d["eligible"]["usdt"] == pytest.approx(7863.91)
    assert d["weights"]["demo_e5_min5_x175_t05top20"] == pytest.approx(0.85)
    assert d["weights"]["demo_r2s_top10_3k"] == pytest.approx(0.15)
    assert d["weights"]["u1_live"] == 0.0 and d["weights"]["demo_funding_arb_1x"] == 0.0
    e = d["targets"]["demo_e5_min5_x175_t05top20"]
    r = d["targets"]["demo_r2s_top10_3k"]
    assert e == 6684.32
    assert d["uncapped_targets"]["demo_r2s_top10_3k"] == 1179.59
    assert r == 1179.59
    assert r < d["slot_caps"]["demo_r2s_top10_3k"]["equity"] == 1464.08
    assert "demo_r2s_top10_3k" not in d["cash_held_by_cap"]
    assert e + r == pytest.approx(7863.91)
    assert d["slot_caps"]["demo_r2s_top10_3k"]["reason"] == "r2s_mdd_review_20261111"
    assert d["slot_caps"]["demo_r2s_top10_3k"]["closes_positions"] is False
    assert O.sizing_capital("demo_r2s_top10_3k", 3000.0, now=NOW, plan_doc=d) == 1179.59
    assert O.sizing_capital("demo_e5_min5_x175_t05top20", 5504.74, now=NOW, plan_doc=d) == 6684.32


def test_cap_does_not_resize_or_flatten_and_signal_exits_still_trade():
    price = 10.0
    weight = 0.1
    qty = weight * 1464.08 / price
    assert demo_r2s.decide_minnot(weight, qty, price, 1464.08, 5.0, 0.0)[1] == "hold"
    assert demo_r2s.decide_minnot(weight, qty, price, 2359.17, 5.0, 0.0)[1] == "order"
    assert demo_r2s.decide_minnot(0.0, qty, price, 1464.08, 5.0, 0.0)[1] == "close"


def test_state_override_and_seed_do_not_touch_positions():
    st = {"approved_families": ["keep"], "demo_r2s_exec": {"book": {"ETHUSDT": -0.071}, "cash": 3000.0}}
    O.ensure_slot_caps(st)
    assert st["slot_caps"]["demo_r2s_top10_3k"]["equity"] == 1464.08
    assert st["slot_caps"]["demo_r2s_top10_3k"]["reason"] == "r2s_mdd_review_20261111"
    assert st["approved_families"] == ["keep"] and st["demo_r2s_exec"]["book"]["ETHUSDT"] == -0.071
    st["slot_caps"]["demo_r2s_top10_3k"] = {"equity": None, "reason": "cleared"}
    O.ensure_slot_caps(st)
    assert st["slot_caps"]["demo_r2s_top10_3k"]["equity"] is None
    O.load(st)
    try:
        plan = {"generated_at": NOW.isoformat(), "sizing": "quote_pool",
                "targets": {"demo_r2s_top10_3k": 2359.17},
                "quote_pool": {"demo_r2s_top10_3k": 7863.91}}
        assert O.sizing_capital("demo_r2s_top10_3k", 3000.0, now=NOW, plan_doc=plan) == 2359.17
    finally:
        O.load({})
