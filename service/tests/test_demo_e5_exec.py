from datetime import datetime, timezone

import demo_e5 as D
import demo_e5_exec as X

NOW = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)


def _info(syms):
    return {"symbols": [{"symbol": s, "status": "TRADING", "filters": [
        {"filterType": "MIN_NOTIONAL", "notional": {"BTCUSDT": "50", "ETHUSDT": "20"}.get(s, "5")},
        {"filterType": "LOT_SIZE", "stepSize": {"BTCUSDT": "0.001", "ETHUSDT": "0.001"}.get(s, "0.1"), "minQty": "0"}]} for s in syms]}


class Fut:
    def __init__(self, px, pos=None):
        self.px, self.pos, self.orders = px, dict(pos or {}), []

    def exchange_info(self): return _info(self.px)
    def mark_price(self, s): return self.px[s]
    def position_risk(self): return [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()]
    def income(self, *a, **k): return []
    def set_margin_type(self, *a, **k): return {}
    def set_leverage(self, *a, **k): return {}
    def get_order(self, s, c): return None

    def new_order(self, **p):
        q = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
        self.pos[p["symbol"]] = self.pos.get(p["symbol"], 0.0) + q; self.orders.append(p)
        return {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": str(self.px[p["symbol"]])}


def _state():
    return {"demo_candidates": {D.SLOT: {"enabled": True, "approved": True, "parity_pass": True, "cloud_ordering_ok": True}}}


def _tgt(bar="2026-10-24", **kw):
    t = {"decided_bar": bar, "w1": [0.5, 0.5], "ws": {"SOLUSDT": 0.5, "DOGEUSDT": 0.001}, "dbs": {"SOLUSDT": 0.01, "DOGEUSDT": 0.0},
         "m": 1.0, "rw": [0.0] * 10, "min_notional": {"BTCUSDT": 50.0, "ETHUSDT": 20.0}, "step": {}}
    t.update(kw); return t


PX = {"BTCUSDT": 100000.0, "ETHUSDT": 4000.0, "SOLUSDT": 200.0, "DOGEUSDT": 0.2}


def test_inactive_without_gates(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)
    st = _state(); st["demo_candidates"][D.SLOT]["approved"] = False
    assert X.execute(st, Fut(PX), _tgt(), NOW)["skipped"] == "未批准"
    monkeypatch.delenv("DEMO_ACCEPTED")
    assert X.execute(_state(), Fut(PX), _tgt(), NOW)["skipped"] == "未驗收"


def test_stale_targets_never_trade(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)
    f = Fut(PX)
    assert X.execute(_state(), f, _tgt("2026-10-21"), NOW)["skipped"].startswith("stale")
    assert X.execute(_state(), f, _tgt(m=None), NOW)["skipped"] == "targets missing m/rw" and not f.orders


def test_executes_plan_live_once_with_min_skip(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)
    monkeypatch.setattr(X, "entry_block", lambda s: None)
    st, f = _state(), Fut(PX)
    out = X.execute(st, f, _tgt(), NOW)
    syms = {o["symbol"] for o in f.orders}
    # U1: Lt=1 (<20 returns) -> 0.5*4000 per leg; S5: SOL 0.5*1.75*1000; DOGE 0.001*1.75*1000=1.75U < 5U -> skipped
    assert syms == {"BTCUSDT", "ETHUSDT", "SOLUSDT"} and out["skipped_lt_min"] == 1
    assert abs(st["demo_e5_exec"]["book"]["SOLUSDT"] - 4.3) < 1e-9 and abs(st["demo_e5_exec"]["book"]["BTCUSDT"] - 0.02) < 1e-9
    assert X.execute(st, f, _tgt(), NOW)["skipped"] == "already executed" and len(f.orders) == 3


def test_same_plan_as_parity_planner(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)
    monkeypatch.setattr(X, "entry_block", lambda s: None)
    st, f = _state(), Fut(PX)
    t = _tgt(); out = X.execute(st, f, t, NOW)
    plan = D.plan_live(t, {}, PX, 5000.0, m=1.0, lt=1.0, mins={**t["min_notional"]}, steps={"BTCUSDT": 0.001, "ETHUSDT": 0.001, "SOLUSDT": 0.1, "DOGEUSDT": 0.1})
    sent = {o["symbol"]: o["delta_qty"] for o in plan if o["kind"] == "order"}
    got = {o["symbol"]: float(o["quantity"]) * (1 if o["side"] == "BUY" else -1) for o in f.orders}
    assert sent.keys() == got.keys() and all(abs(sent[s] - got[s]) < 1e-9 for s in sent)


def test_foreign_holder_and_entry_block_skip_opens_but_not_closes(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)
    monkeypatch.setattr(X, "entry_block", lambda s: None)
    st = _state(); st["demo_fa_positions"] = {"SOLUSDT": {}}
    f = Fut(PX, {"ETHUSDT": -1.0})
    X.execute(st, f, _tgt(), NOW)
    assert {o["symbol"] for o in f.orders} == {"BTCUSDT"}
    # next day: target 0 -> BTC full close runs even under an entry block
    monkeypatch.setattr(X, "entry_block", lambda s: "帳戶暫停")
    X.execute(st, f, _tgt("2026-10-25", w1=[0.0, 0.0], ws={}), datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc))
    last = f.orders[-1]
    assert last["symbol"] == "BTCUSDT" and last["side"] == "SELL" and last.get("reduceOnly") == "true"
    assert "BTCUSDT" not in st["demo_e5_exec"]["book"]
