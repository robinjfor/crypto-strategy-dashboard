"""(2) futures wallets + total assets from Binance; (3) reconcile pauses mismatched slots + alerts."""
from __future__ import annotations

import reconcile


class FC:
    api_key = "x"

    def __init__(self, positions):
        self.pos = positions

    def account(self):
        return {"assets": [{"asset": "USDT", "walletBalance": "5000", "marginBalance": "4990",
                            "unrealizedProfit": "-10", "availableBalance": "4900"},
                           {"asset": "USDC", "walletBalance": "4999.19", "marginBalance": "4997.32",
                            "unrealizedProfit": "-1.87", "availableBalance": "4000"}]}

    def position_risk(self):
        return self.pos


class Spot:
    def __init__(self, bals, stop_status="NEW"):
        self.bals, self.stop_status = bals, stop_status

    def nonzero_balances(self):
        return [{"asset": a, "free": str(v), "locked": "0"} for a, v in self.bals.items()]

    def get_order(self, sym, oid):
        return {"status": self.stop_status}


def _state():
    return {"paused": True, "paused_slots": {}, "positions": {
        "s2": {"status": "FILLED", "venue": "futures", "symbol": "ETHUSDT", "qty": 1.0, "side": "SHORT"},
        "c2": {"status": "FILLED", "venue": "spot", "symbol": "FILUSDT", "qty": 10.0, "stop_order_id": 5}}}


def test_ok_when_binance_matches():
    st = _state()
    fc = FC([{"symbol": "ETHUSDT", "positionAmt": "-1.0", "markPrice": "3000"}])
    rep = reconcile.build_report(st, client=Spot({"FIL": 9.99}), fc=fc, alloc_doc={"slots": []})
    assert rep["ok"], rep
    assert rep["wallets"]["USDT"]["margin_balance"] == 4990
    assert rep["wallets"]["USDC"]["wallet"] == 4999.19
    st = reconcile.apply_report(st, rep)
    assert st["paused_slots"] == {} and st["reconcile"]["consecutive_ok"] == 1


def test_mismatch_pauses_slot_and_alerts_never_resumes():
    st = _state()
    st["paused_slots"] = {"other": {"paused_by": "api"}}
    fc = FC([{"symbol": "ETHUSDT", "positionAmt": "-0.5", "markPrice": "3000"},
             {"symbol": "SOLUSDT", "positionAmt": "2", "markPrice": "150"}])
    rep = reconcile.build_report(st, client=Spot({"FIL": 1.0}), fc=fc, alloc_doc={"slots": []})
    assert not rep["ok"]
    kinds = {m["symbol"]: m["kind"] for m in rep["mismatches"]}
    assert kinds == {"ETHUSDT": "futures_position_mismatch", "SOLUSDT": "untracked_exchange_position",
                     "FILUSDT": "spot_qty_short"}
    st = reconcile.apply_report(st, rep)
    assert set(st["paused_slots"]) == {"other", "s2", "c2"}
    assert st["paused_slots"]["s2"]["paused_by"] == "reconcile"
    assert st["paused"] is True and st["reconcile"]["consecutive_ok"] == 0
    assert len(st["reconcile_alerts"]) == 3
    # same mismatch again: no duplicate alerts; a later clean pass does not unpause
    st = reconcile.apply_report(st, rep)
    assert len(st["reconcile_alerts"]) == 3
    ok = reconcile.build_report({**st, "positions": {}}, client=Spot({}), fc=FC([]), alloc_doc={"slots": []})
    st = reconcile.apply_report(st, ok)
    assert "s2" in st["paused_slots"] and "c2" in st["paused_slots"]


def test_filled_stop_is_alert_only():
    st = _state()
    st["positions"].pop("s2")
    rep = reconcile.build_report(st, client=Spot({}, stop_status="FILLED"), fc=FC([]), alloc_doc={"slots": []})
    assert rep["mismatches"][0]["kind"] == "stop_filled_pending_record"
    st = reconcile.apply_report(st, rep)
    assert st["paused_slots"] == {} and st["reconcile_alerts"]


def test_binance_errors_reported_not_ok():
    class Bad(FC):
        def account(self):
            raise RuntimeError("down")
    rep = reconcile.build_report(_state(), client=None, fc=Bad([]), alloc_doc={"slots": []})
    assert not rep["ok"] and rep["errors"]


def test_total_assets_includes_futures_wallets():
    import api

    class C:
        def all_prices(self):
            return [{"symbol": "FILUSDT", "price": "2"}, {"symbol": "USDCUSDT", "price": "1.0"}]
    bals = [{"asset": "USDT", "free": "5000", "locked": "0"}, {"asset": "USDC", "free": "5000", "locked": "0"},
            {"asset": "FIL", "free": "10", "locked": "0"}]
    books = {"usdt": {"futures": {"margin_balance": 4990.0}}, "usdc": {"futures": {"margin_balance": 4997.32}}}
    t = api._total_assets(C(), bals, books)
    assert t["spot_usdt"] == 10020.0 and t["futures_usdt"] == 9987.32
    assert t["total_usdt"] == 20007.32 and t["complete"]
    books["usdt"]["futures"] = {"error": "x"}
    assert api._total_assets(C(), bals, books)["complete"] is False


def test_record_asset_month():
    import reconcile
    st = {}
    reconcile.record_asset_month(st, {"total_usdt": 100.0}, "2026-10-22T10:00:00+08:00")
    reconcile.record_asset_month(st, {"total_usdt": 110.0}, "2026-10-23T10:00:00+08:00")
    reconcile.record_asset_month(st, None, "2026-10-23T11:00:00+08:00")
    e = st["asset_monthly"]["2026-10"]
    assert e["first_total"] == 100.0 and e["last_total"] == 110.0 and e["n"] == 2
