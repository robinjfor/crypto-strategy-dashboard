"""Targeted tests for shared-slot liquidation / ADL booking. No network and no secrets."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

import exchange_close_sync as x
import netting

FIX = json.loads((Path(__file__).parent / "fixtures" / "exchange_closes" / "ake_apr_20261107.json").read_text())
AKE_CASH = 1378.7273507
APR_CASH = 5126.7016709
AKE_DELTA = 66.5698748
APR_DELTA = 9.2234


class Store:
    def __init__(self, s):
        self.s = s

    def load(self):
        return json.loads(json.dumps(self.s))

    def mutate(self, fn):
        self.s = fn(self.load())
        return self.s


class GenStore(Store):
    """Generation-precondition store: a loser retries on the winner's state."""

    def __init__(self, s):
        super().__init__(s)
        self.gen = 0
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            return json.loads(json.dumps(self.s))

    def mutate(self, fn, retries=6):
        for _ in range(retries):
            with self.lock:
                gen = self.gen
                state = json.loads(json.dumps(self.s))
            new = fn(state)
            with self.lock:
                if self.gen != gen:
                    continue
                self.s = new
                self.gen += 1
                return self.s
        raise RuntimeError("state_mutate_failed")


def _books(ake=AKE_CASH, apr=APR_CASH):
    return {
        "demo_vbo1h_exec": {
            "cash": ake, "funding_since": 111,
            "book": {"AKEUSDT": -15002.0, "ETHUSDT": 1.0},
            "targets": {"AKEUSDT": -0.02},
            "history": [{"bar": "2026-11-06T04:00Z", "tg": {"AKEUSDT": -0.02}}],
        },
        "demo_e5v_exec": {
            "cash": apr, "funding_since": 222,
            "book": {"APRUSDT": -214.0, "ETHUSDT": 0.5},
        },
        "paused_slots": {"demo_r2s_top10_3k": {"paused_by": "human"}},
    }


def _snap(symbol, qty, trades, orders, *, exch="0", income=None, open_orders=None, dual=False, truncated=False, algo=None):
    return {
        "symbol": symbol, "exchange_qty": exch, "dual_side": dual, "hedge": dual, "quote": "USDT",
        "truncated": truncated, "open_orders": [] if open_orders is None else open_orders,
        "open_algo_orders": [] if algo is None else algo,
        "trades": trades, "orders": orders, "income": income or [],
    }


def _liq(tid, oid, qty, price, coid, t, *, side="BUY", fee="0", asset="USDT", pnl="0", extra=None):
    trade = {"id": tid, "orderId": oid, "side": side, "qty": str(qty), "price": str(price),
             "commission": fee, "commissionAsset": asset, "realizedPnl": pnl, "time": t}
    order = {"orderId": oid, "clientOrderId": coid, "side": side}
    if coid.startswith("adl"):
        order["autoCloseType"] = "ADL"
    elif coid.startswith("autoclose-"):
        order["autoCloseType"] = "LIQUIDATION"
    if extra:
        trade.update(extra)
    return trade, order


def test_fixture_matches_verified_contract():
    for mid, spec in x.REPAIRS.items():
        snap = FIX[mid]
        assert snap["symbol"] == spec["symbol"] and snap["exchange_qty"] in ("0", 0)
        fill = spec["fill"]
        got = next(t for t in snap["trades"] if str(t["id"]) == fill["id"])
        assert str(got["orderId"]) == fill["orderId"]
        assert got["side"] == fill["side"] and got["qty"] == fill["qty"] and got["price"] == fill["price"]
        assert got["realizedPnl"] == fill["realizedPnl"] and str(got["commission"]) == fill["commission"]
        order = next(o for o in snap["orders"] if str(o["orderId"]) == fill["orderId"])
        assert order["clientOrderId"] == fill["clientOrderId"]
    ake_entries = [t for t in FIX["ake_liq_1106"]["trades"] if str(t["id"]) in x.REPAIRS["ake_liq_1106"]["entry_trade_ids"]]
    assert sum(x.signed_qty(t) for t in ake_entries) == x.dec("-15002")
    ins = [r for r in FIX["apr_adl_1106"]["income"] if r["incomeType"] == "INSURANCE_CLEAR"]
    assert ins == [{"symbol": "APRUSDT", "incomeType": "INSURANCE_CLEAR", "income": "-0.3589926", "asset": "USDT", "time": 1793030400000, "tranId": 9001}]


