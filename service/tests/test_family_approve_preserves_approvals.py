"""Bug 2026-10-10: approving one family (short_trend_perp) rebuilt state.approved for everyone —
re-stamped approved_at, re-sized notionals and dropped entries whose slots are disabled (C8/A6).
Approving a family must never touch other approvals."""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

import api  # noqa: E402

OLD = "2026-10-01T22:39:23+08:00"
SEED_APPROVED = {
    # live slot, notional differs from allocation (750) — must not be re-sized
    "donchian20_s1.5_t1.5__OP__4h": {"slot": "sat_op_4h", "notional_usdt": 1000.0, "approved_at": "2026-10-08T00:41:41+08:00",
                                     "approved": True, "mode": "live", "status": "live_standby", "family": "donchian_atr"},
    "donchian20_s1.5_t1.5__DOT__4h": {"slot": "sat_dot_4h", "notional_usdt": 750.0, "approved_at": OLD,
                                      "approved": True, "mode": "live", "status": "live_standby", "family": "donchian_atr"},
    "ema12_26_atr_btcRegime__SOL__1d": {"slot": "sat_sol_ema_1d", "notional_usdt": 750.0, "approved_at": OLD,
                                        "approved": True, "mode": "live", "status": "live_standby", "family": "ema_cross_atr"},
    # approved by Emily but slot disabled in allocation (like C8 APT 1h) — must not be dropped
    "donchian55_s2.5_t3.5__ICP__4h": {"slot": "sat_icp_4h", "notional_usdt": 750.0, "approved_at": "2026-10-08T00:41:40+08:00",
                                      "approved": True, "mode": "live", "status": "live_standby", "family": "donchian_atr"},
    # futures entry of another family (like A6) — must not be dropped
    "ls__NEAR__4h__n20__s2.0__t3.0__vt0.03__LS": {"slot": "ls_near_4h", "notional_usdt": 750.0,
                                                  "approved_at": "2026-10-08T00:41:41+08:00", "approved": True,
                                                  "mode": "live", "status": "live_standby",
                                                  "family": "ls_donch_btc_regime_perp"},
}
POSITIONS = {"sat_op_4h": {"qty": 3.0, "entry": 1.2, "stop": 1.1}, "sat_dot_4h": {"qty": 100.0, "entry": 4.0, "stop": 3.8}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "approved_families": ["donchian_atr", "ema_cross_atr", "ls_donch_btc_regime_perp"],
        "approved": copy.deepcopy(SEED_APPROVED),
        "archived_families": {"donchian_fear_greed": {"archived_at": "2026-10-02T00:00:42+08:00", "by": "api", "reason": "manual"}},
        "positions": copy.deepcopy(POSITIONS),
    }))
    monkeypatch.setenv("STATE_LOCAL_PATH", str(path))
    monkeypatch.setattr(api, "_check_pin", lambda: (True, "", 200))
    monkeypatch.setattr(api, "_check_auth", lambda allow_readonly=False: (True, "", 200))
    api.app.config["TESTING"] = True
    with api.app.test_client() as c:
        yield c, path


def _state(path):
    return json.loads(path.read_text())


def test_approve_family_leaves_other_approvals_untouched(client):
    c, path = client
    r = c.post("/control/approve_family", json={"family": "short_trend_perp"})
    assert r.status_code == 200, r.get_json()
    st = _state(path)
    assert "short_trend_perp" in st["approved_families"]
    assert st["approved"] == SEED_APPROVED  # no re-stamp, no re-size, nothing dropped
    assert st["positions"] == POSITIONS
    assert st["archived_families"]["donchian_fear_greed"]["archived_at"] == "2026-10-02T00:00:42+08:00"
    # /approved still lists every entry with its original approved_at / notional
    pub = {a["strategy_id"]: a for a in c.get("/approved").get_json()["approved"]}
    for sid, meta in SEED_APPROVED.items():
        assert pub[sid]["approved_at"] == meta["approved_at"]
        assert pub[sid]["notional_usdt"] == meta["notional_usdt"]


