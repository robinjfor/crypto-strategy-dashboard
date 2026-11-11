"""E5's internal U1 sleeve and S5 sleeve must not both close BTC/ETH.

The backtest keeps uu (U1) and u5 (S5) as separate books. The live slot has one qty.
e5_targets.ws lists BTCUSDT and ETHUSDT at weight 0; feeding that qty into plan_s5
emitted a second full close, and the netter summed them into a flip.

This U1 sleeve is not the paused USDC slot u1_btc_eth_usdc.
"""
from datetime import datetime, timezone

import numpy as np
import pytest

import demo_e5 as D
import demo_e5v as V
import demo_e5_exec as X
import netting
import orchestrator as O
from tests.test_demo_e5_exec import Fut, NOW

ETH, BTC = 0.841, 0.02
PX = {"BTCUSDT": 100000.0, "ETHUSDT": 4000.0, "SOLUSDT": 200.0}


def _tgt(**kw):
    t = {"decided_bar": "2026-10-24", "w1": [0.0, 0.0], "m": 1.0, "rw": [0.0] * 10,
         "ws": {"ETHUSDT": 0.0, "BTCUSDT": 0.0, "SOLUSDT": 0.2},
         "dbs": {"ETHUSDT": 0.0, "BTCUSDT": 0.0, "SOLUSDT": 0.0},
         "min_notional": {"BTCUSDT": 50.0, "ETHUSDT": 20.0, "SOLUSDT": 5.0}, "step": {}}
    t.update(kw)
    return t


def _exec_deltas(plan, book):
    """Same qty rule as demo_e5_exec.execute: a full close uses the whole book, not a second copy."""
    rows = []
    for o in plan:
        if o["kind"] in ("skip_min", "skip_thr", "hold"):
            continue
        mine = float(book.get(o["symbol"], 0.0))
        full = o["kind"] in ("close", "residual_close")
        d = -mine if full else float(o["delta_qty"])
        rows.append({**o, "d": d})
    return rows


def test_backtest_s5_book_stays_flat_when_u1_holds_eth():
    """ledger_e_minnot.sim updates uu and u5 separately. Weight 0 on a flat S5 book emits nothing."""
    nu, od = D.plan_s5(np.array([0.0]), np.array([4000.0]), np.array([0.0]), np.array([0.0]), 1000.0, D.LV, 1.0)
    assert list(nu) == [0.0] and od == []
    _, od1 = D.plan_u1([-ETH, 0.0], [0.0, 0.0], 1.0, 1.0, np.array([4000.0, 100000.0]), 4000.0)
    assert len(od1) == 1 and od1[0][3] == "close" and od1[0][1] == ETH


def test_ws_zero_eth_does_not_add_an_s5_close():
    book = {"ETHUSDT": ETH}
    plan = V.plan_live(_tgt(), book, PX, 5000.0, m=1.0, lt=1.0)
    eth = [o for o in plan if o["symbol"] == "ETHUSDT" and o["kind"] not in ("skip_min", "skip_thr")]
    assert eth and {o["sleeve"] for o in eth} == {"u1"}
    assert _exec_deltas(plan, book) == [o for o in _exec_deltas(plan, book) if o["symbol"] != "ETHUSDT" or o["d"] == -ETH]
    eth_d = [o["d"] for o in _exec_deltas(plan, book) if o["symbol"] == "ETHUSDT"]
    assert eth_d == [-ETH]
    assert D.plan_live(_tgt(), book, PX, 5000.0, m=1.0, lt=1.0)
    eth_d0 = [o["d"] for o in _exec_deltas(D.plan_live(_tgt(), book, PX, 5000.0), book) if o["symbol"] == "ETHUSDT"]
    assert eth_d0 == [-ETH]


