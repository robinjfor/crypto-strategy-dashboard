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


def test_merge_untraded_keeps_current_ledger():
    s = {"demo_r2s_exec": {"book": {"X": 2.0}, "last_bar": "b2", "claim": {"run": "me"}}}
    R.merge_untraded(s, "demo_r2s_exec", {"demo_r2s_exec": {"book": {"X": 1.0}, "last_bar": "b1", "last_run": {"r": 1}}}, "me")
    assert s["demo_r2s_exec"]["book"] == {"X": 2.0} and s["demo_r2s_exec"]["last_run"] == {"r": 1} and "claim" not in s["demo_r2s_exec"]


def test_r2s_and_fa_claims_use_slot_keys():
    st = _Store({"demo_r2s_exec": {"last_bar": "a"}}); s0 = st.load()
    assert R.slot_claim(st, "demo_r2s_exec", s0, "b", "A") is None and R.slot_claim(st, "demo_r2s_exec", s0, "b", "B")


def test_net_reconcile_flags_unowned_alert_only():
    import netting
    st = {V.STATE_KEY: {"book": {"ETHUSDT": 1.0}}}
    r = netting.net_reconcile(st, {"ETHUSDT": 1.0, "BATUSDT": -40.8}, "USDT")
    assert r["ok"] and r["unowned_alert"] and r["unowned"] == {"BATUSDT": -40.8}
    assert not netting.net_reconcile(st, {"BTCUSDC": 0.01}, "USDC")["unowned_alert"]
    rep = {"at": "t", "at_utc": "2026-10-28T02:00", "net_reconcile": {"USDT": r}, "mismatches": [], "errors": []}
    st2 = {}
    import inspect
    nr = rep["net_reconcile"]; day = "2026-10-28"
    assert nr["USDT"]["unowned"] == {"BATUSDT": -40.8}


def test_netter_uses_2x_for_e5v_symbols():
    import netting
    n = netting.Netter.__new__(netting.Netter); n.intents = {"A": [{"slot": V.SLOT}, {"slot": "demo_r2s_top10_3k"}], "B": [{"slot": "demo_r2s_top10_3k"}]}
    assert n.lev_for("A") == 2 and n.lev_for("B") == 3 and n.lev_for("C") == 3


class _LF:
    def __init__(self, avail): self.avail = avail; self.calls = []; self.lev = 3
    def account(self): return {"availableBalance": self.avail}
    def position_risk(self, s=None):
        return [{"symbol": "X", "positionAmt": "-100", "markPrice": "1", "isolatedMargin": "30", "unRealizedProfit": "-3", "marginType": "isolated", "leverage": str(self.lev)}]
    def add_isolated_margin(self, s, a): self.calls.append(("add", s, round(a, 2)))
    def set_leverage(self, s, l): self.calls.append(("lev", s, l)); self.lev = l


def test_lev_down_tops_up_then_sets_and_aborts_when_short():
    import migrate_e5v as M
    f = _LF(1000); r = M.lev_down(f, ["X"], 2)
    assert f.calls == [("add", "X", 22.01), ("lev", "X", 2)] and r["lev_after"] == {"X": "2"}
    f = _LF(10); r = M.lev_down(f, ["X"], 2)
    assert r["aborted"] and f.calls == []


def test_netter_trades_when_leverage_change_refused():
    import netting
    class F:
        def set_margin_type(self, *a): pass
        def set_leverage(self, *a): raise RuntimeError("-4161 Leverage reduction is not supported")
        def new_order(self, **k): return {"status": "FILLED", "executedQty": k["quantity"], "avgPrice": "1"}
    n = netting.Netter.__new__(netting.Netter); n.fut = F(); n.intents = {}; n.orders = []; n.n = 0; n.tag = "t"
    assert n._send({}, "X", "BUY", 5, False) == (5.0, 1.0)
