"""Robin 2026-11-11: demo capital was left idle by min(target, own ledger), and U1 is paused.

These tests stay off the network. They do not place orders or read live credentials.
The full suite is not run here; this file plus test_orchestrator.py are the targeted set.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import demo_e5
import demo_r2s
import main
import orchestrator as O
import u1_live as U

NOW = datetime(2026, 11, 11, 0, 5, tzinfo=timezone.utc)
# Funded virtual books the orchestrator was written against. Not a live Binance read.
BOOKS = {"E": 5000.0, "U": 1000.0, "R": 3000.0, "F": 1000.0}


def _flat(monkeypatch, eq=None, usdt_margin=9000.0):
    monkeypatch.setattr(O, "regime", lambda c: {"bar": "2026-11-10", "bear": False, "hivol": False,
                                                "btc_close": 1, "ma200": 1, "vol30": 1, "vol_thr": 1})
    return O.plan([1.0], eq or BOOKS, None, NOW, usdt_margin=usdt_margin)


def test_old_rule_left_usdt_idle_inside_r2s():
    """Evidence, reconstructed. Pre-change weights on P = E+U+R+F, then min(target, own ledger)."""
    P = sum(BOOKS.values())
    old_w = {"E": 0.60, "U": 0.25, "R": 0.15, "F": 0.0}
    sized = {k: min(P * old_w[k], BOOKS[k]) for k in old_w}
    assert sized == {"E": 5000.0, "U": 1000.0, "R": 1500.0, "F": 0.0}
    idle_in_r = BOOKS["R"] - sized["R"]
    assert idle_in_r == 1500.0
    # E's 60% target was 6000, but the ledger cap stopped it at 5000, so it could not absorb R's idle USDT.
    assert P * old_w["E"] == 6000.0 and sized["E"] == 5000.0
    usdt_pool = BOOKS["E"] + BOOKS["R"]
    assert sized["E"] + sized["R"] == 6500.0 and usdt_pool - (sized["E"] + sized["R"]) == 1500.0


def test_pool_is_usdt_margin_minus_vbo_and_e_takes_all_of_it(monkeypatch):
    d = _flat(monkeypatch)
    assert d["sizing"] == "quote_pool"
    assert d["eligible"]["accounting"] == "usdt_m_margin_minus_vbo"
    assert d["eligible"]["usd"] == 8000.0
    assert d["eligible"]["usdc_excluded"] == 1000.0
    assert d["eligible"]["fa_excluded"] == 1000.0
    assert d["eligible"]["vbo_in_pool"] is False
    assert d["eligible"]["vbo_reserve_usdt"] == 1000.0
    assert d["weights"]["demo_e5_min5_x175_t05top20"] == 1.0
    assert d["weights"]["demo_r2s_top10_3k"] == 0.0
    assert d["targets"]["demo_e5_min5_x175_t05top20"] == 8000.0
    assert d["targets"]["demo_r2s_top10_3k"] == 0.0
    assert d["targets"]["u1_live"] == 0.0 and d["targets"]["demo_funding_arb_1x"] == 0.0
    assert O.sizing_capital("demo_e5_min5_x175_t05top20", 5000.0, now=NOW, plan_doc=d) == 8000.0
    assert O.sizing_capital("demo_r2s_top10_3k", 3000.0, now=NOW, plan_doc=d) == 0.0
    assert O.sizing_capital("u1_live", None, now=NOW, plan_doc=d) == 0.0
    assert O.sizing_capital("demo_funding_arb_1x", 1000.0, now=NOW, plan_doc=d) == 0.0


def test_quote_pool_never_exceeds_the_recorded_pool(monkeypatch):
    d = _flat(monkeypatch)
    d = {**d, "targets": {**d["targets"], "demo_e5_min5_x175_t05top20": 99999.0}}
    assert O.sizing_capital("demo_e5_min5_x175_t05top20", 5000.0, now=NOW, plan_doc=d) == 8000.0


def test_usdc_and_fa_are_not_spendable_usdt_margin(monkeypatch):
    d = _flat(monkeypatch, {"E": 5000.0, "U": 806.0, "R": 3000.0, "F": 2500.0}, usdt_margin=4875.17)
    assert d["eligible"]["usdc_excluded"] == 806.0
    assert d["eligible"]["fa_excluded"] == 2500.0
    assert d["eligible"]["usd"] == pytest.approx(3875.17)
    assert d["targets"]["demo_e5_min5_x175_t05top20"] == pytest.approx(3875.17)
    assert d["targets"]["demo_r2s_top10_3k"] == 0.0
    assert d["eligible"]["vbo_in_pool"] is False


def test_regime_interplay_keeps_the_old_formula(monkeypatch):
    px = [1.0]
    monkeypatch.setattr(O, "regime", lambda c: {"bar": "d", "bear": True, "hivol": False})
    b = O.plan(px, BOOKS, None, NOW, usdt_margin=9000.0)
    assert b["weights"]["demo_e5_min5_x175_t05top20"] == pytest.approx(0.5)
    assert b["weights"]["u1_live"] == 0.0
    assert b["weights"]["demo_r2s_top10_3k"] == 0.0
    assert b["weights"]["demo_funding_arb_1x"] == 0.0
    assert b["eligible"]["usd"] == 8000.0
    assert b["targets"]["demo_e5_min5_x175_t05top20"] == pytest.approx(4000.0)
    assert b["targets"]["demo_r2s_top10_3k"] == 0.0
    assert b["targets"]["demo_funding_arb_1x"] == 0.0
    assert b["targets"]["u1_live"] == 0.0

    monkeypatch.setattr(O, "regime", lambda c: {"bar": "d", "bear": False, "hivol": True})
    h = O.plan(px, BOOKS, None, NOW, usdt_margin=9000.0)
    assert h["exposure_mult"] == 0.7
    assert h["targets"]["demo_e5_min5_x175_t05top20"] == pytest.approx(5600.0)
    assert h["targets"]["demo_r2s_top10_3k"] == 0.0
    assert h["targets"]["u1_live"] == 0.0 and h["targets"]["demo_funding_arb_1x"] == 0.0

    monkeypatch.setattr(O, "regime", lambda c: {"bar": "d", "bear": True, "hivol": True})
    both = O.plan(px, BOOKS, None, NOW, usdt_margin=9000.0)
    assert both["targets"]["demo_e5_min5_x175_t05top20"] == pytest.approx(2800.0)
    assert both["targets"]["demo_r2s_top10_3k"] == 0.0
    assert both["targets"]["demo_funding_arb_1x"] == 0.0


def test_vbo_stays_out_of_the_weights():
    import demo_vbo1h as V
    assert V.SLOT not in O.SLOTS.values()
    assert V.CAPITAL == O.VBO_RESERVE_USDT == 1000.0
    assert O.BASE_W["F"] == 0.0 and O.BASE_W["E"] == 1.0 and O.BASE_W["R"] == 0.0 and O.BASE_W["U"] == 0.0


def test_policy_matches_allocation_slot_and_does_not_close():
    alloc = json.loads((Path(__file__).resolve().parents[2] / "config" / "allocation.json").read_text())
    live = [s for s in alloc["slots"] if s.get("family") == "btc_eth_trend_voltarget_perp" and s.get("enabled")]
    assert [s["slot"] for s in live] == [O.U1_PAUSE_SLOT]
    pol = O.policy_public()
    assert pol["base_weights_keys"] == {"E": 1.0, "U": 0.0, "R": 0.0, "F": 0.0}
    assert pol["base_weights_effective"] == "2026-11-12T08:05:00+08:00"
    assert pol["u1_pause"]["closes_positions"] is False and pol["u1_pause"]["revokes_approval"] is False
    assert pol["r2s_pause"]["closes_positions"] is False and pol["r2s_pause"]["revokes_approval"] is False
    assert pol["vbo"]["in_orchestrator_weights"] is False
    assert pol["regimes"]["bear"]["weights_keys"]["F"] == 0.0
    assert pol["regimes"]["bear"]["weights_keys"]["E"] == pytest.approx(0.5)
    assert pol["regimes"]["bear"]["weights_keys"]["R"] == 0.0
    assert pol["regimes"]["hivol"]["exposure_mult"] == 0.7
    import demo_e5
    assert demo_e5.LV == 1.75


def test_u1_pause_keeps_approval_positions_and_is_idempotent():
    st = {"approved_families": ["btc_eth_trend_voltarget_perp"], "approved": ["keep-me"],
          "u1_live": {"code": "U1", "slot": "u1_btc_eth_usdc", "quote": "USDC", "cash": 200.0,
                      "units": {"BTC": 0.01, "ETH": 0.02}}}
    out = O.apply_u1_pause(st, now_iso="2026-11-10T18:14:00+00:00")
    assert out["applied"] is True and out["orders"] == 0
    assert st["approved_families"] == ["btc_eth_trend_voltarget_perp"]
    assert st["approved"] == ["keep-me"]
    assert st["u1_live"]["units"] == {"BTC": 0.01, "ETH": 0.02}
    assert st["paused_slots"]["u1_btc_eth_usdc"]["closes_positions"] is False
    assert st["paused_slots"]["u1_btc_eth_usdc"]["paused_by"] == "policy"
    assert main.slot_paused(st, "u1_btc_eth_usdc")
    assert st["ledger_adjustments"][-1]["kind"] == "u1_pause" and st["ledger_adjustments"][-1]["orders"] == 0
    # resume is the existing control; the migration does not put the pause back
    st["paused_slots"].pop("u1_btc_eth_usdc")
    again = O.apply_u1_pause(st, now_iso="2026-11-12T00:00:00+00:00")
    assert again["already"] is True and "u1_btc_eth_usdc" not in st["paused_slots"]
    assert st["u1_live"]["units"]["BTC"] == 0.01


def test_weight_zero_does_not_flatten_and_pause_blocks_increases_only():
    sleeve = {"quote": "USDC", "cash": 600.0, "units": {"BTC": 4.0, "ETH": 0.0}, "capital_usdt": 1000.0}
    opens = {"BTC": 100.0, "ETH": 100.0}
    # V = 1000, BTC weight 0.40. In band against target 0.40. Orchestrator target 0 must not flatten.
    assert U.orchestrator_size_equity(0.0, sleeve, opens) is None
    assert U.orchestrator_size_equity(None, sleeve, opens) is None
    assert U.orchestrator_size_equity(500.0, sleeve, opens) == pytest.approx(500.0)
    size = U.orchestrator_size_equity(0.0, sleeve, opens)
    gross, pos = U.caps_for(sleeve["capital_usdt"])
    held, skips = U.plan_orders(sleeve, {"BTC": 0.40, "ETH": 0.0}, opens, cap_gross=gross, cap_pos=pos,
                                allow_increase=False, size_equity=size)
    assert held == [] and not any(s.get("reason") == "cap_trim" or "cap_gross" in str(s.get("reason")) for s in skips)

    # The hazard the helper exists to avoid: a zero gross cap trims even while increases are blocked.
    flat, _ = U.plan_orders(sleeve, {"BTC": 0.40, "ETH": 0.0}, opens, cap_gross=0.0, cap_pos=0.0,
                            allow_increase=False, size_equity=0.0)
    assert flat and all(o["reduce_only"] and o["reason"] == "cap_trim" for o in flat)

    fresh = {"quote": "USDC", "cash": 1000.0, "units": {"BTC": 0.0, "ETH": 0.0}, "capital_usdt": 1000.0}
    opened, skips = U.plan_orders(fresh, {"BTC": 0.5, "ETH": 0.0}, opens, cap_gross=1e9, cap_pos=1e9,
                                  allow_increase=False, size_equity=None)
    assert opened == [] and any(str(s.get("reason", "")).startswith("increase_blocked") for s in skips)

    # Reducing rebalance is existing pause semantics (positions still managed), not a new close-all.
    down, _ = U.plan_orders(sleeve, {"BTC": 0.20, "ETH": 0.0}, opens, cap_gross=1e9, cap_pos=1e9,
                            allow_increase=False, size_equity=None)
    assert len(down) == 1 and down[0]["reduce_only"] and down[0]["reason"] == "rebalance_band"
    up, skips = U.plan_orders(sleeve, {"BTC": 0.90, "ETH": 0.0}, opens, cap_gross=1e9, cap_pos=1e9,
                              allow_increase=False, size_equity=None)
    assert up == [] and any(str(s.get("reason", "")).startswith("increase_blocked") for s in skips)


def test_e_band_holds_twelve_percent_and_r2s_band_crosses_the_old_gap():
    # Position sized at equity 5000, weight 0.4, price 100. New sizing 5600 is inside the 25% band.
    units, orders = demo_e5.plan_u1([20.0, 0.0], [0.4, 0.0], 1.0, 1.0, [100.0, 100.0], 5600.0)
    assert orders == [] and units[0] == pytest.approx(20.0)
    # R2S sized at the old 1500 target, new equity 2400: |delta| > 25% of the new notional.
    got = demo_r2s.plan({"AUSDT": 0.2}, {"AUSDT": 300.0}, 2400.0)
    assert got and got[0]["kind"] == "order" and got[0]["abs_notional"] == pytest.approx(180.0)


def test_runner_pause_predicate_blocks_the_u1_slot_only():
    st = {}
    O.apply_u1_pause(st, now_iso="t")
    assert main._allow_entry_for_slot(st, "u1_btc_eth_usdc", paused=False)[1] == "slot_paused_no_new_entries"
    # A different slot is not paused by this migration. It still fails closed (not approved), not by this pause.
    ok_reason = main._allow_entry_for_slot(st, "demo_r2s_top10_3k", paused=False)[1]
    assert ok_reason != "slot_paused_no_new_entries"
