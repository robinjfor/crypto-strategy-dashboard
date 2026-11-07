"""The production reconcile entry points do not receive a futures client.

main.cmd_run calls ``reconcile.run_once(store, client=client)``. The 15-minute
job calls ``run_once()``. Both used to leave fc=None, and the repair then
returned [] without writing. These tests drive those calls with a mocked
transport and no network.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import exchange_close_sync as x
import futures_client
import main
import migrate_e5v as M
import reconcile
from tests.test_exchange_close_sync import AKE_CASH, AKE_DELTA, APR_CASH, APR_DELTA, FIX

TPE = timezone(timedelta(hours=8))


class MemStore:
    def __init__(self, s):
        self.s = s
        self.feeds = {}

    def load(self):
        return json.loads(json.dumps(self.s))

    def mutate(self, fn, retries=5):
        self.s = fn(self.load())
        return self.s

    def save_feed(self, feed, object_name="trader/dashboard_feed.json"):
        self.feeds[object_name] = feed
        return "mem"

    def load_feed(self, object_name="trader/dashboard_feed.json"):
        return self.feeds.get(object_name)


class Spot:
    api_key = "spot-test"
    api_secret = "spot-test"

    def nonzero_balances(self):
        return []

    def my_trades_from(self, *a, **k):
        return []

    def get_order(self, *a, **k):
        return {}


def _snap(symbol):
    mid = {"AKEUSDT": "ake_liq_1106", "APRUSDT": "apr_adl_1106"}[symbol]
    return FIX[mid]


class Transport:
    """In-memory demo futures transport. Read methods only; orders raise."""

    n = 0
    sent = []

    def __init__(self):
        Transport.n += 1
        self.api_key = "test-demo-key"
        self.api_secret = "test-demo-secret"
        self.account_position_reads = 0

    def new_order(self, **kwargs):
        Transport.sent.append(kwargs)
        raise AssertionError("production repair must not place an order")

    def cancel_all(self, *a, **k):
        raise AssertionError("cancel")

    def set_leverage(self, *a, **k):
        raise AssertionError("set_leverage")

    def set_margin_type(self, *a, **k):
        raise AssertionError("set_margin_type")

    def position_mode_dual(self):
        return False

    def position_risk(self, symbol=None):
        if symbol is None:
            self.account_position_reads += 1
            return [{"symbol": s, "positionAmt": "0", "positionSide": "BOTH"} for s in ("AKEUSDT", "APRUSDT")]
        return [{"symbol": symbol, "positionAmt": "0", "positionSide": "BOTH"}]

    def open_orders(self, symbol=None):
        return []

    def open_algo_orders(self, symbol=None):
        return []

    def all_orders(self, symbol, start_ms=None, limit=1000):
        return list(_snap(symbol)["orders"]) if symbol in ("AKEUSDT", "APRUSDT") else []

    def income(self, symbol, income_type, start_ms, end_ms=None):
        if income_type != "INSURANCE_CLEAR" or symbol not in ("AKEUSDT", "APRUSDT"):
            return []
        return [r for r in _snap(symbol)["income"] if r.get("incomeType") == "INSURANCE_CLEAR"]

    def user_trades_from(self, symbol, from_id=0, limit=1000):
        return []

    def get_order_by_id(self, symbol, order_id):
        for o in self.all_orders(symbol):
            if str(o.get("orderId")) == str(order_id):
                return o
        return None

    def account(self):
        row = {"walletBalance": "0", "unrealizedProfit": "0", "marginBalance": "0",
               "availableBalance": "0", "maintMargin": "0", "initialMargin": "0"}
        return {"assets": [{"asset": "USDT", **row}, {"asset": "USDC", **row}], "positions": []}

    def _signed(self, method, path, params=None):
        assert method == "GET", method
        params = params or {}
        sym = params.get("symbol")
        if path.endswith("/positionSide/dual"):
            return {"dualSidePosition": False}
        if path.endswith("forceOrders"):
            return []
        if path.endswith("userTrades"):
            return list(_snap(sym)["trades"]) if sym in ("AKEUSDT", "APRUSDT") else []
        raise AssertionError(path)


class Keyless:
    n = 0

    def __init__(self):
        Keyless.n += 1
        self.api_key = ""
        self.api_secret = ""


def _state():
    mig = {M.MIG: {"done": True}}
    for key in list(M.DUP) + list(M.LEV2) + list(M.LATE) + list(M.FIXES):
        mig[key] = {"done": True}
    today = datetime.now(TPE).strftime("%Y-%m-%d")
    return {
        "migrations": mig,
        "paused_slots": {"demo_r2s_top10_3k": {"paused_by": "human"}},
        "positions": {},
        "closed_trades": [],
        "asset_daily": {"built_day": today, "points": []},
        "demo_vbo1h_exec": {
            "cash": AKE_CASH, "funding_since": 111,
            "book": {"AKEUSDT": -15002.0},
            "targets": {"AKEUSDT": -0.02},
            "history": [{"bar": "2026-11-06T04:00Z", "tg": {"AKEUSDT": -0.02}}],
        },
        "demo_e5v_exec": {"cash": APR_CASH, "funding_since": 222, "book": {"APRUSDT": -214.0}},
    }


def _block_network(monkeypatch):
    def _net(*a, **k):
        raise AssertionError("network disabled")

    monkeypatch.setattr(requests, "request", _net)
    monkeypatch.setattr(requests, "get", _net)
    monkeypatch.setattr(requests.sessions.Session, "request", _net)
    monkeypatch.delenv("BINANCE_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_DEMO_API_SECRET", raising=False)
    monkeypatch.delenv("GCS_BUCKET", raising=False)


def _spy(monkeypatch):
    seen = []
    real_repairs, real_sync = x.run_repairs, x.run_sync

    def repairs(store, fut=None, snapshots=None, **kw):
        seen.append(("repairs", fut, snapshots))
        return real_repairs(store, fut, snapshots, **kw)

    def sync(store, fut=None, snapshots=None, **kw):
        seen.append(("sync", fut, snapshots))
        return real_sync(store, fut, snapshots, **kw)

    monkeypatch.setattr(x, "run_repairs", repairs)
    monkeypatch.setattr(x, "run_sync", sync)
    return seen


def _assert_booked(store):
    vbo, e5 = store.s["demo_vbo1h_exec"], store.s["demo_e5v_exec"]
    assert "AKEUSDT" not in vbo["book"] and vbo["cash"] == pytest.approx(AKE_CASH - AKE_DELTA)
    assert vbo["funding_since"] == 111 and vbo["forced_flat"]["AKEUSDT"]["cause"] == "liquidation"
    assert "APRUSDT" not in e5["book"] and e5["cash"] == pytest.approx(APR_CASH - APR_DELTA)
    assert e5["funding_since"] == 222
    for mid in ("ake_liq_1106", "apr_adl_1106"):
        rec = store.s["migrations"][mid]
        assert rec["done"] and rec["actor"] == "Robin" and rec["orders"] == 0
    assert "自動交易專家" not in json.dumps(store.s)
    assert store.s["paused_slots"]["demo_r2s_top10_3k"]["paused_by"] == "human"
    assert Transport.sent == []


def test_cmd_run_calls_run_once_without_a_futures_client():
    src = inspect.getsource(main.cmd_run)
    assert "reconcile.run_once(store, client=client)" in src
    assert "fc=" not in src.split("reconcile.run_once", 1)[1].split(")", 1)[0]


def test_production_run_once_books_repairs_and_runs_sync(monkeypatch):
    """main.cmd_run's call, with the futures client resolved inside run_once."""
    _block_network(monkeypatch)
    Transport.n = 0
    Transport.sent = []
    monkeypatch.setattr(futures_client, "FuturesDemoClient", Transport)
    seen = _spy(monkeypatch)
    store = MemStore(_state())
    out = reconcile.run_once(store, client=Spot())
    assert Transport.n == 1 and Transport.sent == []
    assert [row[0] for row in seen] == ["repairs", "sync"]
    assert seen[0][1] is seen[1][1] and seen[0][2] is None and seen[1][2] is None
    assert seen[0][1].__class__ is Transport
    _assert_booked(store)
    assert out["exchange_close_sync"]["ok"] is True
    assert {r["id"] for r in out["exchange_close_sync"]["repairs"] if r.get("applied")} == {"ake_liq_1106", "apr_adl_1106"}
    assert out["exchange_close_sync"]["sync"] == []
    assert seen[0][1].account_position_reads >= 1
    again = reconcile.run_once(store, client=Spot())
    assert again["exchange_close_sync"]["ok"] is True
    assert again["exchange_close_sync"]["repairs"] == []
    assert store.s["demo_vbo1h_exec"]["cash"] == pytest.approx(AKE_CASH - AKE_DELTA)
    assert store.s["migrations"]["ake_liq_1106"]["done"] and store.s["migrations"]["apr_adl_1106"]["done"]
    assert Transport.n == 2 and Transport.sent == []


