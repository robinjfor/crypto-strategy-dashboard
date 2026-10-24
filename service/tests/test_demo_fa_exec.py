import pytest
import demo_fa, demo_fa_exec, demo_fa_guard


class Spot:
    from binance_client import BinanceClient as _B
    round_step = staticmethod(_B.round_step)
    def __init__(self, px=10.0, fail=False): self.px, self.fail, self.buys, self.sells = px, fail, [], []
    def ticker_price(self, s): return self.px
    def load_filters(self, s): return {"stepSize": 0.001}
    def market_buy(self, s, quote, coid):
        if self.fail: raise RuntimeError("spot down")
        q = round(quote / self.px, 3); self.buys.append((s, quote))
        return {"symbol": s, "executedQty": str(q), "cummulativeQuoteQty": str(q * self.px), "fills": [{"commission": "0.001", "commissionAsset": s[:-4]}]}
    def market_sell(self, s, q, coid):
        self.sells.append((s, q)); return {"symbol": s, "executedQty": str(q), "cummulativeQuoteQty": str(q * self.px)}


class Fut:
    def __init__(self, mark=10.0, held=()):
        self.mark, self.held, self.orders, self.pos = mark, set(held), [], {}
    def exchange_info(self): return {"symbols": [{"symbol": s, "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}]} for s in ("AUSDT", "BUSDT", "CUSDT")]}
    def position_risk(self):
        r = [{"symbol": s, "positionAmt": "-1", "markPrice": self.mark, "unRealizedProfit": 0, "isolatedMargin": 25} for s in self.held]
        return r + [{"symbol": s, "positionAmt": str(-q), "markPrice": self.mark, "unRealizedProfit": 0, "isolatedMargin": q * self.mark / 3} for s, q in self.pos.items()]
    def account(self): return {"availableBalance": 100}
    def set_margin_type(self, s, t): return {}
    def set_leverage(self, s, l): assert l == 3; return {}
    def mark_price(self, s): return self.mark
    def get_order(self, s, coid): return None
    def new_order(self, **kw):
        self.orders.append(kw); q = float(kw["quantity"])
        self.pos[kw["symbol"]] = self.pos.get(kw["symbol"], 0) + (q if kw["side"] == "SELL" else -q)
        return {"status": "FILLED", "executedQty": str(q), "avgPrice": str(self.mark)}
    def add_isolated_margin(self, s, a): pass


def _state(**g):
    base = {"enabled": True, "approved": True, "parity_pass": True, "cloud_ordering_ok": True}
    base.update(g)
    return {"demo_candidates": {demo_fa.SLOT: base}}


@pytest.fixture(autouse=True)
def _accept(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", demo_fa.SLOT)
    monkeypatch.setattr(demo_fa_exec, "open_px", lambda *a: None)


T1 = {"decided_bar": "2026-11-01", "K": 10, "held": ["AUSDT", "BUSDT"]}


def test_noop_unless_enabled_approved_accepted(monkeypatch):
    f = Fut()
    for st in (_state(enabled=False), _state(approved=False)):
        assert "skipped" in demo_fa_exec.execute(st, Spot(), f, T1) and not f.orders
    monkeypatch.setenv("DEMO_ACCEPTED", "")
    assert demo_fa_exec.execute(_state(), Spot(), f, T1)["skipped"] == "未驗收" and not f.orders


def test_enter_exit_both_legs_with_fill_readback_and_idempotent():
    st, sp, f = _state(), Spot(), Fut()
    r = demo_fa_exec.execute(st, sp, f, T1)
    assert r["entries"] == ["AUSDT", "BUSDT"]
    a = st["demo_fa_positions"]["AUSDT"]
    assert a["perp_qty"] == 7.5 and abs(a["spot_qty"] - 7.499) < 1e-9   # 75U per leg, spot net of base-asset fee
    assert f.orders[0]["side"] == "SELL" and sp.buys[0] == ("AUSDT", 75.0)
    n = len(f.orders)
    assert demo_fa_exec.execute(st, sp, f, T1)["skipped"] == "already executed" and len(f.orders) == n
    r2 = demo_fa_exec.execute(st, sp, f, {"decided_bar": "2026-11-02", "K": 10, "held": ["BUSDT"]})
    assert r2["exits"] == ["AUSDT"] and f.orders[-1]["reduceOnly"] == "true" and sp.sells[-1] == ("AUSDT", 7.499)
    assert list(st["demo_fa_positions"]) == ["BUSDT"]
    acts = [e["action"] for e in st["control_log"]]
    assert acts.count("fa_entry") == 2 and "fa_exit" in acts and all(e["actor"] == "guard" for e in st["control_log"])


@pytest.mark.parametrize("extra,reason", [({"paused": True}, "帳戶暫停"),
                                          ({"paused_slots": {demo_fa.SLOT: {"by": "reconcile"}}}, "本倉暫停")])
def test_pauses_block_entries_but_exits_run(extra, reason):
    st, sp, f = _state(), Spot(), Fut()
    st["demo_fa_positions"] = {"CUSDT": {"spot_qty": 5.0, "perp_qty": 5.0}}
    st.update(extra)
    r = demo_fa_exec.execute(st, sp, f, T1)
    assert r["exits"] == ["CUSDT"] and r["entries"] == [] and all(reason in x for x in r["skipped"])


def test_guard_block_blocks_entries(monkeypatch):
    import guards
    monkeypatch.setattr(guards, "entry_blocked", lambda st, now=None: (True, "FOMC"))
    st, f = _state(), Fut()
    r = demo_fa_exec.execute(st, Spot(), f, T1)
    assert not f.orders and "FOMC" in r["skipped"][0]


def test_legacy_slot_blocks_but_netted_holder_does_not():
    st, f = _state(), Fut(held=["BUSDT"])        # BUSDT held on the account (e.g. E5 virtual) -> netted, FA still enters
    st["positions"] = {"sat_x": {"status": "FILLED", "symbol": "AUSDC", "side": "LONG"}}
    r = demo_fa_exec.execute(st, Spot(), f, T1)
    assert r["entries"] == ["BUSDT"] and len(r["skipped"]) == 1 and "AUSDT" in r["skipped"][0]
    # and other slots now see the coin as taken
    import main
    st2 = {"demo_fa_positions": {"DOGEUSDT": {}}}
    assert main.same_coin_position_slot(st2, "sat_y", "DOGEUSDC", "LONG") == demo_fa.SLOT


def test_spot_failure_unwinds_perp():
    st, f = _state(), Fut()
    r = demo_fa_exec.execute(st, Spot(fail=True), f, {"decided_bar": "d", "held": ["AUSDT"]})
    assert r["entries"] == [] and f.pos["AUSDT"] == 0 and st.get("demo_fa_positions") == {}


def test_guard_sees_executed_positions():
    st, sp, f = _state(), Spot(), Fut()
    demo_fa_exec.execute(st, sp, f, T1)
    sp.px = 11.0                         # spot leg drifts ~10% above perp
    acts = demo_fa_guard.run(st, sp, f)
    assert {a["symbol"] for a in acts if a["action"] == "fa_rebalance"} == {"AUSDT", "BUSDT"}


def test_untradable_coin_skipped():
    st, f = _state(), Fut()
    st["demo_candidates"][demo_fa.SLOT]["ordering_detail"] = {"universe_check": {"failed": {"AUSDT": ["spot: invalid symbol"]}}}
    r = demo_fa_exec.execute(st, Spot(), f, T1)
    assert r["entries"] == ["BUSDT"] and "lag_s" in r and st["demo_fa_exec"]["lags"][-1][0] == "2026-11-01"


def test_universe_check_reports_failures_per_coin():
    class S(Spot):
        def round_qty(self, s, q, px): return round(q, 3)
        def test_order(self, **kw):
            if kw["symbol"] == "CUSDT": raise RuntimeError("Invalid symbol")
    class F(Fut):
        def test_order(self, **kw): return {}
    r = demo_fa.universe_check(S(), F(), {"universe": ["AUSDT", "BUSDT", "CUSDT"], "decided_bar": "d"})
    assert r["coins"] == 3 and r["orders"] == 6 and r["ok_coins"] == 2 and list(r["failed"]) == ["CUSDT"]


def test_slippage_recorded_per_fill(monkeypatch):
    monkeypatch.setattr(demo_fa_exec, "open_px", lambda s, leg, ms: 9.9 if leg == "spot" else 10.1)
    st = _state()
    demo_fa_exec.execute(st, Spot(), Fut(), {"decided_bar": "2026-11-01", "held": ["AUSDT", "PUMPUSDT"]})
    rec = st["demo_fa_slippage"]
    assert {(r["leg"], r["side"]) for r in rec} == {("perp", "SELL"), ("spot", "BUY")} and all(r["symbol"] == "AUSDT" for r in rec)
    sp = next(r for r in rec if r["leg"] == "spot"); pp = next(r for r in rec if r["leg"] == "perp")
    assert sp["date"] == "2026-11-02" and sp["bps"] == round((10 / 9.9 - 1) * 1e4, 2) and pp["bps"] == round((10.1 / 10 - 1) * 1e4, 2)
    assert isinstance(sp["lag_s"], int)
