"""Bridge residual stays visible. No network and no ledger writes."""
from __future__ import annotations

import hashlib
from pathlib import Path

import review_bridge as B

ROOT = Path(__file__).resolve().parents[1]
# Recorded on the round-2 branch. A later change to these files fails this test.
UNTOUCHED = {
    "netting.py": "503853f612cc1f67b305c01a6d2600c7348e0ddb2365f6cb69c48a5c24b23854",
    "demo_e5_exec.py": "f4e623a4ab4ff9bf9908dd3c50df5ef76f347847503232adc6673790355f7c1d",
    "orchestrator.py": "10ceece7a729aed29747e22f2cc6a9ebbe226d1acf2284bf2dcdf80bb6cf4a6d",
    "reconcile.py": "6dca2fcc9117e38e22c43db42625230bb1e0cbfc3053c45557ad911315351cd0",
    "e5_margin.py": "a7d5af12201472f52454bca8ef13846873a07f599334f15144f0015f831e85a9",
}


def _status():
    return {
        "updated_at": "2026-11-17T12:00:00+08:00",
        "capital_basis": {"nav": 9000.0, "nav_usdt_m": 4000.0, "nav_usdc_m": 5000.0, "starting_total": 10000.0},
        "books": {
            "usdt": {"futures": {"wallet": 4000.0, "margin_balance": 4000.0}},
            "usdc": {"futures": {"wallet": 5000.0, "margin_balance": 5000.0}},
        },
        "paper": {"nav": 50.0},
        "positions": [],
    }


def _state():
    return {
        "demo_e5v_exec": {"cash": 3000.0, "book": {}},
        "demo_vbo1h_exec": {"cash": 1500.0, "book": {}},
        "demo_r2s_exec": {"cash": 493.19, "book": {}},
        "migrations": {"r2s_unfunded_principal_20261111": {"done": True}},
        "u1_live": {"cash": 1000.0, "capital_usdt": 1000.0, "quote": "USDC", "units": {}, "slot": "u1_live"},
        "orchestrator": {"targets": {"demo_e5_min5_x175_t05top20": 1986.13, "u1_live": 1000.0}},
    }


def test_nav_matches_status_and_the_overcount_is_not_absorbed():
    before = _state()
    bridge = B.build_bridge(_status(), before)
    assert bridge["nav"]["value"] == 9000.0
    assert abs(bridge["nav"]["value"] - _status()["capital_basis"]["nav"]) < 0.01
    assert bridge["nav"]["env"] == "demo"
    assert bridge["usdt_book_over_wallet"]["value"] == 993.19
    assert bridge["unallocated_usdc"]["value"] == 4000.0
    assert bridge["unallocated_cash"]["value"] == 4000.0
    assert bridge["bridge_diff_value"] == -993.19
    assert bridge["bridge_diff_red"] is True
    assert bridge["absorbed"] is False
    assert bridge["paper_nav"]["env"] == "paper"
    assert bridge["paper_nav"]["value"] == 50.0
    assert bridge["paper_in_demo_nav"] is False
    assert bridge["nav"]["value"] == 9000.0
    assert bridge["real"]["display"] == "尚未開通"
    assert bridge["real"]["label_zh"] == "尚未開通"
    e5 = next(s for s in bridge["strategies"] if s["code"] == "E5")
    assert e5["virtual_capital"] == 5000.0
    assert e5["orchestrator_target"] == 1986.13
    assert e5["strategy_capital_basis_label"] == "orchestrator_target"
    assert before["demo_e5v_exec"]["cash"] == 3000.0


def test_missing_wallet_is_missing_not_zero():
    row = B.figure(None, formula="x", source="none", as_of=None)
    assert row["display"] == "缺值"
    assert row["label_zh"] == "缺值"
    assert row["value"] is None
    bridge = B.build_bridge({"capital_basis": {}}, {})
    assert bridge["nav"]["display"] == "缺值"
    assert bridge["bridge_diff_value"] is None
    assert bridge["bridge_diff_red"] is False


def test_trading_modules_are_unchanged():
    for name, digest in UNTOUCHED.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    src = (ROOT / "review_mcp.py").read_text(encoding="utf-8")
    assert "/control/revoke" not in src