def test_reconcile_job_run_once_without_either_client(monkeypatch):
    """crypto-trader-reconcile starts reconcile.py, which calls run_once() with no arguments."""
    _block_network(monkeypatch)
    Transport.n = 0
    Transport.sent = []
    monkeypatch.setattr(futures_client, "FuturesDemoClient", Transport)
    monkeypatch.setattr("binance_client.BinanceClient", lambda *a, **k: Spot())
    seen = _spy(monkeypatch)
    store = MemStore(_state())
    out = reconcile.run_once(store)
    assert Transport.n == 1
    assert [row[0] for row in seen] == ["repairs", "sync"]
    assert seen[0][2] is None and seen[1][1] is seen[0][1]
    _assert_booked(store)
    assert out["exchange_close_sync"]["ok"] is True


def test_missing_futures_key_is_not_a_silent_success(monkeypatch):
    _block_network(monkeypatch)
    Keyless.n = 0
    monkeypatch.setattr(futures_client, "FuturesDemoClient", Keyless)
    seen = _spy(monkeypatch)
    store = MemStore(_state())
    out = reconcile.run_once(store, client=Spot())
    assert Keyless.n == 1 and seen == []
    assert store.s["demo_vbo1h_exec"]["cash"] == AKE_CASH
    assert store.s["demo_vbo1h_exec"]["book"]["AKEUSDT"] == -15002.0
    assert "ake_liq_1106" not in store.s["migrations"]
    assert out["exchange_close_sync"]["ok"] is False
    assert "BINANCE_DEMO_API_KEY" in out["exchange_close_sync"]["error"]
    assert out["ok"] is False
    assert any(str(e).startswith("exchange_close_sync:") for e in out["errors"])


