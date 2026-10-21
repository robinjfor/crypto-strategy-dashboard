"""U1 closed outside the runner is booked from Binance trades (sleeve flat, closed_trades, no pause,
no unpause); open orders listed from Binance."""
from __future__ import annotations

import reconcile

T0 = 1792540888166
T1 = 1792584498240


class FC:
    api_key = "x"

    def account(self):
        return {"assets": [{"asset": "USDC", "walletBalance": "1011.52", "marginBalance": "1011.52"}]}

    def position_risk(self):
        return []

    def all_orders(self, sym, start_ms=None, limit=100):
        return [{"orderId": 1, "clientOrderId": "u1-2026102100-BTC"},
                {"orderId": 2, "clientOrderId": "u1-manualclose-BTC-1021"}] if sym == "BTCUSDC" else []

    def user_trades_since(self, sym, start, limit=1000):
        if sym != "BTCUSDC":
            return []
        return [{"id": 10, "orderId": 1, "side": "BUY", "qty": "0.011", "price": "82901.8", "commission": "0.41",
                 "commissionAsset": "USDC", "realizedPnl": "0", "time": T0},
                {"id": 11, "orderId": 2, "side": "SELL", "qty": "0.011", "price": "83364.3",
                 "commission": "0.4127", "commissionAsset": "USDC", "realizedPnl": "5.0875", "time": T1}]

    def income(self, sym, t, a, b=None):
        return [{"income": "-0.0913"}]

    def open_orders(self):
        return [{"symbol": "ETHUSDC", "orderId": 7, "side": "SELL", "type": "LIMIT", "price": "3000", "origQty": "0.1",
                 "status": "NEW"}]

    def open_algo_orders(self):
        return [{"symbol": "BTCUSDT", "algoId": 9, "side": "SELL", "orderType": "STOP_MARKET",
                 "triggerPrice": "80000", "quantity": "0.01", "reduceOnly": True}]


def _state():
    return {"paused": True, "paused_slots": {"u1_btc_eth_usdc": {"paused_by": "reconcile"}}, "closed_trades": [],
            "u1_live": {"quote": "USDC", "slot": "u1_btc_eth_usdc", "code": "U1", "cash": -911.9, "fees": 0.41,
                        "funding": 0.0, "units": {"BTC": 0.011, "ETH": 0.0}, "started_at": "2026-10-20T00:00:00+00:00",
                        "fills": [{"symbol": "BTCUSDC", "qty": 0.011, "qty_signed": 0.011, "price": 82901.8,
                                   "fee": 0.41, "order_id": 1, "filled_at": "2026-10-21T00:01:28+00:00"}]}}


def test_external_close_booked_from_binance_and_slot_stays_paused():
    st = _state()
    rep = reconcile.build_report(st, client=None, fc=FC(), alloc_doc={"slots": []})
    assert [c["symbol"] for c in rep["u1_external_closes"]] == ["BTCUSDC"]
    assert rep["mismatches"] == []  # explained
    st = reconcile.apply_report(st, rep)
    sl = st["u1_live"]
    assert sl["units"]["BTC"] == 0.0
    assert abs(sl["cash"] - (-911.9 + 0.011 * 83364.3 - 0.4127)) < 1e-9
    ct = st["closed_trades"][-1]
    assert ct["order_id"] == "2" and ct["client_order_id"] == ["u1-manualclose-BTC-1021"]
    assert ct["exit"] == 83364.3 and ct["entry"] == 82901.8 and ct["pnl_gross_usdt"] == 5.0875
    assert abs(ct["pnl_usdt"] - (5.0875 - 0.41 - 0.4127)) < 1e-4 and ct["funding_usdt"] == -0.0913
    assert "u1_btc_eth_usdc" in st["paused_slots"]  # never unpaused
    assert any(a["kind"] == "external_close_adopted" for a in st["reconcile_alerts"])
    # idempotent
    n = len(st["closed_trades"])
    st = reconcile.apply_report(st, reconcile.build_report(st, client=None, fc=FC(), alloc_doc={"slots": []}))
    assert len(st["closed_trades"]) == n


def test_open_orders_from_binance():
    import api

    class S:
        def open_orders(self):
            return [{"symbol": "FILUSDT", "orderId": 3, "side": "SELL", "type": "STOP_LOSS_LIMIT",
                     "stopPrice": "1.0", "origQty": "10"}]
    oo = api._open_orders(S(), FC())
    assert oo["count"] == 3 and not oo["errors"]
    assert oo["futures"][0]["quote"] == "USDC" and oo["futures_algo"][0]["quote"] == "USDT"
    assert oo["spot"][0]["stopPrice"] == "1.0"

    class Bad:
        def open_orders(self):
            raise RuntimeError("x")
    assert api._open_orders(Bad(), None)["errors"]
