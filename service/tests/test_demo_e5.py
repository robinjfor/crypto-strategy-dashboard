import numpy as np
import demo_e5 as D
import demo_candidates


def test_slot_not_listed_or_accepted(monkeypatch):
    monkeypatch.delenv("DEMO_ACCEPTED", raising=False)
    assert D.SLOT in {c["id"] for c in demo_candidates.CANDIDATES}
    assert not demo_candidates.accepted(D.SLOT)  # only via DEMO_ACCEPTED env on the service
    st = {"demo_candidates": {D.SLOT: {"parity_pass": True, "cloud_ordering_ok": True}}}
    assert D.active(st) == (False, "未啟用")
    assert D.CAPITAL == 5000.0 and D.MIN_ORDER == 5.0


def test_u1_min_order_skip_and_full_close():
    o2 = np.array([100.0, 10.0]); eu = 1000.0
    # target 0 with tiny residual -> full close even if < 5U
    uu, od = D.plan_u1([0.01, 0.0], [0.0, 0.0], 1.0, 1.0, o2, eu)
    assert uu.tolist() == [0.0, 0.0] and od[0][3] == "close" and od[0][2] < 5
    # nonzero target, adjustment < 5U -> skipped, position kept
    uu, od = D.plan_u1([0.0, 0.0], [0.004, 0.0], 1.0, 1.0, o2, eu)
    assert uu.tolist() == [0.0, 0.0] and od[0][3] == "skip_min"


def test_leg_minnot_rules():
    # step truncation toward 0, then adjust < mn skipped
    assert D.plan_leg(0.0, 0.0567, 1000.0, 50.0, 0.01, False) == (0.05, [(0.05, 50.0, "order")])
    q, od = D.plan_leg(0.05, 0.054, 1000.0, 50.0, 0.0, False); assert q == 0.05 and od[0][2] == "skip_min"
    # residual below mn -> full close
    q, od = D.plan_leg(0.06, 0.005, 1000.0, 50.0, 0.0, False); assert q == 0.0 and od == [(-0.06, 60.0, "residual_close")]
    # flip: close, open only if >= mn
    q, od = D.plan_leg(0.06, -0.01, 1000.0, 50.0, 0.0, False); assert q == 0.0 and [k for *_, k in od] == ["close", "skip_min"]
    q, od = D.plan_leg(0.06, -0.1, 1000.0, 50.0, 0.0, False); assert q == -0.1 and [k for *_, k in od] == ["close", "order"]
    # target 0 -> full close regardless of size
    q, od = D.plan_leg(0.001, 0.0, 1000.0, 50.0, 0.0, True); assert q == 0.0 and od[0][2] == "close"
    send, blk = D.exchange_filter([{"symbol": "BTCUSDT", "delta_qty": -0.001, "notional": 1.0, "kind": "residual_close"}], {"BTCUSDT": 50.0})
    assert not blk and send[0]["reduce_only"]


def test_s5_buffer_and_min():
    ov = np.array([1.0, 2.0, np.nan]); u5 = np.array([0.0, 3.0, 1.0])
    ws = np.array([0.1, 0.0, 0.5]); dbs = np.array([0.0, 0.0, 0.0])
    nu, od = D.plan_s5(u5, ov, ws, dbs, 100.0, 1.0, 1.0)
    assert nu[0] == 10.0 and nu[1] == 0.0 and nu[2] == 1.0
    assert [k for *_, k in od] == ["order", "close"]


def test_exchange_filter_blocks_below_symbol_min():
    orders = [{"symbol": "BTCUSDT", "delta_qty": 1e-4, "notional": 30.0, "kind": "order"},
              {"symbol": "BTCUSDT", "delta_qty": -1e-4, "notional": 3.0, "kind": "close"},
              {"symbol": "XUSDT", "delta_qty": 1.0, "notional": 6.0, "kind": "order"},
              {"symbol": "XUSDT", "delta_qty": 1.0, "notional": 2.0, "kind": "skip_min"}]
    send, blk = D.exchange_filter(orders, {"BTCUSDT": 100.0})
    assert [b["symbol"] for b in blk] == ["BTCUSDT"]
    assert [(s["symbol"], s["reduce_only"]) for s in send] == [("BTCUSDT", True), ("XUSDT", False)]
    st = {}; D.log_blocked(st, blk, "2026-10-22")
    assert st["control_log"][0]["action"] == "e5_order_blocked_min"


def test_parity_detects_mismatch_and_divergence():
    o2 = np.array([100.0, 10.0]); r = np.zeros(5)
    mn = np.array([50.0, 20.0]); stp = np.zeros(2)
    uu_exp, _ = D.plan_u1([0.0, 0.0], [0.5, 0.1], 1.0, 1.0, o2, 400.0, mn, stp)
    cap = {"cols": ["BTCUSDT", "ETHUSDT"], "idx": ["d0", "d1"], "iB": 0, "iE": 1, "stats": [2], "capture_vs_jit_max_rel": 0.0,
           "mn": mn, "step": stp,
           "rec": [("u1", 1, r, 1.0, np.zeros(2), o2, 400.0, np.array([0.5, 0.1])), ("u1post", 1, uu_exp, 1.0)]}
    p = D.parity(cap, {"BTCUSDT": 100.0, "ETHUSDT": 20.0})
    assert p["pass"] and p["mismatches"] == 0 and p["exchange_min_divergence"]["n"] == 0
    p2 = D.parity(cap, {"BTCUSDT": 1000.0, "ETHUSDT": 20.0})
    assert p2["exchange_min_divergence"]["by_symbol"] == {"BTCUSDT": 1}
    cap["rec"][1] = ("u1post", 1, uu_exp * 1.0001, 1.0)
    assert not D.parity(cap)["pass"]


class _Fut:
    def __init__(self, held=None):
        self.pos = dict(held or {}); self.orders = []
    def exchange_info(self):
        f = lambda mn: [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}, {"filterType": "MIN_NOTIONAL", "notional": str(mn)}]
        return {"symbols": [{"symbol": s, "filters": f(mn)} for s, mn in (("BTCUSDT", 100), ("ETHUSDT", 20), ("SOLUSDT", 5), ("DOGEUSDT", 5))]}
    def mark_price(self, s): return {"BTCUSDT": 1000.0, "ETHUSDT": 100.0}.get(s, 10.0)
    def position_risk(self): return [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()]
    def new_order(self, **kw):
        self.orders.append(kw); q = kw["quantity"] * (1 if kw["side"] == "BUY" else -1)
        self.pos[kw["symbol"]] = round(self.pos.get(kw["symbol"], 0) + q, 9); return {"status": "FILLED", "orderId": len(self.orders)}


def test_order_test_opens_closes_flat_and_skips_held(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    f = _Fut({"DOGEUSDT": 5.0})
    r = D.order_test(f)
    assert r["pass"] and r["flat"] and r["orders"] == 6 and r["filled"] == 6
    assert r["blocked_min"] == 2  # BTC 100U, ETH 20U block a 6U order
    assert "skipped" in r["symbols"]["DOGEUSDT"] and f.pos["DOGEUSDT"] == 5.0
    assert all(o.get("reduceOnly") == "true" for o in f.orders[1::2])
