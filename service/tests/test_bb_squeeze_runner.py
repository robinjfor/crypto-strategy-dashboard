"""Z family (bb_squeeze_breakout_perp) runner wiring: code path present but gated (awaiting 資金控管
acceptance: not approvable, not addable to the allocation), perp inputs, stop from fill + workingType,
leverage 1x, conflict flag. Existing families unaffected."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import allocation
import futures_client
import futures_execution
import main
import slots
import strategy
from allocation import slot_to_runtime, validate_allocation, validate_params

Z = "bb_squeeze_breakout_perp"
SPEC = {e["code"]: e for e in json.loads((Path(__file__).resolve().parent / "fixtures" / "bb_squeeze" / "spec.json")
                                         .read_text(encoding="utf-8"))}


def _z_slot(code="Z4", **over):
    e = SPEC[code]
    p = {k: v for k, v in e["params"].items() if k not in ("symbol", "tf")}
    s = {"slot": f"bbsq_{e['params']['symbol'].lower()}_{e['timeframe']}", "family": Z, "strategy_id": e["spec_strategy_id"],
         "symbol": e["symbol"], "timeframe": e["timeframe"], "notional_usdt": 750, "enabled": False, "params": p}
    s.update(over)
    return s


def test_z_accepted_runner_family_approvable():
    assert Z not in allocation.PENDING_REVIEW_FAMILIES
    assert Z in allocation.RUNNER_FAMILIES and Z in slots.SUPPORTED_FAMILIES
    errs = validate_allocation({"book_usdt": 5000, "slots": [_z_slot()]}, check_binance=False)
    assert not any("待資金控管審核" in e for e in errs), errs


def test_repo_backtest_js_shows_z():
    js = (Path(__file__).resolve().parents[2] / "backtest.js").read_text(encoding="utf-8")
    assert 'bb_squeeze_breakout_perp: "bb_squeeze_breakout_perp",' in js
    head = js.split("var RUNNER_FAMILIES", 1)[1].split(";", 1)[0]
    assert "bb_squeeze_breakout_perp: true" in head


def test_repo_catalog_only_z1_z4_approvable():
    import json
    root = Path(__file__).resolve().parents[2]
    cat = json.loads((root / "data/unified-3y/catalog.json").read_text(encoding="utf-8"))
    fam = [f for f in cat["strategies"] if f["strategy_family_id"] == Z]
    assert len(fam) == 1
    rows = {r["code"]: r for r in fam[0]["rows"]}
    assert sorted(rows) == ["Z1", "Z2", "Z3", "Z4", "Z5"]  # Z6 excluded
    flags = json.loads((root / "config/review_flags.json").read_text(encoding="utf-8"))["flags"]
    blocked = {sid for f in flags if f.get("active") and f.get("block_approve") for sid in f.get("strategy_ids") or []}
    shown = [c for c, r in rows.items() if r.get("wf_pass") is not False and r["strategy_id"] not in blocked]
    assert sorted(shown) == ["Z1", "Z4"]
    for c in ("Z2", "Z3", "Z5"):
        assert rows[c]["strategy_id"] in blocked
    assert rows["Z4"]["site_note_zh"] == "和 C5 同幣，C5 啟用時不能開"


@pytest.mark.parametrize("code", ["Z1", "Z2", "Z3", "Z4", "Z5"])
def test_z_spec_params_validate(code):
    errs: list[str] = []
    validate_params(Z, _z_slot(code)["params"], errs, 0)
    assert errs == [], errs


@pytest.mark.parametrize("bad,n", [({"bb_n": 25}, 1), ({"bb_k": 3.0}, 1), ({"sq_q": 0.3}, 1), ({"leverage": 2.0}, 1),
                                   ({"stop_m": 0}, 1), ({"sq_lookback": 100}, 1), ({"atr_mode": "wilder"}, 1),
                                   ({"kind": "short"}, 1)])
def test_z_params_validation_rejects(bad, n):
    errs: list[str] = []
    validate_params(Z, {**_z_slot()["params"], **bad}, errs, 0)
    assert len(errs) == n, errs


def test_z_timeframes():
    assert allocation.FAMILY_TIMEFRAMES[Z] == frozenset({"1h", "4h", "1d"})


def test_slot_to_runtime_z():
    rt = slot_to_runtime(_z_slot("Z1"))
    assert (rt["family"], rt["symbol"], rt["tf"], rt["bb_n"], rt["bb_k"], rt["sq_q"], rt["sq_lookback"], rt["sq_recent"],
            rt["stop_atr_mult"], rt["trail_atr_mult"], rt["max_hold_bars"], rt["atr_mode"], rt["leverage"]) == \
        (Z, "WLDUSDT", "4h", 20, 2.5, 0.2, 120, 5, 1.5, 3.5, 36, "sma", 1.0)
    assert slots.venue_for_family(Z) == "futures"
    other = slot_to_runtime({"slot": "e", "family": "ema_cross_atr", "symbol": "SOLUSDT", "timeframe": "1d",
                             "notional_usdt": 1000, "params": {"fast": 12, "slow": 26, "stop_m": 2, "trail_m": 3}})
    assert "bb_n" not in other
    don = slot_to_runtime({"slot": "d", "family": "donchian_atr", "symbol": "OPUSDT", "timeframe": "4h",
                           "notional_usdt": 1000, "params": {"donch_n": 20, "stop_m": 1.5, "trail_m": 1.5}})
    assert don["atr_mode"] == "wilder"


def test_dispatch_routes_z():
    idx = pd.date_range("2024-01-01", periods=200, freq="4h", tz="UTC")
    kl = pd.DataFrame({"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0}, index=idx)
    rt = {**slot_to_runtime(_z_slot("Z4")), "id": "bbsq_dot_4h"}
    r = strategy.evaluate_slot_dispatch(rt, kl, None, {})
    assert (r["family"], r["side"], r["venue"], r["stop_working_type"], r["leverage"]) == (Z, "LONG", "futures", "CONTRACT_PRICE", 1.0)
    assert r["action"] == "armed" and r["reason"] == "waiting_bb_squeeze_breakout"


def test_bb_squeeze_inputs(monkeypatch):
    import perp_market
    calls = []
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}, index=idx)
    monkeypatch.setattr(perp_market, "fetch_perp_klines", lambda s, tf, limit=0: calls.append((s, tf, limit)) or df)
    slot = {"id": "bbsq_arb_4h", "symbol": "ARBUSDT", "tf": "4h"}
    _, m = strategy._bb_squeeze_inputs(slot, {"x": 1}, {"short_arb_4h": {"status": "FILLED", "symbol": "ARBUSDT", "side": "SHORT"}})
    assert calls == [("ARBUSDT", "4h", 1500)] and m["_opposite_open"] is True and m["x"] == 1
    _, m2 = strategy._bb_squeeze_inputs(slot, {}, {"sat_arb": {"status": "FILLED", "symbol": "ARBUSDT", "side": "LONG"}})
    assert m2["_opposite_open"] is False


class _FakeFC:
    def user_trades(self, *a, **k):
        return []


def test_apply_signal_z_entry_stop_from_fill(monkeypatch):
    fc = _FakeFC()
    seen = {}
    monkeypatch.setattr(futures_client, "FuturesDemoClient", lambda: fc)
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))

    def fake_open(c, **kw):
        seen["open"] = kw
        return {"order": {"orderId": 1}, "qty": 200.0, "mark": 3.71, "avg_price": 3.70, "side": "BUY",
                "leverage": 1, "notional_usdt": 740.0}

    monkeypatch.setattr(futures_execution, "open_market", fake_open)
    monkeypatch.setattr(futures_execution, "place_stop_reduce_only", lambda c, **kw: seen.update(stop=kw) or {"algoId": 9})
    sig = {"slot": "bbsq_dot_4h", "symbol": "DOTUSDT", "action": "enter", "family": Z, "venue": "futures", "side": "LONG",
           "quote_usdt": 750.0, "bar_ts": "2026-10-21T00:00:00+00:00", "leverage": 3.0, "stop_atr_mult": 1.5,
           "stop_atr": 0.1, "suggested_stop": 9.9, "close": 3.69}
    state: dict = {}
    out = main.apply_signal(None, state, sig, live=True, allow_entries=True)
    assert out["executed"]
    pos = state["positions"]["bbsq_dot_4h"]
    assert pos["stop_basis"] == pytest.approx(3.70 * 1.002)
    assert pos["stop"] == pytest.approx(3.70 * 1.002 - 0.15) == seen["stop"]["stop_price"]
    assert pos["family"] == Z and pos["stop_working_type"] == "CONTRACT_PRICE"
    assert seen["stop"]["working_type"] == "CONTRACT_PRICE" and seen["stop"]["is_long"] is True
    assert seen["open"]["side"] == "BUY" and seen["open"]["leverage"] == 1 and seen["open"]["leverage_cap"] == 1
    assert state["slots"]["bbsq_dot_4h"]["last_entry_bar_ts"] == sig["bar_ts"]


def test_z_long_blocked_by_open_short_same_coin(monkeypatch):
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))
    state = {"positions": {"short_arb_4h": {"status": "FILLED", "symbol": "ARBUSDT", "side": "SHORT", "venue": "futures"}}}
    sig = {"slot": "bbsq_arb_4h", "symbol": "ARBUSDT", "action": "enter", "family": Z, "venue": "futures", "side": "LONG",
           "quote_usdt": 750.0, "bar_ts": "2026-10-21T00:00:00+00:00"}
    out = main.apply_signal(None, state, sig, live=False, allow_entries=True)
    assert out["reason"] == "opposite_position_open" and out["blocked_by"] == "short_arb_4h"


def test_unapproved_z_never_orders(monkeypatch):
    """Even if a Z slot existed, an entry without approval is skipped (no order)."""
    sig = {"slot": "bbsq_dot_4h", "symbol": "DOTUSDT", "action": "enter", "family": Z, "venue": "futures", "side": "LONG",
           "quote_usdt": 750.0, "bar_ts": "2026-10-21T00:00:00+00:00"}
    out = main.apply_signal(None, {}, sig, live=True, allow_entries=True)
    assert out["executed"] is False and out["reason"] in ("unknown_slot_strategy", "not_approved")


def test_api_rule_text_and_trigger():
    import api
    rt = slot_to_runtime(_z_slot("Z1"))
    txt = api._entry_condition(rt)
    assert "BB(20,2.5)" in txt and "0.2 分位" in txt and "1.5×ATR14" in txt and "3.5×ATR14" in txt and "36 根" in txt
    tf = api._trigger_fields(rt, {"bb_up": 1.1, "bb_mid": 1.0, "mark": 1.0, "squeeze_recent": True, "bb_state": 0})
    assert tf["trigger_kind"] == "bb_squeeze" and tf["dist_up_pct"] == pytest.approx(10.0)
    assert api.status_zh("waiting_bb_squeeze_breakout").startswith("等布林壓縮")


def test_same_direction_second_position_same_coin_blocked(monkeypatch):
    """Emily 2026-10-21: C5 spot DOT long blocks Z4 perp DOT long (any slot/market/quote, same direction)."""
    monkeypatch.setattr(main, "_allow_entry_for_slot", lambda *a, **k: (True, ""))
    sig = {"slot": "bbsq_dot_4h", "symbol": "DOTUSDT", "action": "enter", "family": Z, "venue": "futures", "side": "LONG",
           "quote_usdt": 750.0, "bar_ts": "2026-10-21T00:00:00+00:00"}
    for holder in ({"status": "FILLED", "symbol": "DOTUSDT", "side": "LONG", "venue": "spot"},
                   {"status": "FILLED", "symbol": "DOTUSDC", "side": "LONG", "venue": "futures"}):
        state = {"positions": {"c5_dot": holder}}
        out = main.apply_signal(None, state, sig, live=False, allow_entries=True)
        assert out["reason"] == "same_coin_position_open" and out["blocked_by"] == "c5_dot"
        assert "bbsq_dot_4h" not in state["positions"]
    # flat / pending holder or other coin does not block
    state = {"positions": {"c5_dot": {"status": "CLOSED", "symbol": "DOTUSDT", "side": "LONG"},
                           "x": {"status": "FILLED", "symbol": "ARBUSDT", "side": "LONG"}}}
    assert main.same_coin_position_slot(state, "bbsq_dot_4h", "DOTUSDT", "LONG") is None
    assert "intended_order" in main.apply_signal(None, state, sig, live=False, allow_entries=True)
    # U1 sleeve long blocks another BTC long; a spot BTC long blocks U1 increases
    assert main.same_coin_position_slot({"u1_live": {"units": {"BTC": 0.01}}}, "s", "BTCUSDT", "LONG") == "u1_live"
    import u1_live as L
    st = {"positions": {"btc_spot": {"status": "FILLED", "symbol": "BTCUSDT", "side": "LONG", "venue": "spot"}}}
    assert L.coin_conflicts(st)["BTC"] == "same_coin_position:btc_spot"
