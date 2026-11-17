"""Strategy card footer keeps the bridge difference."""
import review_strategy as S


def test_footer_uses_the_unabsorbed_diff_and_missing_stays_missing():
    status = {
        "updated_at": "2026-11-17T12:00:00+08:00",
        "runner_version": "abc",
        "capital_basis": {"nav": 9000.0, "nav_usdt_m": 4000.0, "nav_usdc_m": 5000.0},
        "books": {
            "usdt": {"futures": {"wallet": 4000.0}},
            "usdc": {"futures": {"wallet": 5000.0}},
        },
        "recent_fills": [],
        "closed_trades": [],
    }
    state = {
        "demo_e5v_exec": {"cash": 3000.0, "book": {}, "fees": 1.0},
        "demo_vbo1h_exec": {"cash": 1500.0, "book": {}},
        "demo_r2s_exec": {"cash": 493.19, "book": {}},
        "migrations": {"r2s_unfunded_principal_20261111": {"done": True}},
        "u1_live": {"cash": 1000.0, "capital_usdt": 1000.0, "quote": "USDC", "units": {}},
        "orchestrator": {"targets": {"demo_e5_min5_x175_t05top20": 1986.13}, "weights": {"demo_e5_min5_x175_t05top20": 1.0}},
    }
    card = S.detail(status, state, "E5")
    assert card["strategy_capital_basis"] == 1986.13
    assert card["strategy_capital_basis_label"] == "orchestrator_target"
    assert card["weight"] == 1.0
    assert card["realized"] == "缺值"
    assert card["unrealized"] == "缺值"
    assert card["turnover_30d"] == "缺值"
    foot = card["footer"]
    assert foot["text_zh"].startswith("策略權益合計")
    assert foot["bridge_diff"] == 6.81
    assert foot["unallocated_cash"] == 4000.0
    assert foot["unallocated_usdt_unexplained"] == 6.81
    assert foot["unallocated_usdt_warning"]["show"] is True
    assert foot["unallocated_usdt_warning"]["level"] == "yellow"
    assert foot["unallocated_usdt_warning"]["label_zh"] == "未經 income 對到"
    assert card["equity"] == 2000.0
    assert foot["bridge_diff_red"] is True
    assert abs((foot["sum_strategy_equity"] + foot["unallocated_cash"] + foot["bridge_diff"]) - foot["nav"]) < 0.01
