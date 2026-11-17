"""Read-only review figures. No network, no orders, no margin changes."""
from __future__ import annotations

import json

import pytest

import review_audit


def _state():
    return {
        "demo_e5v_exec": {
            "cash": 4200.0, "fees": 2.0, "funding": 1.0,
            "book": {"APEUSDT": 10.0, "COTIUSDT": 4.0, "ETHUSDT": 1.0},
        },
        "demo_vbo1h_exec": {
            "cash": 900.0, "fees": 0.5, "funding": -0.2,
            "book": {"XRPUSDT": 100.0, "SOLUSDT": -3.0, "DOGEUSDT": -1000.0},
        },
        "demo_r2s_exec": {"cash": 50.0, "fees": 0.1, "funding": 0.0, "book": {"ETHUSDT": -0.4}},
        "migrations": {"r2s_unfunded_principal_20261111": {"done": True}},
        "u1_live": {
            "cash": 4800.0, "capital_usdt": 5000.0, "fees": 3.0, "funding": 0.4,
            "quote": "USDC", "units": {"BTC": 0.01, "ETH": 0.0},
            "strategy_id": "u1", "slot": "u1_btc_eth_usdc",
        },
        "external_closes": {
            "xclose-AIOT": {
                "symbol": "AIOTUSDT", "cause": "liquidation", "at": "2026-11-17T03:47:00+08:00",
                "vwap": 0.0792, "realized_pnl": -4.2, "trade_ids": ["9001"],
                "legs": [{"slot": "demo_e5_min5_x175_t05top20", "delta": -80.0, "qty_before": 80.0}],
            }
        },
        "live_vs_backtest_history": [{
            "rows": [
                {"slot": "sat_fil_1h", "symbol": "FILUSDT", "code": "C2", "closed_at": "2026-10-04T03:03:00+08:00",
                 "match": False, "entry_bps": 12.0},
                {"slot": "sat_dot_4h", "symbol": "DOTUSDT", "code": "C5", "closed_at": "2026-10-09T01:00:00+08:00",
                 "match": False, "problems": ["exit_price"], "exit_bps": -40.0},
                {"slot": "sat_op_4h", "symbol": "OPUSDT", "code": "C3", "closed_at": "2026-10-22T02:00:00+08:00",
                 "match": True, "entry_bps": 4.0},
            ]
        }],
    }


def _status():
    return {
        "updated_at": "2026-11-17T12:00:00+08:00",
        "capital_basis": {"starting_total": 10000, "nav": 10050, "nav_usdt_m": 5000, "nav_usdc_m": 5050},
        "nav_history": [["2026-10-01", 10000], ["2026-10-02", 9900], ["2026-10-03", 9950]],
        "positions": [
            {"raw_symbol": "APEUSDT", "symbol": "APEUSDT", "mark": 2.0, "entry": 1.8, "side": "LONG",
             "qty": 10, "leverage": 3, "liq": 1.2, "venue": "futures", "margin_type": "isolated"},
            {"raw_symbol": "COTIUSDT", "symbol": "COTIUSDT", "mark": 0.5, "side": "LONG",
             "qty": 4, "leverage": 2, "venue": "futures"},
            {"raw_symbol": "XRPUSDT", "symbol": "XRPUSDT", "mark": 0.5, "side": "LONG",
             "qty": 100, "leverage": 3, "liq": 0.3, "venue": "futures", "margin_type": "isolated"},
            {"raw_symbol": "SOLUSDT", "symbol": "SOLUSDT", "mark": 150.0, "side": "SHORT",
             "qty": 3, "leverage": 2, "venue": "futures"},
            {"raw_symbol": "DOGEUSDT", "symbol": "DOGEUSDT", "mark": 0.1, "side": "SHORT",
             "qty": 1000, "leverage": 2, "venue": "futures"},
            {"raw_symbol": "ETHUSDT", "symbol": "ETHUSDT", "mark": 3000.0, "side": "LONG",
             "qty": 0.6, "leverage": 2, "venue": "futures"},
            {"raw_symbol": "BTCUSDC", "symbol": "BTCUSDC", "mark": 90000.0, "side": "LONG",
             "qty": 0.01, "leverage": 3, "margin_type": "cross", "venue": "futures"},
            {"raw_symbol": "NEARUSDT", "symbol": "NEARUSDT", "mark": 5.0, "side": "LONG",
             "qty": 2, "leverage": 2, "unrealized_pnl": 1.5, "venue": "futures"},
        ],
        "recent_fills": [
            {"symbol": "BTCUSDT", "time": "2026-11-17T11:00:00+08:00", "side": "BUY", "qty": 0.01,
             "price": 90100.0, "target_price": 90000.0, "id": 1},
        ],
        "closed_trades": [],
        "live_vs_backtest": {"rows": []},
    }


