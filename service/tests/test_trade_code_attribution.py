import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from slots import slot_by_id, all_known_slots
from strategy_codes import code_for, code_for_symbol


def test_sat_op_4h_trade_is_c3_not_a7():
    rt = slot_by_id("sat_op_4h") or next(s for s in all_known_slots() if s.get("id") == "sat_op_4h")
    assert code_for(rt["strategy_id"], rt.get("family")) == "C3"


def test_manual_near_spot_fill_has_no_perp_code():
    assert code_for_symbol("NEARUSDT", all_known_slots(), venue="spot") is None
