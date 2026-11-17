"""E5 isolated-margin top-up: entry notional / 1.75, without touching leverage or VBO's 1000."""
import inspect
from datetime import datetime, timezone

import pytest

import demo_e5 as D
import demo_e5_exec as X
import demo_e5v as V
import e5_margin as M
import reconcile

NOW = datetime(2026, 11, 17, 0, 30, tzinfo=timezone.utc)
SLOT = V.SLOT
KEY = V.STATE_KEY


def test_review_positions_read_the_e5v_topup_and_the_reconcile_time():
    st = {
        "demo_e5v_exec": {
            "last_run": {"at": "2026-11-17T08:08:00+08:00"},
            "margin_topup": {"at": "2026-11-17T08:08:00+08:00", "symbols": {
                "MOVRUSDT": {"target": 2.0, "margin_after": 1.7, "margin_before": 1.6, "status": "deadband", "shortfall": 0.01, "skip_threshold": 0.02},
            }},
            "margin_topup_log": [{"at": "2026-11-17T03:00:00+08:00", "symbols": {"MOVRUSDT": {"added": 0.2, "status": "topped_up"}}}],
        }
    }
    view = M.review_view(st)
    assert view["ledger"] == "demo_e5v_exec"
    assert view["reconcile_last_run_at"] == "2026-11-17T08:08:00+08:00"
    row = view["symbols"][0]
    assert row["symbol"] == "MOVRUSDT"
    assert row["target"] == 2.0
    assert row["isolated_margin"] == 1.7
    assert row["last_topup"]["added"] == 0.2
    assert row["skipped_reason"] == "deadband"


def test_shortfall_uses_entry_notional_over_1_75():
    assert M.TARGET_LV == 1.75
    assert M.shortfall(175, 87.5) == pytest.approx(12.5)
    assert M.shortfall(175, 100) == 0
    assert M.shortfall(175, 130) == 0
    # AIOT shape: mark has already fallen through the 2x wallet, entry cushion has not.
    # entry 0.1584 * 1000 = 158.4, 2x wallet 79.2, mark notional 90.
    # mark/1.75 = 51.43 is already below the wallet, so a mark target would add nothing.
    assert M.margin_base(158.4, 90.0) == 158.4
    assert M.shortfall(90.0, 79.2) == 0
    assert M.shortfall(158.4, 79.2) == pytest.approx(158.4 / 1.75 - 79.2)
    assert M.posted_margin({"isolatedMargin": "80", "unRealizedProfit": "-5"}) == pytest.approx(85)
    assert M.posted_margin({"isolatedWallet": "40", "isolatedMargin": "80", "unRealizedProfit": "-5"}) == pytest.approx(40)


def test_aiot_long_liq_moves_from_2x_to_1_75_and_clears_the_day_low():
    """Observed 2x liquidation 0.0792. cum=0, so mmr cancels in the leverage ratio."""
    liq_2x = 0.0792
    liq_175 = M.liq_scale_long(liq_2x, 2, 1.75)
    assert liq_175 == pytest.approx(0.0792 * (1 - 1 / 1.75) / 0.5)
    assert liq_175 == pytest.approx(0.0678857142857)
    low = 0.0732
    assert liq_175 < low < liq_2x
    entry = liq_2x / (1 - 1 / 2)  # mmr 0: liq = entry / 2
    assert entry == pytest.approx(0.1584)
    assert M.liquidation_price_for_margin(entry, 1.0, entry / 2, 0.0) == pytest.approx(liq_2x)
    assert M.liquidation_price_for_margin(entry, 1.0, entry / 1.75, 0.0) == pytest.approx(liq_175)
    # 1% maintenance (isolated_liq default tier) changes entry, not the liq ratio.
    entry_mmr = liq_2x * 0.99 / 0.5
    assert M.liquidation_price_for_margin(entry_mmr, 1.0, entry_mmr / 1.75, 0.01) == pytest.approx(liq_175)
    dist_entry_2 = (entry - liq_2x) / entry
    dist_entry_175 = (entry - liq_175) / entry
    assert dist_entry_2 == pytest.approx(0.5)
    assert dist_entry_175 == pytest.approx(1 - (1 - 1 / 1.75))
    # Distance of the day's mark low to each liquidation price. Negative means the low is through it.
    assert (low - liq_2x) / low == pytest.approx(-0.081967213)
    assert (low - liq_175) / low == pytest.approx(0.072599532)


