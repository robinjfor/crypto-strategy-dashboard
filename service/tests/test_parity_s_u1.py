"""Daily parity extended to S (short_trend_perp) and U1; bt_short_trend == analyst reference engine."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference_short_trend as R  # noqa: E402

import bt_short_trend as S  # noqa: E402
import live_vs_backtest as lvb  # noqa: E402

ROWS = {"S2": ("ARB", "4h", 0, 2.0, 5.0, "fpos", 60), "S8": ("GALA", "6h", 1, 3.0, 5.0, "none", 40),
        "S5": ("APT", "4h", 1, 2.0, 2.0, "coin50", 60)}


@pytest.mark.parametrize("code", list(ROWS))
def test_bt_short_trend_port_equals_reference(code):
    coin, tf, pair, sm, tm, filt, mh = ROWS[code]
    ref = R.Data(coin, tf)
    ent, *x = R.signals(ref, "sema", {"pair": pair, "filt": filt})
    want, _ = R.bt(ref, ent, tuple(x), sm, tm, mh)
    D = S.Data(R.load(coin, tf), R.load_funding(coin), R.load("BTC", "1d"), R.load(coin, "1d"), tf, start=R.START)
    ent2, *x2 = S.signals(D, "sema", {"pair": pair, "filt": filt})
    got, _ = S.bt(D, ent2, tuple(x2), sm, tm, mh)
    assert len(want) > 5
    assert [(t["entry"], t["exit"], t["reason"], round(t["entry_px"], 10)) for t in got] == \
           [(t["entry"], t["exit"], t["reason"], round(t["entry_px"], 10)) for t in want]


def test_next_open_compare_short():
    bar = pd.Timestamp("2026-10-20T08:00:00Z")
    bt = [{"entry_bar": bar, "exit_bar": bar + pd.Timedelta(hours=8), "side": "SHORT", "reason": "stop_loss",
           "entry_px": 0.30, "exit_px": 0.31}]
    live = {"entry_ms": int((bar + pd.Timedelta(minutes=1)).timestamp() * 1000),
            "exit_ms": int((bar + pd.Timedelta(hours=8, minutes=30)).timestamp() * 1000),
            "entry_px": 0.3003, "exit_px": 0.3101, "side": "SHORT", "reason": "stop"}
    r = lvb.compare_trade(live, bt, "4h", "next_open")
    assert r["match"], r
    assert not lvb.compare_trade({**live, "side": "LONG"}, bt, "4h", "next_open")["match"]


def _old_u1_rows_vs_paper():
    st = {"u1_live": {"slot": "u1_btc_eth_usdc", "capital_usdt": 1000.0, "fills": [
        {"symbol": "BTCUSDC", "qty_signed": 0.011, "price": 82901.8, "units_after": 0.011,
         "filled_at": "2026-10-21T00:01:28+00:00"},
        {"symbol": "ETHUSDC", "qty_signed": -0.361, "price": 2466.48, "units_after": 0.0, "external": True,
         "filled_at": "2026-10-21T12:08:19+00:00"}]}}
    paper = {"fills": [{"symbol": "BTCUSDT", "book": "lev", "fill_time_utc": "2026-10-21T00:00:00+00:00",
                        "price": 82890.0, "qty_delta": 0.1, "target_weight": 0.91, "equity_before": 10000}]}
    rows = lvb.u1_rows(st, paper)
    assert len(rows) == 1 and rows[0]["match"], rows  # external (人工單) close skipped
    paper["fills"][0]["target_weight"] = 0.5
    assert lvb.u1_rows(st, paper)[0]["problems"] == ["weight"]
    assert lvb.u1_rows(st, {"fills": []})[0]["problems"] == ["no_paper_fill_at_bar"]


def test_recent_mismatch_pauses_old_does_not(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)
    st = {"closed_trades": [], "u1_live": {"slot": "u1_btc_eth_usdc", "capital_usdt": 1000.0, "fills": [
        {"symbol": "BTCUSDC", "qty_signed": 0.011, "price": 82901.8, "units_after": 0.011,
         "filled_at": "2026-10-21T00:01:28+00:00"},
        {"symbol": "ETHUSDC", "qty_signed": 0.36, "price": 2443.0, "units_after": 0.36,
         "filled_at": "2026-10-15T00:01:28+00:00"}]}}
    rep = lvb.run(st, {"fills": []}, client=None, slot_lookup=lambda i: None, now=now, paper_u1={"decisions": [
        {"fill_bar": "2026-10-21T00:00:00+00:00", "targets_lev": {"BTC": 0.2, "ETH": 0.2}},
        {"fill_bar": "2026-10-15T00:00:00+00:00", "targets_lev": {"BTC": 0.2, "ETH": 0.2}}]})
    by = {r["closed_at"][:10]: r for r in rep["rows"]}
    assert by["2026-10-21"]["pause"] is True and by["2026-10-15"]["pause"] is False
    out = lvb.apply({"paused": True, "paused_slots": {}, "approved": ["x"]}, rep)
    assert list(out["paused_slots"]) == ["u1_btc_eth_usdc"] and out["approved"] == ["x"]


def test_u1_rows_use_paper_decision_not_paper_fills():
    st = {"u1_live": {"slot": "u1_btc_eth_usdc", "capital_usdt": 1000.0, "fills": [
        {"symbol": "BTCUSDC", "qty_signed": 0.011, "price": 82901.8, "units_after": 0.011, "equity_before": 1000.0,
         "fill_bar": "2026102100", "filled_at": "2026-10-21T00:01:28+00:00"}]}}
    paper = {"fills": [], "decisions": [{"decision_bar": "2026-10-20T23:00:00+00:00",
                                         "fill_bar": "2026-10-21T00:00:00+00:00",
                                         "targets_lev": {"BTC": 0.89, "ETH": 0.89}}]}
    r = lvb.u1_rows(st, paper)[0]
    assert r["match"] is True and r["bt_weight"] == 0.89, r
    paper["decisions"][0]["targets_lev"]["BTC"] = 0.3
    assert lvb.u1_rows(st, paper)[0]["problems"] == ["weight"]
    assert lvb.u1_rows(st, {"decisions": []})[0]["match"] is None


def test_parity_summary_in_status():
    import api
    s = api._parity_summary({})
    assert s["status"] == "never_run"
    st = {"live_vs_backtest": {"at": "t", "day_utc": "d", "n_match": 2, "n_mismatch": 1, "rows": [{"pause": True}]},
          "parity_status": {"last_attempt_at": "t", "last_error": None}}
    s = api._parity_summary(st)
    assert s["status"] == "mismatch" and s["n_paused_now"] == 1 and s["last_run_at"] == "t"
    st["parity_status"]["last_error"] = "boom"
    assert api._parity_summary(st)["status"] == "error"
