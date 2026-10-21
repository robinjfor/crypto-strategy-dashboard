"""Ledger backfill/diff, manual-order tagging, one-position-per-coin exchange guard, live-vs-backtest."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import ledger
import live_vs_backtest as lvb
import reconcile
from execution import is_manual_coid

T = ledger.BACKFILL_START_MS + 3600_000


class Spot:
    def __init__(self):
        self.calls = 0

    def my_trades_from(self, sym, from_id=0, limit=1000):
        if sym != "FILUSDT" or from_id > 3:
            return []
        rows = [
            {"id": 1, "orderId": 10, "isBuyer": True, "qty": "100", "price": "2", "commission": "0.1",
             "commissionAsset": "FIL", "time": T},
            {"id": 2, "orderId": 11, "isBuyer": False, "qty": "99.9", "price": "2.2", "commission": "0.2",
             "commissionAsset": "USDT", "time": T + 7200_000},
            {"id": 3, "orderId": 12, "isBuyer": True, "qty": "1", "price": "2", "commission": "0.001",
             "commissionAsset": "BNB", "time": T + 9000_000},
            {"id": 0, "orderId": 9, "isBuyer": True, "qty": "1", "price": "2", "commission": "0",
             "commissionAsset": "USDT", "time": ledger.BACKFILL_START_MS - 1},
        ]
        return [r for r in rows if r["id"] >= from_id]

    def get_order(self, sym, oid):
        return {10: {"clientOrderId": "sat_fil_1h-enter-2026091600"}, 11: {"clientOrderId": "sat_fil_1h-exit-x"},
                12: {"clientOrderId": "manual-fil-test"}}.get(oid, {"clientOrderId": "web_abc"})


def test_manual_prefixes():
    assert is_manual_coid("manual-x") and is_manual_coid("u1-manualclose-BTC-1021")
    assert is_manual_coid(["a", "manual-b"]) and not is_manual_coid("sat_fil_1h-exit-1")
    assert ledger.classify("web_abc", ["sat_fil_1h"]) == ("manual", True)
    assert ledger.classify("sat_fil_1h-exit-1", ["sat_fil_1h"]) == ("sat_fil_1h", False)


def test_ledger_backfill_dedup_fees_and_diff():
    doc, summ = ledger.sync(None, client=Spot(), fc=None, symbols={"spot": {"FILUSDT"}, "futures": set()},
                            slot_ids=["sat_fil_1h"])
    assert summ["added"] == 3  # pre-9/15 trade skipped
    doc, summ = ledger.sync(doc, client=Spot(), fc=None, symbols={"spot": {"FILUSDT"}, "futures": set()},
                            slot_ids=["sat_fil_1h"])
    assert summ["added"] == 0 and len(doc["fills"]) == 3  # cursor + tradeId dedup
    f = {r["tradeId"]: r for r in doc["fills"]}
    assert abs(f[1]["commission_quote"] - 0.2) < 1e-12 and f[1]["slot"] == "sat_fil_1h"  # base fee at fill px
    assert f[3]["commission_quote"] is None and f[3]["manual"] and f[3]["tag"] == "人工單"  # BNB unpriced
    s = ledger.summarize(doc)
    assert s["fee_unpriced_fills"] == 1 and s["n_manual_fills"] == 1
    closed = [{"slot": "sat_fil_1h", "symbol": "FILUSDT", "order_id": 11, "qty": 99.9, "exit": 2.2,
               "pnl_usdt": 19.98, "venue": "spot"}]
    d = ledger.diff_closed_trades(doc, closed)[0]
    assert d["status"] == "ok" and abs(d["ledger_pnl"] - (0.2 * 99.9 - 0.4)) < 1e-6
    assert abs(d["diff_pnl"] + 0.4) < 1e-6


def test_reconcile_manual_spot_order_no_pause():
    class S:
        def nonzero_balances(self):
            return []

        def all_orders(self, sym, oid=0):
            import time
            return [{"orderId": 5, "clientOrderId": "manual-sell-dot", "side": "SELL", "executedQty": "10",
                     "time": int(time.time() * 1000)}]

        def get_order(self, s, o):
            return {"status": "NEW"}
    st = {"positions": {"c5": {"status": "FILLED", "venue": "spot", "symbol": "DOTUSDT", "qty": 10.0}}}
    m = reconcile._spot_checks(S(), st)[0]
    assert m["kind"] == "manual_order" and m["pause"] is False


def test_exchange_guard_blocks_duplicates(monkeypatch):
    import futures_client
    import main

    class FC:
        api_key = "x"

        def __init__(self, *a, **k):
            pass

        def position_risk(self):
            return [{"symbol": "ARBUSDC", "positionAmt": "-5"}]

        def get_order(self, s, c):
            return None
    monkeypatch.setattr(futures_client, "FuturesDemoClient", FC)
    sig = {"symbol": "ARBUSDT", "close": 0.5}
    assert main.exchange_entry_block(None, sig, "futures", "s2-enter-x", 750)[0] == "exchange_position_open"

    class FC2(FC):
        def position_risk(self):
            return []

        def get_order(self, s, c):
            return {"orderId": 1}
    monkeypatch.setattr(futures_client, "FuturesDemoClient", FC2)
    assert main.exchange_entry_block(None, sig, "futures", "s2-enter-x", 750)[0] == "duplicate_client_order_id"

    class FC3(FC2):
        def get_order(self, s, c):
            return None
    monkeypatch.setattr(futures_client, "FuturesDemoClient", FC3)
    assert main.exchange_entry_block(None, sig, "futures", "s2-enter-x", 750) is None

    class Sp:
        def get_order_by_coid(self, s, c):
            return None

        def nonzero_balances(self):
            return [{"asset": "ARB", "free": "1000", "locked": "0"}]
    assert main.exchange_entry_block(Sp(), sig, "spot", "c-enter", 750)[0] == "exchange_spot_holding"

    class Boom(FC3):
        def position_risk(self):
            raise RuntimeError("down")
    monkeypatch.setattr(futures_client, "FuturesDemoClient", Boom)
    assert main.exchange_entry_block(None, sig, "futures", "x", 750)[0] == "exchange_check_failed"


def test_live_vs_backtest_compare_and_pause():
    bar = pd.Timestamp("2026-10-10T02:00:00Z")
    bt = [{"entry_bar": bar, "exit_bar": bar + pd.Timedelta(hours=5), "side": "LONG", "reason": "stop_loss",
           "entry_px": 1.0, "exit_px": 0.95}]
    live = {"entry_ms": int((bar + pd.Timedelta(hours=1, minutes=1)).timestamp() * 1000),
            "exit_ms": int((bar + pd.Timedelta(hours=5, minutes=20)).timestamp() * 1000),
            "entry_px": 1.001, "exit_px": 0.9496, "side": "LONG", "reason": "stop"}
    r = lvb.compare_trade(live, bt, "1h")
    assert r["match"] and r["entry_bps"] == 10.0
    r2 = lvb.compare_trade({**live, "exit_px": 0.94}, bt, "1h")
    assert not r2["match"] and r2["problems"] == ["exit_price"]
    r3 = lvb.compare_trade({**live, "entry_ms": live["entry_ms"] + 5 * 3600_000}, bt, "1h")
    assert r3["problems"] == ["no_backtest_trade_at_entry_bar"]
    now = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)
    assert lvb.due({}, now) and not lvb.due({"live_vs_backtest": {"day_utc": "2026-10-22"}}, now)
    assert not lvb.due({}, datetime(2026, 10, 22, 0, 10, tzinfo=timezone.utc))
    rep = {"at": "t", "rows": [{**r2, "slot": "sat_fil_1h", "closed_at": "2026-10-21T20:00:00+08:00", "pause": True},
                               {**r2, "slot": "old", "closed_at": "2026-10-01T00:00:00+08:00", "pause": False}]}
    st = lvb.apply({"paused": True, "paused_slots": {}}, rep)
    assert list(st["paused_slots"]) == ["sat_fil_1h"] and st["paused"] is True
    assert st["reconcile_alerts"][0]["kind"] == "live_vs_backtest_mismatch"
