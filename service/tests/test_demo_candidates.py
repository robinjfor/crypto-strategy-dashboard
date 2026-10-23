import demo_candidates as dc


def test_disabled_and_not_approvable_by_default():
    v = dc.view({})
    assert len(v) == 2 and all(x["enabled"] is False and x["approvable"] is False for x in v)


def test_approvable_needs_all_three_gates(monkeypatch):
    monkeypatch.setenv("DEMO_ACCEPTED", "demo_e_8020_x175")
    s = {"demo_candidates": {"demo_e_8020_x175": {"cloud_ordering_ok": True}}}
    assert not dc.view(s)[0]["approvable"]
    s["demo_candidates"]["demo_e_8020_x175"]["parity_pass"] = True
    assert dc.view(s)[0]["approvable"] and dc.view(s)[0]["enabled"] is False


def test_weekly_breach():
    assert dc.weekly_breach(1006, 1000) and not dc.weekly_breach(1004, 1000)


def test_demo_e_planner_band_and_parity_roundtrip():
    import numpy as np, pandas as pd, demo_e
    # U1: within 25% band -> keep; outside -> retarget
    u = demo_e.plan_u1(np.array([1.0, 0.0]), np.array([0.5, 0.4]), np.array([100.0, 10.0]), 220.0)
    assert u[0] == 1.0 and abs(u[1] - 0.4 * 220 / 10) < 1e-12
    tw = pd.Series({"X": 0.3, "Y": 0.0}); db = pd.Series({"X": 0.05, "Y": 0.05}); o = pd.Series({"X": 2.0, "Y": 5.0})
    cur = pd.Series({"Y": 1.0})
    nu = demo_e.plan_s5(cur, tw, db, o, 100.0)
    assert abs(nu["X"] - 15.0) < 1e-12 and nu["Y"] == 0.0
    snap = {"date": "d", "cur_u": cur, "tw": tw, "db": db, "o5": o, "nw": nu * o / 100.0, "nu": nu,
            "u1_u": demo_e.plan_u1(np.zeros(2), np.array([0.5, 0.4]), np.array([100.0, 10.0]), 400.0),
            "uo": np.array([100.0, 10.0]), "lev": np.array([0.5, 0.4]), "liq": 0}
    r = demo_e.parity([snap])
    assert r["pass"] and r["trades"] == 4


def test_demo_e_weekly_pause_only_when_enabled():
    import demo_e
    st = {}
    assert demo_e.reconcile(st, 1000, 990, "2026-11-02") is None
    st = {"demo_candidates": {demo_e.SLOT: {"enabled": True, "daily": [{"date": f"2026-10-{d}", "demo": 1, "ledger": 1, "dev": 0} for d in range(26, 32)]}}}
    r = demo_e.reconcile(st, 1000, 990, "2026-11-02")
    assert r["paused"] and demo_e.SLOT in st["paused_slots"]


def test_accepted_gate_default_false(monkeypatch):
    import demo_candidates as dc
    st = {"demo_candidates": {"demo_funding_arb_1x": {"cloud_ordering_ok": True, "parity_pass": True}}}
    monkeypatch.delenv("DEMO_ACCEPTED", raising=False)
    v = {x["id"]: x for x in dc.view(st)}["demo_funding_arb_1x"]
    assert not v["accepted"] and not v["approvable"] and v["status"] == "等待資金控管驗收"
    monkeypatch.setenv("DEMO_ACCEPTED", "demo_funding_arb_1x")
    assert {x["id"]: x for x in dc.view(st)}["demo_funding_arb_1x"]["approvable"]
