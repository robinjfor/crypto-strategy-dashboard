"""Family approval must unlock allocation-enabled slots even if in DEFAULT_SIGNAL_ONLY."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slots import approval_mode, DEFAULT_SIGNAL_ONLY


FET_4H = "donchian55_s2.0_t3.0__FET__4h"
OP_4H = "donchian20_s1.5_t1.5__OP__4h"


def test_fet_in_default_signal_only():
    assert FET_4H in DEFAULT_SIGNAL_ONLY


def test_family_approved_fet_is_live_even_without_state_alloc():
    state = {"approved_families": ["donchian_atr"], "approved": {}}
    assert approval_mode(state, FET_4H) == "live"


def test_family_approved_fet_live_with_alloc_enabled(monkeypatch):
    import slots as slots_mod

    def fake_resolve():
        return (
            {
                "slots": [
                    {"strategy_id": FET_4H, "enabled": True, "slot": "sat_fet_4h"},
                ]
            },
            "test",
        )

    monkeypatch.setattr(
        "allocation.resolve_allocation",
        fake_resolve,
        raising=False,
    )
    # Also patch the import path used inside slots
    import allocation
    monkeypatch.setattr(allocation, "resolve_allocation", fake_resolve)

    state = {"approved_families": ["donchian_atr"], "approved": {}}
    assert approval_mode(state, FET_4H) == "live"


def test_alloc_disabled_stays_signal_only(monkeypatch):
    import allocation

    def fake_resolve():
        return (
            {
                "slots": [
                    {"strategy_id": FET_4H, "enabled": False, "slot": "sat_fet_4h"},
                ]
            },
            "test",
        )

    monkeypatch.setattr(allocation, "resolve_allocation", fake_resolve)
    state = {"approved_families": ["donchian_atr"], "approved": {}}
    assert approval_mode(state, FET_4H) == "signal_only"


def test_other_family_keeps_fet_signal_only():
    state = {"approved_families": ["ema_cross_atr"], "approved": {}}
    assert approval_mode(state, FET_4H) == "signal_only"


def test_op_still_live():
    state = {"approved_families": ["donchian_atr"], "approved": {}}
    assert approval_mode(state, OP_4H) == "live"


def test_explicit_demotion_wins(monkeypatch):
    import allocation

    def fake_resolve():
        return ({"slots": [{"strategy_id": FET_4H, "enabled": True}]}, "test")

    monkeypatch.setattr(allocation, "resolve_allocation", fake_resolve)
    state = {
        "approved_families": ["donchian_atr"],
        "approved": {FET_4H: {"mode": "signal_only", "approved": False}},
    }
    assert approval_mode(state, FET_4H) == "signal_only"
