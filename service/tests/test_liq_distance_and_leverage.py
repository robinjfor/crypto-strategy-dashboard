"""Liquidation-distance alerts, and R2S isolated leverage on new opens only."""
import liq_distance as L
import isolated_liq as I
import netting


def test_distance_and_alert_fields_deduped_and_no_orders():
    rows = [
        {"symbol": "ETHUSDT", "positionAmt": "-0.912", "markPrice": "3000", "liquidationPrice": "2700",
         "isolatedMargin": "400", "isolatedWallet": "420", "notional": "-2736", "maintMargin": "15",
         "leverage": "2", "marginType": "isolated"},
        {"symbol": "BTCUSDT", "positionAmt": "0.01", "markPrice": "100000", "liquidationPrice": "50000",
         "isolatedMargin": "500", "isolatedWallet": "500", "maintMargin": "4", "leverage": "2", "marginType": "isolated"},
    ]
    holders = {"ETHUSDT": ["demo_e5_min5_x175_t05top20", "demo_r2s_top10_3k"], "BTCUSDT": ["demo_r2s_top10_3k"]}
    st = {}
    view = L.apply(st, rows, "2026-11-11T00:08:00+00:00", holders)
    assert view["orders"] == 0 and view["pause"] is False
    eth = next(p for p in view["near"] if p["symbol"] == "ETHUSDT")
    assert eth["side"] == "SHORT" and eth["distance"] == round(300 / 3000, 6)
    assert eth["liq"] == 2700 and eth["mark"] == 3000
    assert eth["holders"] == holders["ETHUSDT"]
    assert eth["isolatedMargin"] == 400 and eth["isolatedWallet"] == 420
    alerts = [a for a in st["reconcile_alerts"] if a["kind"] == "liq_distance"]
    assert len(alerts) == 1 and alerts[0]["symbol"] == "ETHUSDT" and alerts[0]["pause"] is False
    assert alerts[0]["orders"] == 0 and alerts[0]["side"] == "SHORT"
    # still inside the band: no second alert
    L.apply(st, rows, "2026-11-11T00:23:00+00:00", holders)
    assert len([a for a in st["reconcile_alerts"] if a["kind"] == "liq_distance"]) == 1
    # recovered, then breached again: one new alert
    far = [{**rows[0], "liquidationPrice": "1500"}]
    L.apply(st, far, "2026-11-11T00:38:00+00:00", holders)
    L.apply(st, rows, "2026-11-11T00:53:00+00:00", holders)
    assert len([a for a in st["reconcile_alerts"] if a["kind"] == "liq_distance"]) == 2
    assert "BTCUSDT" not in {a["symbol"] for a in st["reconcile_alerts"]}


def test_snapshot_keeps_liquidation_fields():
    row = {"symbol": "ETHUSDT", "positionAmt": "-1", "entryPrice": "3000", "markPrice": "2900",
           "unRealizedProfit": "10", "leverage": "2", "marginType": "isolated", "positionSide": "BOTH",
           "liquidationPrice": "1500", "isolatedMargin": "1500", "isolatedWallet": "1500",
           "notional": "-2900", "maintMargin": "14.5", "extraSecret": "no"}
    got = L.snapshot_position(row)
    for k in ("liquidationPrice", "isolatedMargin", "isolatedWallet", "notional", "maintMargin"):
        assert got[k] == row[k]
    assert "extraSecret" not in got


def test_isolated_liq_price_matches_the_wallet_identity():
    # long: wallet + qty*(liq-entry) == liq*qty*mmr - cum
    liq = I.liquidation_price(100.0, 1.0, 2.0, 0.004, 0.0)
    assert abs((50.0 + 1.0 * (liq - 100.0)) - (liq * 0.004)) < 1e-9
    liq_s = I.liquidation_price(100.0, -1.0, 2.0, 0.004, 0.0)
    assert abs((50.0 + 1.0 * (100.0 - liq_s)) - (liq_s * 0.004)) < 1e-9
    assert I.breached(1.0, liq, liq - 0.01) and not I.breached(1.0, liq, liq + 1)
    assert I.breached(-1.0, liq_s, liq_s + 0.01)
    cash, qty = I.settle_liquidation(1000.0 - 100.0, 100.0, 1.0, 2.0, liq)
    assert qty == 0.0
    loss = I.liquidation_loss(100.0, 1.0, 2.0, liq)
    assert abs((1000.0 - loss) - cash) < 1e-9


def test_replay_liquidates_at_3x_and_not_at_2x_on_the_same_wick():
    import numpy as np
    # weight 1 decided on bar 0, filled at the open of bar 1. Bar 1 low is through the 3x liq and not the 2x liq.
    o = np.array([[100.0], [100.0], [90.0]])
    h = np.array([[100.0], [100.0], [90.0]])
    l = np.array([[100.0], [60.0], [90.0]])
    c = np.array([[100.0], [90.0], [90.0]])
    w = np.array([[1.0], [1.0], [1.0]])
    br = {"0": [(1e18, 0.004, 0.0)]}
    base = I.replay(o, h, l, c, w, 100.0, leverage=None, cost=0.0)
    two = I.replay(o, h, l, c, w, 100.0, leverage=2.0, brackets=br, cost=0.0)
    three = I.replay(o, h, l, c, w, 100.0, leverage=3.0, brackets=br, cost=0.0)
    assert base["n_liq"] == 0 and two["n_liq"] == 0 and three["n_liq"] == 1
    assert three["liq_loss"] > 0
    # the wick recovers, so full mark-to-market keeps the position; 3x isolated locks in the margin
    assert three["equity"][-1] < two["equity"][-1]


def test_r2s_sets_2x_on_a_new_open_and_does_not_lower_an_existing_position():
    class F:
        def __init__(self, amt, lev):
            self.amt, self.lev, self.calls, self.orders = amt, lev, [], []

        def position_risk(self):
            if self.amt == 0:
                return []
            return [{"symbol": "ETHUSDT", "positionAmt": str(self.amt), "leverage": str(self.lev)}]

        def set_margin_type(self, *a):
            self.calls.append(("margin",) + a)

        def set_leverage(self, sym, lev):
            self.calls.append(("lev", sym, lev))
            if self.amt and lev < self.lev:
                raise RuntimeError("-4161 Leverage reduction is not supported")

        def new_order(self, **k):
            self.orders.append(k)
            return {"status": "FILLED", "executedQty": k["quantity"], "avgPrice": "1"}

        def get_order(self, *a):
            return None

    flat = F(0, 3)
    n = netting.Netter.__new__(netting.Netter)
    n.fut, n.intents, n.orders, n.n, n.tag = flat, {"ETHUSDT": [{"slot": "demo_r2s_top10_3k"}]}, [], 0, "t"
    assert n.lev_for("ETHUSDT") == 2
    assert n._send({}, "ETHUSDT", "SELL", 1.0, False)[0] == 1.0
    assert ("lev", "ETHUSDT", 2) in flat.calls and flat.orders

    held = F(-0.071, 3)
    n.fut = held
    assert n._send({}, "ETHUSDT", "SELL", 0.05, False)[0] == 0.05
    assert not any(c[0] == "lev" for c in held.calls)
    assert held.orders and held.orders[0].get("reduceOnly") != "true"
