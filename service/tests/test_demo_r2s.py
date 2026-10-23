import json
from pathlib import Path
import numpy as np
import demo_r2s as D
import demo_candidates


def test_slot_disabled_not_listed_or_accepted(monkeypatch):
    monkeypatch.delenv("DEMO_ACCEPTED", raising=False)
    assert D.SLOT not in {c["id"] for c in demo_candidates.CANDIDATES}
    assert not demo_candidates.accepted(D.SLOT)
    st = {"demo_candidates": {D.SLOT: {"parity_pass": True, "cloud_ordering_ok": True}}}
    assert D.active(st) == (False, "未啟用")
    st["demo_candidates"][D.SLOT]["enabled"] = True
    assert D.active(st) == (False, "未批准")
    assert D.CAPITAL == 1000.0 and D.LEVERAGE == 1.0 and D.MIN_ORDER == 5.0


def test_plan_band_min5_close_flip():
    eq = 1000.0
    o = {x["symbol"]: x for x in D.plan({"A": 0.1, "B": 0.1, "C": 0.0, "D": -0.05, "E": 0.004, "F": 0.104}, {"A": 70.0, "B": 98.0, "C": 2.0, "D": 30.0, "F": 80.0}, eq)}
    assert o["A"]["kind"] == "order" and abs(o["A"]["notional"] - 30) < 1e-9   # band 25% exceeded
    assert "B" not in o                                                           # inside band
    assert o["C"]["kind"] == "close" and o["C"]["abs_notional"] < 5               # target 0: close even < 5U
    assert o["D"]["kind"] == "order"                                              # flip
    assert o["E"]["kind"] == "order"                                              # open from 0 (sign change) is sent
    o2 = D.plan({"A": 0.01}, {"A": 6.0}, eq)
    assert o2[0]["kind"] == "skip_min"                                            # 4U adjustment > band but < 5U


def test_exchange_filter_and_log():
    orders = [{"symbol": "BTCUSDT", "notional": 60.0, "abs_notional": 60.0, "kind": "order"},
              {"symbol": "BTCUSDT", "notional": -3.0, "abs_notional": 3.0, "kind": "close"},
              {"symbol": "XRPUSDT", "notional": 6.0, "abs_notional": 6.0, "kind": "order"},
              {"symbol": "XRPUSDT", "notional": 2.0, "abs_notional": 2.0, "kind": "skip_min"}]
    send, blk = D.exchange_filter(orders, {"BTCUSDT": 100.0})
    assert [b["symbol"] for b in blk] == ["BTCUSDT"]
    assert [(s["symbol"], s["reduce_only"]) for s in send] == [("BTCUSDT", True), ("XRPUSDT", False)]
    st = {}; D.log_blocked(st, blk, "2026-10-23")
    assert st["control_log"][0]["action"] == "r2s_order_blocked_min"


def test_min_divergence_counts():
    led = [("d", "BTCUSDT", 0.1, 0.0, 60.0), ("d", "BTCUSDT", 0.0, 0.1, -60.0), ("d", "ETHUSDT", 0.1, 0.0, 25.0), ("d", "X", 0.1, 0.0, 3.0)]
    d = D.min_divergence(led, {"BTCUSDT": 100.0, "ETHUSDT": 20.0})
    assert d["n"] == 1 and d["by_symbol"] == {"BTCUSDT": 1, "X": 1} and d["lt5_opens"] == 1


def test_regime_hysteresis_and_engine():
    adx = np.array([[10.0], [24.0], [26.0], [22.0], [16.0]]); sl = np.array([[0.01], [0.01], [0.001], [-0.001], [-0.01]])
    assert D.regime(adx, sl, 20, 3).ravel().tolist() == [0, 1, 1, 1, 0]
    c = np.array([[1.0], [2.0], [3.0], [3.5], [1.0]]); atr = np.ones((5, 1)); reg = np.ones((5, 1), dtype=int)
    hi = np.array([[np.nan], [1.0], [2.0], [3.0], [3.5]]); P = D.sub_engine(c, atr, reg, np.zeros((5, 1), bool), hi, hi, np.ones((5, 1), bool), 2.0, 0.5)
    assert P.ravel().tolist() == [0.0, 1.0, 1.0, 1.0, 0.0]


def test_committed_parity_passes():
    p = json.load(open(Path(D.__file__).parent / "shadow_ab" / "parity_R2S.json"))
    assert p["pass"] and p["mismatches"] == 0 and p["orders"] == p["ledger_orders"] > 0


class _Fut:
    def __init__(self, held=None):
        self.pos = dict(held or {}); self.orders = []
    def exchange_info(self):
        f = lambda mn: [{"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"}, {"filterType": "MIN_NOTIONAL", "notional": str(mn)}]
        return {"symbols": [{"symbol": s, "filters": f(mn)} for s, mn in (("BTCUSDT", 100), ("ETHUSDT", 20), ("XRPUSDT", 5), ("ADAUSDT", 5), ("LINKUSDT", 5))]}
    def mark_price(self, s): return 1.0
    def position_risk(self): return [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()]
    def new_order(self, **kw):
        self.orders.append(kw); q = kw["quantity"] * (1 if kw["side"] == "BUY" else -1)
        self.pos[kw["symbol"]] = round(self.pos.get(kw["symbol"], 0) + q, 9); return {"status": "FILLED", "orderId": len(self.orders)}


def test_order_test_opens_closes_flat_skips_held(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    f = _Fut({"LINKUSDT": 5.0})
    r = D.order_test(f)
    assert r["pass"] and r["flat"] and r["orders"] == 4 and r["filled"] == 4 and r["blocked_min"] == 2
    assert "skipped" in r["symbols"]["LINKUSDT"] and f.pos["LINKUSDT"] == 5.0
    assert not any(o["symbol"] in ("BTCUSDT", "ETHUSDT") for o in f.orders)
    assert all(o.get("reduceOnly") == "true" for o in f.orders[1::2])
