"""U1 on the USDC book (Robin 2026-10-20): BTCUSDC + ETHUSDC USD-M perps, cross 3x, notional_usdc.

Parity: the full analyst fixture replayed through paper (decision source) and executed by u1_live on a fake
USDC-M exchange must equal trades.csv trade by trade (symbols mapped BTCUSDT→BTCUSDC, ETHUSDT→ETHUSDC) when
the USDC perps trade at the fixture prices. Plus: validator, USDC margin wallet (not the USDT totals),
commission asset, own-perp reference opens, quote switch safety, status reconciliation vs Binance.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import paper_u1 as P  # noqa: E402
import u1_live as L  # noqa: E402
from allocation import validate_allocation  # noqa: E402

F = Path(__file__).resolve().parent / "fixtures" / "u1"
RT = 1e-8
USDC_SLOT = {"slot": "u1_btc_eth_usdc", "family": P.FAMILY, "strategy_id": P.STRATEGY_ID, "symbol": "BTCUSDC",
             "quote_currency": "USDC", "timeframe": "1d", "notional_usdc": 10000.0, "enabled": True,
             "params": {"symbols": ["BTCUSDC", "ETHUSDC"]}}
PM = {"BTC": "BTCUSDC", "ETH": "ETHUSDC"}


class FakeFCq:
    """USD-M stand-in on one margin asset; account() carries USDT totals AND per-asset rows."""

    def __init__(self, quote="USDC", step=0.0, usdc_avail=1e15, usdt_avail=1e15, usdc_mb=1e15, usdc_mm=0.0, fee=P.COST):
        self.q = quote
        self.step, self.fee = step, fee
        self.usdc_avail, self.usdt_avail, self.usdc_mb, self.usdc_mm = usdc_avail, usdt_avail, usdc_mb, usdc_mm
        self.px, self.pos = {}, {"BTC" + quote: 0.0, "ETH" + quote: 0.0}
        self.orders, self.comm, self.incomes, self.calls = {}, {}, [], []
        self.n = 0

    def exchange_info(self):
        return {"symbols": [{"symbol": s, "filters": [{"filterType": "MARKET_LOT_SIZE", "stepSize": str(self.step)},
                                                      {"filterType": "MIN_NOTIONAL", "notional": "0"}]} for s in self.pos]}

    def new_order(self, **p):
        self.calls.append(("new_order", p))
        q = float(p["quantity"]); sym = p["symbol"]; px = self.px[sym]
        self.pos[sym] += q if p["side"] == "BUY" else -q
        self.n += 1
        r = {"orderId": self.n, "status": "FILLED", "executedQty": repr(q), "avgPrice": repr(px),
             "side": p["side"], "clientOrderId": p["newClientOrderId"]}
        self.orders[(sym, p["newClientOrderId"])] = r
        self.comm[self.n] = (sym, q * px * self.fee)
        return r

    def get_order(self, sym, cid):
        return self.orders.get((sym, cid))

    def user_trades(self, sym, limit=50):
        return [{"orderId": i, "commission": c, "commissionAsset": self.q} for i, (s, c) in self.comm.items() if s == sym]

    def income(self, sym, typ, start, end=None):
        return [r for r in self.incomes if r["symbol"] == sym and start <= r["time"] <= (end or 1e20)]

    def account(self):
        return {"availableBalance": str(self.usdt_avail), "totalMarginBalance": "1e15", "totalMaintMargin": "0",
                "multiAssetsMargin": False,
                "assets": [{"asset": "USDT", "availableBalance": str(self.usdt_avail), "marginBalance": "1e15", "maintMargin": "0"},
                           {"asset": "USDC", "availableBalance": str(self.usdc_avail), "marginBalance": str(self.usdc_mb),
                            "maintMargin": str(self.usdc_mm), "walletBalance": str(self.usdc_mb), "unrealizedProfit": "0"}]}

    def position_risk(self, symbol=None):
        return [{"symbol": s, "positionAmt": repr(v), "markPrice": repr(self.px.get(s, 0.0)), "entryPrice": "0",
                 "unRealizedProfit": "0", "marginType": "cross", "leverage": "3"} for s, v in self.pos.items()]

    def position_mode_dual(self):
        return False

    def set_margin_type(self, sym, mt):
        self.calls.append(("margin", sym, mt))

    def set_leverage(self, sym, lev, cap=3):
        self.calls.append(("leverage", sym, lev))


def _replay_usdc(fc, monkeypatch, slot=None, open_mult=1.0):
    monkeypatch.setattr(L, "NOTIONAL_CAP_MULT", float("inf"))
    monkeypatch.setattr(L, "POS_CAP_MULT", float("inf"))
    bars, spot = P.load_fixture(F)
    doc = {"slots": [slot or USDC_SLOT]}
    state = {"positions": {}}
    ts_all = pd.to_datetime(bars["bar_open_utc"], utc=True)
    frames = {PM[k]: pd.DataFrame({"open": bars[f"{k}_open"].to_numpy(float) * open_mult,
                                   "low": bars[f"{k}_low"].to_numpy(float) * open_mult}, index=ts_all) for k in P.SYMS}

    def fetch_1h(sym, now):
        return frames[sym].loc[: pd.Timestamp(now)].tail(48)

    def rec(i, ts, st):
        sl = state.get("u1_live")
        if sl:
            for k in P.SYMS:
                r = float(bars[f"{k}_funding_rate"].iat[i])
                if sl["units"][k] and r:
                    fc.incomes.append({"symbol": PM[k], "incomeType": "FUNDING_FEE", "time": int(ts.value // 10**6),
                                       "income": -sl["units"][k] * float(bars[f"{k}_open"].iat[i]) * open_mult * r})
        d = st.get("last_fill_decision")
        if d and P._ts(d["fill_bar"]) == ts:
            for k in P.SYMS:
                fc.px[PM[k]] = float(bars[f"{k}_open"].iat[i]) * open_mult
            out = L.run_u1_live(state, st, (ts + timedelta(minutes=1)).to_pydatetime(), alloc_doc=doc,
                                approved_families=[P.FAMILY], live=True, allow_increase=True,
                                fc_factory=lambda: fc, fetch_1h=fetch_1h)
            assert out["active"] and not out.get("blocked"), out

    st = P.replay_bars(bars, spot, record=rec)
    L.run_u1_live(state, st, (ts_all.iloc[-1] + timedelta(minutes=61)).to_pydatetime(), alloc_doc=doc,
                  approved_families=[P.FAMILY], live=True, allow_increase=True, fc_factory=lambda: fc, fetch_1h=fetch_1h)
    return state, st


@pytest.fixture(scope="module")
def usdc_exact():
    mp = pytest.MonkeyPatch()
    fc = FakeFCq()
    state, st = _replay_usdc(fc, mp)
    mp.undo()
    return fc, state, st


def test_usdc_live_orders_equal_trades_csv(usdc_exact):
    fc, state, st = usdc_exact
    tr = pd.read_csv(F / "trades.csv")
    orders = [c[1] for c in fc.calls if c[0] == "new_order"]
    assert len(orders) == len(tr)
    exp = tr.sort_values(["fill_time_utc", "symbol"]).reset_index(drop=True)
    got = sorted(orders, key=lambda p: (p["newClientOrderId"].split("-")[1], p["symbol"]))
    for (_, e), o in zip(exp.iterrows(), got):
        assert o["symbol"] == e["symbol"].replace("USDT", "USDC") and o["side"] == e["side"] and o["type"] == "MARKET"
        assert float(o["quantity"]) == pytest.approx(abs(e["qty_delta"]), rel=RT)
        assert ("reduceOnly" in o) == (e["side"] == "SELL")
    by_key = {(f["fill_bar"], f["symbol"]): f for f in state["u1_live"]["fills"]}
    for _, e in exp.tail(L.KEEP).iterrows():
        f = by_key[(pd.Timestamp(e["fill_time_utc"]).strftime("%Y%m%d%H"), e["symbol"].replace("USDT", "USDC"))]
        assert f["reason"] == e["reason"]
        assert f["fee"] == pytest.approx(e["fee"], rel=RT) and not f["fee_estimated"]   # USDC commission read
        assert f["equity_before"] == pytest.approx(e["equity_before"], rel=RT)


def test_usdc_sleeve_equals_paper_book_and_only_usdc_perps(usdc_exact):
    fc, state, st = usdc_exact
    sl, book = state["u1_live"], st["books"]["lev"]
    assert sl["quote"] == "USDC" and sl["perps"] == PM
    for k in P.SYMS:
        assert sl["units"][k] == pytest.approx(book["units"][k], rel=RT)
        assert fc.pos[PM[k]] == pytest.approx(book["units"][k], rel=RT)
    assert sl["cash"] == pytest.approx(book["cash"], rel=RT)
    assert {c[1] for c in fc.calls if c[0] in ("margin", "leverage")} <= set(PM.values())
    assert {c[2] for c in fc.calls if c[0] == "margin"} == {"CROSSED"}
    assert {c[2] for c in fc.calls if c[0] == "leverage"} == {3}
    assert all(c[1]["symbol"].endswith("USDC") for c in fc.calls if c[0] == "new_order")


def test_usdc_sizes_on_its_own_perp_open(monkeypatch):
    """USDC perp opens 0.1% above USDT: first order qty = target · V / USDC open (not the USDT open)."""
    fc = FakeFCq()
    state, st = _replay_usdc(fc, monkeypatch, open_mult=1.001)
    tr = pd.read_csv(F / "trades.csv").sort_values(["fill_time_utc", "symbol"])
    o = sorted([c[1] for c in fc.calls if c[0] == "new_order"], key=lambda p: (p["newClientOrderId"].split("-")[1], p["symbol"]))[0]
    e = tr.iloc[0]
    assert float(o["quantity"]) == pytest.approx(e["target_weight"] * 10000.0 / (e["price"] * 1.001), rel=1e-9)


# ---------------------------------------------------------------- margin wallet, gates
def _dec():
    return {"last_fill_decision": {"fill_bar": "2026-10-21T00:00:00+00:00", "decision_bar": "2026-10-20T23:00:00+00:00",
                                   "indicator_day": "2026-10-20", "L": 1.6, "w1x": {"BTC": 0.5, "ETH": 0.5},
                                   "targets_lev": {"BTC": 0.8, "ETH": 0.8}, "fill_opens": {"BTC": 60000.0, "ETH": 2500.0}}}


NOW = pd.Timestamp("2026-10-21T00:01:30Z").to_pydatetime()
SLOT1000 = dict(USDC_SLOT, notional_usdc=1000.0)


def _run(state, fc, doc=None, fams=(P.FAMILY,)):
    fc.px = {"BTCUSDC": 60000.0, "ETHUSDC": 2500.0}
    return L.run_u1_live(state, _dec(), NOW, alloc_doc=doc or {"slots": [SLOT1000]}, approved_families=list(fams),
                         live=True, allow_increase=True, fc_factory=lambda: fc, fetch_1h=None)


def test_usdc_trade_uses_usdc_capital_and_symbols():
    fc = FakeFCq(); state = {"positions": {}}
    out = _run(state, fc)
    o = {c[1]["symbol"]: float(c[1]["quantity"]) for c in fc.calls if c[0] == "new_order"}
    assert o == pytest.approx({"BTCUSDC": 800 / 60000, "ETHUSDC": 800 / 2500})
    assert state["u1_live"]["capital_usdt"] == 1000.0 and out["quote"] == "USDC"


def test_usdc_margin_from_usdc_wallet_not_usdt_totals():
    # USDT wallet is huge, USDC wallet has 300 free → increase limited by USDC: (300/1.1)·3 ≈ 818
    fc = FakeFCq(usdc_avail=300.0, usdt_avail=1e9); state = {"positions": {}}
    out = _run(state, fc)
    add = sum(float(c[1]["quantity"]) * fc.px[c[1]["symbol"]] for c in fc.calls if c[0] == "new_order")
    assert add == pytest.approx(300 / L.IM_BUFFER * 3, rel=1e-9)
    assert any(s.get("reason") == "margin_limited" for s in out["skips"])


def test_usdc_margin_ratio_guard_uses_usdc_row():
    fc = FakeFCq(usdc_mb=1000.0, usdc_mm=600.0); state = {"positions": {}}
    _run(state, fc)
    assert not [c for c in fc.calls if c[0] == "new_order"]  # 60% ≥ block → no increases


def test_usdt_reserve_does_not_reduce_usdc_room():
    s_slot = {"slot": "short_arb_4h", "family": "short_trend_perp", "symbol": "ARBUSDT", "enabled": True,
              "notional_usdt": 750, "params": {"leverage": 1.0}}
    doc = {"slots": [SLOT1000, s_slot]}
    assert L.margin_reserve(doc, {"positions": {}}, ["short_trend_perp", P.FAMILY], "USDC") == 0.0
    assert L.margin_reserve(doc, {"positions": {}}, ["short_trend_perp", P.FAMILY], "USDT") == 750.0


def test_quote_switch_blocked_while_holding_and_resets_when_flat():
    fc = FakeFCq(quote="USDT"); state = {"positions": {}}
    usdt_slot = {"slot": "u1_btc_eth", "family": P.FAMILY, "strategy_id": P.STRATEGY_ID, "symbol": "BTCUSDT",
                 "timeframe": "1d", "notional_usdt": 1000.0, "enabled": True, "params": {}}
    fc.px = {"BTCUSDT": 60000.0, "ETHUSDT": 2500.0}
    L.run_u1_live(state, _dec(), NOW, alloc_doc={"slots": [usdt_slot]}, approved_families=[P.FAMILY], live=True,
                  allow_increase=True, fc_factory=lambda: fc, fetch_1h=None)
    assert state["u1_live"]["quote"] == "USDT" and state["u1_live"]["units"]["BTC"] > 0
    out = _run(state, FakeFCq())
    assert out["blocked"] == "quote_change_while_holding" and out["orders"] == 0
    state["u1_live"]["units"] = {"BTC": 0.0, "ETH": 0.0}
    fc2 = FakeFCq()
    _run(state, fc2)
    assert state["u1_live"]["quote"] == "USDC" and any(e["event"] == "quote_changed" for e in state["u1_live"]["events"])


def test_main_shared_perp_block_follows_u1_quote():
    import main
    st = {"u1_live": {"quote": "USDC", "units": {"BTC": 0.1, "ETH": 0.0}}}
    assert main.u1_perp_in_use(st, "BTCUSDC", "futures") and not main.u1_perp_in_use(st, "BTCUSDT", "futures")
    assert main.opposite_position_slot(st, "s_x", "BTCUSDT", "SHORT") == "u1_live"   # one direction per coin


# ---------------------------------------------------------------- validator
def _base(slots, **kw):
    return {"version": 1, "book_usdt": 5000, "book_usdc": 5000, "max_notional_per_order_usdt": 1500,
            "max_notional_per_order_usdc": 1500, "slots": slots, **kw}


def test_validator_accepts_u1_usdc_and_reserves_ethusdc():
    assert validate_allocation(_base([SLOT1000]), check_binance=False) == []
    clash = _base([SLOT1000, {"slot": "x", "family": "donchian_atr", "strategy_id": "y", "symbol": "ETHUSDC",
                              "quote_currency": "USDC", "timeframe": "4h", "notional_usdc": 500, "enabled": True,
                              "params": {"donch_n": 20, "stop_atr_mult": 1.5, "trail_atr_mult": 1.5}}])
    assert any("ETHUSDC" in e for e in validate_allocation(clash, check_binance=False))


def test_validator_rejects_wrong_u1_usdc_shapes_and_other_usdc_perps():
    bad_sym = dict(SLOT1000, symbol="BTCUSDT")
    assert validate_allocation(_base([bad_sym]), check_binance=False)
    bad_params = dict(SLOT1000, params={"symbols": ["BTCUSDT", "ETHUSDT"]})
    assert any("BTCUSDC" in e for e in validate_allocation(_base([bad_params]), check_binance=False))
    s_usdc = {"slot": "s", "family": "short_trend_perp", "strategy_id": "z", "symbol": "ARBUSDC", "quote_currency": "USDC",
              "timeframe": "4h", "notional_usdc": 750, "enabled": True,
              "params": {"ema_fast": 9, "ema_slow": 21, "stop_atr_mult": 2, "trail_atr_mult": 5, "filt": "fpos", "leverage": 1.0}}
    assert any("USD-M(USDT)" in e for e in validate_allocation(_base([s_usdc]), check_binance=False))
    over = dict(SLOT1000, notional_usdc=1500.0)
    big = _base([over, dict(over, slot="b", strategy_id="b"), dict(over, slot="c", strategy_id="c"),
                 dict(over, slot="d", strategy_id="d")])
    errs = validate_allocation(big, check_binance=False)
    assert any("book_usdc" in e for e in errs)


def test_repo_allocation_is_valid():
    import json
    doc = json.loads((Path(__file__).resolve().parents[2] / "config" / "allocation.json").read_text())
    assert validate_allocation(doc, check_binance=False) == []


# ---------------------------------------------------------------- /status USDC futures reconciliation
def test_status_usdc_futures_reconciles_with_binance():
    import api
    fc = FakeFCq(); fc.px = {"BTCUSDC": 60000.0, "ETHUSDC": 2500.0}
    fc.pos["BTCUSDC"] = 0.01
    state = {"u1_live": {"quote": "USDC", "units": {"BTC": 0.01, "ETH": 0.0}, "cash": 400.0, "capital_usdt": 1000.0}}
    r = api._futures_quote_reconcile(state, "USDC", {"slots": [SLOT1000]}, fc=fc)
    assert r["reconcile"]["ok"] and r["allocated"] == 1000.0
    assert r["positions"] == [pytest.approx(r["positions"][0])] and r["positions"][0]["symbol"] == "BTCUSDC"
    assert r["u1_sleeve"]["equity"] == pytest.approx(400 + 0.01 * 60000)
    fc.pos["ETHUSDC"] = 0.5   # someone traded ETHUSDC outside the trader
    r2 = api._futures_quote_reconcile(state, "USDC", {"slots": [SLOT1000]}, fc=fc)
    assert not r2["reconcile"]["ok"] and r2["reconcile"]["mismatches"][0]["symbol"] == "ETHUSDC"


# ---------------------------------------------------------------- regressions 2026-10-21
def test_hourly_run_while_holding_accepts_klines_frame_columns():
    """08:07 run failed with KeyError 'low': perp_market.klines_frame returns Open/High/Low/Close."""
    fc = FakeFCq(); state = {"positions": {}}
    _run(state, fc)
    assert state["u1_live"]["units"]["BTC"] > 0
    idx = pd.date_range("2026-10-21T00:00:00Z", periods=8, freq="1h")
    def fetch_1h(sym, now):  # same shape as perp_market.fetch_perp_klines
        px = fc.px[sym]
        return pd.DataFrame({"Open": px, "High": px, "Low": px * 0.99, "Close": px, "Volume": 1.0}, index=idx)
    n_before = len([c for c in fc.calls if c[0] == "new_order"])
    out = L.run_u1_live(state, _dec(), pd.Timestamp("2026-10-21T07:07:00Z").to_pydatetime(),
                        alloc_doc={"slots": [SLOT1000]}, approved_families=[P.FAMILY], live=True,
                        allow_increase=True, fc_factory=lambda: fc, fetch_1h=fetch_1h)
    assert out["active"] and not out.get("blocked") and out["orders"] == 0
    assert state["u1_live"]["liq_checked_bar"] == "2026-10-21T06:00:00+00:00"
    assert len([c for c in fc.calls if c[0] == "new_order"]) == n_before   # no trading change


def test_usdt_book_excludes_usdc_perp_positions():
    import api
    positions = [
        {"symbol": "ETHUSDC 多×3", "raw_symbol": "ETHUSDC", "venue": "futures", "market_value": 885.64, "unrealized_pnl": 3.47},
        {"symbol": "BTCUSDC 多×3", "raw_symbol": "BTCUSDC", "venue": "futures", "market_value": 913.97, "unrealized_pnl": 2.05},
        {"symbol": "ARBUSDT 空×1", "raw_symbol": "ARBUSDT", "venue": "futures", "market_value": 750.0, "unrealized_pnl": 1.0},
    ]
    b = api._books_overview({"USDT": 5473.52, "USDC": 5000.0}, positions, [], book_usdt=5000, book_usdc=5000)
    assert b["usdt"]["open_positions"] == 1 and b["usdt"]["positions_mv"] == 750.0
    b2 = api._books_overview({"USDT": 5473.52, "USDC": 5000.0}, positions[:2], [], book_usdt=5000, book_usdc=5000)
    assert b2["usdt"]["open_positions"] == 0 and b2["usdt"]["equity"] == 5473.52   # = Binance spot USDT
    assert b2["usdc"]["open_positions"] == 0 and b2["usdc"]["equity"] == 5000.0    # U1 is in books.usdc.futures


def test_usdc_ref_open_from_klines_frame_columns():
    """Live fetch returns Open/High/Low/Close: the USDC sleeve must still size on its own perp's 00:00 open."""
    fb = pd.Timestamp("2026-10-21T00:00:00Z")
    idx = pd.date_range("2026-10-20T20:00:00Z", periods=5, freq="1h")
    def fetch_1h(sym, now):
        px = {"BTCUSDC": 60100.0, "ETHUSDC": 2501.0}[sym]
        return pd.DataFrame({"Open": px, "High": px, "Low": px, "Close": px}, index=idx)
    opens = L.fill_opens_for(_dec()["last_fill_decision"], PM, fb, NOW, fetch_1h)
    assert opens == {"BTC": 60100.0, "ETH": 2501.0}
    fc = FakeFCq(); state = {"positions": {}}
    fc.px = {"BTCUSDC": 60100.0, "ETHUSDC": 2501.0}
    L.run_u1_live(state, _dec(), NOW, alloc_doc={"slots": [SLOT1000]}, approved_families=[P.FAMILY], live=True,
                  allow_increase=True, fc_factory=lambda: fc, fetch_1h=fetch_1h)
    o = {c[1]["symbol"]: float(c[1]["quantity"]) for c in fc.calls if c[0] == "new_order"}
    assert o == pytest.approx({"BTCUSDC": 800 / 60100, "ETHUSDC": 800 / 2501})


