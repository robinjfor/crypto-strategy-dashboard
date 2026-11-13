"""E5 resizes its held book when the orchestrator sizing capital changes (2026-11-13 incident).

Orchestrator backtest (spec_2026-11/research_orchestrator/orchestrator.py): E's PnL is caps['E'] x
E's daily return, so a capital change resizes the whole E book at once. Live used the new capital
only inside the deadband test, so held legs inside the band kept their old (2x) size.
"""
import json
from datetime import timezone
from pathlib import Path

import numpy as np
import pytest

import demo_e5 as D
import demo_e5_exec as X
import demo_e5v as V
import orchestrator as O
from tests.test_demo_e5_exec import Fut, NOW, PX, _tgt

FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "e5_rescale_20261113.json").read_text())


def _st(book=None, frame=None, last=None):
    ex = {"cash": 2000.0, "book": dict(book or {}), "last_bar": "2026-10-23", "funding_since": 1}
    if frame is not None:
        ex["frame_cap"] = frame
    if last is not None:
        ex["last"] = last
    return {"demo_candidates": {V.SLOT: {"enabled": True, "approved": True, "parity_pass": True, "cloud_ordering_ok": True}},
            "demo_e5v_exec": ex}


def _plan(cap):
    return {"generated_at": NOW.astimezone(timezone.utc).isoformat(), "sizing": "quote_pool",
            "targets": {V.SLOT: cap}, "quote_pool": {V.SLOT: 10000.0}}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", V.SLOT)
    monkeypatch.setattr(X, "entry_block", lambda s, *a: None)
    yield
    O.load(None)


def test_rescale_book_halves_held_legs_with_step_and_min_rules():
    book = {"A": 10.0, "B": 3.0, "C": 100.0}
    px = {"A": 10.0, "B": 3.0, "C": 1.0}
    out, orders, held = X.rescale_book(book, {"A": 4000, "B": 4000, "C": 4000}, 2000.0, px,
                                       {"A": 5, "B": 5, "C": 5}, {"A": 1.0, "B": 1.0, "C": 1.0}, {"A": 0, "B": 0, "C": 0})
    k = {o["symbol"]: o for o in orders}
    assert out["A"] == 5.0 and k["A"]["kind"] == "rescale" and k["A"]["delta_qty"] == -5.0
    assert out["B"] == 0.0 and k["B"]["kind"] == "residual_close"          # 1 x 3 = 3U < MN 5 -> backtest residual close
    assert out["C"] == 50.0 and k["C"]["delta_qty"] == -50.0
    assert not held


def test_small_capital_drift_is_held_back_and_keeps_its_frame():
    out, orders, held = X.rescale_book({"A": 10.0}, {"A": 4000}, 3960.0, {"A": 10.0}, {"A": 5}, {"A": 0.1}, {"A": 2.0})
    assert not orders and held == {"A"} and out["A"] == 10.0                # 1% drift = 1U < max(MN, 0.5% sleeve)


def test_merge_plan_close_wins_and_orders_sum():
    resize = [{"symbol": "A", "delta_qty": -5.0, "kind": "rescale", "k": 0.5},
              {"symbol": "B", "delta_qty": 4.0, "kind": "residual_close", "k": 0.5},
              {"symbol": "C", "delta_qty": -2.0, "kind": "rescale", "k": 0.5}]
    plan = [{"symbol": "A", "delta_qty": 2.0, "kind": "order"},
            {"symbol": "B", "delta_qty": -9.0, "kind": "order"},
            {"symbol": "C", "delta_qty": 3.0, "kind": "close"}]
    m = {o["symbol"]: o for o in X.merge_rescale(plan, resize, {"A": 10.0, "B": 10.0, "C": 10.0}, {})}
    assert m["A"]["kind"] == "order" and m["A"]["delta_qty"] == -3.0        # resize -5 then signal +2
    assert m["B"]["kind"] == "order" and m["B"]["delta_qty"] == -5.0        # residual close +4 then reopen at -9
    assert m["C"]["kind"] == "close"                                       # plan close closes the real qty


def test_executor_resizes_held_leg_inside_the_deadband(live):
    t = _tgt(w1=[0.0, 0.0], ws={"SOLUSDT": 0.5}, dbs={"SOLUSDT": 0.6})
    st = _st({"SOLUSDT": 3.5}, frame={"SOLUSDT": 4000.0})                    # 700U, sized at 4000
    f = Fut(PX, pos={"SOLUSDT": 3.5})
    O.load({"orchestrator": _plan(2000.0)})
    out = X.execute(st, f, t, NOW, mod=V)
    ex = st["demo_e5v_exec"]
    # S5 sleeve 0.2 x 2000 = 400; SOL target 0.5 x 1.75 x 400 = 350U = 1.75 SOL; resized 3.5 x 0.5 = 1.7 (step 0.1)
    assert abs(ex["book"]["SOLUSDT"] - 1.7) < 1e-9 and out["rescale"]["k"] == {"SOLUSDT": 0.5}
    assert ex["frame_cap"] == {"SOLUSDT": 2000.0} and ex["sizing_equity"] == 2000.0
    assert [o["side"] for o in f.orders] == ["SELL"]