class Exchange:
    def __init__(self, rows, available=100000.0):
        self.rows = [dict(r) for r in rows]
        self.available = float(available)
        self.adds = []
        self.leverage_calls = []
        self.orders = []

    def position_risk(self, symbol=None):
        rows = self.rows if symbol is None else [r for r in self.rows if r.get("symbol") == symbol]
        return [dict(r) for r in rows]

    def account(self):
        return {"availableBalance": self.available,
                "assets": [{"asset": "USDT", "availableBalance": self.available}]}

    def add_isolated_margin(self, symbol, amount):
        amount = float(amount)
        self.adds.append((symbol, round(amount, 2)))
        self.available -= amount
        for row in self.rows:
            if row.get("symbol") != symbol:
                continue
            wallet = M.posted_margin(row)
            wallet = 0.0 if wallet is None else wallet + amount
            upnl = float(row.get("unRealizedProfit") or 0)
            row["isolatedWallet"] = wallet
            row["isolatedMargin"] = wallet + upnl
            entry = float(row.get("entryPrice") or 0)
            qty = float(row.get("positionAmt") or 0)
            row["liquidationPrice"] = M.liquidation_price_for_margin(entry, qty, wallet, 0.0)
        return {"code": 200, "amount": amount, "type": 1}

    def set_leverage(self, *args, **kwargs):
        self.leverage_calls.append((args, kwargs))

    def new_order(self, **kwargs):
        self.orders.append(kwargs)
        raise AssertionError("margin top-up must not place an order")


def _row(sym, qty, entry, wallet, lev, mark=None, wallet_field=True, upnl=0.0):
    mark = entry if mark is None else mark
    liq = M.liquidation_price_for_margin(entry, qty, wallet, 0.0)
    row = {"symbol": sym, "positionAmt": str(qty), "entryPrice": str(entry), "markPrice": str(mark),
           "notional": str(qty * mark), "unRealizedProfit": str(upnl), "leverage": str(lev),
           "marginType": "isolated", "liquidationPrice": None if liq is None else str(liq)}
    if wallet_field:
        row["isolatedWallet"] = str(wallet)
        row["isolatedMargin"] = str(wallet + upnl)
    else:
        row["isolatedMargin"] = str(wallet + upnl)
    return row


def _state(book, cash=20000.0, vbo=None, plan=None, key=KEY):
    st = {key: {"cash": cash, "book": dict(book)},
          "demo_vbo1h_exec": {"cash": 1000.0, "book": dict(vbo or {})}}
    if plan:
        st["orchestrator"] = plan
    return st


def test_legacy_3x_is_topped_up_without_changing_leverage():
    # qty 175 at 1. Wallet at 3x is 175/3. Target wallet is 100. No set_leverage.
    row = _row("XUSDT", 175, 1.0, 175 / 3, 3)
    ex = Exchange([row], available=5000)
    st = _state({"XUSDT": 175.0}, cash=1000.0)
    cash, book = st[KEY]["cash"], dict(st[KEY]["book"])
    report = M.topup(st, ex, slot=SLOT, key=KEY, now=NOW)
    assert ex.leverage_calls == [] and ex.orders == []
    assert ex.adds == [("XUSDT", 41.67)]
    sym = report["symbols"]["XUSDT"]
    assert sym["leverage"] == 3
    assert sym["notional"] == pytest.approx(175)
    assert sym["margin_before"] == pytest.approx(175 / 3, abs=1e-4)
    assert sym["margin_after"] == pytest.approx(175 / 3 + 41.67, abs=1e-4)
    assert sym["margin_after"] >= sym["notional"] / 1.75
    assert sym["liq_before"] == pytest.approx(1 - 1 / 3)
    assert sym["liq_after"] == pytest.approx(1 - (175 / 3 + 41.67) / 175, abs=1e-6)
    assert sym["liq_after"] < sym["liq_before"]
    assert sym["status"] == "topped_up" and report["leverage_changes"] == 0 and report["orders"] == 0
    assert st[KEY]["cash"] == cash and st[KEY]["book"] == book


