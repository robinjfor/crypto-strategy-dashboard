import pandas as pd
import pytest

import netting
import tradfi


def _info(syms, mn=5.0, step=0.001, tf=()):
    return {"symbols": [{"symbol": s, "status": "TRADING", "contractType": "TRADIFI_PERPETUAL" if s in tf else "PERPETUAL",
                         "underlyingType": "EQUITY" if s in tf else "COIN", "quoteAsset": "USDT", "onboardDate": 1767830400000,
                         "filters": [{"filterType": "MIN_NOTIONAL", "notional": str(mn)}, {"filterType": "LOT_SIZE", "stepSize": str(step)}]}
                        for s in syms]}


class Fut:
    def __init__(self, pos=None, px=100.0, info=None, err=None):
        self.pos, self.px, self.orders, self.info, self.err = dict(pos or {}), px, [], info, err
    def exchange_info(self): return self.info
    def position_risk(self): return [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()]
    def mark_price(self, s): return self.px
    def set_margin_type(self, *a, **k): return {}
    def set_leverage(self, *a, **k): return {}
    def get_order(self, s, c): return None
    def income(self, s, *a, **k): return [{"income": "-1.0"}] if s == "BTCUSDT" else []
    def new_order(self, **p):
        if self.err:
            raise RuntimeError(self.err)
        self.orders.append(p); q = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
        self.pos[p["symbol"]] = self.pos.get(p["symbol"], 0.0) + q
        return {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": str(self.px)}


def _books():
    return {"e5": {"cash": 1000.0, "book": {}}, "r2s": {"cash": 1000.0, "book": {}}}


def test_opposite_intents_cross_internally_and_net_is_one_order_with_prorata_fee():
    f = Fut(info=_info(["BTCUSDT"])); n = netting.Netter(f, f.info, tag="t"); b = _books(); st = {}
    n.add(st, "e5", "BTCUSDT", 0.5, 100.0, netting.ledger_cb(b["e5"], "BTCUSDT"))
    n.add(st, "r2s", "BTCUSDT", -0.2, 100.0, netting.ledger_cb(b["r2s"], "BTCUSDT"))
    n.add(st, "fa", "BTCUSDT", 0.1, 100.0, netting.ledger_cb({"cash": 0.0}, "BTCUSDT"))
    n.flush()
    assert [(o["side"], float(o["quantity"])) for o in f.orders] == [("BUY", 0.4)]   # net only
    assert b["e5"]["book"]["BTCUSDT"] == 0.5 and b["r2s"]["book"]["BTCUSDT"] == -0.2
    fee = 0.4 * 100 * netting.FEE
    assert b["e5"]["cash"] == pytest.approx(1000 - 50 - fee * 0.5 / 0.6)     # fee pro rata to the net-side deltas
    assert b["r2s"]["cash"] == pytest.approx(1000 + 20)                      # crossed: no fee
    assert any(e["action"] == "net_alloc" for e in st["control_log"])


def test_min_notional_on_the_net_not_per_slot():
    f = Fut(info=_info(["AUSDT"], mn=100.0, step=0.1), px=10.0); n = netting.Netter(f, f.info); b = _books(); st = {}
    n.add(st, "e5", "AUSDT", 6.0, 10.0, netting.ledger_cb(b["e5"], "AUSDT"))     # 60U each < 100U min
    n.add(st, "r2s", "AUSDT", 6.0, 10.0, netting.ledger_cb(b["r2s"], "AUSDT"))
    n.flush()
    assert [float(o["quantity"]) for o in f.orders] == [12.0]                   # 120U net passes
    f2 = Fut(info=_info(["AUSDT"], mn=100.0, step=0.1), px=10.0); n2 = netting.Netter(f2, f2.info); b2 = _books(); st2 = {}
    n2.add(st2, "e5", "AUSDT", 6.0, 10.0, netting.ledger_cb(b2["e5"], "AUSDT"))
    n2.add(st2, "r2s", "AUSDT", -2.0, 10.0, netting.ledger_cb(b2["r2s"], "AUSDT"))
    n2.flush()
    assert not f2.orders and any(e["action"] == "net_skip_min" for e in st2["control_log"])
    assert b2["r2s"]["book"]["AUSDT"] == -2.0 and b2["e5"]["book"]["AUSDT"] == pytest.approx(2.0)  # only the crossed part


def test_crossing_zero_is_reduce_only_close_plus_open_and_close_ignores_min():
    f = Fut(pos={"AUSDT": 3.0}, info=_info(["AUSDT"], mn=100.0, step=0.1), px=10.0); n = netting.Netter(f, f.info); st = {}
    n.add(st, "r2s", "AUSDT", -15.0, 10.0, netting.ledger_cb({"cash": 0.0, "book": {"AUSDT": 3.0}}, "AUSDT"))
    n.flush()
    assert [(o["side"], float(o["quantity"]), o.get("reduceOnly")) for o in f.orders] == [("SELL", 3.0, "true"), ("SELL", 12.0, None)]
    f = Fut(pos={"AUSDT": 3.0}, info=_info(["AUSDT"], mn=100.0, step=0.1), px=10.0); n = netting.Netter(f, f.info)
    n.add({}, "r2s", "AUSDT", -3.0, 10.0, lambda *a: None); n.flush()
    assert [(float(o["quantity"]), o.get("reduceOnly")) for o in f.orders] == [(3.0, "true")]   # 30U < min but reduce-only


def test_tradfi_open_refused_and_4411_learned():
    f = Fut(info=_info(["PLTRUSDT"], tf=("PLTRUSDT",))); n = netting.Netter(f, f.info); st = {}
    n.add(st, "e5", "PLTRUSDT", 1.0, 100.0, lambda *a: None); n.flush()
    assert not f.orders
    f = Fut(info=_info(["XUSDT"]), err="APIError(code=-4411): Please sign TradFi-Perps agreement contract"); st = {}
    n = netting.Netter(f, f.info); n.add(st, "e5", "XUSDT", 1.0, 100.0, lambda *a: None); n.flush()
    assert st["tradfi_blocked"] == ["XUSDT"] and tradfi.is_tradfi("XUSDT", f.info, st)


def test_crypto_filter_and_static_snapshot():
    i = _info(["BTCUSDT", "PLTRUSDT"], tf=("PLTRUSDT",))
    assert [s["symbol"] for s in i["symbols"] if tradfi.is_crypto_perp(s)] == ["BTCUSDT"]
    snap = tradfi.tradfi_map(static=True)
    assert "PLTRUSDT" in snap and "AAPLUSDT" in snap and "BTCUSDT" not in snap


def test_virtual_ledgers_sum_to_exchange_and_attribution():
    st = {"demo_r2s_exec": {"book": {"BTCUSDT": 0.0049, "NEARUSDT": 85.0}},
          "demo_e5_exec": {"book": {"BTCUSDT": 0.02, "SUSDT": 171.0}},
          "demo_fa_positions": {"DOGEUSDT": {"spot_qty": 10, "perp_qty": 10}}}
    assert netting.virtual_net(st) == pytest.approx({"BTCUSDT": 0.0249, "NEARUSDT": 85.0, "SUSDT": 171.0, "DOGEUSDT": -10.0})
    r = netting.net_reconcile(st, {"BTCUSDT": 0.0249, "NEARUSDT": 85.0, "SUSDT": 171.0, "DOGEUSDT": -10.0})
    assert r["ok"] and r["symbols"]["BTCUSDT"]["slots"] == {"demo_r2s_top10_3k": 0.0049, "demo_e5_min5_x175": 0.02}
    assert not netting.net_reconcile(st, {"BTCUSDT": 0.0049})["ok"]
    assert sorted(netting.holders(st)["BTCUSDT"]) == ["demo_e5_min5_x175", "demo_r2s_top10_3k"]


def test_funding_pro_rata_on_net():
    st = {"demo_r2s_exec": {"book": {"BTCUSDT": 0.01}}, "demo_e5_exec": {"book": {"BTCUSDT": 0.03}}}
    got = netting.funding_share(Fut(), st, "demo_r2s_top10_3k", st["demo_r2s_exec"]["book"], 0, 1)
    assert got == pytest.approx(-0.25)


def test_e5_live_masks_tradfi_from_onboard_only():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shadow_ab"))
    import e5_live
    idx = pd.date_range("2026-10-18", periods=5, freq="D", tz="UTC")
    p = pd.concat({s: pd.DataFrame({"close": [1.0] * 5}, index=idx) for s in ("MDTUSDT", "BTCUSDT")}, axis=1)
    on = {"MDTUSDT": int(pd.Timestamp("2026-10-21", tz="UTC").timestamp() * 1000)}
    q, hit = e5_live.mask_tradfi(p.copy(), on)
    assert hit == ["MDTUSDT"] and q["MDTUSDT"]["close"].notna().tolist() == [True, True, True, False, False]
    assert q["BTCUSDT"]["close"].notna().all()


def test_reconcile_attributes_netted_positions(monkeypatch):
    import reconcile
    st = {"demo_r2s_exec": {"book": {"BTCUSDT": 0.0049, "NEARUSDT": 85.0}}}
    exch = [{"symbol": "BTCUSDT", "qty": 0.0049}, {"symbol": "NEARUSDT", "qty": 85.0}, {"symbol": "XUSDT", "qty": 1.0}]

    def fq(state, q, alloc_doc, fc=None):
        import api
        class FC:
            def account(self): return {"assets": [{"asset": q}]}
            def position_risk(self): return [{"symbol": x["symbol"], "positionAmt": x["qty"], "markPrice": 1} for x in exch if x["symbol"].endswith(q)]
        return api._futures_quote_reconcile(state, q, alloc_doc, fc=FC())
    monkeypatch.setattr(reconcile, "futures_quote_reconcile", fq)
    monkeypatch.setattr(reconcile, "find_u1_external_closes", lambda *a: [])
    monkeypatch.setattr(reconcile, "u1_funding_true_up", lambda *a: {})
    rep = reconcile.build_report(st, client=None, fc=object(), alloc_doc={})
    kinds = {m["symbol"]: m["kind"] for m in rep["mismatches"]}
    assert kinds == {"XUSDT": "untracked_exchange_position"}          # R2S BTC/NEAR tracked now
    assert rep["net_reconcile"]["USDT"]["ok"]
    st["demo_r2s_exec"]["book"]["NEARUSDT"] = 80.0
    rep = reconcile.build_report(st, client=None, fc=object(), alloc_doc={})
    m = next(x for x in rep["mismatches"] if x["symbol"] == "NEARUSDT")
    assert m["kind"] == "net_ledger_mismatch" and m["pause"] is False and m["slots"] == ["demo_r2s_top10_3k"]
    s2 = reconcile.apply_report(dict(st), rep)
    assert s2["net_reconcile"]["ok"] is False and s2["net_reconcile"]["daily"][-1]["bad"]["NEARUSDT"]["diff"] == 5.0


def test_manual_overrides_spx_kept_gold_tokens_excluded():
    m = tradfi.tradfi_map(static=True)
    assert "SPXUSDT" not in m and m["PAXGUSDT"] == 0 and m["XAUTUSDT"] == 0
    assert tradfi.is_tradfi("PAXGUSDT") and tradfi.is_tradfi("XAUTUSDT") and not tradfi.is_tradfi("SPXUSDT")
    i = _info(["SPXUSDT", "PAXGUSDT"])
    assert [s["symbol"] for s in i["symbols"] if tradfi.is_crypto_perp(s)] == ["SPXUSDT"]
