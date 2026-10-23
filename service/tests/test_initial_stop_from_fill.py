"""Initial stop rule (backtest-anchor fix a770d7e): close-fill families anchor at signal close x (1+20bps) - m*ATR;
fallback to fill - m*ATR when the fill is >1% off the close. Never the stale suggested_stop."""
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


def test_apply_signal_enter_late_fill_falls_back_to_fill_anchor(monkeypatch):
    """Fill 0.2289 is 2.2% above close 0.2239 (>1%) -> 1% fallback: fill anchor, not suggested, not close anchor."""
    import main
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))
    monkeypatch.setattr(main, "exchange_entry_block", lambda *a, **k: None)  # pre-entry exchange check is covered elsewhere
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
    assert out["executed"], out.get("reason")
    pos = st["positions"]["sat_fet_4h"]
    expected = 0.2289 - 2.0 * 0.009695
    assert abs(pos["stop"] - expected) < 1e-6
    assert abs(pos["stop"] - 0.204942) > 1e-4  # not the stale suggested
    assert abs(pos["stop"] - main.spot_initial_stop(0.2289, 0.2239, 2.0, 0.009695)) < 1e-9
    assert pos["stop_order_id"] == 55


def test_close_fill_family_stop_anchored_at_signal_close_like_backtest():
    """C2 FIL 2026-10-04: fill 24 bps below the close put the live stop under the backtest stop and the
    stop hit one bar later. Backtest anchor = signal close x (1 + 20 bps)."""
    import main
    close, atr = 1.0797, 0.01566760129652035
    stop = main.spot_initial_stop(1.07707, close, 1.5, atr)
    assert abs(stop - (close * 1.002 - 1.5 * atr)) < 1e-12  # 1.05836 → hit in the 18:00 bar (low 1.0547)
    # late fill far from the close (> 1%): anchor stays at the fill
    assert main.spot_initial_stop(0.2289, 0.2239, 2.0, 0.009695) == 0.2289 - 2.0 * 0.009695
    # non close-fill family (no close passed) → fill anchor
    assert main.spot_initial_stop(1.0, 0.0, 2.0, 0.01) == 1.0 - 0.02
