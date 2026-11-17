"""Risk page flags 3x and effective leverage above 1.75. Display only."""
import review_audit
import review_riskpage as P


def test_flags_and_separate_drawdowns():
    status = {
        "capital_basis": {"nav": 10000.0, "current_drawdown": 0.025, "max_drawdown_pct": 0.0293},
        "positions": [
            {"raw_symbol": "XRPUSDT", "mark": 0.5, "entry": 0.5, "side": "LONG", "qty": 300,
             "leverage": 3, "liq": 0.2, "margin_type": "isolated", "venue": "futures", "isolated_wallet": 50.0},
            {"raw_symbol": "SOLUSDT", "mark": 100.0, "entry": 100.0, "side": "SHORT", "qty": 1,
             "leverage": 2, "margin_type": "isolated", "venue": "futures", "isolated_wallet": 40.0},
        ],
        "paused": False,
        "guards": {"entry_blocked": False},
    }
    state = {"demo_vbo1h_exec": {"cash": 1000, "book": {"XRPUSDT": 300, "SOLUSDT": -1}}}
    risk = review_audit.portfolio_risk(status, state)
    body = P.page(status, state, risk)
    assert body["current_drawdown"] == 0.025
    assert body["max_drawdown_pct"] == 0.0293
    assert body["current_drawdown"] != body["max_drawdown_pct"]
    by = {r["symbol"]: r for r in body["legs"]}
    assert "3x" in by["XRPUSDT"]["flags"]
    # |qty| * entry / wallet = 300 * 0.5 / 50 = 3 > 1.75
    assert "eff_gt_1_75" in by["XRPUSDT"]["flags"]
    assert by["XRPUSDT"]["effective_leverage"] == 3.0
    assert body["correlation"]["display"] == "missing"
    assert body["halt"] == "not_halted"
    assert body["stress_loss_short_squeeze"] != "missing"
    assert body["short_squeeze"]["loss"] > 0
