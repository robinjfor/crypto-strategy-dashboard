import json
import os
from datetime import datetime, timezone

import numpy as np
import pytest

import demo_e5 as D
import demo_e5v as V
import demo_e5_exec as X
import migrate_e5v as M
import netting
from tests.test_demo_e5_exec import Fut, PX, NOW, _tgt

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _st():
    return {"demo_candidates": {V.SLOT: {"enabled": True, "approved": True, "parity_pass": True, "cloud_ordering_ok": True}}}


def test_topn_keeps_top20_and_rescales_gross():
    w = np.array([(-1) ** i * (i + 1) for i in range(30)], dtype=float)
    k, sc = V.topn_weights(w, 20)
    assert (k != 0).sum() == 20 and set(np.nonzero(k)[0]) == set(range(10, 30))
    assert abs(sc - np.abs(w).sum() / np.abs(w[10:]).sum()) < 1e-12
    k2, sc2 = V.topn_weights(w[:15], 20)          # topn >= n -> unchanged
    assert sc2 == 1.0 and np.array_equal(k2, w[:15])


def test_thr_skips_small_adjust_but_not_close_or_open():
    # es=1000 -> thr notional 5U; MN 1. A 3U adjust of an existing position is skipped (thr), a 3U open is not.
    u5 = np.array([1.0, 0.0]); ov = np.array([100.0, 100.0])
    nu, od = V.plan_s5(u5, ov, np.array([0.103 / 1.75, 0.03 / 1.75]), np.zeros(2), 1000.0, 1.75, 1.0, np.array([1.0, 1.0]), None, thr=0.005, topn=0)
    kinds = {c: kd for c, _, _, kd in od}
    assert kinds[0] == "skip_thr" and nu[0] == 1.0 and kinds[1] == "order" and nu[1] > 0
    nu, od = V.plan_s5(u5, ov, np.zeros(2), np.zeros(2), 1000.0, 1.75, 1.0, np.array([1.0, 1.0]), None, thr=0.005, topn=0)
    assert nu[0] == 0.0 and od[0][3] in ("close", "order")