def test_diagnosis_net_reconcile_reports_and_legacy_close_ignores_virtual_books():
    from execution import reconcile_exchange_closes
    st = {"demo_vbo1h_exec": {"cash": 5.0, "book": {"AKEUSDT": -15002.0}}, "positions": {}}
    rep = netting.net_reconcile(st, {"AKEUSDT": 0.0})
    assert rep["ok"] is False and rep["symbols"]["AKEUSDT"]["diff"] == 15002.0
    assert st["demo_vbo1h_exec"]["cash"] == 5.0 and st["demo_vbo1h_exec"]["book"]["AKEUSDT"] == -15002.0
    assert reconcile_exchange_closes(None, st) == []
    assert st["demo_vbo1h_exec"]["book"]["AKEUSDT"] == -15002.0


def test_opposing_legs_exchange_flat_is_not_a_missing_leg():
    e5, r2s = "demo_e5_min5_x175", "demo_r2s_top10_3k"
    st = {"demo_e5_exec": {"cash": 10.0, "book": {"SOLUSDT": 3.0}},
          "demo_r2s_exec": {"cash": 10.0, "book": {"SOLUSDT": -3.0}}}
    assert netting.own_leg_on_exchange(st, e5, "SOLUSDT", 0.0) == 3.0
    assert netting.leg_missing(st, e5, "SOLUSDT", 0.0, 3.0) is False
    assert netting.leg_missing(st, r2s, "SOLUSDT", 0.0, -3.0) is False
    trade, order = _liq(1, 2, 3, 10, "autoclose-1", 100)
    plan = x.plan_sync(st, _snap("SOLUSDT", 3, [trade], [order]), at="t")
    assert plan["status"] == "in_sync"
    assert st["demo_e5_exec"]["book"]["SOLUSDT"] == 3.0 and st["demo_r2s_exec"]["cash"] == 10.0
    assert netting.leg_missing(st, e5, "SOLUSDT", 0.0, 3.0) is False


def test_single_owner_liquidation_and_replay_and_funding_not_booked():
    st = {"demo_vbo1h_exec": {"cash": 100.0, "funding_since": 5, "book": {"ZZUSDT": -10.0, "ETHUSDT": 2.0}}}
    trade, order = _liq(7, 8, 10, 2, "autoclose-9", 1_000_000, fee="0.05", pnl="-3")
    snap = _snap("ZZUSDT", 10, [trade, dict(trade)], [order], income=[
        {"incomeType": "FUNDING_FEE", "income": "5", "asset": "USDT", "time": 1_000_000, "tranId": 1},
        {"incomeType": "INSURANCE_CLEAR", "income": "-0.4", "asset": "USDT", "time": 1_000_000, "tranId": 2},
        {"incomeType": "INSURANCE_CLEAR", "income": "-0.4", "asset": "USDT", "time": 1_000_000, "tranId": 2},
    ])
    plan = x.plan_sync(st, snap, at="t")
    assert plan["status"] == "book" and plan["cause"] == "liquidation"
    assert plan["realized_pnl"] == -3.0
    assert plan["legs"][0]["cash_delta"] == pytest.approx(-20.45)  # -10*2 - 0.05 - 0.4; funding 5 excluded; insurance once
    x.apply_plan(st, plan)
    ex = st["demo_vbo1h_exec"]
    assert "ZZUSDT" not in ex["book"] and ex["book"]["ETHUSDT"] == 2.0
    assert ex["cash"] == pytest.approx(79.55) and ex["funding_since"] == 5
    assert ex["forced_flat"]["ZZUSDT"]["cause"] == "liquidation"
    assert st["ledger_adjustments"][-1]["actor"] == x.SYNC_ACTOR
    assert st["ledger_adjustments"][-1]["funding_booked"] is False
    again = x.apply_plan(st, x.plan_sync(st, snap, at="t2"))
    assert again["status"] == "in_sync" and ex["cash"] == pytest.approx(79.55)
    assert "自動交易專家" not in json.dumps(st)


