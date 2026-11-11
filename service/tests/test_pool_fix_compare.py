"""USD pool includes USDC and FA. ETH still nets to one order. No flatten, no VBO add."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

import orchestrator as O

_spec = importlib.util.spec_from_file_location(
    "pool_fix_compare", Path(__file__).resolve().parents[1] / "shadow_ab" / "pool_fix_compare.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)


def _status():
    return {
        "demo_e5v_exec": {"cash": 6640.62, "book": {"ETHUSDT": -0.841, "SOLUSDT": -3.0}},
        "demo_r2s_exec": {"cash": 4216.94, "book": {"ETHUSDT": -0.071}},
        "u1_live": {"quote": "USDC", "cash": 69.93, "units": {"BTC": 0.01, "ETH": 0.0}, "slot": "u1_btc_eth_usdc"},
        "demo_fa_positions": {},
        "liq_distance": {"positions": [
            {"symbol": "ETHUSDT", "qty": -0.912, "mark": 2053.82},
            {"symbol": "SOLUSDT", "qty": -3.0, "mark": 100.0},
            {"symbol": "BTCUSDC", "qty": 0.01, "mark": 73854.69},
        ]},
        "net_reconcile": {"symbols": {
            "ETHUSDT": {"exchange": -0.912},
            "SOLUSDT": {"exchange": -3.0},
        }},
        "orchestrator": {"regime": {"bear": False, "hivol": False}},
    }


def test_usd_pool_adds_usdc_and_fa_and_leaves_vbo_out(monkeypatch):
    O.load({})
    monkeypatch.setattr(O, "regime", lambda c: {"bar": "2026-11-11", "bear": False, "hivol": False})
    st = _status()
    doc = C.compare(st)
    eq = doc["equities"]
    assert doc["pool_before"] == pytest.approx(eq["E"] + eq["R"], abs=0.02)
    assert doc["pool_after"] == pytest.approx(eq["E"] + eq["R"] + eq["U"] + eq["F"], abs=0.02)
    assert doc["added"]["vbo_added"] == 0.0
    assert doc["targets_after"]["E"] == pytest.approx(round(doc["pool_after"] * 0.85, 2), abs=0.02)
    assert doc["targets_after"]["U"] == 0.0 and doc["targets_after"]["F"] == 0.0
    assert doc["targets_after"]["R"] <= 1464.08
    plan = O.plan([1.0], {k: eq[k] for k in "ERUF"}, None, datetime(2026, 11, 12, 0, 5, tzinfo=timezone.utc))
    assert plan["targets"]["demo_e5_min5_x175_t05top20"] == pytest.approx(doc["targets_after"]["E"], abs=0.02)
    assert plan["eligible"]["vbo_in_pool"] is False
    assert plan["eligible"]["accounting"] == "usd_1to1"


def test_weight0_books_are_not_flattened_and_eth_is_one_order():
    doc = C.compare(_status())
    u = [r for r in doc["symbols"] if r["sleeve"] == "U"]
    assert u and u[0]["symbol"] == "BTCUSDC" and u[0]["target_qty_after"] == u[0]["book_qty"] == 0.01
    eth = doc["ethusdt"]
    assert eth["sleeves"] == ["u1"]
    assert eth["n_orders"] == 1
    assert eth["orders"][0]["side"] == "BUY" and eth["orders"][0]["reduce_only"] is True
    assert eth["orders"][0]["qty"] == pytest.approx(0.841)
    assert eth["flipped"] is False
    assert eth["exchange_after"] == pytest.approx(-0.071)
    assert eth["e5_book_after"] == 0.0
