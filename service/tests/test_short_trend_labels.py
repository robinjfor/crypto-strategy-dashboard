"""Dashboard labels for S short_trend_perp come from the slot params (not the Donchian default)."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import api  # noqa: E402
from allocation import slot_to_runtime  # noqa: E402

ALLOC = json.loads((Path(__file__).resolve().parents[2] / "config" / "allocation.json").read_text())


def _rt(slot_id):
    return slot_to_runtime(next(s for s in ALLOC["slots"] if s["slot"] == slot_id))


def test_s2_s8_entry_condition_is_ema_death_cross_with_params():
    a = api._entry_condition(_rt("short_arb_4h"))
    g = api._entry_condition(_rt("short_gala_6h"))
    assert "突破上軌" not in a and "突破上軌" not in g
    assert a.startswith("EMA9/21 死叉做空") and "停損 2×ATR14" in a and "移動停損 5×ATR14" in a and "資金費率" in a and "60 根" in a
    assert g.startswith("EMA12/26 死叉做空") and "停損 3×ATR14" in g and "移動停損 5×ATR14" in g and "無濾網" in g and "40 根" in g


def test_short_trend_trigger_fields():
    rt = _rt("short_gala_6h")
    f = api._trigger_fields(rt, {"ema_fast": 0.00193, "ema_slow": 0.00200, "bear": True, "filter_on": True, "mark": 0.002})
    assert f["trigger_kind"] == "short_trend" and f["ema_bear"] is True and f["ema_fast_n"] == 12 and f["ema_slow_n"] == 26
    assert f["ema_gap_pct"] < 0 and f["s_filter"] == "none"
    assert api.status_zh("waiting_short_entry").startswith("等做空進場邊緣")


def test_sdon_and_u1_labels():
    assert api._entry_condition({"family": "short_trend_perp", "signal": "sdon", "donch_n": 40, "stop_atr_mult": 1.5,
                                 "trail_atr_mult": 1.0, "filter": "none"}).startswith("Donchian40 收盤跌破下軌做空")
    assert "槓桿上限 3x" in api._entry_condition({"family": "btc_eth_trend_voltarget_perp"})