def test_owners_leverage_and_vbo_3x_are_read_only():
    state = _state()
    before = json.dumps(state, sort_keys=True)
    view = review_audit.annotate_positions(_status(), state)
    assert json.dumps(state, sort_keys=True) == before
    by = {r["raw_symbol"]: r for r in view["positions"]}
    assert by["APEUSDT"]["owner"] == "E5"
    assert by["APEUSDT"]["strategy"] == "demo_e5_min5_x175_t05top20"
    assert by["COTIUSDT"]["owner"] == "E5"
    assert by["XRPUSDT"]["owner"] == "VBO"
    assert by["SOLUSDT"]["owner"] == "VBO"
    assert by["DOGEUSDT"]["owner"] == "VBO"
    assert by["BTCUSDC"]["owner"] == "U1"
    assert by["NEARUSDT"]["owner"] == "unowned"
    assert by["NEARUSDT"]["strategy"] == "unowned"
    eth = by["ETHUSDT"]
    assert eth["owner"] == [{"owner": "E5", "qty": 1.0}, {"owner": "R2S", "qty": -0.4}]
    assert eth["strategy"] == [
        {"strategy": "demo_e5_min5_x175_t05top20", "qty": 1.0},
        {"strategy": "demo_r2s_top10_3k", "qty": -0.4},
    ]
    assert by["XRPUSDT"]["leverage"] == 3
    assert by["COTIUSDT"]["leverage"] == 2
    assert view["vbo_3x"] == [{
        "symbol": "XRPUSDT", "owner": "VBO", "strategy": "demo_vbo1h_top50_1k",
        "slot": "demo_vbo1h_top50_1k", "qty": 100.0, "leverage": 3,
        "liq": 0.3, "liq_distance": 0.4, "mark": 0.5, "side": "LONG",
        "margin_type": "isolated", "read_only": True,
    }]
    assert view["e5_owned_3x"][0]["symbol"] == "APEUSDT"
    assert view["e5_owned_3x"][0]["read_only"] is True
    assert view["e5_topup"]["includes_e5_owned_3x"] is True
    assert "demo_e5v_exec" in view["e5_topup"]["note"]
    assert "3x" in view["e5_topup"]["note"]
    src = open(review_audit.__file__, encoding="utf-8").read()
    for banned in ("new_order(", "set_leverage(", "positionMargin", "/control/revoke"):
        assert banned not in src


def test_capital_bridge_identity_and_residual():
    bridge = review_audit.capital_bridge(_status(), _state())
    by = {s["code"]: s for s in bridge["strategies"]}
    assert bridge["start"] == 10000
    assert by["E5"]["equity"] == 4220 + 4 * 0.5 + 1 * 3000
    # 4200 + 10*2 + 4*0.5 + 1*3000 = 4200+20+2+3000 = 7222
    assert by["E5"]["equity"] == 7222
    assert by["E5"]["net_pnl"] == 7222 - 5000
    assert by["E5"]["fees"] == 2
    assert by["E5"]["funding"] == 1
    assert by["VBO"]["equity"] == 900 + 100 * 0.5 + (-3) * 150 + (-1000) * 0.1
    assert by["VBO"]["net_pnl"] == pytest.approx(by["VBO"]["equity"] - 1000)
    assert by["R2S"]["capital"] == 0
    assert by["R2S"]["equity"] == 50 + (-0.4) * 3000
    assert by["U1"]["equity"] == 4800 + 0.01 * 90000
    assert by["U1"]["net_pnl"] == 700
    check = bridge["check"]
    assert check["sum_ledgers"] == pytest.approx(
        by["E5"]["equity"] + by["VBO"]["equity"] + by["R2S"]["equity"] + by["U1"]["equity"]
    )
    assert check["unallocated"] == 1.5
    assert check["wallet"] == 10050
    assert check["residual"] == pytest.approx(10050 - (check["sum_ledgers"] + 1.5))
    assert check["residual"] != 0
    assert "1000" in by["VBO"]["note"]