def test_adl_partial_and_full_wipe_of_same_side_only():
    st = {"demo_e5v_exec": {"cash": 50.0, "book": {"ZZUSDT": -100.0}}}
    trade, order = _liq(1, 2, 60, 1, "adl_autoclose", 5000, pnl="1.5")
    plan = x.plan_sync(st, _snap("ZZUSDT", 60, [trade], [order], exch="-40"), at="t")
    x.apply_plan(st, plan)
    assert st["demo_e5v_exec"]["book"]["ZZUSDT"] == pytest.approx(-40.0)
    assert st["demo_e5v_exec"]["cash"] == pytest.approx(-10.0)  # 50 - 60*1
    assert plan["realized_pnl"] == 1.5 and st["demo_e5v_exec"]["cash"] != pytest.approx(-8.5)

    st2 = {
        "demo_e5_exec": {"cash": 100.0, "book": {"MUSDT": -4.0}},
        "demo_r2s_exec": {"cash": 80.0, "book": {"MUSDT": -6.0}},
        "demo_vbo1h_exec": {"cash": 70.0, "book": {"MUSDT": 1.0}},
    }
    tr, od = _liq(3, 4, 10, 2, "autoclose-2", 9000)
    plan = x.plan_sync(st2, _snap("MUSDT", 10, [tr], [od], exch="1"), at="t")
    assert plan["status"] == "book"
    x.apply_plan(st2, plan)
    assert "MUSDT" not in st2["demo_e5_exec"]["book"] and "MUSDT" not in st2["demo_r2s_exec"]["book"]
    assert st2["demo_vbo1h_exec"]["book"]["MUSDT"] == 1.0 and st2["demo_vbo1h_exec"]["cash"] == 70.0
    assert st2["demo_e5_exec"]["cash"] == pytest.approx(92.0)   # 100 - 4*2
    assert st2["demo_r2s_exec"]["cash"] == pytest.approx(68.0)  # 80 - 6*2
    assert netting.virtual_net(st2)["MUSDT"] == pytest.approx(1.0)


def test_ambiguous_partial_alerts_without_pause_or_touching_other_slots():
    st = {"demo_e5_exec": {"cash": 10.0, "book": {"ZZUSDT": -5.0, "ETHUSDT": 1.0}},
          "demo_r2s_exec": {"cash": 9.0, "book": {"ZZUSDT": -5.0}},
          "paused_slots": {"demo_vbo1h_top50_1k": {"paused_by": "human"}}}
    tr, od = _liq(1, 2, 3, 1, "autoclose-1", 100)
    plan = x.plan_sync(st, _snap("ZZUSDT", 3, [tr], [od], exch="-7"), at="t")
    assert plan["status"] == "ambiguous" and plan["pause"] is False
    x.apply_plan(st, plan)
    assert st["demo_e5_exec"]["cash"] == 10.0 and st["demo_e5_exec"]["book"]["ETHUSDT"] == 1.0
    assert st["demo_r2s_exec"]["book"]["ZZUSDT"] == -5.0
    assert st["paused_slots"]["demo_vbo1h_top50_1k"]["paused_by"] == "human"
    alert = st["reconcile_alerts"][-1]
    assert alert["kind"] == "external_close_ambiguous" and alert["pause"] is False
    x.apply_plan(st, plan)
    assert len(st["reconcile_alerts"]) == 1


def test_unknown_outside_order_gap_open_orders_hedge_fee_and_adl_insurance():
    st = {"demo_vbo1h_exec": {"cash": 3.0, "book": {"ZZUSDT": -10.0}}}

    def run(snap):
        s = copy.deepcopy(st)
        plan = x.plan_sync(s, snap, at="t")
        x.apply_plan(s, plan)
        assert s["demo_vbo1h_exec"]["cash"] == 3.0 and s["demo_vbo1h_exec"]["book"]["ZZUSDT"] == -10.0
        assert plan["pause"] is False
        return plan["status"]

    outside, od = _liq(1, 2, 10, 1, "web_user", 100)
    assert run(_snap("ZZUSDT", 10, [outside], [od])) == "unknown_outside"
    gap_trade = {"id": 3, "orderId": 4, "side": "BUY", "qty": "10", "price": "1", "commission": "0", "time": 100}
    assert run(_snap("ZZUSDT", 10, [gap_trade], [])) == "order_history_gap"
    tr, od = _liq(1, 2, 10, 1, "autoclose-1", 100)
    assert run(_snap("ZZUSDT", 10, [tr], [od], open_orders=[{"orderId": 9}])) == "open_orders"
    assert run(_snap("ZZUSDT", 10, [tr], [od], algo=[{"algoId": 1}])) == "open_orders"
    assert run(_snap("ZZUSDT", 10, [tr], [od], dual=True)) == "hedge"
    bnb, od = _liq(1, 2, 10, 1, "autoclose-1", 100, fee="0.01", asset="BNB")
    assert run(_snap("ZZUSDT", 10, [bnb], [od])) == "fee_asset"
    ins = [{"incomeType": "INSURANCE_CLEAR", "income": "-1", "asset": "USDT", "time": 100, "tranId": 5}]
    adl, od = _liq(1, 2, 10, 1, "adl_autoclose", 100)
    assert run(_snap("ZZUSDT", 10, [adl], [od], income=ins)) == "adl_insurance"
    two = [{"incomeType": "INSURANCE_CLEAR", "income": "-0.1", "asset": "USDT", "time": 100, "tranId": 1},
           {"incomeType": "INSURANCE_CLEAR", "income": "-0.2", "asset": "USDT", "time": 100, "tranId": 2}]
    liq, od = _liq(1, 2, 10, 1, "autoclose-1", 100)
    assert run(_snap("ZZUSDT", 10, [liq], [od], income=two)) == "insurance_ambiguous"
    small, o1 = _liq(1, 2, 30, 1, "autoclose-1", 300)
    big, o2 = _liq(3, 4, 60, 1, "autoclose-2", 100)
    s = {"demo_vbo1h_exec": {"cash": 3.0, "book": {"ZZUSDT": -60.0}}}
    plan = x.plan_sync(s, _snap("ZZUSDT", 60, [small, big], [o1, o2]), at="t")
    assert plan["status"] == "ambiguous" and plan["pause"] is False
    x.apply_plan(s, plan)
    assert s["demo_vbo1h_exec"]["cash"] == 3.0 and s["demo_vbo1h_exec"]["book"]["ZZUSDT"] == -60.0


