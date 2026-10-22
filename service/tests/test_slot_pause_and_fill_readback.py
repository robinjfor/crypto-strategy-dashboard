"""(A) single-slot pause routes + runner gating; (1) Binance fill readback recorded on trades."""
from __future__ import annotations

import os

os.environ.setdefault("CONTROL_PIN_SALT", "crypto-trader-v1")
os.environ.setdefault("CONTROL_PIN_HASH", "a" * 64)
os.environ["ALLOW_HTTP_PIN"] = "1"
os.environ.setdefault("SESSION_HMAC_SECRET", "test-session-secret-for-unit-tests")

import pytest  # noqa: E402

import fill_readback as fr  # noqa: E402


class MemStore:
    state: dict = {}

    def mutate(self, fn):
        MemStore.state = fn(MemStore.state)
        return MemStore.state

    def load(self):
        return MemStore.state


@pytest.fixture
def client(monkeypatch):
    import api
    MemStore.state = {"paused": True, "paused_slots": {}}
    monkeypatch.setattr(api, "StateStore", MemStore)
    monkeypatch.setattr(api, "_check_pin", lambda: (True, "", 200))
    return api.app.test_client()


def test_pause_resume_slot_routes(client):
    r = client.post("/control/pause_slot", json={"slot": "sat_c2"})
    assert r.status_code == 200 and r.get_json()["slot_paused"] is True
    assert "sat_c2" in MemStore.state["paused_slots"]
    assert MemStore.state["paused"] is True
    r = client.post("/control/resume_slot", json={"slot": "sat_c2"})
    assert r.status_code == 200 and r.get_json()["slot_paused"] is False
    assert MemStore.state["paused_slots"] == {}
    # resume_slot must never lift the global pause
    assert MemStore.state["paused"] is True and r.get_json()["global_paused"] is True


def test_pause_slot_requires_slot(client):
    assert client.post("/control/pause_slot", json={}).status_code == 400
    assert client.post("/control/resume_slot", json={"slot": ""}).status_code == 400


def test_pause_slot_requires_pin(monkeypatch):
    import api
    MemStore.state = {"paused_slots": {}}
    monkeypatch.setattr(api, "StateStore", MemStore)
    tok = "r" * 64
    monkeypatch.setattr(api, "READONLY_TOKEN", tok)
    api._fail_buckets.clear()
    c = api.app.test_client()
    for path in ("/control/pause_slot", "/control/resume_slot"):
        assert c.post(path, json={"slot": "x"}).status_code in (401, 403)
        h = {"Authorization": f"Bearer {tok}", "X-Forwarded-Proto": "https"}
        assert c.post(path, headers=h, json={"slot": "x"}).status_code == 403
        assert c.options(path).status_code == 204
    assert MemStore.state["paused_slots"] == {}


def test_runner_skips_entry_for_paused_slot_but_manages_exit():
    import main
    st = {"paused": False, "paused_slots": {"s1": {"paused_at": "t"}},
          "positions": {"s1": {"symbol": "FILUSDT", "qty": 2.0, "entry": 1.0}}}
    ok, why = main._allow_entry_for_slot(st, "s1", paused=False)
    assert not ok and why == "slot_paused_no_new_entries"
    out = main.apply_signal(None, st, {"slot": "s1", "action": "enter", "symbol": "FILUSDT"},
                            live=True, allow_entries=True)
    assert out["executed"] is False and out["reason"] == "slot_paused_no_new_entries"
    ex = main.apply_signal(None, st, {"slot": "s1", "action": "exit", "symbol": "FILUSDT", "bar_ts": "b"},
                           live=False, allow_entries=True)
    assert ex["intended_order"]["side"] == "SELL"  # existing position still managed
    assert main.slot_paused(st, "s1") and not main.slot_paused(st, "s2")


class FakeSpotRB:
    def __init__(self):
        self.calls = 0

    def get_order(self, symbol, oid):
        self.calls += 1
        return {"orderId": oid, "status": "FILLED" if self.calls > 1 else "NEW"}

    def my_trades(self, symbol, limit=50):
        return [
            {"id": 1, "orderId": 7, "price": "1.10", "qty": "10", "commission": "0.01", "commissionAsset": "FIL"},
            {"id": 2, "orderId": 7, "price": "1.20", "qty": "10", "commission": "0.01", "commissionAsset": "FIL"},
            {"id": 3, "orderId": 8, "price": "9", "qty": "1", "commission": "1", "commissionAsset": "USDT"},
        ]


def test_spot_readback_uses_exchange_trades():
    c = FakeSpotRB()
    fb = fr.readback_spot(c, "FILUSDT", {"orderId": 7, "status": "NEW"}, timeout_s=5, sleep=lambda s: None)
    assert fb["complete"] and fb["order_status"] == "FILLED" and fb["n_trades"] == 2
    assert fb["qty"] == 20 and abs(fb["price"] - 1.15) < 1e-9
    assert fb["commissions"] == [{"asset": "FIL", "amount": 0.02}]
    assert abs(fb["commission_quote"] - (0.011 + 0.012)) < 1e-9
    pos = fr.apply_entry_fill({"entry": 1.0, "qty": 19.98}, fb)
    assert pos["entry"] == fb["price"] and pos["entry_fee_usdt"] == fb["commission_quote"]
    assert pos["qty"] == 19.98  # net sellable qty unchanged


def test_exit_readback_overrides_price_and_fees():
    class F:
        def get_order(self, s, coid):
            return {"orderId": 5, "status": "FILLED"}

        def user_trades(self, s, limit=50):
            return [{"id": 9, "orderId": 5, "price": "100", "qty": "0.5", "commission": "0.02",
                     "commissionAsset": "USDC"}]
    fb = fr.readback_futures(F(), "BTCUSDC", {"orderId": 5, "status": "NEW"}, coid="c", timeout_s=1,
                             sleep=lambda s: None)
    closed = fr.apply_exit_fill({"entry": 110.0, "exit": 0, "qty": 0.5, "side": "SHORT"}, fb)
    assert closed["exit"] == 100 and closed["pnl_usdt"] == 5.0
    assert closed["exit_commissions"] == [{"asset": "USDC", "amount": 0.02}]
    assert fb["commission_quote"] == 0.02


def test_bnb_commission_kept_as_asset_amount():
    fb = fr.summarize_trades([{"id": 1, "price": "2", "qty": "1", "commission": "0.0001",
                               "commissionAsset": "BNB"}], "FILUSDT")
    assert fb["commission_quote"] is None
    assert fb["commissions"] == [{"asset": "BNB", "amount": 0.0001}]


def test_readback_failure_is_safe():
    class Bad:
        def my_trades(self, *a, **k):
            raise RuntimeError("down")
    fb = fr.readback_spot(Bad(), "FILUSDT", {"orderId": 1, "fills": [
        {"price": "1", "qty": "2", "commission": "0.002", "commissionAsset": "USDT"}]})
    assert fb["complete"] and fb["commission_quote"] == 0.002


def test_control_log_records_actor_source_time(client):
    client.post("/control/pause_slot", json={"slot": "s9", "actor": "emily"}, headers={"Origin": "https://dash"})
    client.post("/control/resume_slot", json={"slot": "s9"}, headers={"X-Actor": "bot"})
    log = MemStore.state["control_log"][-2:]
    assert [e["action"] for e in log] == ["pause_slot", "resume_slot"]
    assert log[0]["actor"] == "emily" and log[0]["source"] == "https://dash" and log[0]["at"]
    assert log[1]["actor"] == "bot" and log[1]["was_paused"] is True