def test_reconciliation_splits_on_fix_dates_and_labels_apt():
    rep = review_audit.reconciliation(_status(), _state())
    assert [f["date"] for f in rep["fixes"]] == ["2026-10-08", "2026-10-21"]
    apt = rep["apt_1003"]
    assert apt["day"] == "2026-10-03" and apt["symbol"] == "APTUSDT"
    assert apt["match"] is False
    assert "9e6f7db" in apt["reason"] or "2026-10-08" in apt["reason"]
    assert all(r.get("reason") for r in rep["mismatches"])
    fil = next(r for r in rep["mismatches"] if r["symbol"] == "FILUSDT")
    assert "停損" in fil["reason"] or "10/21" in fil["reason"] or "20bps" in fil["reason"]
    dot = next(r for r in rep["mismatches"] if r["symbol"] == "DOTUSDT")
    assert dot["reason"] == "exit_price"
    before, middle, after = rep["periods"]
    assert before["until"] == "2026-10-08"
    assert any(r["symbol"] == "APTUSDT" and r["day"] == "2026-10-03" for r in before["mismatches"])
    assert middle["from"] == "2026-10-08" and middle["until"] == "2026-10-21"
    assert any(r["symbol"] == "DOTUSDT" for r in middle["mismatches"])
    assert after["from"] == "2026-10-21"
    assert after["n_match"] == 1 and after["match_rate"] == 1
    assert rep["rolling_match_rate"][-1]["symbol"] == "OPUSDT"
    assert rep["rolling_match_rate"][-1]["match_rate"] == 0.2
    days = {d["day"]: d["mean_bps"] for d in rep["slippage"]["daily"]}
    assert days["2026-11-17"] == pytest.approx((90100 / 90000 - 1) * 1e4, abs=0.02)


def test_risk_stress_caps_isolated_liquidation_and_worst_day():
    status = {
        "nav_history": [["2026-10-01", 10000], ["2026-10-02", 9900], ["2026-10-03", 9950]],
        "positions": [{
            "raw_symbol": "XRPUSDT", "mark": 0.32, "entry": 0.5, "side": "LONG", "qty": 100,
            "leverage": 3, "liq": 0.3, "margin_type": "isolated", "venue": "futures",
            "isolated_wallet": 16.67,
        }],
    }
    state = {"demo_vbo1h_exec": {"cash": 900, "book": {"XRPUSDT": 100}}}
    risk = review_audit.portfolio_risk(status, state)
    assert risk["display_only"] is True
    assert risk["gross_long"] == pytest.approx(32)
    assert risk["gross_short"] == 0
    assert risk["net_exposure"] == pytest.approx(32)
    assert risk["stress_down_30"]["liquidations"] == 1
    assert risk["stress_down_30"]["pnl"] == pytest.approx(-(16.67 + 0.0005 * 100 * 0.3), abs=0.02)
    assert risk["stress_up_30"]["liquidations"] == 0
    assert risk["stress_up_30"]["pnl"] == pytest.approx(100 * (0.32 * 1.3 - 0.32), abs=0.02)
    assert risk["worst_day"] == "2026-10-02"
    assert risk["worst_day_loss"] == 100


def test_liquidation_fill_is_flagged_with_taipei_time():
    from review_mcp import get_trades
    state = _state()
    status = _status()
    out = get_trades(status, state, {"limit": 10})
    liq = [t for t in out.body["trades"] if t.get("symbol") == "AIOTUSDT"]
    assert len(liq) == 1
    assert liq[0]["liquidation"] is True
    assert liq[0]["time"] == "2026-11-17T03:47:00+08:00"
    assert liq[0]["price"] == 0.0792
    assert liq[0]["owner"] == "E5"
    plain = next(t for t in out.body["trades"] if t.get("symbol") == "BTCUSDT")
    assert plain["liquidation"] is False
