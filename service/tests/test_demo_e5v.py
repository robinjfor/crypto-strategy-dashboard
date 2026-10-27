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


def test_symbol_fix_zeroes_variant_leg_once():
    st = {"demo_e5v_exec": {"cash": 100.0, "book": {"USUSDT": -1265.0, "ETHUSDT": 1.0}}}
    inv = {"symbol": "USUSDT", "exchange_position": 0.0, "close_px": 0.01, "fees": 0.0, "cause": "liquidation", "exchange_realized_pnl": -1.0}
    r = M.apply_symbol_fix(st, inv, V.STATE_KEY, V.SLOT, "us_1026", "t")
    assert st["demo_e5v_exec"]["book"] == {"ETHUSDT": 1.0} and abs(st["demo_e5v_exec"]["cash"] - 87.35) < 1e-9 and r["adjustment"]["cause"] == "liquidation"
    assert st["control_log"][-1]["action"] == "ledger_adjustment"
    M.apply_symbol_fix(st, inv, V.STATE_KEY, V.SLOT, "us_1026", "t2")
    assert len(st["ledger_adjustments"]) == 1


def test_huma_fix_registered_long_leg():
    assert M.FIXES["hu_1027"] == ("HUMAUSDT",) and "us_1026" in M.FIXES
    st = {"demo_e5v_exec": {"cash": 10.0, "book": {"HUMAUSDT": 347.0}}}
    inv = {"symbol": "HUMAUSDT", "exchange_position": 0.0, "close_px": 0.02, "fees": 0.01, "cause": "adl", "exchange_realized_pnl": 0.5}
    M.apply_symbol_fix(st, inv, V.STATE_KEY, V.SLOT, "hu_1027", "t")
    assert st["demo_e5v_exec"]["book"] == {} and abs(st["demo_e5v_exec"]["cash"] - (10.0 + 347 * 0.02 - 0.01)) < 1e-9
    assert st["ledger_adjustments"][-1]["id"].startswith("hu_1027")


def test_run_cloud_e5_only_dispatch(monkeypatch):
    import importlib, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shadow_ab"))
    rc = importlib.import_module("run_cloud")
    called = {}
    monkeypatch.setattr(rc, "e5_only", lambda: called.setdefault("e5", 0))
    monkeypatch.setenv("E5_ONLY", "1"); monkeypatch.delenv("FA_ONLY", raising=False)
    assert rc.main() == 0 and "e5" in called


def test_exec_skips_close_of_leg_missing_on_exchange(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT + ";" + V.SLOT)
    st = _st(); st[V.STATE_KEY] = {"cash": 5000.0, "book": {"SOLUSDT": 3.0}}
    f = Fut(PX)                                   # exchange flat: the ledger's SOL leg is gone
    r = X.execute(st, f, _tgt(ws={}, dbs={}), NOW, mod=V)
    assert not any(o["symbol"] == "SOLUSDT" for o in f.orders) and "SOLUSDT leg missing on exchange" in r["skipped"]
    assert st[V.STATE_KEY]["book"]["SOLUSDT"] == 3.0
    assert any(e["action"] == "e5_skip_missing_leg" for e in st["control_log"])


class _Store:
    def __init__(self, s): self.s = s
    def load(self): return json.loads(json.dumps(self.s))
    def mutate(self, f): self.s = f(self.load()); return self.s


class _PhFut(_InvFut):
    def __init__(self):
        super().__init__(force=[{"orderId": 7, "autoCloseType": "ADL"}]); self.pos = -347.0; self.sent = []
    def exchange_info(self): return {"symbols": [{"symbol": "HUMAUSDT", "status": "TRADING"}]}
    def position_risk(self, s=None): return [{"symbol": "HUMAUSDT", "positionAmt": str(self.pos)}]
    def get_order(self, s, c):
        return {"orderId": 90 if c.startswith("net-") else 91, "status": "FILLED", "executedQty": "347", "avgPrice": "0.022", "updateTime": 2 * 10**12}
    def new_order(self, **p):
        self.sent.append(p); self.pos += float(p["quantity"]); return {"status": "FILLED"}
    def _signed(self, m, path, p=None):
        if path.endswith("userTrades") and p.get("orderId") == 90: return [{"qty": "347", "price": "0.022", "commission": "0.004"}]
        if path.endswith("userTrades") and p.get("orderId") == 91: return [{"qty": "347", "price": "0.023", "commission": "0.004"}]
        if path.endswith("userTrades"): return [{"side": "SELL", "qty": "347", "price": "0.02", "realizedPnl": "-1", "commission": "0", "orderId": 7, "time": 1}] if p["startTime"] < 10**13 and not getattr(self, "_t", 0) and not setattr(self, "_t", 1) else []
        return super()._signed(m, path, p)


def test_phantom_repair_flattens_orphan_and_rebooks_once():
    store = _Store({"migrations": {M.MIG: {"done": True}}, V.STATE_KEY: {"cash": 100.0, "book": {}}})
    f = _PhFut()
    r = M.run_fixes(store, f)
    assert f.sent and f.sent[0]["reduceOnly"] == "true" and f.sent[0]["side"] == "BUY" and f.pos == 0.0
    adj = r[-1]; assert adj["cause"] == "ADL" and adj["close_px"] == 0.02
    exp = (347 * 0.02 - 0) + 347 * (0.022 - 0.023) - 0.008 - (347 * 0.022 * 0.9995)
    assert abs(store.s[V.STATE_KEY]["cash"] - (100.0 + exp)) < 1e-6
    assert store.s["migrations"]["hu_1027"]["done"] and store.s["ledger_adjustments"][-1]["id"] == "hu_1027-HUMAUSDT"
    M.run_fixes(store, f); assert len(f.sent) == 1


def test_phantom_repair_skips_when_exchange_not_orphan():
    store = _Store({"migrations": {M.MIG: {"done": True}}, V.STATE_KEY: {"cash": 100.0, "book": {}}})
    f = _PhFut(); f.pos = 0.0
    M.run_fixes(store, f)
    assert not f.sent and "exchange position" in store.s["migrations"]["hu_1027"]["skipped"] and store.s[V.STATE_KEY]["cash"] == 100.0