def test_newest_exact_fill_is_not_mixed_with_an_older_close_or_a_newer_own_fill():
    st = {"demo_vbo1h_exec": {"cash": 0.0, "book": {"ZZUSDT": -10.0}}}
    old, o1 = _liq(1, 2, 10, 1, "autoclose-old", 100)
    new, o2 = _liq(3, 4, 10, 3, "autoclose-new", 500)
    plan = x.plan_sync(st, _snap("ZZUSDT", 10, [old, new], [o1, o2]), at="t")
    assert plan["status"] == "book" and plan["trade_ids"] == ["3"] and plan["vwap"] == 3.0
    x.apply_plan(st, plan)
    assert st["demo_vbo1h_exec"]["cash"] == pytest.approx(-30.0)
    st2 = {"demo_vbo1h_exec": {"cash": 5.0, "book": {"ZZUSDT": -10.0}}}
    liq, o1 = _liq(1, 2, 10, 2, "autoclose-1", 100)
    own = {"id": 8, "orderId": 9, "side": "BUY", "qty": "1", "price": "2", "commission": "0",
           "commissionAsset": "USDT", "realizedPnl": "0", "time": 200}
    plan = x.plan_sync(st2, _snap("ZZUSDT", 10, [liq, own], [o1, {"orderId": 9, "clientOrderId": "net-ZZUSDT-1"}]), at="t")
    assert plan["status"] == "mixed_new_fills"
    x.apply_plan(st2, plan)
    assert st2["demo_vbo1h_exec"]["cash"] == 5.0 and st2["demo_vbo1h_exec"]["book"]["ZZUSDT"] == -10.0
    later = dict(own, time=1793940450000)
    dirty = copy.deepcopy(FIX["ake_liq_1106"])
    dirty["trades"] = dirty["trades"] + [later]
    dirty["orders"] = dirty["orders"] + [{"orderId": 9, "clientOrderId": "net-AKEUSDT-1"}]
    blocked = x.commit_repair(_books(), dirty, x.REPAIRS["ake_liq_1106"], "ake_liq_1106", at="t")
    assert blocked["skipped"] == "mixed_new_fills"


def test_same_order_partials_and_direct_order_lookup_fill_a_history_gap():
    st = {"demo_e5v_exec": {"cash": 0.0, "book": {"ZZUSDT": -10.0}}}
    a = {"id": 1, "orderId": 9, "side": "BUY", "qty": "4", "price": "2", "commission": "0.01", "commissionAsset": "USDT", "realizedPnl": "0", "time": 10}
    b = {"id": 2, "orderId": 9, "side": "BUY", "qty": "6", "price": "2", "commission": "0.02", "commissionAsset": "USDT", "realizedPnl": "0", "time": 11}
    order = {"orderId": 9, "clientOrderId": "autoclose-9"}
    plan = x.plan_sync(st, _snap("ZZUSDT", 10, [a, b], [order]), at="t")
    assert plan["status"] == "book" and plan["legs"][0]["cash_delta"] == pytest.approx(-20.03)
    x.apply_plan(st, plan)
    assert "ZZUSDT" not in st["demo_e5v_exec"]["book"]


