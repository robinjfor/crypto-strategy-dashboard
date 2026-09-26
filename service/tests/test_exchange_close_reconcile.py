"""Stop filled on the exchange (spot order / futures algo) or balance gone →
position closed in state + closed_trades record; never a second sell."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binance_client import BinanceClient  # noqa: E402
from execution import reconcile_exchange_closes  # noqa: E402

FILLED_MS = 1790455200000  # arbitrary


class Spot:
    def __init__(self, balances, order=None, trades=None, px=1.23):
        self.balances, self.order, self.trades, self.px = balances, order, trades or [], px
        self.sells = 0

    def get_order(self, symbol, oid):
        assert self.order is not None
        return self.order

    def nonzero_balances(self, acct=None):
        return self.balances

    def load_filters(self, s):
        return {"stepSize": 0.01, "minQty": 0.01, "minNotional": 5.0, "tickSize": 0.001}

    round_step = staticmethod(BinanceClient.round_step)

    def ticker_price(self, s):
        return self.px

    def my_trades(self, s, limit=50):
        return self.trades

    def market_sell(self, *a, **k):
        self.sells += 1
        raise AssertionError("must never sell")

    def cancel_open_orders(self, s):
        raise AssertionError("no cancels needed")


def _state(stop_oid=11804889):
    return {"positions": {
        "sat_dot_4h": {"status": "FILLED", "symbol": "DOTUSDT", "qty": 581.25, "entry": 1.289,
                       "stop": 1.2295278, "venue": "spot", "stop_order_id": stop_oid,
                       "filled_at": "2026-09-26T13:01:00+08:00"},
        "sat_op_4h": {"status": "FILLED", "symbol": "OPUSDT", "qty": 5124.82, "entry": 0.1462,
                      "stop": 0.1405, "venue": "spot", "stop_order_id": 1},
    }, "slots": {}, "closed_trades": []}


def test_spot_stop_filled_records_close_and_no_second_sell():
    c = Spot(balances=[{"asset": "DOT", "free": 0.00816, "locked": 0.0}],
             order={"status": "FILLED", "executedQty": "581.25", "cummulativeQuoteQty": "714.35625",
                    "orderId": 11804889, "updateTime": FILLED_MS})
    st = _state()
    out = reconcile_exchange_closes(c, st, only_slots={"sat_dot_4h"}, tf_by_slot={"sat_dot_4h": "4h"})
    assert len(out) == 1 and c.sells == 0
    rec = st["closed_trades"][-1]
    assert rec["slot"] == "sat_dot_4h" and rec["reason"] == "stop" and rec["order_id"] == 11804889
    assert rec["qty"] == pytest.approx(581.25) and rec["exit"] == pytest.approx(1.229)
    assert rec["pnl_usdt"] == pytest.approx((1.229 - 1.289) * 581.25, abs=1e-3)
    assert "sat_dot_4h" not in st["positions"] and "sat_op_4h" in st["positions"]
    meta = st["slots"]["sat_dot_4h"]
    assert meta["last_exit_reason"] == "stop" and meta["last_exit_bar_ts"].endswith("+00:00")
    # idempotent: second pass does nothing
    assert reconcile_exchange_closes(c, st, only_slots={"sat_dot_4h"}) == []
    assert len(st["closed_trades"]) == 1


def test_open_position_with_locked_stop_untouched():
    c = Spot(balances=[{"asset": "OP", "free": 0.00005, "locked": 5124.82}],
             order={"status": "NEW", "executedQty": "0", "orderId": 1}, px=0.15)
    st = _state()
    assert reconcile_exchange_closes(c, st, only_slots={"sat_op_4h"}) == []
    assert "sat_op_4h" in st["positions"]


def test_balance_gone_without_stop_id_uses_my_trades():
    trades = [{"isBuyer": True, "time": 1, "orderId": 5, "qty": "581.84", "price": "1.289"},
              {"isBuyer": False, "time": FILLED_MS, "orderId": 11804889, "qty": "300", "price": "1.229", "quoteQty": "368.7"},
              {"isBuyer": False, "time": FILLED_MS, "orderId": 11804889, "qty": "281.25", "price": "1.229", "quoteQty": "345.65625"}]
    c = Spot(balances=[], trades=trades)
    st = _state(stop_oid=None)
    out = reconcile_exchange_closes(c, st, only_slots={"sat_dot_4h"}, tf_by_slot={"sat_dot_4h": "4h"})
    assert out and out[0]["qty"] == pytest.approx(581.25) and out[0]["exit"] == pytest.approx(1.229)
    assert out[0]["reason"] == "stop"


def test_runner_does_not_manage_or_resell_after_reconcile(monkeypatch):
    """apply path: after reconcile, a stale manage signal finds no position."""
    import main
    c = Spot(balances=[{"asset": "DOT", "free": 0.0, "locked": 0.0}],
             order={"status": "FILLED", "executedQty": "581.25", "cummulativeQuoteQty": "714.35625",
                    "orderId": 11804889, "updateTime": FILLED_MS})
    st = _state()
    reconcile_exchange_closes(c, st, only_slots={"sat_dot_4h"})
    out = main.apply_signal(c, st, {"slot": "sat_dot_4h", "symbol": "DOTUSDT", "action": "manage",
                                    "stop": 1.25, "mark": 1.23}, live=True, allow_entries=False)
    assert not out.get("executed") and c.sells == 0


def test_futures_position_gone_records_and_cancels_algo(monkeypatch):
    import futures_client
    calls = []

    class FC:
        def __init__(self, *a, **k): pass
        def position_risk(self, s=None): return [{"symbol": "ARBUSDT", "positionAmt": "0"}]
        def user_trades(self, s, limit=50):
            return [{"time": FILLED_MS, "side": "SELL", "orderId": 42, "qty": "100", "price": "0.21", "quoteQty": "21"}]
        def open_algo_orders(self, s): return [{"algoId": 7}]
        def cancel_algo_order(self, **k): calls.append(k["algoId"])
        def new_order(self, **k): raise AssertionError("never trade")
    monkeypatch.setattr(futures_client, "FuturesDemoClient", FC)
    st = {"positions": {"ls_arb_4h": {"status": "FILLED", "symbol": "ARBUSDT", "qty": 100.0, "entry": 0.23,
                                      "side": "LONG", "venue": "futures", "leverage": 1.5}}, "slots": {}}
    out = reconcile_exchange_closes(Spot(balances=[]), st)
    assert out and out[0]["venue"] == "futures" and out[0]["exit"] == pytest.approx(0.21)
    assert out[0]["pnl_usdt"] == pytest.approx(-2.0)
    assert calls == [7] and "ls_arb_4h" not in st["positions"]
