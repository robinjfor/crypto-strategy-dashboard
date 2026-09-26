"""Spot exit must cancel the resting stop FIRST, then market-sell the FREE
balance floored to stepSize (state qty is gross of the base-asset buy fee).
Mocked exchange only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binance_client import BinanceClient  # noqa: E402


class FakeSpot:
    api_key = "x"

    def __init__(self, free=718.51077, locked=0.0, step=0.01):
        self.free, self.locked, self.step = free, locked, step
        self.calls: list = []
        self._filters: dict = {}

    # exchange-ish behaviour
    def nonzero_balances(self, acct=None):
        return [{"asset": "FIL", "free": self.free, "locked": self.locked}]

    def cancel_open_orders(self, symbol):
        self.calls.append(("cancel", symbol))
        self.free += self.locked
        self.locked = 0.0
        return []

    def open_orders(self, symbol=None):
        return [{"orderId": 1}] if self.locked else []

    def ticker_price(self, symbol):
        return 1.1489

    def load_filters(self, symbol):
        return {"stepSize": self.step, "minQty": 0.01, "minNotional": 5.0, "tickSize": 0.0001}

    round_step = staticmethod(BinanceClient.round_step)

    def round_qty(self, symbol, qty, price):
        return BinanceClient.round_qty(self, symbol, qty, price)

    def market_sell(self, symbol, qty, coid):
        self.calls.append(("sell", symbol, qty))
        if qty > self.free + 1e-12:
            raise RuntimeError("Account has insufficient balance for requested action.")
        self.free -= qty
        return {"orderId": 77, "executedQty": str(qty), "fills": [{"price": "1.1480", "qty": str(qty)}]}

    def market_buy(self, symbol, quote, coid):
        raise AssertionError("no buys in exit test")

    def stop_loss_limit(self, symbol, qty, stop, limit, coid):
        self.calls.append(("stop", symbol, qty, stop))
        if qty > self.free + 1e-12:
            raise RuntimeError("insufficient balance")
        self.free -= qty
        self.locked += qty
        return {"orderId": 5, "clientOrderId": coid}


def _state(qty=719.23):
    return {"approved_families": ["donchian_atr"], "slots": {},
            "positions": {"sat_fil_1h": {"status": "FILLED", "symbol": "FILUSDT", "qty": qty,
                                         "entry": 1.0428, "stop": 1.15055, "venue": "spot"}}}


def _exit_sig():
    return {"slot": "sat_fil_1h", "symbol": "FILUSDT", "action": "exit", "reason": "stop",
            "exit_ref": 1.1493, "exit_bar_ts": "2026-09-26T18:00:00+00:00", "mark": 1.1489,
            "family": "donchian_atr", "venue": "spot"}


@pytest.fixture(autouse=True)
def _no_futures(monkeypatch):
    import futures_client

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("spot exit must never touch futures")
    monkeypatch.setattr(futures_client, "FuturesDemoClient", Boom)


def test_fil_exit_with_resting_stop_cancels_then_sells_free_floor_step():
    import main
    # 0.1% fee taken in FIL: state 719.23 gross, exchange 718.51077 of which all locked by stop
    c = FakeSpot(free=0.0, locked=718.51077)
    st = _state()
    out = main.apply_signal(c, st, _exit_sig(), live=True, allow_entries=True)
    assert out["executed"], out
    kinds = [k[0] for k in c.calls]
    assert kinds.index("cancel") < kinds.index("sell")
    sell = [k for k in c.calls if k[0] == "sell"][0]
    assert sell[2] == pytest.approx(718.51)  # floored to 0.01 step, ≤ free
    assert "sat_fil_1h" not in st["positions"]
    assert st["closed_trades"][-1]["qty"] == pytest.approx(718.51)
    assert out["fill"]["venue"] == "spot" and out["fill"]["price"] == pytest.approx(1.148)
    assert c.locked == 0.0


def test_fil_exit_without_stop_old_bug_regression():
    """Old path sold state qty 719.23 > free 718.51 → insufficient balance."""
    import main
    c = FakeSpot(free=718.51077)
    st = _state()
    out = main.apply_signal(c, st, _exit_sig(), live=True, allow_entries=True)
    assert out["executed"] and not out.get("error")
    assert c.free == pytest.approx(0.00077, abs=1e-6)


def test_exit_error_is_reported_not_silent():
    import main
    c = FakeSpot(free=0.0)
    st = _state()
    out = main.apply_signal(c, st, _exit_sig(), live=True, allow_entries=True)
    assert not out["executed"] and out.get("error")
    assert "sat_fil_1h" in st["positions"]


def test_net_filled_qty_subtracts_base_fee():
    from execution import net_filled_qty
    order = {"executedQty": "719.23", "fills": [
        {"price": "1.0427", "qty": "220.09", "commission": "0.22009", "commissionAsset": "FIL"},
        {"price": "1.0428", "qty": "499.14", "commission": "0.49914", "commissionAsset": "FIL"}]}
    assert net_filled_qty(order, "FILUSDT") == pytest.approx(718.51077)
    order_bnb = {**order, "fills": [{**f, "commissionAsset": "BNB"} for f in order["fills"]]}
    assert net_filled_qty(order_bnb, "FILUSDT") == pytest.approx(719.23)


def test_hard_stop_clamped_to_free_and_restored_on_manage():
    import main
    c = FakeSpot(free=718.51077)
    st = _state()
    sig = {"slot": "sat_fil_1h", "symbol": "FILUSDT", "action": "manage", "stop": 1.10,
           "mark": 1.20, "bar_ts": "x", "family": "donchian_atr"}
    main.apply_signal(c, st, sig, live=True, allow_entries=True)
    stops = [k for k in c.calls if k[0] == "stop"]
    assert stops and stops[0][2] == pytest.approx(718.51) and stops[0][3] == pytest.approx(1.15055)
    assert st["positions"]["sat_fil_1h"].get("stop_order_id") == 5


def test_manual_close_cancels_before_reading_free():
    from execution import market_close_slot
    c = FakeSpot(free=0.0, locked=718.51077)
    st = _state()
    r = market_close_slot(c, st, "sat_fil_1h", reason="manual")
    assert r["ok"] and r["closed"]["qty"] == pytest.approx(718.51)


def test_futures_close_cancels_algo_stops():
    from futures_execution import close_position_market
    calls = []

    class FC:
        def cancel_all(self, s): calls.append("cancel_all")
        def open_algo_orders(self, s): return [{"algoId": 9}]
        def cancel_algo_order(self, **k): calls.append(("algo_cancel", k["algoId"]))
        def exchange_info(self): return {"symbols": [{"symbol": "ARBUSDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.0001"}]}]}
        def new_order(self, **p): calls.append(("order", p["side"], p["reduceOnly"])); return {"orderId": 1}
    close_position_market(FC(), symbol="ARBUSDT", qty=100.0, is_long=True)
    assert ("algo_cancel", 9) in calls and calls.index(("algo_cancel", 9)) < calls.index(("order", "SELL", "true"))
