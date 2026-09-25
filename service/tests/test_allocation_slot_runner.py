"""Every allocation slot must resolve from allocation (not hardcoded SLOTS),
produce an enter on a simulated signal bar, and be routed to the right venue
(ls_* perp → Demo futures, everything else → spot). Mocked exchanges only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONTROL_PIN_SALT", "crypto-trader-v1")
os.environ.setdefault("CONTROL_PIN_HASH", "a" * 64)
os.environ["ALLOW_HTTP_PIN"] = "1"
os.environ.setdefault("SESSION_HMAC_SECRET", "test-session-secret-for-unit-tests")

ALLOC = json.loads((ROOT.parent / "config" / "allocation.json").read_text(encoding="utf-8"))
ENABLED = [s for s in ALLOC["slots"] if s.get("enabled")]
FUTURES_FAMS = {"ls_donch_btc_regime_perp", "ls_univ_portfolio_perp",
                "donchian_long_short_btc_regime", "donchian_lev_vol"}
TF_FREQ = {"1h": "1h", "4h": "4h", "1d": "1D"}


def _klines(tf: str, n: int, family: str) -> pd.DataFrame:
    """Closed bars in the past; last bar is the signal bar."""
    idx = pd.date_range("2025-01-01", periods=n, freq=TF_FREQ[tf], tz="UTC")
    if family == "ema_cross_atr":
        close = np.linspace(110.0, 100.0, n - 1).tolist() + [130.0]  # gentle downtrend → jump
    else:
        pat = [100, 101, 102, 101, 100, 99, 98, 99]
        close = [float(pat[i % len(pat)]) for i in range(n - 1)] + [120.0]
    c = pd.Series(close, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 0.5, "Low": c - 0.5, "Close": c,
                         "Volume": 1000.0}, index=idx)


def _btc_bull(n: int = 800) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    c = pd.Series(np.linspace(50000, 90000, n), index=idx)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1.0}, index=idx)


class FakeSpot:
    api_key = "x"

    def __init__(self, fam_by_symbol):
        self.fam_by_symbol = fam_by_symbol
        self.buys: list = []
        self.stops: list = []

    def fetch_klines(self, symbol, tf, limit=250, use_vision=True):
        if symbol == "BTCUSDT" and tf == "1d":
            return _btc_bull()
        return _klines(tf, max(limit, 300), self.fam_by_symbol[symbol])

    def market_buy(self, symbol, quote, coid):
        self.buys.append((symbol, quote, coid))
        return {"orderId": 1, "executedQty": str(quote / 120.0),
                "fills": [{"price": "120", "qty": str(quote / 120.0)}]}

    def stop_loss_limit(self, symbol, qty, stop, limit, coid):
        self.stops.append((symbol, qty, stop))
        return {"orderId": 2, "clientOrderId": coid}

    def cancel_open_orders(self, symbol):
        return []

    def ticker_price(self, symbol):
        return 120.0


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    import slots
    slots.invalidate_allocation_cache()
    import futures_client
    import futures_execution
    fut_calls: list = []

    class FakeFut:
        def __init__(self, *a, **k):
            pass

    def fake_open_market(fc, *, symbol, side, notional_usdt, leverage, client_order_id):
        fut_calls.append(("open", symbol, side, notional_usdt, leverage))
        return {"qty": notional_usdt / 120.0, "mark": 120.0, "order": {"orderId": 9},
                "leverage": leverage, "notional_usdt": notional_usdt}

    def fake_stop(fc, *, symbol, is_long, stop_price, qty, client_order_id=None):
        fut_calls.append(("stop", symbol, is_long, stop_price))
        return {"algoId": 10}

    monkeypatch.setattr(futures_client, "FuturesDemoClient", FakeFut)
    monkeypatch.setattr(futures_execution, "open_market", fake_open_market)
    monkeypatch.setattr(futures_execution, "place_stop_reduce_only", fake_stop)
    yield fut_calls
    slots.invalidate_allocation_cache()


def _state():
    return {"approved_families": sorted({s["family"] for s in ENABLED}), "positions": {}, "slots": {}}


def test_allocation_has_expected_six_slots():
    ids = {s["slot"] for s in ENABLED}
    assert {"sat_op_4h", "sat_dot_4h", "sat_fet_4h", "sat_fil_1h", "sat_sol_ema_1d", "ls_arb_4h"} <= ids


@pytest.mark.parametrize("raw", ENABLED, ids=[s["slot"] for s in ENABLED])
def test_slot_resolves_from_allocation(env, raw):
    from slots import slot_by_id, strategy_id_for_slot, slot_by_strategy_id
    import main
    rt = slot_by_id(raw["slot"])
    assert rt and rt["strategy_id"] == raw["strategy_id"]
    assert rt["family"] == raw["family"]
    assert strategy_id_for_slot(raw["slot"]) == raw["strategy_id"]
    assert slot_by_strategy_id(raw["strategy_id"])["id"] == raw["slot"]
    want_venue = "futures" if raw["family"] in FUTURES_FAMS else "spot"
    assert rt["venue"] == want_venue
    ok, why = main._allow_entry_for_slot(_state(), raw["slot"], paused=False)
    assert why != "unknown_slot_strategy"
    assert ok, why


@pytest.mark.parametrize("raw", ENABLED, ids=[s["slot"] for s in ENABLED])
def test_signal_bar_places_order_on_right_venue(env, raw):
    import main
    from strategy import evaluate_all
    from allocation import slot_to_runtime
    fut_calls = env
    rt = slot_to_runtime(raw)
    fam_by_symbol = {rt["symbol"]: rt["family"]}
    client = FakeSpot(fam_by_symbol)
    state = _state()
    state["runtime_eval_slots"] = [rt]
    sigs = evaluate_all(client, state)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.get("action") == "enter", sig
    assert sig["strategy_id"] == raw["strategy_id"]
    out = main.apply_signal(client, state, sig, live=True, allow_entries=True)
    assert out.get("executed"), out
    pos = state["positions"][raw["slot"]]
    if raw["family"] in FUTURES_FAMS:
        assert [c for c in fut_calls if c[0] == "open"], "expected Demo futures order"
        assert client.buys == []
        assert pos["venue"] == "futures"
        assert fut_calls[0][4] == pytest.approx(float(raw["params"].get("leverage") or 1))
        assert any(c[0] == "stop" for c in fut_calls)
    else:
        assert fut_calls == []
        assert len(client.buys) == 1 and client.buys[0][0] == raw["symbol"]
        assert client.buys[0][1] == pytest.approx(min(float(raw["notional_usdt"]), 1500.0))
        assert client.stops, "spot hard stop must be placed"
        assert pos.get("venue", "spot") == "spot"


def test_approved_records_family_and_code(env):
    import api
    state = {
        "approved_families": ["donchian_atr"],
        "approved": {
            "donchian55_s2.0_t3.0_btcRegimeD__FET__4h": {"slot": "sat_fet_4h_btc", "family": "donchian_atr",
                                                        "approved_at": "2026-09-25T02:02:03+08:00", "approved": True, "mode": "live"},
            "donchian55_s1.5_t2.5__FIL__1h": {"approved_at": "2026-09-25T01:58:06+08:00", "approved": True, "mode": "live"},
        },
        "signal_only": {},
    }
    pub = api._approved_public(state)
    by = {r["strategy_id"]: r for r in pub["approved"]}
    fet = by["donchian55_s2.0_t3.0_btcRegimeD__FET__4h"]
    assert fet["code"] == "J1" and fet["family"] == "donchian_btc_regime"
    fil = by["donchian55_s1.5_t2.5__FIL__1h"]
    assert fil["slot"] == "sat_fil_1h" and fil["family"] == "donchian_atr" and fil["code"] == "C2"


def test_family_approvals_backfill_and_store():
    from slots import family_approvals, approve_family
    state = {
        "approved_families": ["donchian_atr"],
        "approved": {
            "donchian20_s1.5_t1.5__OP__4h": {"approved_at": "2026-09-20T10:00:00+08:00"},
            "donchian20_s1.5_t1.5__DOT__4h": {"approved_at": "2026-09-19T10:00:00+08:00"},
        },
    }
    fa = family_approvals(state)
    assert fa == [{"family": "donchian_atr", "code": "C", "approved_at": "2026-09-19T10:00:00+08:00",
                   "source": "backfill_slot_approval"}]
    approve_family(state, "ema_cross_atr", at="2026-09-25T12:00:00+08:00")
    fa = {r["family"]: r for r in family_approvals(state)}
    assert fa["ema_cross_atr"]["approved_at"] == "2026-09-25T12:00:00+08:00"
    assert fa["ema_cross_atr"]["source"] == "stored"
    assert fa["donchian_atr"]["source"] == "stored"  # persisted after backfill


def test_readonly_token_scope(monkeypatch):
    import api
    tok = "r" * 64
    monkeypatch.setattr(api, "READONLY_TOKEN", tok)
    monkeypatch.setattr(api, "build_status", lambda: {"ok": True, "x": 1})
    monkeypatch.setattr(api, "_approved_public", lambda state=None: {"ok": True, "approved": []})
    api._fail_buckets.clear()
    c = api.app.test_client()
    h = {"Authorization": f"Bearer {tok}", "X-Forwarded-Proto": "https"}
    assert c.get("/status", headers=h).status_code == 200
    assert c.get("/approved", headers=h).status_code == 200
    for path in ("/control/pause", "/control/resume", "/control/close", "/control/close_all",
                 "/control/approve", "/control/revoke", "/control/approve_family", "/control/revoke_family"):
        r = c.post(path, headers=h, json={})
        assert r.status_code == 403, (path, r.status_code)
    r = c.post("/auth/login", headers=h, json={"pin": tok})
    assert r.status_code in (401, 429)
    r = c.post("/auth/login", headers=h, json={})
    assert r.status_code in (401, 429)
    # wrong token still 401
    assert c.get("/status", headers={"Authorization": "Bearer nope", "X-Forwarded-Proto": "https"}).status_code == 401