def test_reapproving_existing_family_does_not_restamp(client):
    c, path = client
    r = c.post("/control/approve_family", json={"family": "donchian_atr"})
    assert r.status_code == 200
    after = _state(path)["approved"]
    for sid, meta in SEED_APPROVED.items():
        assert after[sid] == meta  # existing entries byte-identical; only missing live slots may be added
    assert all(after[s]["family"] == "donchian_atr" for s in set(after) - set(SEED_APPROVED))


def test_approve_family_only_adds_missing_live_slots_of_that_family(client):
    c, path = client
    st = _state(path)
    st["approved_families"] = ["donchian_atr", "ema_cross_atr"]
    path.write_text(json.dumps(st))
    r = c.post("/control/approve_family", json={"family": "ls_donch_btc_regime_perp"})
    assert r.status_code == 200
    after = _state(path)["approved"]
    for sid, meta in SEED_APPROVED.items():
        assert after[sid] == meta
    new = set(after) - set(SEED_APPROVED)
    assert new, "enabled ls slot of the approved family should be added"
    assert all(after[s]["family"] == "ls_donch_btc_regime_perp" for s in new)


def test_revoke_family_drops_only_that_family(client):
    c, path = client
    r = c.post("/control/revoke_family", json={"family": "ema_cross_atr"})
    assert r.status_code == 200
    after = _state(path)["approved"]
    expected = {k: v for k, v in SEED_APPROVED.items() if k != "ema12_26_atr_btcRegime__SOL__1d"}
    assert after == expected


def test_restore_approval_exact_and_isolated(client):
    c, path = client
    st = _state(path)
    st["approved"].pop("donchian55_s2.5_t3.5__ICP__4h")
    st["approved"]["donchian20_s1.5_t1.5__DOT__4h"]["approved_at"] = "2026-10-10T14:41:25+08:00"
    path.write_text(json.dumps(st))
    before = _state(path)
    r = c.post("/control/restore_approval", json={"strategy_id": "donchian55_s2.5_t3.5__ICP__4h",
                                                  "approved_at": "2026-10-08T00:41:40+08:00", "notional_usdt": 750})
    assert r.status_code == 200 and r.get_json()["created"] is True
    r = c.post("/control/restore_approval", json={"strategy_id": "donchian20_s1.5_t1.5__DOT__4h", "approved_at": OLD})
    assert r.status_code == 200 and r.get_json()["created"] is False
    after = _state(path)
    icp = after["approved"]["donchian55_s2.5_t3.5__ICP__4h"]
    assert icp["approved_at"] == "2026-10-08T00:41:40+08:00" and icp["notional_usdt"] == 750.0 and icp["slot"] == "sat_icp_4h"
    dot = after["approved"]["donchian20_s1.5_t1.5__DOT__4h"]
    assert dot["approved_at"] == OLD and dot["notional_usdt"] == 750.0
    for sid in ("donchian20_s1.5_t1.5__OP__4h", "ema12_26_atr_btcRegime__SOL__1d", "ls__NEAR__4h__n20__s2.0__t3.0__vt0.03__LS"):
        assert after["approved"][sid] == before["approved"][sid]
    for k in ("approved_families", "archived_families", "positions"):
        assert after[k] == before[k]
    # disabled slot stays not-live for the runner
    from slots import approval_mode
    assert approval_mode(after, "donchian55_s2.5_t3.5__ICP__4h") == "signal_only"


@pytest.mark.parametrize("body", [{"strategy_id": "x"}, {"approved_at": OLD},
                                  {"strategy_id": "donchian20_s1.5_t1.5__DOT__4h", "approved_at": "2026-10-01 22:39"},
                                  {"strategy_id": "no_such_strategy", "approved_at": OLD},
                                  {"strategy_id": "donchian20_s1.5_t1.5__DOT__4h", "approved_at": OLD, "notional_usdt": 99999}])
def test_restore_approval_rejects_bad_input(client, body):
    c, path = client
    before = _state(path)
    r = c.post("/control/restore_approval", json=body)
    assert r.status_code == 400
    assert _state(path) == before