def test_vbo_reserve_is_not_spent_and_vbo_legs_are_not_topped_up():
    # E5 shortfall 800. Available 1200 and VBO has not locked margin, so 1000 stays put and only 200 moves.
    # VBOUSDT is a 3x short on the exchange. It is not an E5 leg, so it gets no transfer.
    e5 = _row("XUSDT", 112, 100.0, 5600.0, 2)
    vbo = _row("VBOUSDT", -10, 30.0, 100.0, 3)
    ex = Exchange([e5, vbo], available=1200)
    st = _state({"XUSDT": 112.0}, cash=20000.0)
    report = M.topup(st, ex, slot=SLOT, key=KEY, now=NOW)
    assert ex.adds == [("XUSDT", 200.0)]
    assert ex.leverage_calls == [] and "VBOUSDT" not in report["symbols"]
    sym = report["symbols"]["XUSDT"]
    assert sym["status"] == "partial" and sym["added"] == 200.0 and sym["limit"] == "vbo_reserve"
    assert report["limit"] == "vbo_reserve" and report["vbo_kept"] == pytest.approx(1000)
    assert report["pool_source"] == "virtual_equity"


def test_vbo_margin_already_locked_is_not_reserved_a_second_time():
    # VBO's 1000 is already in its isolated wallet. Free balance is E5's and can fund the shortfall.
    vbo = _row("VBOUSDT", -100, 20.0, 1000.0, 2)
    e5 = _row("XUSDT", 14, 100.0, 700.0, 2)  # notional 1400, target 800, shortfall 100
    ex = Exchange([e5, vbo], available=500)
    st = _state({"XUSDT": 14.0}, cash=5000.0, vbo={"VBOUSDT": -100.0})
    report = M.topup(st, ex, slot=SLOT, key=KEY, now=NOW)
    assert ex.adds == [("XUSDT", 100.0)]
    assert "VBOUSDT" not in report["symbols"]
    assert report["vbo_kept"] == pytest.approx(0)
    assert report["symbols"]["XUSDT"]["status"] == "topped_up"


def test_insufficient_pool_tops_up_only_the_remainder():
    # Sizing pool 60, wallet already 55, shortfall 20. Available balance is not the limit.
    row = _row("XUSDT", 10, 13.125, 55.0, 2)
    ex = Exchange([row], available=10000)
    plan = {"generated_at": datetime.now(timezone.utc).isoformat(), "sizing": "quote_pool",
            "targets": {SLOT: 60.0}, "quote_pool": {SLOT: 60.0}}
    st = _state({"XUSDT": 10.0}, cash=0.0, plan=plan)
    report = M.topup(st, ex, slot=SLOT, key=KEY)
    assert ex.adds == [("XUSDT", 5.0)]
    sym = report["symbols"]["XUSDT"]
    assert sym["status"] == "partial" and sym["limit"] == "e5_pool" and sym["added"] == 5.0
    assert report["pool_source"] == "sizing_pool" and report["pool"] == pytest.approx(60)
    assert report["pool_remaining"] == pytest.approx(5)
    assert sym["margin_after"] == pytest.approx(60)


def test_second_run_adds_nothing():
    row = _row("XUSDT", 175, 1.0, 87.5, 2)
    ex = Exchange([row])
    st = _state({"XUSDT": 175.0}, cash=1000.0)
    first = M.topup(st, ex, slot=SLOT, key=KEY, now=NOW)
    assert ex.adds == [("XUSDT", 12.5)]
    assert first["symbols"]["XUSDT"]["margin_before"] == pytest.approx(87.5)
    assert first["symbols"]["XUSDT"]["margin_after"] == pytest.approx(100)
    second = M.topup(st, ex, slot=SLOT, key=KEY, now=NOW)
    assert ex.adds == [("XUSDT", 12.5)]
    sym = second["symbols"]["XUSDT"]
    assert sym["status"] == "ok" and sym["added"] == 0
    assert sym["margin_before"] == pytest.approx(sym["margin_after"])
    assert sym["liq_before"] == pytest.approx(sym["liq_after"])
    assert len(st[KEY]["margin_topup_log"]) == 1


