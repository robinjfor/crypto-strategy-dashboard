from datetime import datetime, timedelta, timezone

import guards as G

T0 = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)   # FOMC


def test_calendar_window_blocks_1h_before_to_4h_after():
    assert G.calendar_window(T0 - timedelta(minutes=61)) is None
    assert G.calendar_window(T0 - timedelta(minutes=59))["kind"] == "FOMC"
    assert G.calendar_window(T0 + timedelta(hours=3, minutes=59))
    assert G.calendar_window(T0 + timedelta(hours=4, minutes=1)) is None
    st = {}
    r = G.check_calendar(st, T0)
    assert r["active"] and G.entry_blocked(st, T0)[0]
    assert st["control_log"][-1]["actor"] == "guard"
    assert not G.entry_blocked(st, T0 + timedelta(hours=5))[0]   # expires by itself


def test_announcement_pauses_slot_never_unpauses_and_no_orders():
    arts = {"data": {"catalogs": [{"articles": [
        {"id": 1, "code": "a", "title": "Binance Will Delist FIL on 2026-11-01", "releaseDate": int(T0.timestamp() * 1000)},
        {"id": 2, "code": "b", "title": "Notice of Removal of Spot Trading Pairs", "releaseDate": int(T0.timestamp() * 1000)}]}]}}

    def fetch(url, params=None, **k):
        if "detail" in url:
            return {"data": {"body": "pairs: XYZ/BTC, ABC/ETH"}}
        return arts
    st = {"paused_slots": {"sat_dot_4h": {"paused_by": "api"}}}
    slots = [{"id": "sat_fil_1h", "symbol": "FILUSDT"}, {"id": "sat_dot_4h", "symbol": "DOTUSDT"}]
    r = G.check_announcements(st, slots, T0, fetch)
    assert [h["symbol"] for h in r["hits"]] == ["FILUSDT"]          # XYZ/BTC must not hit U1
    assert st["paused_slots"]["sat_fil_1h"]["paused_by"] == "guard" and "sat_dot_4h" in st["paused_slots"]
    assert G.check_announcements(st, slots, T0, fetch)["hits"] == []  # seen once only


class FC:
    def __init__(self):
        self.calls = []

    def set_leverage(self, sym, lev, cap=3):
        self.calls.append((sym, lev))

    def new_order(self, **k):  # pragma: no cover
        raise AssertionError("guards must never place orders")


def test_depeg_sets_leverage_1x_only():
    st, fc = {}, FC()
    r = G.check_depeg(st, T0, lambda s: 0.993, fc)
    assert r["active"] and all(l == 1 for _, l in fc.calls) and G.leverage_override(st) == 1
    assert G.check_depeg({}, T0, lambda s: 1.001, FC())["active"] is False


def test_spike_blocks_2h():
    now = T0
    base = int((now - timedelta(minutes=15 * 300)).timestamp() * 1000)
    k = [[base + i * 900000, 100, 100.2, 99.8, 100, 0, base + i * 900000 + 899999] for i in range(299)]
    k.append([base + 299 * 900000, 100, 104, 96, 100, 0, base + 299 * 900000 + 899999])
    st = {}
    G.check_spike(st, now, lambda s: k, symbols=("BTCUSDT",))
    assert G.entry_blocked(st, now + timedelta(minutes=119))[0] and not G.active_blocks(st, now + timedelta(hours=2, minutes=1))


def test_farside_parse():
    html = "<tr><td>21 Oct 2026</td><td>1</td><td>524.8</td></tr><tr><td>20 Oct 2026</td><td>(23.2)</td></tr>"
    r = G.farside_flows(lambda x: html)
    assert r["days"] == [["2026-10-21", 524.8], ["2026-10-20", -23.2]]
