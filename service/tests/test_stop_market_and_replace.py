"""Spot hard stop must rest as STOP_LOSS (market) and trail via cancelReplace."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from execution import place_hard_stop, replace_trail_stop  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []
        self.filters = {"stepSize": 0.01, "minQty": 0.01, "minNotional": 5.0, "tickSize": 0.0001}
        self._fail_market = False

    def load_filters(self, s):
        return self.filters

    round_step = staticmethod(lambda qty, step: float(f"{qty:.2f}"))

    def round_qty(self, symbol, qty, price):
        return float(f"{qty:.2f}")

    def ticker_price(self, s):
        return 0.15

    def nonzero_balances(self, acct=None):
        return [{"asset": "OP", "free": 100.0, "locked": 0.0}]

    def cancel_open_orders(self, symbol):
        self.calls.append(("cancel_open", symbol))
        return []

    def stop_loss(self, symbol, qty, stop, coid=""):
        self.calls.append(("STOP_LOSS", symbol, qty, stop, coid))
        if self._fail_market:
            raise RuntimeError("STOP_LOSS not allowed")
        return {"orderId": 101, "type": "STOP_LOSS", "clientOrderId": coid, "stopPrice": str(stop)}

    def stop_loss_limit(self, symbol, qty, stop, limit=None, coid="", slip_pct=0.03):
        self.calls.append(("STOP_LOSS_LIMIT", symbol, qty, stop, limit, slip_pct, coid))
        return {"orderId": 102, "type": "STOP_LOSS_LIMIT", "clientOrderId": coid}

    def cancel_replace_stop_loss(self, symbol, qty, stop, cancel_order_id, coid=""):
        self.calls.append(("cancelReplace", symbol, qty, stop, cancel_order_id, coid))
        return {
            "cancelResult": "SUCCESS",
            "newOrderResult": "SUCCESS",
            "newOrderResponse": {
                "orderId": 202,
                "type": "STOP_LOSS",
                "clientOrderId": coid,
                "stopPrice": str(stop),
            },
        }


def test_place_hard_stop_prefers_stop_loss_market():
    c = FakeClient()
    o = place_hard_stop(c, "OPUSDT", 100.0, 0.1405, "sat_op_4h")
    assert o["orderId"] == 101 and o["type"] == "STOP_LOSS"
    kinds = [x[0] for x in c.calls]
    assert "STOP_LOSS" in kinds and "STOP_LOSS_LIMIT" not in kinds


def test_place_hard_stop_falls_back_to_limit_with_slip():
    c = FakeClient()
    c._fail_market = True
    o = place_hard_stop(c, "OPUSDT", 100.0, 0.1405, "sat_op_4h")
    assert o["type"] == "STOP_LOSS_LIMIT"
    lim = [x for x in c.calls if x[0] == "STOP_LOSS_LIMIT"][0]
    assert lim[5] == pytest.approx(0.03)


def test_replace_trail_uses_cancel_replace_keeps_old_on_fail():
    c = FakeClient()
    pos = {"symbol": "OPUSDT", "qty": 100.0, "stop": 0.14, "stop_order_id": 101}
    o = replace_trail_stop(c, pos, 0.145, "sat_op_4h")
    assert pos["stop"] == 0.145 and pos["stop_order_id"] == 202
    assert any(x[0] == "cancelReplace" for x in c.calls)
    assert not any(x[0] == "cancel_open" for x in c.calls)  # no gap via cancel-all

    c2 = FakeClient()
    def boom(*a, **k):
        raise RuntimeError("reject new")
    c2.cancel_replace_stop_loss = boom
    pos2 = {"symbol": "OPUSDT", "qty": 100.0, "stop": 0.14, "stop_order_id": 101}
    with pytest.raises(RuntimeError):
        replace_trail_stop(c2, pos2, 0.145, "sat_op_4h")
    assert pos2["stop"] == 0.14 and pos2["stop_order_id"] == 101  # unchanged