def test_increase_is_not_booked_and_fa_ledger_is_not_guessed():
    st = {"demo_vbo1h_exec": {"cash": 1.0, "book": {"ZZUSDT": -10.0}}}
    tr, od = _liq(1, 2, 4, 1, "autoclose-1", 10, side="SELL")
    plan = x.plan_sync(st, _snap("ZZUSDT", 4, [tr], [od], exch="-14"), at="t")
    assert plan["status"] == "not_a_close"
    x.apply_plan(st, plan)
    assert st["demo_vbo1h_exec"]["book"]["ZZUSDT"] == -10.0
    fa = {"demo_fa_positions": {"ZZUSDT": {"perp_qty": 10, "spot_qty": 10}}}
    tr, od = _liq(1, 2, 10, 1, "autoclose-1", 10)
    plan = x.plan_sync(fa, _snap("ZZUSDT", 10, [tr], [od]), at="t")
    assert plan["status"] == "unsupported_ledger"
    x.apply_plan(fa, plan)
    assert fa["demo_fa_positions"]["ZZUSDT"]["perp_qty"] == 10


def test_no_evidence_leaves_the_stale_leg():
    st = {"demo_vbo1h_exec": {"cash": 4.0, "book": {"ZZUSDT": -10.0}}}
    plan = x.plan_sync(st, _snap("ZZUSDT", 10, [], []), at="t")
    assert plan["status"] == "no_evidence"
    x.apply_plan(st, plan)
    assert st["demo_vbo1h_exec"]["book"]["ZZUSDT"] == -10.0
    assert st["reconcile_alerts"][-1]["pause"] is False


def test_ake_apr_repair_is_idempotent_delta_and_robin():
    store = Store(_books())
    results = x.run_repairs(store, snapshots=FIX, at="2026-11-07T07:22:00+00:00")
    assert {r["id"] for r in results if r.get("applied")} == {"ake_liq_1106", "apr_adl_1106"}
    vbo, e5 = store.s["demo_vbo1h_exec"], store.s["demo_e5v_exec"]
    assert "AKEUSDT" not in vbo["book"] and vbo["book"]["ETHUSDT"] == 1.0
    assert vbo["cash"] == pytest.approx(AKE_CASH - AKE_DELTA) and vbo["funding_since"] == 111
    assert vbo["forced_flat"]["AKEUSDT"]["target"] == -0.02
    assert "APRUSDT" not in e5["book"] and e5["book"]["ETHUSDT"] == 0.5
    assert e5["cash"] == pytest.approx(APR_CASH - APR_DELTA) and e5["funding_since"] == 222
    blob = json.dumps(store.s)
    assert "自動交易專家" not in blob
    for mid in ("ake_liq_1106", "apr_adl_1106"):
        rec = store.s["migrations"][mid]
        assert rec["actor"] == "Robin" and "15:22" in rec["authorization"] and rec["orders"] == 0
        assert rec["funding_booked"] is False and rec["realized_pnl_not_booked"]
    # realized PnL is not piled on the buyback, and the Oct 27 insurance is not in the APR delta
    assert e5["cash"] != pytest.approx(APR_CASH - APR_DELTA + 1.81258)
    assert e5["cash"] != pytest.approx(APR_CASH - APR_DELTA - 0.3589926)
    again = x.run_repairs(store, snapshots=FIX, at="later")
    assert all(r.get("skipped") == "done" for r in again)
    assert vbo["cash"] == pytest.approx(AKE_CASH - AKE_DELTA)
    assert store.s["paused_slots"]["demo_r2s_top10_3k"]["paused_by"] == "human"
    assert any(e["actor"] == "Robin" and e["action"] == "external_close_booked" for e in store.s["control_log"])


def test_repair_uses_delta_not_a_hardcoded_cash_balance():
    store = Store(_books(ake=10.0, apr=4.0))
    x.run_repairs(store, snapshots=FIX, at="t")
    assert store.s["demo_vbo1h_exec"]["cash"] == pytest.approx(10.0 - AKE_DELTA)
    assert store.s["demo_e5v_exec"]["cash"] == pytest.approx(4.0 - APR_DELTA)


def test_repair_preconditions_fail_closed_then_a_later_good_snapshot_applies():
    store = Store(_books())
    bad = copy.deepcopy(FIX)
    bad["ake_liq_1106"] = {**bad["ake_liq_1106"], "exchange_qty": "1"}
    bad["apr_adl_1106"] = {**bad["apr_adl_1106"], "open_orders": [{"orderId": 1}]}
    out = x.run_repairs(store, snapshots=bad, at="t")
    assert {r["skipped"] for r in out} == {"exchange_qty", "open_orders"}
    assert "migrations" not in store.s or "ake_liq_1106" not in store.s.get("migrations", {})
    assert store.s["demo_vbo1h_exec"]["cash"] == AKE_CASH
    mismatched = copy.deepcopy(FIX)
    mismatched["ake_liq_1106"]["trades"] = [dict(t, price="0.01") if str(t["id"]) == "87896577" else t
                                            for t in mismatched["ake_liq_1106"]["trades"]]
    one = x.commit_repair(store.s, mismatched["ake_liq_1106"], x.REPAIRS["ake_liq_1106"], "ake_liq_1106", at="t")
    assert one["skipped"] == "fill_mismatch" and store.s["demo_vbo1h_exec"]["book"]["AKEUSDT"] == -15002.0
    moved = copy.deepcopy(_books())
    moved["demo_e5v_exec"]["book"]["APRUSDT"] = -100.0
    apr = x.commit_repair(moved, FIX["apr_adl_1106"], x.REPAIRS["apr_adl_1106"], "apr_adl_1106", at="t")
    assert apr["skipped"] == "ledger_qty" and moved["demo_e5v_exec"]["cash"] == APR_CASH
    x.run_repairs(store, snapshots=FIX, at="t2")
    assert "AKEUSDT" not in store.s["demo_vbo1h_exec"]["book"]