def test_explicit_futures_client_is_reused(monkeypatch):
    _block_network(monkeypatch)

    def _boom():
        raise AssertionError("second futures client")

    monkeypatch.setattr(futures_client, "FuturesDemoClient", _boom)
    seen = _spy(monkeypatch)
    held = Transport()
    Transport.sent = []
    store = MemStore(_state())
    out = reconcile.run_once(store, client=Spot(), fc=held)
    assert seen[0][1] is held and seen[1][1] is held
    _assert_booked(store)
    assert out["exchange_close_sync"]["ok"] is True


class AprHistory(Transport):
    """APR window contains the two already-booked liquidations plus the Nov 6 ADL."""

    def __init__(self):
        super().__init__()
        self.fetched = []

    def position_risk(self, symbol=None):
        rows = super().position_risk(symbol)
        extra = [{"symbol": "BTCUSDC", "positionAmt": "0.01", "positionSide": "BOTH"},
                 {"symbol": "ETHUSDC", "positionAmt": "-0.02", "positionSide": "BOTH"}]
        if symbol is None:
            return rows + extra
        if symbol in ("BTCUSDC", "ETHUSDC"):
            self.fetched.append(symbol)
            return [r for r in extra if r["symbol"] == symbol]
        return rows

    def _signed(self, method, path, params=None):
        sym = (params or {}).get("symbol")
        if sym in ("BTCUSDC", "ETHUSDC"):
            self.fetched.append(sym)
        rows = super()._signed(method, path, params)
        if path.endswith("userTrades") and sym == "APRUSDT":
            from tests.test_exchange_close_sync import OCT25, OCT27, _old_liq
            older = [_old_liq(70294043, OCT25, "autoclose-70294043")[0],
                     _old_liq(71391559, OCT27, "autoclose-71391559")[0]]
            return older + rows
        return rows

    def all_orders(self, symbol, start_ms=None, limit=1000):
        rows = super().all_orders(symbol, start_ms=start_ms, limit=limit)
        if symbol != "APRUSDT":
            return rows
        from tests.test_exchange_close_sync import _old_liq, OCT25, OCT27
        return [_old_liq(70294043, OCT25, "autoclose-70294043")[1],
                _old_liq(71391559, OCT27, "autoclose-71391559")[1], *rows]