def test_opposite_exchange_leg_is_not_funded_by_e5():
    # E5's virtual long is crossed by other slots. The exchange short is not E5's liquidation.
    ex = Exchange([_row("XUSDT", -10, 5.0, 10.0, 3)])
    st = _state({"XUSDT": 4.0}, cash=5000.0)
    report = M.topup(st, ex, slot=SLOT, key=KEY, now=NOW)
    assert ex.adds == [] and ex.orders == [] and ex.leverage_calls == []
    assert report["symbols"]["XUSDT"]["status"] == "skipped_opposite"


def test_skip_threshold_is_five_cents_or_one_percent_of_target_whichever_is_smaller():
    # Target wallet 100: 1% is 1.00, so the cap 0.05 binds.
    assert M.skip_threshold(100) == pytest.approx(0.05)
    # Target wallet 2: 1% is 0.02, so the percent binds. This is the small-alt case.
    assert M.skip_threshold(2) == pytest.approx(0.02)
    dust = Exchange([_row("XUSDT", 175, 1.0, 99.97, 2)])
    st = _state({"XUSDT": 175.0}, cash=1000.0)
    report = M.topup(st, dust, slot=SLOT, key=KEY, now=NOW)
    assert dust.adds == []
    assert report["symbols"]["XUSDT"]["status"] == "deadband"
    assert report["symbols"]["XUSDT"]["shortfall"] == pytest.approx(0.03)
    # 0.40 used to be skipped by the 0.5 band (MOVR, W, ICNT, APR, PROM stayed at 2x).
    kept = Exchange([_row("MOVRUSDT", 175, 1.0, 99.6, 2)])
    st2 = _state({"MOVRUSDT": 175.0}, cash=1000.0)
    report2 = M.topup(st2, kept, slot=SLOT, key=KEY, now=NOW)
    assert kept.adds == [("MOVRUSDT", 0.4)]
    assert report2["symbols"]["MOVRUSDT"]["status"] == "topped_up"
    small = Exchange([_row("WUSDT", 3.5, 1.0, 1.97, 2)])
    st3 = _state({"WUSDT": 3.5}, cash=1000.0)
    report3 = M.topup(st3, small, slot=SLOT, key=KEY, now=NOW)
    assert report3["symbols"]["WUSDT"]["skip_threshold"] == pytest.approx(0.02)
    assert small.adds == [("WUSDT", 0.03)]


def test_underwater_mark_still_funds_the_entry_wallet():
    # Mark 0.09, entry 0.1584. 2x wallet 79.2 already exceeds mark/1.75, not entry/1.75.
    row = _row("AIOTUSDT", 1000, 0.1584, 79.2, 2, mark=0.09)
    ex = Exchange([row])
    st = _state({"AIOTUSDT": 1000.0}, cash=5000.0)
    report = M.topup(st, ex, slot=SLOT, key=KEY, now=NOW)
    assert ex.adds == [("AIOTUSDT", 11.32)]
    sym = report["symbols"]["AIOTUSDT"]
    assert sym["notional"] == pytest.approx(158.4)
    assert sym["mark_notional"] == pytest.approx(90)
    assert sym["liq_before"] == pytest.approx(0.0792)
    assert sym["liq_after"] < 0.0732 < sym["liq_before"]
    assert sym["margin_after"] >= 158.4 / 1.75


