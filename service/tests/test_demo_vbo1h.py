import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import demo_candidates
import demo_vbo1h as V
import demo_vbo1h_exec as X
import netting
import reconcile

GATES = {"enabled": True, "approved": True, "parity_pass": True, "cloud_ordering_ok": True}
NOW = datetime(2026, 11, 4, 3, 8, tzinfo=timezone.utc)      # decided bar 02:00 UTC
BAR = "2026-11-04T02:00Z"
COLS = V.universe_symbols()
A, B = COLS[0], COLS[1]


def _info(syms, mn=5.0, step=0.001):
    return {"symbols": [{"symbol": s, "status": "TRADING", "filters": [
        {"filterType": "MIN_NOTIONAL", "notional": str(mn)}, {"filterType": "MARKET_LOT_SIZE", "stepSize": str(step)}]} for s in syms]}


class Fut:
    def __init__(self, syms, pos=None):
        self.info = _info(syms); self.pos = dict(pos or {}); self.orders = []
    def exchange_info(self): return self.info
    def position_risk(self, symbol=None): return [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()]
    def mark_price(self, s): return 10.0
    def income(self, *a, **k): return []
    def set_margin_type(self, *a, **k): return {}
    def set_leverage(self, *a, **k): self.lev = (a, k); return {}
    def get_order(self, s, c): return None
    def new_order(self, **p):
        self.orders.append(p); q = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
        self.pos[p["symbol"]] = self.pos.get(p["symbol"], 0.0) + q
        return {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": "10"}


@pytest.fixture(autouse=True)
def _acc(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", V.SLOT)


def _patch(monkeypatch, w: dict):
    bar = pd.Timestamp(BAR.replace("Z", ""), tz="UTC")
    C = pd.DataFrame({s: [10.0] for s in COLS}, index=[bar])
    monkeypatch.setattr(X, "build", lambda fr, b, cols: (C, C, C, C, {s: 10.0 for s in COLS}, {}))
    monkeypatch.setattr(V, "signal_panel", lambda *a: {"W": pd.DataFrame({s: [w.get(s, 0.0)] for s in COLS}, index=[bar])})


def _st(**kw):
    return {"demo_candidates": {V.SLOT: dict(GATES)}, **kw}


def test_registration_and_constants():
    assert V.SLOT == "demo_vbo1h_top50_1k" and V.CAPITAL == 1000.0 and V.GROSS == 0.5 and V.MAXPOS == 10 and V.HOLD_BARS == 168
    assert netting.SLOTS[V.SLOT] == V.STATE_KEY and netting.LEV_BY_SLOT[V.SLOT] == 2
    assert V.SLOT in reconcile.NETTED and V.SLOT in {c["id"] for c in demo_candidates.CANDIDATES}
    import orchestrator
    assert V.SLOT not in orchestrator.SLOTS.values()          # not part of the orchestrator weights
    assert orchestrator.BASE_W["F"] == 0.0                    # FA untouched (weight 0, still listed)
    assert len(COLS) == 98 and V.REVIEW_DATE == "2026-12-02"


def test_gates(monkeypatch):
    assert V.active(_st()) == (True, "")
    monkeypatch.setenv("DEMO_ACCEPTED", "")
    assert V.active(_st()) == (False, "未驗收")
    monkeypatch.setenv("DEMO_ACCEPTED", V.SLOT)
    assert V.active({"demo_candidates": {V.SLOT: {**GATES, "approved": False}}}) == (False, "未批准")


def _synth(n_days=70, cols=("AUSDT", "BUSDT")):
    idx = pd.date_range("2026-01-01", periods=n_days * 24, freq="h", tz="UTC")
    C = pd.DataFrame(100.0, index=idx, columns=list(cols)); H = C * 1.001; L = C * 0.999
    QV = pd.DataFrame(1e6, index=idx, columns=list(cols))
    return idx, C, H, L, QV


def test_signal_breakout_hold_and_cap():
    idx, C, H, L, QV = _synth()
    t = idx[62 * 24 + 5]
    C.loc[t:, "AUSDT"] = 102.0; H.loc[t:, "AUSDT"] = 102.1        # cross above 55d high*1.01 (100.1*1.01=101.1)
    C.loc[t:, "BUSDT"] = 97.0; L.loc[t:, "BUSDT"] = 96.9          # cross below 55d low*0.99
    W = V.signal_panel(H, L, C, QV)["W"]
    assert W.at[t, "AUSDT"] == 0.1 and W.at[t, "BUSDT"] == -0.1      # 1/max(n,10)
    assert W.at[idx[idx.get_loc(t) - 1], "AUSDT"] == 0
    assert W.at[t + pd.Timedelta(hours=167), "AUSDT"] == 0.1         # held 7d
    # the new day's levels include the breakout high -> no new cross; the hold expires after 168 bars
    assert W.at[t + pd.Timedelta(hours=168), "AUSDT"] == 0


def test_decide_kinds():
    assert V.decide(0, 0.05, 0, 10, 1000, 0.001, 5) == (5.0, "open")
    assert V.decide(0.05, 0.05, 5.0, 11, 1000, 0.001, 5) == (5.0, "hold")            # unchanged target: no drift trade
    assert V.decide(0.05, 0, 5.0, 10, 1000, 0.001, 5) == (0.0, "close")
    assert V.decide(0.05, -0.05, 5.0, 10, 1000, 0.001, 5) == (-5.0, "flip")
    assert V.decide(0.05, 0.0454545, 5.0, 10, 1000, 0.001, 5)[1] == "resize"        # n 10 -> 11 reduce (reduceOnly)
    assert V.decide(0.0454545, 0.05, 4.545, 10, 1000, 0.001, 5) == (4.545, "skip_min")  # +4.5U increase < MN
    assert V.decide(0, 0.04, 0, 10, 1000, 0.001, 50) == (0.0, "skip_min")            # BTC-like MN 50 > 40U
    assert V.decide(0.05, 0.05, 0, 10, 1000, 0.001, 5) == (5.0, "open")              # target held but book empty -> enter


def test_apply_min():
    k, d = V.apply_min({"BTCUSDT": 0.04, "XUSDT": 0.04, "Z": 0.0}, 1000, {"BTCUSDT": 50.0})
    assert k == {"XUSDT": 0.04} and d == ["BTCUSDT"]


def test_window():
    assert X.in_window(NOW) == (True, BAR)
    assert X.in_window(datetime(2026, 11, 4, 3, 53, tzinfo=timezone.utc)) == (False, BAR)
    assert X.in_window(datetime(2026, 11, 4, 0, 8, tzinfo=timezone.utc))[1] == "2026-11-03T23:00Z"


def test_inactive_noop():
    f = Fut(COLS)
    assert X.execute({"demo_candidates": {V.SLOT: {**GATES, "enabled": False}}}, f, NOW)["skipped"] == "未啟用" and not f.orders


def test_execute_open_once_per_bar_and_netting_lev(monkeypatch):
    _patch(monkeypatch, {A: 0.1, B: -0.1})
    f = Fut(COLS); st = _st()
    late = datetime(2026, 11, 4, 3, 55, tzinfo=timezone.utc)
    assert "outside" in X.execute(st, f, late, panel_fn=lambda: {})["skipped"] and not f.orders
    r = X.execute(st, f, NOW, panel_fn=lambda: {})
    assert r["bar"] == BAR and len(f.orders) == 2
    ex = st[V.STATE_KEY]
    assert ex["book"] == {A: 5.0, B: -5.0}                 # 0.1 * 0.5 * 1000 / 10
    assert ex["targets"] == {A: 0.05, B: -0.05} and ex["last_bar"] == BAR and ex["review_date"] == "2026-12-02"
    assert X.execute(st, f, NOW, panel_fn=lambda: {})["skipped"] == "already executed" and len(f.orders) == 2
    assert ex["history"][-1]["tg"] == {A: 0.05, B: -0.05}


def test_no_close_for_leg_missing_on_exchange(monkeypatch):
    _patch(monkeypatch, {})
    f = Fut(COLS, pos={})            # exchange flat (e.g. liquidated outside)
    st = _st(**{V.STATE_KEY: {"cash": 950.0, "book": {A: 5.0}, "targets": {A: 0.05}}})
    r = X.execute(st, f, NOW, panel_fn=lambda: {})
    assert not f.orders and f"{A} leg missing on exchange" in r["skipped"]
    assert st[V.STATE_KEY]["book"] == {A: 5.0}


def test_close_when_exchange_holds(monkeypatch):
    _patch(monkeypatch, {})
    f = Fut(COLS, pos={A: 5.0})
    st = _st(**{V.STATE_KEY: {"cash": 950.0, "book": {A: 5.0}, "targets": {A: 0.05}}})
    X.execute(st, f, NOW, panel_fn=lambda: {})
    assert len(f.orders) == 1 and f.orders[0]["side"] == "SELL" and st[V.STATE_KEY].get("book") == {}


def test_untraded_run_never_overwrites_ledger():
    s = {V.STATE_KEY: {"cash": 900.0, "book": {A: 5.0}, "last_bar": BAR, "claim": {"run": "r1"}}}
    sc = {V.STATE_KEY: {"cash": 1000.0, "book": {}, "last_run": {"at": "x"}}}
    reconcile.merge_untraded(s, V.STATE_KEY, sc, "r1")
    assert s[V.STATE_KEY]["book"] == {A: 5.0} and s[V.STATE_KEY]["cash"] == 900.0 and "claim" not in s[V.STATE_KEY]
    assert not reconcile.e5_traded({"skipped": "outside HH:00-HH:50 window", "bar": BAR})


def test_owned_by_net_reconcile():
    st = {V.STATE_KEY: {"book": {A: 5.0}}}
    r = netting.net_reconcile(st, {A: 5.0})
    assert not r.get("unowned") and r["rows"][A]["ok"] if "rows" in r else not r.get("unowned")


def test_committed_parity_passes():
    p = json.load(open(Path(V.__file__).parent / "shadow_ab" / "parity_VBO1H.json"))
    assert p["pass"] and p["spec"] == V.SPEC and p["per_hour_mismatches"] == 0 and p["trade_by_trade_identical_spec_mask"]
    assert p["window_max_abs_weight_diff"] == 0 and p["target_changes_without_order"] == 0
