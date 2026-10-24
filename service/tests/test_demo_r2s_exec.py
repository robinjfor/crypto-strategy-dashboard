from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import demo_candidates
import demo_r2s as D
import demo_r2s_exec as X

GATES = {"enabled": True, "approved": True, "parity_pass": True, "cloud_ordering_ok": True}
NOW = datetime(2026, 10, 24, 0, 8, tzinfo=timezone.utc)   # 08:08 Taipei -> decided bar 2026-10-23


def _info(syms, mn=5.0, step=0.1):
    return {"symbols": [{"symbol": s, "status": "TRADING", "filters": [
        {"filterType": "MIN_NOTIONAL", "notional": str(mn)}, {"filterType": "MARKET_LOT_SIZE", "stepSize": str(step)}]} for s in syms]}


class Fut:
    def __init__(self, syms, pos=None, mn=5.0):
        self.info = _info(syms, mn); self.pos = dict(pos or {}); self.orders = []
    def exchange_info(self): return self.info
    def position_risk(self): return [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()]
    def mark_price(self, s): return 10.0
    def income(self, *a, **k): return []
    def set_margin_type(self, *a, **k): return {}
    def set_leverage(self, *a, **k): return {}
    def get_order(self, s, c): return None
    def new_order(self, **p):
        self.orders.append(p); q = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
        self.pos[p["symbol"]] = self.pos.get(p["symbol"], 0.0) + q
        return {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": "10"}


def _state(**kw):
    return {"demo_candidates": {D.SLOT: dict(GATES)}, **kw}


@pytest.fixture(autouse=True)
def _acc(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)


def _panel(tw):
    """stub: bypass signal; targets() is patched; O_today = 10 for all."""
    return lambda: None


def _patch(monkeypatch, tw, bar="2026-10-23"):
    idx = pd.DatetimeIndex([pd.Timestamp(bar, tz="UTC")])
    C = pd.DataFrame({s: [10.0] for s in tw or ["BTCUSDT"]}, index=idx)
    monkeypatch.setattr(X, "build", lambda fr, b: (C, C, C, C, {s: 10.0 for s in ["AUSDT", "BUSDT", "CUSDT", "DUSDT"]}))
    monkeypatch.setattr(X, "targets", lambda *a: dict(tw))


def test_cfg_for_quarter():
    assert X.cfg_for("2026-09-30") == "th20_h3_hv0.0_L2.0"     # trades 10-01 -> Q4 pick
    assert X.cfg_for("2026-12-30") == "th20_h3_hv0.0_L2.0"
    assert X.cfg_for("2026-09-29") is None


def test_inactive_is_noop(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", "")
    f = Fut(["AUSDT"])
    assert X.execute(_state(), f, NOW)["skipped"] == "未驗收" and not f.orders
    assert X.execute({"demo_candidates": {D.SLOT: {**GATES, "enabled": False}}}, f, NOW)["skipped"] == "未啟用"


def test_window_and_once_per_bar(monkeypatch):
    _patch(monkeypatch, {"AUSDT": 0.1})
    f = Fut(["AUSDT"]); st = _state()
    late = datetime(2026, 10, 24, 5, 0, tzinfo=timezone.utc)
    assert "outside" in X.execute(st, f, late, panel_fn=lambda: None)["skipped"] and not f.orders
    r = X.execute(st, f, NOW, panel_fn=lambda: None)
    assert r["bar"] == "2026-10-23" and len(f.orders) == 1
    assert X.execute(st, f, NOW, panel_fn=lambda: None)["skipped"] == "already executed" and len(f.orders) == 1


def test_decisions_follow_decide_minnot(monkeypatch):
    # E = 3000; A open 0.1 -> 30 qty; B close (reduce-only); C small adjust < MN skipped; D flip -> close + open
    tw = {"AUSDT": 0.1, "CUSDT": 0.1, "DUSDT": -0.05}
    _patch(monkeypatch, tw)
    book = {"BUSDT": 5.0, "CUSDT": 21.0, "DUSDT": 4.0}
    cash = 3000.0 - sum(q * 10 for q in book.values())
    f = Fut(["AUSDT", "BUSDT", "CUSDT", "DUSDT"], pos=book, mn=100.0)
    st = _state(demo_r2s_exec={"cash": cash, "book": dict(book)})
    r = X.execute(st, f, NOW, panel_fn=lambda: None)
    for s in ("AUSDT", "BUSDT", "CUSDT", "DUSDT"):
        exp = D.decide_minnot(tw.get(s, 0.0), book.get(s, 0.0), 10.0, 3000.0, 100.0, 0.1)
        assert pytest.approx(st["demo_r2s_exec"]["book"].get(s, 0.0)) == exp[0], s
    o = {(x["symbol"], x["side"], x.get("reduceOnly")) for x in f.orders}
    assert ("BUSDT", "SELL", "true") in o and ("DUSDT", "SELL", "true") in o and ("AUSDT", "BUY", None) in o
    assert not any(x["symbol"] == "CUSDT" for x in f.orders) and r["kinds"]["skip_min"] == 1
    assert f.orders[0].get("reduceOnly") == "true"            # closes first
    assert any(e["action"] == "r2s_skip_min" for e in st["control_log"])


def test_coin_held_by_other_slot_is_netted_not_skipped(monkeypatch):
    _patch(monkeypatch, {"AUSDT": 0.1})
    f = Fut(["AUSDT"], pos={"AUSDT": 3.0})                     # E5 holds 3 AUSDT (virtual), R2S book empty
    st = _state(demo_e5_exec={"book": {"AUSDT": 3.0}, "cash": 0.0})
    X.execute(st, f, NOW, panel_fn=lambda: None)
    assert [(o["symbol"], o["side"], float(o["quantity"])) for o in f.orders] == [("AUSDT", "BUY", 30.0)]
    assert st["demo_r2s_exec"]["book"]["AUSDT"] == 30.0 and f.pos["AUSDT"] == 33.0


def test_pick_fallback_to_previous_quarter():
    assert X.pick_for("2026-12-31") == ("th20_h3_hv0.0_L2.0", "2026-10-01", True)   # Q1-2027 missing -> Q4-2026
    assert X.pick_for("2026-10-23") == ("th20_h3_hv0.0_L2.0", "2026-10-01", False)
    assert X.pick_for("2026-09-29") == (None, None, False)                          # nothing earlier


def test_fallback_trades_and_logs(monkeypatch):
    _patch(monkeypatch, {"AUSDT": 0.1}, bar="2026-12-31")
    f = Fut(["AUSDT"])
    st = _state()
    X.execute(st, f, datetime(2027, 1, 1, 0, 8, tzinfo=timezone.utc), panel_fn=lambda: None)
    ev = [e for e in st["control_log"] if e.get("action") == "r2s_wf_pick_fallback"]
    assert ev and ev[0]["from_quarter"] == "2026-10-01" and f.orders


def test_no_earlier_pick_no_trade(monkeypatch):
    _patch(monkeypatch, {"AUSDT": 0.1}, bar="2026-09-29")
    f = Fut(["AUSDT"])
    r = X.execute(_state(), f, datetime(2026, 9, 30, 0, 8, tzinfo=timezone.utc), panel_fn=lambda: None)
    assert "WF pick" in r["skipped"] and not f.orders


def test_build_uses_closed_days_and_today_open():
    days = pd.date_range("2026-10-20", periods=5, freq="D", tz="UTC")
    fr = {"AUSDT": pd.DataFrame({"o": [1, 2, 3, 4, 5.0], "h": 1.0, "l": 1.0, "c": [1, 2, 3, 4, 5.0], "qv": 1.0}, index=days)}
    C, H, L, QV, O1 = X.build(fr, "2026-10-23")
    assert str(C.index[-1].date()) == "2026-10-23" and O1 == {"AUSDT": 5.0}


def test_view_exposes_approved_by():
    v = demo_candidates.view({"demo_candidates": {D.SLOT: {"approved": True, "approved_by": "自動交易專家"}}})
    assert next(x for x in v if x["id"] == D.SLOT)["approved_by"] == "自動交易專家"


def test_reconcile_scratch_with_none_keys(monkeypatch):
    # reconcile copies keys absent from state as None (e.g. demo_r2s_exec on the first run)
    _patch(monkeypatch, {"AUSDT": 0.1})
    f = Fut(["AUSDT"])
    st = _state()
    for k in ("demo_r2s_exec", "demo_fa_positions", "paused", "paused_slots", "guard_blocks", "positions", "u1_live"):
        st[k] = None
    X.execute(st, f, NOW, panel_fn=lambda: None)
    assert f.orders and st["demo_r2s_exec"]["last_bar"]