def test_hourly_reconcile_tops_up_an_existing_book_without_trading():
    row = _row("XUSDT", 175, 1.0, 87.5, 2)
    ex = Exchange([row])
    st = _state({"XUSDT": 175.0}, cash=1000.0)
    st[KEY]["last_run"] = {"result": {"skipped": "missed open window"}}
    pack = M.run_for_reconcile(st, {"control_log": [], "_last_run_only": True}, False, KEY, SLOT, ex)
    assert ex.adds == [("XUSDT", 12.5)] and ex.orders == []
    assert st[KEY]["book"] == {"XUSDT": 175.0} and "margin_topup" not in st[KEY]
    saved = {KEY: {"cash": 1000.0, "book": {"XUSDT": 175.0}, "last_run": st[KEY]["last_run"]}}
    reconcile.merge_e5_margin(saved, pack)
    assert saved[KEY]["book"] == {"XUSDT": 175.0}
    assert saved[KEY]["last_run"]["result"]["skipped"] == "missed open window"
    sym = saved[KEY]["margin_topup"]["symbols"]["XUSDT"]
    assert sym["added"] == 12.5 and sym["notional"] == 175
    assert sym["margin_before"] == pytest.approx(87.5) and sym["margin_after"] == pytest.approx(100)
    assert sym["liq_before"] > sym["liq_after"]
    assert saved["control_log"][0]["action"] == "e5_margin_topup" and saved["control_log"][0]["orders"] == 0


def test_reconcile_runs_the_topup_after_the_net_flush():
    src = inspect.getsource(reconcile.run_once)
    assert src.index(".flush()") < src.index("run_for_reconcile")
    assert "merge_e5_margin" in src


def test_direct_execute_tops_up_after_the_open(monkeypatch):
    from tests.test_demo_e5_exec import NOW as EXEC_NOW, Fut, PX, _state, _tgt

    class MarginFut(Fut):
        def __init__(self, px):
            super().__init__(px)
            self.entry = {}
            self.extra = {}
            self.adds = []
            self.available = 100000.0

        def new_order(self, **p):
            out = super().new_order(**p)
            self.entry[p["symbol"]] = float(self.px[p["symbol"]])
            return out

        def account(self):
            return {"availableBalance": self.available,
                    "assets": [{"asset": "USDT", "availableBalance": self.available}]}

        def position_risk(self):
            rows = []
            for sym, qty in self.pos.items():
                if abs(qty) < 1e-12:
                    continue
                px = float(self.px[sym])
                entry = float(self.entry.get(sym, px))
                wallet = abs(qty) * entry / 2 + float(self.extra.get(sym) or 0)
                rows.append({"symbol": sym, "positionAmt": str(qty), "entryPrice": str(entry), "markPrice": str(px),
                             "notional": str(qty * px), "isolatedWallet": str(wallet), "isolatedMargin": str(wallet),
                             "unRealizedProfit": "0", "leverage": "2", "marginType": "isolated",
                             "liquidationPrice": str(M.liquidation_price_for_margin(entry, qty, wallet, 0.0))})
            return rows

        def add_isolated_margin(self, symbol, amount):
            self.adds.append((symbol, round(float(amount), 2)))
            self.extra[symbol] = float(self.extra.get(symbol) or 0) + float(amount)
            self.available -= float(amount)
            return {"code": 200}

    monkeypatch.setenv("DEMO_ACCEPTED", D.SLOT)
    monkeypatch.setattr(X, "entry_block", lambda s, *a: None)
    fut = MarginFut(PX)
    st = _state()
    out = X.execute(st, fut, _tgt(), EXEC_NOW)
    assert {s for s, _ in fut.adds} == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert all(amount >= M.SKIP_CAP_USDT for _, amount in fut.adds)
    btc = st["demo_e5_exec"]["margin_topup"]["symbols"]["BTCUSDT"]
    assert btc["added"] >= M.SKIP_CAP_USDT
    assert btc["margin_after"] >= btc["notional"] / 1.75
    assert btc["liq_after"] < btc["liq_before"]
    assert out["sent"] and st["demo_e5_exec"]["cash"] > 0
    n = len(fut.adds)
    again = M.topup(st, fut, slot=D.SLOT, key="demo_e5_exec", now=NOW)
    assert len(fut.adds) == n and again["symbols"]["BTCUSDT"]["added"] == 0