def test_entry_window_and_sum_and_extra_forced_fill():
    st = _books()
    missing = copy.deepcopy(FIX["ake_liq_1106"])
    missing["trades"] = [t for t in missing["trades"] if str(t["id"]) == "87896577"]
    r = x.commit_repair(st, missing, x.REPAIRS["ake_liq_1106"], "ake_liq_1106", at="t")
    assert r["applied"] and r["entry_check"] == "not_in_fresh_window"
    assert st["demo_vbo1h_exec"]["book"].get("AKEUSDT") is None
    st2 = _books()
    wrong = copy.deepcopy(FIX["ake_liq_1106"])
    for t in wrong["trades"]:
        if str(t["id"]) == "87156866":
            t["qty"] = "1"
    r = x.commit_repair(st2, wrong, x.REPAIRS["ake_liq_1106"], "ake_liq_1106", at="t")
    assert r["skipped"] == "entry_sum_mismatch" and st2["demo_vbo1h_exec"]["cash"] == AKE_CASH
    st3 = _books()
    bad_entry = copy.deepcopy(FIX["apr_adl_1106"])
    for t in bad_entry["trades"]:
        if str(t["id"]) == "71809638":
            t["price"] = "0.1"
    r = x.commit_repair(st3, bad_entry, x.REPAIRS["apr_adl_1106"], "apr_adl_1106", at="t")
    assert r["skipped"] == "entry_mismatch" and "APRUSDT" in st3["demo_e5v_exec"]["book"]
    st4 = _books()
    extra = copy.deepcopy(FIX["apr_adl_1106"])
    extra["trades"] = extra["trades"] + [{"id": 99, "orderId": 100, "side": "BUY", "qty": "1", "price": "0.04",
                                          "commission": "0", "commissionAsset": "USDT", "realizedPnl": "0", "time": 1793955600000,
                                          "clientOrderId": "adl_autoclose"}]
    extra["orders"] = extra["orders"] + [{"orderId": 100, "clientOrderId": "adl_autoclose", "autoCloseType": "ADL"}]
    r = x.commit_repair(st4, extra, x.REPAIRS["apr_adl_1106"], "apr_adl_1106", at="t")
    assert r["skipped"] == "extra_forced_fill" and st4["demo_e5v_exec"]["cash"] == APR_CASH


def test_repair_does_not_run_when_another_slot_also_holds_the_symbol():
    st = _books()
    st["demo_r2s_exec"] = {"cash": 1.0, "book": {"AKEUSDT": -1.0}}
    r = x.commit_repair(st, FIX["ake_liq_1106"], x.REPAIRS["ake_liq_1106"], "ake_liq_1106", at="t")
    assert r["skipped"] == "not_sole_holder"
    assert st["demo_vbo1h_exec"]["book"]["AKEUSDT"] == -15002.0
    assert st["reconcile_alerts"][-1]["pause"] is False


def test_sync_defers_pending_repair_symbols_and_sends_no_orders():
    class Boom:
        def new_order(self, **k):
            raise AssertionError("order")

    store = Store(_books())
    out = x.run_sync(store, Boom(), snapshots={"AKEUSDT": FIX["ake_liq_1106"], "ZZUSDT": _snap("ZZUSDT", 1, [], [])}, at="t")
    assert out[0]["status"] == "deferred_repair" or any(r["status"] == "deferred_repair" for r in out)
    assert store.s["demo_vbo1h_exec"]["book"]["AKEUSDT"] == -15002.0
    x.run_repairs(store, Boom(), snapshots=FIX, at="t")
    assert "AKEUSDT" not in store.s["demo_vbo1h_exec"]["book"]
    again = x.run_sync(store, Boom(), snapshots={"AKEUSDT": FIX["ake_liq_1106"]}, at="t2")
    assert again[0]["status"] == "in_sync"
    assert store.s["demo_vbo1h_exec"]["cash"] == pytest.approx(AKE_CASH - AKE_DELTA)


