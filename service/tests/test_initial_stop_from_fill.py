"""Initial stop must be fill_px - stop_m * ATR (backtest parity), not stale suggested_stop."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))



class Px:
    def ticker_price(self, s):
        return 0.2289


def test_fill_stop_math_matches_backtest():
    """Document expected stop after fill (applied in apply_signal)."""
    px, atr, stop_m = 0.228921354, 0.009695, 2.0
    stop = px - stop_m * atr
    assert abs(stop - 0.209531354) < 1e-6
    # Stale suggested from breakout close must NOT be used when atr known
    suggested_stale = 0.204942
    assert stop != suggested_stale


def test_apply_signal_enter_uses_fill_minus_atr_not_suggested():
    import main
    from tests.test_spot_exit_path import FakeSpot

    class BuySpot(FakeSpot):
        def __init__(self):
            super().__init__(free=0.0, locked=0.0, step=0.1)
            self.free_usdt = 2000.0

        def nonzero_balances(self, acct=None):
            return [
                {"asset": "USDT", "free": self.free_usdt, "locked": 0.0},
                {"asset": "FET", "free": self.free, "locked": self.locked},
            ]

        def market_buy(self, symbol, quote, coid):
            # Fill at 0.2289; net qty after 0.1% FET fee
            gross = 5460.3
            fee = 5.4603
            self.free = gross - fee
            return {
                "orderId": 9,
                "executedQty": str(gross),
                "fills": [{
                    "price": "0.2289", "qty": str(gross),
                    "commission": str(fee), "commissionAsset": "FET",
                }],
            }

        def stop_loss(self, symbol, qty, stop, coid=""):
            self.calls.append(("stop", symbol, qty, stop))
            self.locked += qty
            self.free -= qty
            return {"orderId": 55, "clientOrderId": coid, "type": "STOP_LOSS"}

    c = BuySpot()
    st = {"positions": {}, "slots": {}, "approved_families": ["donchian_atr"]}
    sig = {
        "slot": "sat_fet_4h", "symbol": "FETUSDT", "action": "enter",
        "quote_usdt": 1250, "close": 0.2239, "atr": 0.009695,
        "stop_atr_mult": 2.0, "suggested_stop": 0.204942,
        "bar_ts": "2026-09-24T12:00:00+00:00",
        "family": "donchian_atr", "venue": "spot",
    }
    out = main.apply_signal(c, st, sig, live=True, allow_entries=True)
    assert out["executed"], out
    pos = st["positions"]["sat_fet_4h"]
    expected = 0.2289 - 2.0 * 0.009695
    assert abs(pos["stop"] - expected) < 1e-6
    assert abs(pos["stop"] - 0.204942) > 1e-4  # not the stale suggested
    assert pos["stop_order_id"] == 55
