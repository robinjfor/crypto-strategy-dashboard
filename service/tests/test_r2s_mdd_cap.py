"""R2S ceiling stays 1464.08. On the 11/11 pool the 15% weight is under that ceiling."""
from datetime import datetime, timezone

import pytest

import demo_r2s
import orchestrator as O

NOW = datetime(2026, 11, 12, 0, 5, tzinfo=timezone.utc)
# 11/11 USDT books 7863.91, plus the FA ledger. USDC is 0 in this fixture.
EQ = {"E": 5504.74, "R": 2359.17, "U": 0.0, "F": 1000.0}


def test_nov12_targets_use_usdt_margin_minus_vbo(monkeypatch):
    monkeypatch.setattr(O, "regime", lambda c: {"bar": "2026-11-11", "bear": False, "hivol": False})
    # 20:36 Taipei snapshot: USDT wallet 4762.98515654 + unrealized 112.18358041.
    margin = 4762.98515654 + 112.18358041
    d = O.plan([1.0], EQ, None, NOW, usdt_margin=margin)
    assert d["eligible"]["usd"] == 3875.17
    assert d["eligible"]["usdc_excluded"] == 0.0
    assert d["eligible"]["fa_excluded"] == 1000.0
    assert d["eligible"]["vbo_in_pool"] is False
    assert d["weights"]["demo_e5_min5_x175_t05top20"] == 1.0
    assert d["weights"]["demo_r2s_top10_3k"] == 0.0
    assert d["targets"]["demo_e5_min5_x175_t05top20"] == 3875.17
    assert d["targets"]["demo_r2s_top10_3k"] == 0.0
    assert d["targets"]["u1_live"] == 0.0 and d["targets"]["demo_funding_arb_1x"] == 0.0
    assert O.sizing_capital("demo_r2s_top10_3k", 3000.0, now=NOW, plan_doc=d) == 0.0
    assert O.sizing_capital("demo_e5_min5_x175_t05top20", 5504.74, now=NOW, plan_doc=d) == 3875.17


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