def test_rollback_keeps_later_funding_and_does_not_rebook():
    st = {"demo_vbo1h_exec": {"cash": 100.0, "funding_since": 1, "book": {"ZZUSDT": -10.0}}}
    tr, od = _liq(7, 8, 10, 2, "autoclose-9", 1000, fee="0")
    snap = _snap("ZZUSDT", 10, [tr], [od])
    plan = x.plan_sync(st, snap, at="t")
    x.apply_plan(st, plan)
    st["demo_vbo1h_exec"]["cash"] = st["demo_vbo1h_exec"]["cash"] + 7.0  # funding booked by the slot afterwards
    funded = st["demo_vbo1h_exec"]["cash"]
    assert x.rollback_booking(st, plan["booking_id"], actor="Robin", at="t2")["ok"]
    ex = st["demo_vbo1h_exec"]
    assert ex["book"]["ZZUSDT"] == -10.0
    assert ex["cash"] == pytest.approx(funded + 20.0)  # buyback reversed, funding kept
    assert ex["funding_since"] == 1
    assert "forced_flat" not in ex
    assert x.rollback_booking(st, plan["booking_id"], actor="Robin")["skipped"] == "already_rolled_back"
    ex["book"]["ZZUSDT"] = 5.0
    st["external_closes"][plan["booking_id"]]["rolled_back"] = False
    refused = x.rollback_booking(st, plan["booking_id"], actor="Robin")
    assert refused["reason"] == "qty_moved" and ex["book"]["ZZUSDT"] == 5.0
    st["demo_vbo1h_exec"]["book"]["ZZUSDT"] = -10.0
    st["external_closes"][plan["booking_id"]]["rolled_back"] = True
    replay = x.plan_sync(st, snap, at="t3")
    assert replay["status"] == "already_booked"
    cash = ex["cash"]
    x.apply_plan(st, replay)
    assert ex["cash"] == cash and ex["book"]["ZZUSDT"] == -10.0
    assert st["reconcile_alerts"][-1]["pause"] is False


def test_concurrent_repairs_apply_each_delta_once():
    store = GenStore(_books())
    errs = []

    def run():
        try:
            x.run_repairs(store, snapshots=FIX, at="t")
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert store.s["demo_vbo1h_exec"]["cash"] == pytest.approx(AKE_CASH - AKE_DELTA)
    assert store.s["demo_e5v_exec"]["cash"] == pytest.approx(APR_CASH - APR_DELTA)
    assert "AKEUSDT" not in store.s["demo_vbo1h_exec"]["book"]
    assert "APRUSDT" not in store.s["demo_e5v_exec"]["book"]
    assert list(store.s["booked_external_trades"]) == ["87896577", "73620325"] or set(store.s["booked_external_trades"]) == {"87896577", "73620325"}


def test_fetch_is_read_only_and_resolves_orders_all_orders_missed():
    class Fut:
        def __init__(self):
            self.sent = []

        def new_order(self, **k):
            self.sent.append(k)
            raise AssertionError("order")

        def position_mode_dual(self):
            return False

        def position_risk(self, symbol=None):
            return [{"symbol": symbol or "ZZUSDT", "positionAmt": "0", "positionSide": "BOTH"}]

        def open_orders(self, symbol=None):
            return []

        def open_algo_orders(self, symbol=None):
            return []

        def all_orders(self, symbol, start_ms=None, limit=1000):
            return []

        def income(self, *a, **k):
            return []

        def get_order_by_id(self, symbol, oid):
            if str(oid) == "9":
                return {"orderId": 9, "clientOrderId": "autoclose-9", "side": "BUY"}
            return None

        def _signed(self, method, path, params=None):
            assert method == "GET"
            if path.endswith("forceOrders"):
                return []
            if path.endswith("userTrades"):
                if params["startTime"] > 10:
                    return []
                return [{"id": 1, "orderId": 9, "side": "BUY", "qty": "4", "price": "2", "commission": "0",
                         "commissionAsset": "USDT", "realizedPnl": "0", "time": 10}]
            raise AssertionError(path)

    fut = Fut()
    snap = x.fetch_symbol(fut, "ZZUSDT", start_ms=0, end_ms=10)
    assert not fut.sent and snap["truncated"] is False
    assert snap["orders"][0]["clientOrderId"] == "autoclose-9"
    st = {"demo_vbo1h_exec": {"cash": 0.0, "book": {"ZZUSDT": -4.0}}}
    plan = x.plan_sync(st, snap, at="t")
    assert plan["status"] == "book"
    fut.get_order_by_id = lambda s, o: None
    missed = x.fetch_symbol(fut, "ZZUSDT", start_ms=0, end_ms=10)
    assert x.plan_sync(st, missed, at="t")["status"] == "order_history_gap"
    wide = x.fetch_symbol(fut, "ZZUSDT", start_ms=0, end_ms=0)
    # 1000-row window is a gap, not a guessed close
    fut._signed = lambda *a, **k: [{"id": i, "orderId": i, "side": "BUY", "qty": "1", "price": "1", "commission": "0", "time": 1} for i in range(1000)] if a[1].endswith("userTrades") else []
    truncated = x.fetch_symbol(fut, "ZZUSDT", start_ms=0, end_ms=0)
    assert truncated["truncated"] is True
    assert x.plan_sync(st, truncated, at="t")["status"] == "truncated"
    assert not fut.sent


