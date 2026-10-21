"""donchian_atr_perp wiring (2026-10-21, 資金控管 BATCH2_ACCEPT): allocation/slot family lists, validator,
runtime slot, family resolution for the perp C rows (runner_family_overrides), site card split, card notes,
and family approve/revoke isolation between spot donchian_atr (C1–C21) and donchian_atr_perp (no 0213114
regression)."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

import allocation  # noqa: E402
import api  # noqa: E402
import main  # noqa: E402
import slots  # noqa: E402
import strategy_codes  # noqa: E402
from allocation import slot_to_runtime, validate_allocation  # noqa: E402

FAM = "donchian_atr_perp"
ROOT = Path(__file__).resolve().parents[2]
C23 = "rcr1020_donchian70_s2.0_t1.5_reset__WLD__4h"
C35 = "strictwf1021_C35_donch_n40_stop_m1.5_trail_m2.5_reset_below_hiTrue__SEI__4h"
Z9 = "strictwf1021_Z9_bb_n40_bb_k2.5_sq_q0.35_stop_m1.5_trail_m3.5__ARB__1h"
ACCEPTED = ["C23", "C25", "C29", "C31", "C32", "C35", "C36", "C39"]


def _slot(**over):
    s = {"slot": "dperp_wld_4h", "strategy_id": C23, "family": FAM, "symbol": "WLDUSDT", "timeframe": "4h",
         "enabled": False, "notional_usdt": 750,
         "params": {"donch_n": 70, "stop_m": 2.0, "trail_m": 1.5, "max_hold": 36, "reset_below_hi": True,
                    "atr_mode": "sma", "leverage": 1.0}}
    s.update(over)
    return s


def test_family_lists():
    assert FAM in allocation.RUNNER_FAMILIES and FAM in allocation.FUTURES_FAMILIES
    assert FAM not in allocation.PENDING_REVIEW_FAMILIES
    assert FAM in slots.SUPPORTED_FAMILIES and slots.venue_for_family(FAM) == "futures"
    assert allocation.normalize_family(FAM) == FAM
    assert main._next_open_perp_module(FAM).FAMILY == FAM


def test_validator_accepts_spec_params():
    errs = validate_allocation({"book_usdt": 5000, "slots": [_slot()]}, check_binance=False)
    assert not any("donchian_atr_perp" in e or "尚未支援" in e for e in errs), errs


@pytest.mark.parametrize("bad,needle", [
    ({"leverage": 3.0}, "鎖定 1 倍"), ({"atr_mode": "wilder"}, "atr_mode"), ({"donch_n": None}, "donch_n"),
    ({"stop_m": 0}, "stop_atr_mult"), ({"kind": "short"}, "只做多"), ({"btc_regime": True}, "btc_regime"),
])
def test_validator_rejects(bad, needle):
    s = _slot()
    s["params"] = {**s["params"], **bad}
    errs = validate_allocation({"book_usdt": 5000, "slots": [s]}, check_binance=False)
    assert any(needle in e for e in errs), errs


def test_slot_to_runtime_defaults():
    s = _slot()
    s["params"] = {k: v for k, v in s["params"].items() if k not in ("atr_mode", "reset_below_hi")}
    rt = slot_to_runtime(s)
    assert rt["family"] == FAM and rt["atr_mode"] == "sma" and rt["require_reset_below_hi"] is True
    assert (rt["donch_n"], rt["stop_atr_mult"], rt["trail_atr_mult"], rt["max_hold_bars"]) == (70, 2.0, 1.5, 36)


def test_family_resolution_perp_rows():
    assert strategy_codes.family_for_strategy_id(C23) == FAM
    assert strategy_codes.family_for_strategy_id(C35) == FAM
    assert strategy_codes.code_for(C23, FAM) == "C23" and strategy_codes.code_for(C35, FAM) == "C35"
    assert strategy_codes.family_for_strategy_id("donchian20_s1.5_t1.5__OP__4h") == "donchian_atr"
    assert strategy_codes.family_for_strategy_id(Z9) == "bb_squeeze_breakout_perp"
    assert strategy_codes.code_for(Z9, "bb_squeeze_breakout_perp") == "Z9"


def test_site_catalog_cards_and_notes():
    cat = json.loads((ROOT / "data/unified-3y/catalog.json").read_text(encoding="utf-8"))
    fams = {f["strategy_family_id"]: f for f in cat["strategies"]}
    perp = fams[FAM]
    assert perp["code"] == "C" and perp["split_from"] == "donchian_atr" and perp.get("entry_zh")
    assert sorted((r["code"] for r in perp["rows"]), key=lambda c: int(c[1:])) == ACCEPTED
    assert all(r["gate_pass_3y"] and r["wf_pass"] for r in perp["rows"])
    spot_codes = [r["code"] for r in fams["donchian_atr"]["rows"]]
    assert spot_codes and all(int(c[1:]) <= 21 for c in spot_codes)  # spot card unchanged: C1–C21
    notes = {r["code"]: r.get("site_note_zh") for f in cat["strategies"] for r in f["rows"]}
    assert notes["C29"] == "和 C2 同幣 FIL，C2 啟用時不能開"
    assert notes["C32"].startswith("和 C5 同幣 DOT") and notes["C39"].startswith("和 C3 同幣 OP")
    assert "C25 與 Z9 只能選一" in notes["C25"] and "C25 與 Z9 只能選一" in notes["Z9"]
    assert notes["C25"].startswith("和 S2 同幣 ARB") and notes["C36"].startswith("和 S8 同幣 GALA")
    assert notes["C23"] == notes["Z1"] == "C23 與 Z1 同幣 WLD，只能選一"
    assert notes["C35"] == "後半段只有 +7%，成交量剛過門檻"
    js = (ROOT / "backtest.js").read_text(encoding="utf-8")
    assert 'donchian_atr_perp: "donchian_atr_perp",' in js
    assert "donchian_atr_perp: true" in js.split("var RUNNER_FAMILIES", 1)[1].split(";", 1)[0]


def test_api_rule_text():
    t = api._entry_condition(slot_to_runtime(_slot()))
    assert "Donchian70" in t and "1x" in t and "永續" in t and "2×ATR14" in t


SEED = {
    "donchian20_s1.5_t1.5__OP__4h": {"slot": "sat_op_4h", "notional_usdt": 1000.0, "approved_at": "2026-10-08T00:41:41+08:00",
                                     "approved": True, "mode": "live", "family": "donchian_atr"},
    C23: {"slot": "dperp_wld_4h", "notional_usdt": 750.0, "approved_at": "2026-10-21T12:00:00+08:00",
          "approved": True, "mode": "live", "family": FAM},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"approved_families": ["donchian_atr", FAM], "approved": copy.deepcopy(SEED)}))
    monkeypatch.setenv("STATE_LOCAL_PATH", str(path))
    monkeypatch.setattr(api, "_check_pin", lambda: (True, "", 200))
    monkeypatch.setattr(api, "_check_auth", lambda allow_readonly=False: (True, "", 200))
    api.app.config["TESTING"] = True
    with api.app.test_client() as c:
        yield c, path


def test_revoke_spot_family_keeps_perp_and_vice_versa(client):
    c, path = client
    assert c.post("/control/revoke_family", json={"family": "donchian_atr"}).status_code == 200
    st = json.loads(path.read_text())
    assert FAM in st["approved_families"] and "donchian_atr" not in st["approved_families"]
    assert st["approved"] == {C23: SEED[C23]}
    assert c.post("/control/approve_family", json={"family": "donchian_atr"}).status_code == 200
    assert c.post("/control/revoke_family", json={"family": FAM}).status_code == 200
    st = json.loads(path.read_text())
    assert "donchian_atr" in st["approved_families"] and FAM not in st["approved_families"]
    assert C23 not in st["approved"]


def test_approve_perp_family_touches_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    seed = {k: v for k, v in SEED.items() if k != C23}
    path.write_text(json.dumps({"approved_families": ["donchian_atr"], "approved": copy.deepcopy(seed)}))
    monkeypatch.setenv("STATE_LOCAL_PATH", str(path))
    monkeypatch.setattr(api, "_check_pin", lambda: (True, "", 200))
    api.app.config["TESTING"] = True
    with api.app.test_client() as c:
        r = c.post("/control/approve_family", json={"family": FAM})
        assert r.status_code == 200, r.get_json()
    st = json.loads(path.read_text())
    assert st["approved_families"] == ["donchian_atr", FAM]
    assert st["approved"] == seed


def test_site_gate_45_and_z_cloud_supported():
    """資金控管 2026-10-21: batch gate stored/displayed at MaxDD 45% (until Robin confirms); Z cloud_supported."""
    cat = json.loads((ROOT / "data/unified-3y/catalog.json").read_text(encoding="utf-8"))
    rows = {r["code"]: (f, r) for f in cat["strategies"] for r in f["rows"]}
    for c in ACCEPTED + ["Z1", "Z4", "Z9"]:
        f, r = rows[c]
        assert r["gate_maxdd_limit"] == 45.0 and "MaxDD<=45%" in r["gate_spec"] and "60%" not in r["gate_spec"].split("（")[0]
        assert abs(float(r["maxdd_3y"])) <= 45 and r["gate_pass_3y"] is True
    z = next(f for f in cat["strategies"] if f["strategy_family_id"] == "bb_squeeze_breakout_perp")
    assert z["cloud_supported"] is True and all(r.get("cloud_supported", True) for r in z["rows"])
    sc = json.loads((ROOT / "data/unified-3y/scores.json").read_text(encoding="utf-8"))
    assert all(r["supported_by_runner"] for r in sc["strategies"] if (r.get("code") or "").startswith("Z"))