def test_btc_book_is_u1_only_when_ws_is_zero():
    book = {"BTCUSDT": BTC, "ETHUSDT": 0.0}
    plan = V.plan_live(_tgt(w1=[0.0, 0.5]), book, PX, 5000.0, m=1.0, lt=1.0)
    btc = [o for o in _exec_deltas(plan, book) if o["symbol"] == "BTCUSDT"]
    assert len(btc) == 1 and btc[0]["sleeve"] == "u1" and btc[0]["kind"] == "close" and btc[0]["d"] == -BTC
    assert not [o for o in plan if o["symbol"] == "BTCUSDT" and o["sleeve"] == "s5"]


def test_stray_eth_short_nets_to_one_cover_not_a_second_flip():
    """Book -0.841, U1 target flat. One BUY 0.841. A second sleeve close would buy 1.682 and flip to +0.841."""
    book = {"ETHUSDT": -ETH}
    plan = V.plan_live(_tgt(), book, PX, 5000.0, m=1.0, lt=1.0)
    rows = [o for o in _exec_deltas(plan, book) if o["symbol"] == "ETHUSDT"]
    assert len(rows) == 1 and rows[0]["sleeve"] == "u1" and rows[0]["d"] == ETH
    # exchange is E5 -0.841 plus R2S -0.071
    fut = Fut({**PX, "ETHUSDT": 4000.0}, pos={"ETHUSDT": -(ETH + 0.071)})
    st = {"demo_e5v_exec": {"cash": 5000.0, "book": dict(book)}}
    ex = st["demo_e5v_exec"]
    n = netting.Netter(fut, tag="e5")
    for o in rows:
        n.add(st, V.SLOT, "ETHUSDT", o["d"], PX["ETHUSDT"], netting.ledger_cb(ex, "ETHUSDT"), kind=o["kind"])
    sent = n.flush()
    assert len(sent) == 1
    assert sent[0]["side"] == "BUY" and sent[0]["qty"] == ETH and sent[0]["reduce_only"] is True
    assert "ETHUSDT" not in ex["book"]
    assert fut.pos["ETHUSDT"] == pytest.approx(-0.071)


def test_long_target_is_one_net_from_the_stray_short():
    """U1 target long: close + open are one sleeve. Net = target qty - book, not an extra 0.841."""
    book = {"ETHUSDT": -ETH}
    plan = V.plan_live(_tgt(w1=[0.0, 0.5]), book, PX, 5000.0, m=1.0, lt=1.0)
    rows = [o for o in _exec_deltas(plan, book) if o["symbol"] == "ETHUSDT"]
    assert rows and {o["sleeve"] for o in rows} == {"u1"}
    # eu = 0.8 * 5000, wt = 0.5, price 4000 → target qty 0.5
    assert sum(o["d"] for o in rows) == 0.5 - (-ETH)


def test_paused_usdc_u1_slot_does_not_change_the_e5_sleeve(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", V.SLOT)
    monkeypatch.setattr(X, "entry_block", lambda state, slot=None: None)
    O.load({})
    book = {"ETHUSDT": -ETH}
    paused = {"paused_slots": {"u1_btc_eth_usdc": {"paused_by": "policy", "closes_positions": False}},
              "u1_live": {"slot": "u1_btc_eth_usdc", "units": {"BTC": 0.01, "ETH": 0.2}, "quote": "USDC"},
              "demo_candidates": {V.SLOT: {"enabled": True, "approved": True, "parity_pass": True, "cloud_ordering_ok": True}},
              "demo_e5v_exec": {"cash": 5000.0, "book": dict(book)},
              "demo_r2s_exec": {"cash": 3000.0, "book": {"ETHUSDT": -0.071}}}
    fut = Fut(PX, pos={"ETHUSDT": -(ETH + 0.071)})
    out = X.execute(paused, fut, _tgt(), NOW, mod=V)
    eth = [o for o in fut.orders if o["symbol"] == "ETHUSDT"]
    assert out.get("skipped") in (None, []) or not isinstance(out.get("skipped"), str)
    assert len(eth) == 1 and eth[0]["side"] == "BUY" and float(eth[0]["quantity"]) == pytest.approx(ETH)
    assert paused["u1_live"]["units"]["ETH"] == 0.2
    assert "u1_btc_eth_usdc" in paused["paused_slots"]