def test_without_a_capital_change_or_a_plan_nothing_is_resized(live):
    t = _tgt(w1=[0.0, 0.0], ws={"SOLUSDT": 0.5}, dbs={"SOLUSDT": 0.6})
    st = _st({"SOLUSDT": 3.5}, frame={"SOLUSDT": 2000.0})
    O.load({"orchestrator": _plan(2000.0)})
    f = Fut(PX, pos={"SOLUSDT": 3.5})
    out = X.execute(st, f, t, NOW, mod=V)
    assert out["rescale"]["orders"] == 0 and not f.orders                  # in band, same capital: hold (backtest)
    O.load(None)
    st2 = _st({"SOLUSDT": 3.5}, frame={"SOLUSDT": 4000.0}); f2 = Fut(PX, pos={"SOLUSDT": 3.5})
    out2 = X.execute(st2, f2, t, NOW, mod=V)
    assert out2["rescale"]["orders"] == 0 and "frame_cap" in st2["demo_e5v_exec"] and st2["demo_e5v_exec"]["frame_cap"] == {"SOLUSDT": 4000.0}


def test_frame_seed_from_the_1113_run():
    ex = {"book": FIX["book_after"], "last": FIX["last"]}
    fr = X.frame_caps(ex)
    sent = {o["symbol"] for o in FIX["last"]["sent"]}
    for s in FIX["book_after"]:
        assert fr[s] == (1959.85 if s in sent else 3974.03)


def _replay():
    t = FIX["targets"]; px = FIX["marks"]; eq = FIX["sizing_capital"]
    mins = {k: float(v) for k, v in t["min_notional"].items()}
    steps = {k: float(v or 0) for k, v in t["step"].items()}
    for k in px:
        mins.setdefault(k, D.MIN_ORDER)
    es = (1 - V.W_U1) * eq
    thr = {k: V.THR * es for k in px}
    book = dict(FIX["book_after"])
    pbook, res, held = X.rescale_book(book, X.frame_caps({"book": book, "last": FIX["last"]}), eq, px, mins, steps, thr)
    lt = D.plan_lt(np.array(t["rw"], dtype=float), t["m"])
    plan = X.merge_rescale(V.plan_live(t, pbook, px, eq, m=t["m"], lt=lt, mins=mins, steps=steps), res, px, mins)
    fin = dict(book)
    for o in plan:
        if o["kind"] in ("skip_min", "skip_thr", "hold"):
            continue
        fin[o["symbol"]] = 0.0 if o["kind"] in X.FULL else fin.get(o["symbol"], 0.0) + o["delta_qty"]
    return t, px, eq, es, fin


def test_1113_target_gross_is_sleeve_x_lv_x_m_not_capital_x_175():
    t, px, eq, es, fin = _replay()
    allw = np.array([v for k, v in t["ws"].items() if k not in D.U1_SYMS])
    w, sc = V.topn_weights(allw)
    gross = float(np.abs(w).sum() * sc * V.LV * t["m"] * es)
    assert t["w1"] == [0.0, 0.0] and V.W_U1 == 0.8 and t["m"] == 0.5
    assert abs(gross - 181.9) < 0.5                                        # not 1959.85 x 1.75
    # the six "missing" legs: target notional below the 5U min -> never opened (backtest skip_min)
    syms = [k for k in t["ws"] if k not in D.U1_SYMS]
    tn = dict(zip(syms, np.abs(w) * sc * V.LV * t["m"] * es))
    for s in ("BTWUSDT", "UAIUSDT", "ARIAUSDT", "QNTUSDT", "WUSDT", "NMRUSDT"):
        assert tn[s] < 5.0 and not fin.get(s)


def test_1113_replay_shrinks_the_old_2x_legs():
    t, px, eq, es, fin = _replay()
    assert fin["ICNTUSDT"] == 44.0 and fin["PLUMEUSDT"] == -538.0 and fin["APRUSDT"] == -160.0
    assert fin["APEUSDT"] == -134.0 and fin["HUMAUSDT"] == -1523.0          # undersized legs grow to target
    assert fin["COTIUSDT"] == -2473.0 and fin["USUSDT"] == -5827.0          # already sized at 1959.85
    gross = sum(abs(q) * px[s] for s, q in fin.items() if q)
    assert 140 < gross < 165
