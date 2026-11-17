"""Parity capital follows the orchestrator target. Fewer than 21 days is unverified."""
import e5_tracking as T
import review_credibility as C
import tradfi

SLOT = "demo_e5_min5_x175_t05top20"


def test_latest_parity_capital_is_the_orchestrator_target_not_5000():
    targets = {
        "decided_bar": "2026-11-16",
        "w1": [0.0, 0.0],
        "ws": {"SOLUSDT": 0.05},
        "m": 1.0,
        "rw": [0.01],
        "lv": 1.75,
        "equity": 5000.0,
        "prices": {"SOLUSDT": 100.0},
    }
    state = {
        "orchestrator": {"targets": {SLOT: 1986.13}, "weights": {SLOT: 1.0}},
        "e5_targets": targets,
        "demo_e5v_exec": {"cash": 5000.0, "book": {}, "sizing_equity": 5000.0},
    }
    day = T.compare(state, status={})["days"][0]
    assert day["sizing_capital"] == 1986.13
    assert day["virtual_capital"] == 5000.0
    assert day["capital_basis_label"] == "orchestrator_target"


def test_seven_unpriced_signals_each_have_a_cause_and_short_samples_are_unverified():
    names = list(tradfi.tradfi_map())[:2]
    assert len(names) == 2
    plain = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    ws = {sym: 0.02 for sym in names + plain + ["EEEUSDT"]}
    # EEE is not listed. The other four have no mark. The two tradfi names are on the static list.
    targets = {
        "decided_bar": "2026-11-16",
        "w1": [0.0, 0.0],
        "ws": ws,
        "m": 1.0,
        "rw": [0.01],
        "lv": 1.75,
        "prices": {},
        "last": {"skipped": ["EEEUSDT not listed"]},
    }
    state = {
        "orchestrator": {"targets": {SLOT: 1986.13}},
        "e5_targets": targets,
        "demo_e5v_exec": {"cash": 1000.0, "book": {}},
    }
    report = T.compare(state, status={})
    unpriced = {row["symbol"]: row["cause"] for row in report["days"][0]["unpriced_signals"]}
    assert len(unpriced) == 7
    for sym in names:
        assert unpriced[sym] == "tradfi"
    assert unpriced["EEEUSDT"] == "not_listed_on_demo"
    for sym in plain:
        assert unpriced[sym] == "no_mark"
    cred = C.build({}, state)
    assert cred["parity_days"] < 21
    assert cred["parity_status"] == "unverified"
    assert cred["parity_capital"] == 1986.13
    assert cred["dsr"] == "missing"