def test_exec_uses_variant_slot_and_ledger(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", V.SLOT)
    monkeypatch.setattr(X, "entry_block", lambda s, *a: None)
    st, f = _st(), Fut(PX)
    out = X.execute(st, f, _tgt(), NOW, mod=V)
    assert out["slot"] == V.SLOT and "demo_e5v_exec" in st and "demo_e5_exec" not in st
    plan = V.plan_live(_tgt(), {}, PX, 5000.0, m=1.0, lt=1.0, mins=_tgt()["min_notional"], steps={"BTCUSDT": 0.001, "ETHUSDT": 0.001, "SOLUSDT": 0.1, "DOGEUSDT": 0.1})
    sent = {o["symbol"]: o["delta_qty"] for o in plan if o["kind"] == "order"}
    got = {o["symbol"]: float(o["quantity"]) * (1 if o["side"] == "BUY" else -1) for o in f.orders}
    assert sent.keys() == got.keys() and all(abs(sent[s] - got[s]) < 1e-9 for s in sent)
    assert X.execute(st, f, _tgt(), NOW, mod=V)["skipped"] == "already executed"


def test_variant_not_accepted_or_paused(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)          # only the old slot accepted
    assert X.execute(_st(), Fut(PX), _tgt(), NOW, mod=V)["skipped"] == "未驗收"


def test_netting_slots_include_variant():
    assert netting.SLOTS[V.SLOT] == "demo_e5v_exec"


def _old_state():
    return {"demo_candidates": {D.SLOT: {"approved": True, "enabled": True}},
            "demo_e5_exec": {"cash": 2449.58, "book": {"APRUSDT": -652.0, "ETHUSDT": 1.044, "APEUSDT": -48.0}, "last_bar": "2026-10-25", "funding_since": 1},
            "demo_r2s_exec": {"cash": 3000.0, "book": {"NEARUSDT": 21.0}}}


def test_migration_transfers_ledger_and_zeroes_apr():
    st = _old_state()
    inv = {"symbol": "APRUSDT", "exchange_position": 0.0, "close_px": 0.1, "fees": 0.02, "cause": "outside_close", "detail": "", "exchange_realized_pnl": 3.0}
    before = netting.virtual_net(st)
    rec = M.apply(st, inv, V.STATE_KEY, "demo_e5_exec", V.SLOT, D.SLOT, "2026-10-26T03:00:00+00:00")
    after = netting.virtual_net(st)
    assert rec["net_unchanged"] and set(rec["net_diff"]) == {"APRUSDT"}
    assert after == {k: v for k, v in before.items() if k != "APRUSDT"}
    n = st["demo_e5v_exec"]
    assert n["book"] == {"ETHUSDT": 1.044, "APEUSDT": -48.0} and abs(n["cash"] - (2449.58 - 65.2 - 0.02)) < 1e-9 and n["last_bar"] == "2026-10-25"
    assert st["demo_e5_exec"]["book"] == {} and st["demo_e5_exec"]["cash"] == 0.0
    assert st["demo_candidates"][D.SLOT]["approved"] is True          # approval kept
    a = [x for x in st["ledger_adjustments"] if x["id"].endswith("APRUSDT")][0]
    assert a["cause"] == "outside_close" and a["qty_after"] == 0.0
    assert {e["action"] for e in st["control_log"]} >= {"ledger_adjustment", "ledger_transfer"}
    # idempotent
    snap = json.dumps(st, sort_keys=True)
    M.apply(st, inv, V.STATE_KEY, "demo_e5_exec", V.SLOT, D.SLOT, "later")
    assert json.dumps(st, sort_keys=True) == snap


def test_migration_no_adjust_when_exchange_still_holds():
    st = _old_state()
    M.apply(st, {"symbol": "APRUSDT", "exchange_position": -652.0, "close_px": 0.1}, V.STATE_KEY, "demo_e5_exec", V.SLOT, D.SLOT, "t")
    assert st["demo_e5v_exec"]["book"]["APRUSDT"] == -652.0


class _InvFut:
    def __init__(self, status="TRADING", coid="web_abc", force=None):
        self.status, self.coid, self.force = status, coid, force or []
    def exchange_info(self): return {"symbols": [{"symbol": "APRUSDT", "status": self.status}]}
    def position_risk(self, s=None): return [{"symbol": "APRUSDT", "positionAmt": "0"}]
    def all_orders(self, *a, **k): return [{"orderId": 7, "clientOrderId": self.coid}]
    def mark_price(self, s): return 0.11
    def _public(self, p, q=None): return [[0, 0, 0, 0, "0.12"]]
    def _signed(self, m, path, p=None):
        if path.endswith("userTrades"): return [{"side": "BUY", "qty": "652", "price": "0.1", "realizedPnl": "3", "commission": "0.02", "orderId": 7, "time": 1}] if p["startTime"] < 10**13 and not getattr(self, "_t", 0) and not setattr(self, "_t", 1) else []
        if path.endswith("forceOrders"): return self.force
        return []


@pytest.mark.parametrize("fut,cause", [(_InvFut(), "outside_close"), (_InvFut(coid="net-APRUSDT-1"), "missed_fill"),
                                       (_InvFut(status="SETTLING"), "delist_settlement"),
                                       (_InvFut(force=[{"orderId": 7, "autoCloseType": "ADL"}]), "ADL"),
                                       (_InvFut(force=[{"orderId": 7, "autoCloseType": "LIQUIDATION"}]), "liquidation")])
def test_investigate_classifies(fut, cause):
    inv = M.investigate(fut, "APRUSDT", -652.0)
    assert inv["cause"] == cause and inv["exchange_position"] == 0.0
    assert abs(inv["close_px"] - 0.1) < 1e-12 and inv["exchange_realized_pnl"] == 3.0


def test_backtest_parity_committed_and_passes():
    p = json.load(open(os.path.join(HERE, "shadow_ab", "parity_E5V.json")))
    assert p["pass"] and p["day_mismatches"] == 0 and p["live_trades"] == p["ledger_trades"] > 0
    assert p["spec"] == V.SPEC and p["skip_rate"] == p["ledger_skip_rate"]


@pytest.mark.skipif(not os.path.isdir("/workspace/strategy-unified-3y/spec_2026-11/research_minnot_1026"), reason="research data not on this host")
def test_backtest_parity_live_rerun():
    import importlib.util
    spec = importlib.util.spec_from_file_location("e5vp", os.path.join(HERE, "shadow_ab", "e5v_parity.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    import shutil, tempfile
    keep = os.path.join(HERE, "shadow_ab", "parity_E5V.json"); bak = tempfile.mktemp(); shutil.copy(keep, bak)
    try:
        p = m.main()
    finally:
        shutil.copy(bak, keep)
    assert p["pass"] and p["day_mismatches"] == 0