def _booked_ake_state():
    st = _state()
    st["migrations"]["ake_liq_1106"] = {"done": True, "actor": "Robin", "trade_ids": ["87896577"]}
    st["demo_vbo1h_exec"] = {"cash": 1312.18391819, "funding_since": 111, "book": {},
                             "forced_flat": {"AKEUSDT": {"target": -0.03125, "cause": "liquidation"}}}
    st["demo_e5v_exec"]["cash"] = 5126.701670928106
    st["booked_external_trades"] = {"87896577": {"booking": "ake_liq_1106", "symbol": "AKEUSDT"}}
    st["u1_live"] = {"quote": "USDC", "cash": 80.0, "units": {"BTC": 0.01, "ETH": -0.02}, "fills": []}
    st["demo_fa_positions"] = {"SOLUSDT": {"perp_qty": 1.0, "spot_qty": 1.0}}
    return st


def test_production_path_books_only_the_apr_adl_and_leaves_ake(monkeypatch):
    _block_network(monkeypatch)
    Transport.n = 0
    Transport.sent = []
    monkeypatch.setattr(futures_client, "FuturesDemoClient", AprHistory)
    store = MemStore(_booked_ake_state())
    out = reconcile.run_once(store, client=Spot())
    e5, vbo = store.s["demo_e5v_exec"], store.s["demo_vbo1h_exec"]
    assert "APRUSDT" not in e5["book"] and e5["cash"] == pytest.approx(5126.701670928106 - 9.2234)
    assert e5["funding_since"] == 222
    assert vbo["cash"] == 1312.18391819 and vbo["book"] == {}
    assert vbo["forced_flat"]["AKEUSDT"]["target"] == -0.03125
    assert store.s["migrations"]["apr_adl_1106"]["trade_ids"] == ["73620325"]
    assert {p["id"] for p in store.s["migrations"]["apr_adl_1106"]["prior_lifecycle"]} == {"70294043", "71391559"}
    assert "70294043" not in store.s["booked_external_trades"] and "71391559" not in store.s["booked_external_trades"]
    assert store.s["u1_live"]["cash"] == 80.0 and store.s["demo_fa_positions"]["SOLUSDT"]["perp_qty"] == 1.0
    sync_syms = {r.get("symbol") for r in out["exchange_close_sync"]["sync"]}
    assert "BTCUSDC" not in sync_syms and "ETHUSDC" not in sync_syms
    assert out["exchange_close_sync"]["ok"] is True and Transport.sent == []
    again = reconcile.run_once(store, client=Spot())
    assert again["exchange_close_sync"]["repairs"] == []
    assert e5["cash"] == pytest.approx(5126.701670928106 - 9.2234)
    assert vbo["cash"] == 1312.18391819


def test_production_path_rejects_a_new_forced_fill_beside_the_history(monkeypatch):
    _block_network(monkeypatch)
    Transport.sent = []

    class Dirty(AprHistory):
        def _signed(self, method, path, params=None):
            rows = super()._signed(method, path, params)
            if path.endswith("userTrades") and (params or {}).get("symbol") == "APRUSDT":
                rows = rows + [{"id": 999001, "orderId": 999001, "side": "BUY", "qty": "1", "price": "0.04",
                                "commission": "0", "commissionAsset": "USDT", "realizedPnl": "0",
                                "time": 1793955800000, "clientOrderId": "adl_autoclose"}]
            return rows

    monkeypatch.setattr(futures_client, "FuturesDemoClient", Dirty)
    store = MemStore(_booked_ake_state())
    out = reconcile.run_once(store, client=Spot())
    assert store.s["demo_e5v_exec"]["book"]["APRUSDT"] == -214.0
    assert store.s["demo_e5v_exec"]["cash"] == 5126.701670928106
    assert store.s["demo_vbo1h_exec"]["cash"] == 1312.18391819
    assert "apr_adl_1106" not in store.s["migrations"]
    assert out["exchange_close_sync"]["ok"] is False
    assert Transport.sent == []


def test_direct_repair_without_client_does_not_pretend_success():
    store = MemStore(_state())
    got = x.run_repairs(store)
    assert got == [{"ok": False, "applied": False, "skipped": "futures_client_unavailable"}]
    assert store.s["demo_vbo1h_exec"]["book"]["AKEUSDT"] == -15002.0
    sync = x.run_sync(store)
    assert sync[0]["status"] == "futures_client_unavailable" and sync[0]["pause"] is False
    assert "ake_liq_1106" not in store.s["migrations"]
