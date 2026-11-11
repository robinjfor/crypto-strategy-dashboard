"""R2S weight 0 closes the live book reduce-only, 3x names first, and does not flip."""
from datetime import datetime, timezone

import pytest

import demo_e5
import demo_r2s
import demo_r2s_exec as X
import netting
import orchestrator as O

# Books from the 11/11 20:08 Taipei state. Exchange qtys from the 20:36 snapshot.
BOOK = {"ETHUSDT": -0.071, "XRPUSDT": -133.4, "SOLUSDT": -3.18, "DOGEUSDT": -4198.0, "HYPEUSDT": -4.05}
EXCHANGE = {"ETHUSDT": -0.912, "XRPUSDT": -151.8, "SOLUSDT": -3.41, "DOGEUSDT": -4487.0, "HYPEUSDT": -4.32}
MARKS = {"ETHUSDT": 2053.82, "XRPUSDT": 1.0534, "SOLUSDT": 83.71, "DOGEUSDT": 0.0679, "HYPEUSDT": 71.98}
NOW = datetime(2026, 11, 12, 0, 8, tzinfo=timezone.utc)


class Fut:
    def __init__(self):
        self.pos = dict(EXCHANGE)
        self.orders = []

    def exchange_info(self):
        return {"symbols": [{"symbol": s, "status": "TRADING", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001" if s == "ETHUSDT" else "0.01"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in EXCHANGE]}

    def position_risk(self):
        return [{"symbol": s, "positionAmt": str(q), "leverage": "3" if s != "ETHUSDT" else "2"} for s, q in self.pos.items()]

    def mark_price(self, s):
        return MARKS[s]

    def set_margin_type(self, *a, **k):
        return {}

    def set_leverage(self, *a, **k):
        return {}

    def get_order(self, s, c):
        return None

    def new_order(self, **p):
        signed = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
        self.pos[p["symbol"]] = self.pos.get(p["symbol"], 0.0) + signed
        self.orders.append(p)
        return {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": str(MARKS[p["symbol"]])}

    def income(self, *a, **k):
        return []


def test_weight_zero_closes_are_reduce_only_and_3x_names_go_first():
    assert demo_e5.LV == 1.75
    plan = {"generated_at": NOW.isoformat(), "sizing": "quote_pool",
            "targets": {"demo_r2s_top10_3k": 0.0, "demo_e5_min5_x175_t05top20": 3875.17},
            "quote_pool": {"demo_r2s_top10_3k": 3875.17, "demo_e5_min5_x175_t05top20": 3875.17}}
    assert O.sizing_capital("demo_r2s_top10_3k", 3088.0, now=NOW, plan_doc=plan) == 0.0
    for sym, qty in BOOK.items():
        q, kind = demo_r2s.decide_minnot(0.0, qty, MARKS[sym], 3088.0, 5.0, 0.0)
        assert (q, kind) == (0.0, "close")
    fut = Fut()
    st = {"demo_r2s_exec": {"cash": 4216.94, "book": dict(BOOK), "funding_since": 1},
          "demo_e5v_exec": {"cash": 6640.62, "book": {"ETHUSDT": -0.841}},
          "paused_slots": {"demo_r2s_top10_3k": {"paused_by": "policy", "closes_positions": False}},
          "approved_families": ["keep"]}
    kept = list(st["approved_families"])
    n = netting.Netter(fut, tag="r2s")
    # The same order the executor uses: priority index, reduce-only close of the whole book.
    for sym in sorted(BOOK, key=X._close_rank):
        qty = BOOK[sym]
        n.add(st, "demo_r2s_top10_3k", sym, -qty, MARKS[sym],
              netting.ledger_cb(st["demo_r2s_exec"], sym), kind="close", reduce_only=True, priority=X._close_rank(sym))
    sent = n.flush()
    assert [o["symbol"] for o in sent] == ["XRPUSDT", "SOLUSDT", "DOGEUSDT", "HYPEUSDT", "ETHUSDT"]
    assert all(o["reduce_only"] is True and o["side"] == "BUY" for o in sent)
    assert [o["qty"] for o in sent] == [133.4, 3.18, 4198.0, 4.05, 0.071]
    assert st["demo_r2s_exec"]["book"] == {}
    assert fut.pos["ETHUSDT"] == pytest.approx(-0.841)
    for sym, exch in EXCHANGE.items():
        assert fut.pos[sym] * exch > 0  # still the same side: the close did not flip
    assert st["approved_families"] == kept


def test_reduce_only_close_does_not_open_when_the_book_exceeds_the_exchange():
    fut = Fut()
    fut.pos = {"XRPUSDT": -10.0}
    st = {"demo_r2s_exec": {"cash": 1.0, "book": {"XRPUSDT": -133.4}}}
    n = netting.Netter(fut, tag="over")
    n.add(st, "demo_r2s_top10_3k", "XRPUSDT", 133.4, 1.0, netting.ledger_cb(st["demo_r2s_exec"], "XRPUSDT"),
          kind="close", reduce_only=True, priority=0)
    sent = n.flush()
    assert len(sent) == 1 and sent[0]["reduce_only"] is True and sent[0]["qty"] == 10.0
    assert fut.pos["XRPUSDT"] == pytest.approx(0.0)


def test_r2s_pause_keeps_approval():
    st = {"approved_families": ["keep"], "approved": ["keep-me"],
          "demo_r2s_exec": {"book": dict(BOOK), "cash": 4216.94}}
    out = O.apply_r2s_pause(st, now_iso="2026-11-11T12:46:00+00:00")
    assert out["applied"] is True and out["orders"] == 0
    assert st["approved_families"] == ["keep"] and st["approved"] == ["keep-me"]
    assert st["paused_slots"]["demo_r2s_top10_3k"]["closes_positions"] is False
    assert st["paused_slots"]["demo_r2s_top10_3k"]["approval"] == "kept"
    assert st["demo_r2s_exec"]["book"] == BOOK
    assert O.apply_r2s_pause(st)["already"] is True
