"""S family runner wiring: gated out of allocation/approval, perp inputs, stop basis + workingType,
leverage caps, conflict flag. Existing families must be unaffected."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import allocation
import futures_client
import futures_execution
import main
import slots
import strategy
from allocation import validate_allocation, validate_params, slot_to_runtime


def _s_slot(**over):
    s = {"slot": "short_gala_6h", "family": "short_trend_perp", "strategy_id": "wf_sema_ema12_26_s3.0_t5.0_none__GALA__6h",
         "symbol": "GALAUSDT", "timeframe": "6h", "notional_usdt": 750, "enabled": False,
         "params": {"signal": "sema", "ema_fast": 12, "ema_slow": 26, "stop_m": 3.0, "trail_m": 5.0, "filt": "none",
                    "leverage": 1.0, "maxdd_pct": -36.858}}
    s.update(over)
    return s


def test_s_family_approvable_but_approval_alone_opens_nothing(monkeypatch):
    """資金控管 passed S (2026-10-09): approvable; live orders still need an enabled allocation slot."""
    doc = {"book_usdt": 5000, "slots": [_s_slot()]}
    errs = validate_allocation(doc, check_binance=False)
    assert errs == [], errs  # analyst can add S slots (6h valid, 1x)
    assert "short_trend_perp" in allocation.RUNNER_FAMILIES and "short_trend_perp" in slots.SUPPORTED_FAMILIES
    st: dict = {}
    slots.approve_family(st, "short_trend_perp", at="t")
    assert "short_trend_perp" in st["approved_families"]
    # fixture allocation has no S slot -> approving the family yields no S trade slot / approved id
    trade = main._iter_trade_slots(st)
    assert not [x for x in trade if x.get("family") == "short_trend_perp"]
    assert not [i for i in main._approved_ids(st) if "sema" in i]


def test_existing_families_keep_1h_4h_1d_only():
    doc = {"book_usdt": 5000, "slots": [{"slot": "x", "family": "donchian_atr", "strategy_id": "d", "symbol": "OPUSDT",
                                          "timeframe": "6h", "notional_usdt": 500, "enabled": False,
                                          "params": {"donch_n": 20, "stop_m": 1.5, "trail_m": 1.5}}]}
    assert any("timeframe 不支援" in e for e in validate_allocation(doc, check_binance=False))


@pytest.mark.parametrize("lev,dd,ok", [(1.0, -36.9, True), (1.0, -48.7, True), (2.0, -36.9, False),
                                       (5.0, -36.9, False), (0.5, -10, False)])
def test_s_leverage_locked_1x(lev, dd, ok):
    errs: list[str] = []
    p = {**_s_slot()["params"], "leverage": lev, "maxdd_pct": dd}
    validate_params("short_trend_perp", p, errs, 0)
    assert (errs == []) is ok, errs


def test_s_params_validation():
    errs: list[str] = []
    validate_params("short_trend_perp", {"ema_fast": 10, "ema_slow": 20, "stop_m": 2, "trail_m": 0, "filt": "x"}, errs, 0)
    assert len(errs) == 3


def test_slot_to_runtime_s_and_unchanged_for_others():
    rt = slot_to_runtime(_s_slot())
    assert (rt["family"], rt["tf"], rt["signal"], rt["filter"], rt["ema_fast"], rt["ema_slow"], rt["stop_atr_mult"],
            rt["trail_atr_mult"], rt["maxdd_pct"]) == ("short_trend_perp", "6h", "sema", "none", 12, 26, 3.0, 5.0, -36.858)
    other = slot_to_runtime({"slot": "e", "family": "ema_cross_atr", "symbol": "SOLUSDT", "timeframe": "1d",
                             "notional_usdt": 1000, "params": {"fast": 12, "slow": 26, "stop_m": 2, "trail_m": 3}})
    assert "filter" not in other and "signal" not in other and "maxdd_pct" not in other
    assert slots.venue_for_family("short_trend_perp") == "futures"


def test_leverage_clamp_default_unchanged():
    assert futures_client.clamp_leverage(5) == 3
    assert futures_client.clamp_leverage(5, cap=5) == 5
    assert futures_client.clamp_leverage(9, cap=9) == 5


class _FakeFC:
    def __init__(self):
        self.algo = []

    def exchange_info(self):
        return {"symbols": [{"symbol": "GALAUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.00001"}, {"filterType": "LOT_SIZE", "stepSize": "1"}]}]}

    def algo_order(self, **p):
        self.algo.append(p)
        return {"algoId": 7}


def test_stop_working_type():
    fc = _FakeFC()
    futures_execution.place_stop_reduce_only(fc, symbol="GALAUSDT", is_long=False, stop_price=0.02, qty=100)
    futures_execution.place_stop_reduce_only(fc, symbol="GALAUSDT", is_long=False, stop_price=0.02, qty=100,
                                             working_type="CONTRACT_PRICE")
    assert [a["workingType"] for a in fc.algo] == ["MARK_PRICE", "CONTRACT_PRICE"]
    assert fc.algo[1]["side"] == "BUY" and fc.algo[1]["reduceOnly"] == "true"


def test_dispatch_routes_s():
    idx = pd.date_range("2024-01-01", periods=50, freq="6h", tz="UTC")
    kl = pd.DataFrame({"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0}, index=idx)
    slot = {"id": "s", "symbol": "GALAUSDT", "tf": "6h", "family": "short_trend_perp", "signal": "sema",
            "ema_fast": 12, "ema_slow": 26, "stop_atr_mult": 3.0, "trail_atr_mult": 5.0, "filter": "none"}
    r = strategy.evaluate_slot_dispatch(slot, kl, None, {})
    assert r["family"] == "short_trend_perp" and r["side"] == "SHORT" and r["stop_working_type"] == "CONTRACT_PRICE"


def test_short_trend_inputs(monkeypatch):
    import perp_market
    calls = []
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}, index=idx)
    monkeypatch.setattr(perp_market, "fetch_perp_klines", lambda s, tf, limit=0: calls.append((s, tf, limit)) or df)
    monkeypatch.setattr(perp_market, "fetch_funding", lambda s, limit=0: calls.append(("F", s)) or pd.Series(dtype=float))
    slot = {"id": "short_apt_4h", "symbol": "APTUSDT", "tf": "4h", "filter": "coin50"}
    pos = {"core": {"status": "FILLED", "symbol": "APTUSDT", "side": "LONG"}}
    kl, m = strategy._short_trend_inputs(slot, {"x": 1}, pos)
    assert calls == [("APTUSDT", "4h", 1500), ("APTUSDT", "1d", 200)]
    assert m["_opposite_open"] is True and "_coin_daily" in m and m["x"] == 1
    _, m2 = strategy._short_trend_inputs({**slot, "filter": "fpos"}, {}, {"o": {"status": "FILLED", "symbol": "APTUSDT", "side": "SHORT"}})
    assert m2["_opposite_open"] is False and "_funding" in m2


def test_apply_signal_s_entry_stop_from_fill(monkeypatch):
    fc = _FakeFC()
    seen = {}
    monkeypatch.setattr(futures_client, "FuturesDemoClient", lambda: fc)
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))

    def fake_open(c, **kw):
        seen["open"] = kw
        return {"order": {"orderId": 1, "avgPrice": "0.02000"}, "qty": 37500.0, "mark": 0.0201, "avg_price": 0.02,
                "side": "SELL", "leverage": 1, "notional_usdt": 750.0}

    def fake_stop(c, **kw):
        seen["stop"] = kw
        return {"algoId": 9}

    monkeypatch.setattr(futures_execution, "open_market", fake_open)
    monkeypatch.setattr(futures_execution, "place_stop_reduce_only", fake_stop)
    fc.user_trades = lambda *a, **k: []
    sig = {"slot": "short_gala_6h", "symbol": "GALAUSDT", "action": "enter", "family": "short_trend_perp",
           "venue": "futures", "side": "SHORT", "quote_usdt": 750.0, "bar_ts": "2026-10-09T00:00:00+00:00",
           "leverage": 3.0, "leverage_cap": 5.0, "stop_atr_mult": 3.0, "stop_atr": 0.001, "suggested_stop": 0.5,
           "close": 0.0199}
    state: dict = {}
    out = main.apply_signal(None, state, sig, live=True, allow_entries=True)
    assert out["executed"]
    pos = state["positions"]["short_gala_6h"]
    assert pos["stop_basis"] == pytest.approx(0.02 * 0.998)
    assert pos["stop"] == pytest.approx(0.02 * 0.998 + 0.003) == seen["stop"]["stop_price"]
    assert seen["stop"]["working_type"] == "CONTRACT_PRICE" and seen["stop"]["is_long"] is False
    assert seen["open"]["side"] == "SELL" and seen["open"]["leverage_cap"] == 1 and seen["open"]["leverage"] == 1
    assert state["slots"]["short_gala_6h"]["last_entry_bar_ts"] == sig["bar_ts"]


def test_reconcile_records_funding_for_s_stop(monkeypatch):
    import execution
    import perp_market

    class FC:
        pass

    monkeypatch.setattr(futures_client, "FuturesDemoClient", lambda: FC())
    monkeypatch.setattr(futures_execution, "cancel_algo_stops", lambda fc, sym: 0)
    monkeypatch.setattr(execution, "_futures_closed_info", lambda fc, sid, pos: {
        "qty": 100.0, "price": 0.021, "time_ms": 1_760_000_000_000, "order_id": 5, "reason": "stop"})
    got = {}
    monkeypatch.setattr(perp_market, "funding_income_usdt",
                        lambda fc, sym, a, b=None: got.update(args=(sym, a, b)) or 0.42)
    state = {"positions": {
        "short_gala_6h": {"status": "FILLED", "venue": "futures", "symbol": "GALAUSDT", "side": "SHORT", "qty": 100.0,
                          "entry": 0.02, "family": "short_trend_perp", "filled_ms": 1_759_000_000_000},
        "other": {"status": "FILLED", "venue": "futures", "symbol": "OPUSDT", "side": "LONG", "qty": 1.0, "entry": 1.0}}}
    out = execution.reconcile_exchange_closes(None, state, tf_by_slot={"short_gala_6h": "6h", "other": "4h"})
    by = {c["slot"]: c for c in out}
    assert by["short_gala_6h"]["funding_usdt"] == 0.42
    assert got["args"] == ("GALAUSDT", 1_759_000_000_000, 1_760_000_000_000)
    assert "funding_usdt" not in by["other"]
    assert state["slots"]["short_gala_6h"]["last_exit_reason"] == "stop"


def test_runner_side_stop_reentry_follow_up(monkeypatch):
    """cmd_run applies the attached same-bar entry only after the stop close executed."""
    calls = []

    def fake_apply(client, state, sig, live, allow_entries):
        calls.append(sig["action"])
        return {"executed": True, "slot": sig["slot"]}

    enter = {"slot": "short_gala_6h", "action": "enter", "symbol": "GALAUSDT"}
    sig = {"slot": "short_gala_6h", "action": "exit", "reason": "stop_trail", "symbol": "GALAUSDT", "after_stop": enter}
    monkeypatch.setattr(main, "apply_signal", fake_apply)
    monkeypatch.setattr(main, "evaluate_all", lambda client, state: [dict(sig)])
    monkeypatch.setattr(main, "resolve_allocation", lambda: ({"slots": []}, "test"))

    class Store:
        st: dict = {}

        def load(self):
            return {}

        def save(self, st):
            Store.st = st

        def mutate(self, fn):
            Store.st = fn(Store.st)
            return Store.st

    monkeypatch.setattr(main, "StateStore", Store)
    monkeypatch.setenv("TRADER_ENABLED", "false")
    try:
        main.cmd_run(None, dry_run=True)
    except Exception:  # noqa: BLE001  (later bookkeeping may need real clients; the apply order is what we test)
        pass
    assert calls[:2] == ["exit", "enter"]


def _spot_enter(slot="sat_op_4h", sym="OPUSDT", side=None):
    sig = {"slot": slot, "symbol": sym, "action": "enter", "family": "donchian_atr", "venue": "spot",
           "quote_usdt": 1000.0, "bar_ts": "2026-10-09T08:00:00+00:00", "close": 0.12}
    if side:
        sig["side"] = side
    return sig


@pytest.mark.parametrize("sym", ["OPUSDT", "OPUSDC"])
def test_open_s_short_blocks_c_f_long_same_coin(monkeypatch, sym):
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))
    state = {"positions": {"short_op_4h": {"status": "FILLED", "symbol": "OPUSDT", "side": "SHORT", "venue": "futures"}}}
    out = main.apply_signal(None, state, _spot_enter(sym=sym), live=False, allow_entries=True)
    assert out["reason"] == "opposite_position_open" and out["blocked_by"] == "short_op_4h"
    assert "intended_order" not in out and "sat_op_4h" not in state["positions"]


def test_long_blocks_s_short_and_other_coins_unaffected(monkeypatch):
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))
    state = {"positions": {"sat_op_4h": {"status": "FILLED", "symbol": "OPUSDT", "qty": 1.0}}}  # spot long (no side)
    s_sig = {**_spot_enter("short_op_4h"), "family": "short_trend_perp", "venue": "futures", "side": "SHORT"}
    assert main.apply_signal(None, state, s_sig, live=False, allow_entries=True)["reason"] == "opposite_position_open"
    # different coin, or same direction: not blocked (dry run -> intended order)
    assert "intended_order" in main.apply_signal(None, state, _spot_enter("sat_dot_4h", "DOTUSDT"), live=False, allow_entries=True)
    state2 = {"positions": {"short_op_4h": {"status": "FILLED", "symbol": "OPUSDT", "side": "SHORT"}}}
    other = {**s_sig, "slot": "short_op_6h"}
    assert "intended_order" in main.apply_signal(None, state2, other, live=False, allow_entries=True)
    # a non-FILLED (pending/closed) opposite position does not block
    state3 = {"positions": {"short_op_4h": {"status": "CLOSED", "symbol": "OPUSDT", "side": "SHORT"}}}
    assert "intended_order" in main.apply_signal(None, state3, _spot_enter(), live=False, allow_entries=True)


def test_stale_protection_survives_slot_removed_from_allocation(monkeypatch):
    """S slot removed from the allocation while holding a position: reconcile has no tf from the
    allocation, so the position's own tf must floor the exit bar; the stale-signal rule still applies."""
    import execution
    from short_trend import evaluate_short_trend_slot

    monkeypatch.setattr(futures_client, "FuturesDemoClient", lambda: object())
    monkeypatch.setattr(futures_execution, "cancel_algo_stops", lambda fc, sym: 0)
    stop_ms = int(pd.Timestamp("2026-10-09T13:30:00Z").timestamp() * 1000)  # inside the 12:00 UTC 4h bar
    monkeypatch.setattr(execution, "_futures_closed_info", lambda fc, sid, pos: {
        "qty": 1.0, "price": 1.0, "time_ms": stop_ms, "order_id": 1, "reason": "stop"})
    state = {"positions": {"short_dot_4h": {"status": "FILLED", "venue": "futures", "symbol": "DOTUSDT", "side": "SHORT",
                                            "qty": 1.0, "entry": 1.1, "family": "short_trend_perp", "tf": "4h"}}}
    execution.reconcile_exchange_closes(None, state, tf_by_slot={})  # slot no longer in allocation
    meta = state["slots"]["short_dot_4h"]
    assert pd.Timestamp(meta["last_exit_bar_ts"]) == pd.Timestamp("2026-10-09T12:00:00Z")
    # an edge on 08:00 (position open then) is stale; an edge on the 12:00 stop bar itself may enter
    idx = pd.date_range(end="2026-10-09T12:00:00Z", periods=300, freq="4h", tz="UTC")
    c = np.r_[np.linspace(1.0, 2.0, 298), [1.0, 0.5]]
    kl = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c}, index=idx)
    slot = {"id": "short_dot_4h", "symbol": "DOTUSDT", "tf": "4h", "family": "short_trend_perp", "signal": "sema",
            "ema_fast": 9, "ema_slow": 21, "stop_atr_mult": 2.0, "trail_atr_mult": 4.0, "filter": "none", "quote_usdt": 750}
    win_stale = kl.iloc[:-1]  # last closed bar 08:00 (hourly run inside the 12:00 bar)
    assert evaluate_short_trend_slot(slot, win_stale, None, meta)["reason"] == "stale_signal_before_exit"
    # same window without the exit record would have entered (proves the edge exists)
    assert evaluate_short_trend_slot(slot, win_stale, None, {})["action"] == "enter"


def test_s_entry_stores_tf_on_position(monkeypatch):
    fc = _FakeFC()
    fc.user_trades = lambda *a, **k: []
    monkeypatch.setattr(futures_client, "FuturesDemoClient", lambda: fc)
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))
    monkeypatch.setattr(futures_execution, "open_market", lambda c, **kw: {
        "order": {"orderId": 1}, "qty": 10.0, "mark": 1.0, "avg_price": 1.0, "side": "SELL", "leverage": 1, "notional_usdt": 10.0})
    monkeypatch.setattr(futures_execution, "place_stop_reduce_only", lambda c, **kw: {"algoId": 2})
    sig = {"slot": "short_dot_4h", "symbol": "DOTUSDT", "action": "enter", "family": "short_trend_perp", "venue": "futures",
           "side": "SHORT", "quote_usdt": 750.0, "bar_ts": "2026-10-09T08:00:00+00:00", "tf": "4h", "stop_atr_mult": 2.0,
           "stop_atr": 0.01}
    state: dict = {}
    main.apply_signal(None, state, sig, live=True, allow_entries=True)
    assert state["positions"]["short_dot_4h"]["tf"] == "4h"
