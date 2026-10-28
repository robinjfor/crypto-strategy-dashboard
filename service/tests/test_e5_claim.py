import json

import demo_e5v as V
import migrate_e5v as M
import reconcile as R


class _Store:
    def __init__(self, s): self.s = s
    def load(self): return json.loads(json.dumps(self.s))
    def mutate(self, f): self.s = f(self.load()); return self.s


def test_second_concurrent_run_cannot_claim_bar():
    st = _Store({"k": {"last_bar": "2026-10-26", "book": {}}}); s0 = st.load(); tgt = {"decided_bar": "2026-10-27"}
    assert R.e5_claim(st, "k", s0, tgt, "A") is None
    assert "claimed by run A" in R.e5_claim(st, "k", s0, tgt, "B")
    st.s["k"]["last_bar"] = "2026-10-27"
    assert R.e5_claim(st, "k", s0, tgt, "C") == "already executed (other run)"
    assert R.e5_claim(st, "k", st.load(), tgt, "C") is None


def test_stale_load_refused():
    st = _Store({"k": {"last_bar": "2026-10-27"}})
    assert R.e5_claim(st, "k", {"k": {"last_bar": "2026-10-26"}}, {"decided_bar": "2026-10-28"}, "B") == "ledger changed since load"


def test_traded_detection():
    assert R.e5_traded({"bar": "x", "sent": [], "skipped": []})
    assert not R.e5_traded({"skipped": "already executed", "bar": "x"}) and not R.e5_traded(None)


class _DupFut:
    def __init__(self): self.pos = {"ETHUSDT": 1.757, "BATUSDT": -40.8, "BTCUSDT": -0.0253}; self.sent = []
    def position_risk(self, s=None): return [{"symbol": s, "positionAmt": str(self.pos.get(s, 0))}]
    def get_order(self, s, c): return {"orderId": (1 if c.startswith("net-") else 2), "status": "FILLED"}
    def new_order(self, **p):
        self.sent.append(p); self.pos[p["symbol"]] += float(p["quantity"]) * (1 if p["side"] == "BUY" else -1); return {"status": "FILLED"}
    def _signed(self, m, path, p=None):
        return [{"qty": "1", "price": "2" if p.get("orderId") == 1 else "1", "commission": "0.01"}]


def test_dup_orphans_unwound_reduce_only_once():
    st = _Store({"migrations": {M.MIG: {"done": True}, "ab_1028": {"done": True}, "us_1026": {"done": True}, "hu_1027": {"done": True}},
                 V.STATE_KEY: {"cash": 100.0, "book": {"ETHUSDT": 0.878}}})
    f = _DupFut(); M.run_fixes(st, f)
    assert len(f.sent) == 3 and all(o["reduceOnly"] == "true" for o in f.sent)
    assert abs(f.pos["ETHUSDT"] - 0.878) < 1e-9 and abs(f.pos["BATUSDT"]) < 1e-9 and abs(f.pos["BTCUSDT"]) < 1e-9
    adj = st.s["ledger_adjustments"][-1]; assert adj["id"] == "dup_1028-orphans" and adj["cause"] == "double_execution"
    exp = 0.879 * (1 - 2) + (-40.8) * (1 - 2) + (-0.0253) * (1 - 2) - 6 * 0.01
    assert abs(st.s[V.STATE_KEY]["cash"] - (100 + exp)) < 1e-9
    M.run_fixes(st, f); assert len(f.sent) == 3


def test_dup_skipped_when_exchange_changed():
    st = _Store({"migrations": {M.MIG: {"done": True}, "ab_1028": {"done": True}, "us_1026": {"done": True}, "hu_1027": {"done": True}},
                 V.STATE_KEY: {"cash": 100.0, "book": {"ETHUSDT": 0.878}}})
    f = _DupFut(); f.pos = {"ETHUSDT": 0.878, "BATUSDT": 0.0, "BTCUSDT": 0.0}; M.run_fixes(st, f)
    assert not f.sent and st.s[V.STATE_KEY]["cash"] == 100.0