def test_u1_effective_leverage_labels_not_exchange_cap():
    """08:01 10/21: targets 0.883 each (L 1.766); BTC 0.01064 → step 0.001 → 0.011. Show actual ×, cap ×3 alongside."""
    import api
    state = {"u1_live": {"quote": "USDC", "slot": "u1_btc_eth_usdc", "cash": -794.8920081, "units": {"BTC": 0.011, "ETH": 0.361}}}
    rows = [{"venue": "futures", "raw_symbol": "BTCUSDC", "symbol": "BTCUSDC 多×3", "qty": 0.011, "mark": 82901.8, "leverage": 3},
            {"venue": "futures", "raw_symbol": "ETHUSDC", "symbol": "ETHUSDC 多×3", "qty": 0.361, "mark": 2443.67, "leverage": 3},
            {"venue": "futures", "raw_symbol": "ARBUSDT", "symbol": "ARBUSDT 空×1", "qty": 1.0, "mark": 1.0, "leverage": 1}]
    api._label_u1_effective_leverage(rows, state)
    eq = -794.8920081 + 0.011 * 82901.8 + 0.361 * 2443.67
    assert rows[0]["effective_leverage"] == pytest.approx(0.011 * 82901.8 / eq, rel=1e-3)
    assert rows[0]["symbol"].startswith("BTCUSDC 多 實際×0.91") and "上限×3" in rows[0]["symbol"]
    assert rows[0]["u1_gross_leverage"] == pytest.approx(1.79, abs=0.01)
    assert rows[2]["symbol"] == "ARBUSDT 空×1"
    pv = api._u1_live_public(state, rows)["sleeve"]
    assert pv["exchange_leverage_cap"] == 3 and pv["gross_leverage"] == pytest.approx(1.79, abs=0.01)
    # sizing per spec: want = target·V/open, rounded to the 0.001 lot
    assert L._round_step(0.8829321187226429 * 1000 / 82984.7, 0.001) == 0.011
    assert L._round_step(0.8829321187226429 * 1000 / 2447.77, 0.001) == 0.361