def test_release_forced_flat_sends_nothing_and_clears_only_the_marker():
    st = {"demo_vbo1h_exec": {"cash": 12.0, "book": {"ETHUSDT": 1.0}, "forced_flat": {"AKEUSDT": {"target": -0.02, "cause": "liquidation"}}}}
    assert x.release_forced_flat(st, "demo_vbo1h_top50_1k", "AKEUSDT", actor="Robin", reason="parent allows this weight", at="t")["released"]
    assert st["demo_vbo1h_exec"]["cash"] == 12.0 and st["demo_vbo1h_exec"]["book"] == {"ETHUSDT": 1.0}
    assert "forced_flat" not in st["demo_vbo1h_exec"]
    assert st["ledger_adjustments"][-1]["orders"] == 0 and st["ledger_adjustments"][-1]["actor"] == "Robin"
    assert x.release_forced_flat(st, "demo_vbo1h_top50_1k", "AKEUSDT", actor="Robin", reason="again")["skipped"] == "not_set"


def test_vbo_does_not_reopen_until_the_target_changes(monkeypatch):
    import demo_vbo1h_exec as vx
    from tests.test_demo_vbo1h import A, B, Fut, _patch, _st, NOW
    monkeypatch.setenv("DEMO_ACCEPTED", "demo_vbo1h_top50_1k")
    _patch(monkeypatch, {A: 0.1, B: -0.1})
    st = _st(**{"demo_vbo1h_exec": {"cash": 100.0, "book": {}, "targets": {A: 0.05},
                                    "forced_flat": {A: {"target": 0.05, "cause": "liquidation"}}}})
    f = Fut([A, B])
    r = vx.execute(st, f, NOW, panel_fn=lambda: {})
    assert f.orders and {o["symbol"] for o in f.orders} == {B}
    assert A not in st["demo_vbo1h_exec"]["book"]
    assert any(A in s and "forced flat" in s for s in r["skipped"])
    assert st["demo_vbo1h_exec"]["forced_flat"][A]["target"] == 0.05

    _patch(monkeypatch, {A: -0.1})
    st2 = _st(**{"demo_vbo1h_exec": {"cash": 100.0, "book": {}, "targets": {A: 0.05},
                                     "forced_flat": {A: {"target": 0.05, "cause": "ADL"}}}})
    f2 = Fut([A, B])
    vx.execute(st2, f2, NOW, panel_fn=lambda: {})
    assert {o["symbol"] for o in f2.orders} == {A} and f2.orders[0]["side"] == "SELL"
    assert "forced_flat" not in st2["demo_vbo1h_exec"]


def test_e5_forced_flat_skips_only_that_symbol(monkeypatch):
    import demo_e5_exec as ex
    from tests.test_demo_e5_exec import NOW, Fut, PX, _state, _tgt
    monkeypatch.setenv("DEMO_ACCEPTED", "demo_e5_min5_x175")
    monkeypatch.setattr(ex, "entry_block", lambda s, *a: None)
    st = _state()
    st["demo_e5_exec"] = {"cash": 5000.0, "book": {}, "forced_flat": {"SOLUSDT": {"cause": "liquidation", "target": None}}}
    f = Fut(PX)
    out = ex.execute(st, f, _tgt(), NOW)
    assert "SOLUSDT" not in {o["symbol"] for o in f.orders}
    assert {o["symbol"] for o in f.orders} == {"BTCUSDT", "ETHUSDT"}
    assert any("SOLUSDT" in s and "forced flat" in s for s in out["skipped"])
    assert st["demo_e5_exec"]["forced_flat"]["SOLUSDT"]["target"] == pytest.approx(0.5)
    assert "SOLUSDT" not in st["demo_e5_exec"]["book"]
