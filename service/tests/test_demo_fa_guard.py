import demo_fa, demo_fa_guard


class Spot:
    def __init__(self, px=10.0): self.px, self.sells = px, []
    def ticker_price(self, s): return self.px
    def load_filters(self, s): return {"stepSize": 0.001}
    from binance_client import BinanceClient as _B
    round_step = staticmethod(_B.round_step)
    def market_sell(self, s, q, coid): self.sells.append(q)


class Fut:
    def __init__(self, upl=0.0, margin=25.0, mark=10.0, amt=-7.5, avail=1000.0):
        self.upl, self.margin, self.mark, self.amt, self.avail = upl, margin, mark, amt, avail
        self.orders, self.topups = [], []
    def exchange_info(self): return {"symbols": [{"symbol": "AUSDT", "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}]}]}
    def position_risk(self): return [{"symbol": "AUSDT", "positionAmt": self.amt, "markPrice": self.mark, "unRealizedProfit": self.upl, "isolatedMargin": self.margin}]
    def account(self): return {"availableBalance": self.avail}
    def add_isolated_margin(self, s, a): self.topups.append(a)
    def new_order(self, **kw): self.orders.append(kw)


def _st(enabled=True):
    return {"demo_candidates": {demo_fa.SLOT: {"enabled": enabled}}, "demo_fa_positions": {"AUSDT": {"spot_qty": 7.5, "perp_qty": 7.5}}}


def test_inactive_when_disabled():
    f = Fut(upl=-20); st = _st(False)
    assert demo_fa_guard.run(st, Spot(), f) == [] and not f.topups and "control_log" not in st


def test_squeeze_tops_up_margin():
    f = Fut(upl=-15, margin=25); st = _st()
    a = demo_fa_guard.run(st, Spot(12.0), f)
    assert f.topups and abs(f.topups[0] - (7.5 * 10 / 3 - 10)) < 1e-9 and a[0]["action"] == "fa_margin_topup"
    assert st["control_log"][0]["actor"] == "guard"


def test_squeeze_reduces_both_legs_when_no_balance():
    f = Fut(upl=-15, margin=25, avail=0); sp = Spot(12.0); st = _st()
    a = demo_fa_guard.run(st, sp, f)
    assert a[0]["action"] == "fa_reduce_squeeze" and f.orders[0]["side"] == "BUY" and f.orders[0]["reduceOnly"] == "true"
    assert sp.sells == [3.75] and st["demo_fa_positions"]["AUSDT"] == {"spot_qty": 3.75, "perp_qty": 3.75}


def test_drift_trims_larger_leg_only():
    f = Fut(); sp = Spot(11.0); st = _st()   # spot 82.5U vs perp 75U -> 9.5% drift
    a = demo_fa_guard.run(st, sp, f)
    assert a[0]["action"] == "fa_rebalance" and a[0]["leg"] == "spot" and not f.orders
    assert abs(sp.sells[0] - 0.681) < 1e-9


def test_no_action_within_limits():
    f = Fut(upl=-5); st = _st()
    assert demo_fa_guard.run(st, Spot(10.2), f) == []
