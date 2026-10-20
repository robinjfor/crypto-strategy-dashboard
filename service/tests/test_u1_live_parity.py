"""U1 live order path (u1_live) — parity vs the analyst fixture and the safety gates.

Parity: the full 2020-06-24..2026-10-10 fixture is replayed through the paper engine (the decision source);
on every 00:00 UTC fill bar, u1_live.run_u1_live executes paper's decision against a fake Binance Demo
futures exchange (MARKET fills at the bar open, commission = spec cost 0.2%, FUNDING_FEE income = −units·open·rate
at the settlement bars). The live orders must equal trades.csv trade by trade (time, symbol, side, reason,
qty, units after, notional, fee, equity before). Exact parity runs with the slot notional cap lifted
(the fixture compounds 10k → 343k, far past 3 × slot capital); the cap itself is tested separately.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import paper_u1 as P  # noqa: E402
import u1_live as L  # noqa: E402

F = Path(__file__).resolve().parent / "fixtures" / "u1"
RT = 1e-8
SLOT = {"slot": "u1_btc_eth", "family": P.FAMILY, "strategy_id": P.STRATEGY_ID, "symbol": "BTCUSDT",
        "timeframe": "1d", "notional_usdt": 10000.0, "enabled": True, "params": {}}
DOC = {"slots": [SLOT]}


class FakeFC:
    """Binance Demo USDT-M futures stand-in: MARKET orders fill at self.px; records every call."""

    def __init__(self, step=0.0, min_notional=None, avail=1e15, mb=1e15, mm=0.0, fee=P.COST, dual=False):
        self.step, self.mn = step, min_notional or {}
        self.avail, self.mb, self.mm, self.fee, self.dual = avail, mb, mm, fee, dual
        self.px, self.pos = {}, {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        self.orders, self.comm, self.incomes, self.calls = {}, {}, [], []
        self.n = 0

    def exchange_info(self):
        return {"symbols": [{"symbol": s, "filters": [
            {"filterType": "MARKET_LOT_SIZE", "stepSize": str(self.step)},
            {"filterType": "MIN_NOTIONAL", "notional": str(self.mn.get(s, 0.0))}]} for s in self.pos]}

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
        return [{"orderId": i, "commission": c, "commissionAsset": "USDT"} for i, (s, c) in self.comm.items() if s == sym]

    def income(self, sym, typ, start, end=None):
        return [r for r in self.incomes if r["symbol"] == sym and start <= r["time"] <= (end or 1e20)]

    def account(self):
        return {"availableBalance": str(self.avail), "totalMarginBalance": str(self.mb), "totalMaintMargin": str(self.mm)}

    def position_risk(self, symbol=None):
        return [{"symbol": s, "positionAmt": repr(v)} for s, v in self.pos.items()]

    def position_mode_dual(self):
        return self.dual

    def set_margin_type(self, sym, mt):
        self.calls.append(("margin", sym, mt))

    def set_leverage(self, sym, lev, cap=3):
        self.calls.append(("leverage", sym, lev))


def _replay_live(fc, monkeypatch, *, lift_cap=True, slot=None):
    if lift_cap:
        monkeypatch.setattr(L, "NOTIONAL_CAP_MULT", float("inf"))
        monkeypatch.setattr(L, "POS_CAP_MULT", float("inf"))
    bars, spot = P.load_fixture(F)
    doc = {"slots": [slot or SLOT]}
    state = {"positions": {}}
    frames = {}
    ts_all = pd.to_datetime(bars["bar_open_utc"], utc=True)
    for k in P.SYMS:
        frames[L.PERP[k]] = pd.DataFrame({"low": bars[f"{k}_low"].to_numpy(float)}, index=ts_all)

    def fetch_1h(sym, now):
        fr = frames[sym]
        return fr.loc[: pd.Timestamp(now)].tail(48)

    gross_seen = []

    def rec(i, ts, st):
        sl = state.get("u1_live")
        rate = {k: float(bars[f"{k}_funding_rate"].iat[i]) for k in P.SYMS}
        if sl:
            for k in P.SYMS:
                u = sl["units"][k]
                if u and rate[k]:
                    fc.incomes.append({"symbol": L.PERP[k], "incomeType": "FUNDING_FEE", "time": int(ts.value // 10**6),
                                       "income": -u * float(bars[f"{k}_open"].iat[i]) * rate[k]})
        d = st.get("last_fill_decision")
        if d and P._ts(d["fill_bar"]) == ts:
            for k in P.SYMS:
                fc.px[L.PERP[k]] = float(bars[f"{k}_open"].iat[i])
            out = L.run_u1_live(state, st, (ts + timedelta(minutes=1)).to_pydatetime(), alloc_doc=doc,
                                approved_families=[P.FAMILY], live=True, allow_increase=True,
                                fc_factory=lambda: fc, fetch_1h=fetch_1h)
            assert out["active"] and not out.get("blocked"), out
            sl = state["u1_live"]
            gross_seen.append(sum(sl["units"][k] * fc.px[L.PERP[k]] for k in P.SYMS))

    st = P.replay_bars(bars, spot, record=rec)
    # one more hourly run after the last bar: books the funding settled since the last fill
    L.run_u1_live(state, st, (ts_all.iloc[-1] + timedelta(minutes=61)).to_pydatetime(), alloc_doc=doc,
                  approved_families=[P.FAMILY], live=True, allow_increase=True, fc_factory=lambda: fc,
                  fetch_1h=fetch_1h)
    return state, st, gross_seen


@pytest.fixture(scope="module")
def exact():
    mp = pytest.MonkeyPatch()
    fc = FakeFC()
    state, st, _ = _replay_live(fc, mp)
    mp.undo()
    return fc, state, st


def test_live_orders_equal_trades_csv_trade_by_trade(exact):
    fc, state, st = exact
    tr = pd.read_csv(F / "trades.csv")
    fills = state["u1_live"]["fills"]
    # KEEP caps the stored list; compare against the order log for the full history
    orders = [c[1] for c in fc.calls if c[0] == "new_order"]
    assert len(orders) == len(tr) == len(st["fills"])
    exp = tr.sort_values(["fill_time_utc", "symbol"]).reset_index(drop=True)
    got = sorted(orders, key=lambda p: (p["newClientOrderId"].split("-")[1], p["symbol"]))
    for (_, e), o in zip(exp.iterrows(), got):
        tag = pd.Timestamp(e["fill_time_utc"]).strftime("%Y%m%d%H")
        assert o["newClientOrderId"] == f"u1-{tag}-{e['symbol'][:3]}"
        assert o["symbol"] == e["symbol"] and o["side"] == e["side"] and o["type"] == "MARKET"
        assert float(o["quantity"]) == pytest.approx(abs(e["qty_delta"]), rel=RT)
        assert ("reduceOnly" in o) == (e["side"] == "SELL")
    by_key = {(f["fill_bar"], f["symbol"]): f for f in fills}
    n_cmp = 0
    for _, e in exp.iterrows():
        f = by_key.get((pd.Timestamp(e["fill_time_utc"]).strftime("%Y%m%d%H"), e["symbol"]))
        if f is None:
            continue  # older than KEEP
        n_cmp += 1
        assert f["reason"] == e["reason"]
        assert f["qty_signed"] == pytest.approx(e["qty_delta"], rel=RT)
        assert f["units_after"] == pytest.approx(e["units_after"], rel=RT)
        assert f["price"] == pytest.approx(e["price"], rel=RT)
        assert f["notional"] == pytest.approx(e["notional"], rel=RT)
        assert f["fee"] == pytest.approx(e["fee"], rel=RT)
        assert f["equity_before"] == pytest.approx(e["equity_before"], rel=RT)
        assert f["target_weight"] == pytest.approx(e["target_weight"], rel=RT)
        assert f["weight_before"] == pytest.approx(e["weight_before"], rel=RT, abs=1e-12)
    assert n_cmp == min(len(tr), L.KEEP)


def test_live_sleeve_equals_paper_levered_book(exact):
    fc, state, st = exact
    sl, book = state["u1_live"], st["books"]["lev"]
    for k in P.SYMS:
        assert sl["units"][k] == pytest.approx(book["units"][k], rel=RT)
        assert fc.pos[L.PERP[k]] == pytest.approx(book["units"][k], rel=RT)
    assert sl["cash"] == pytest.approx(book["cash"], rel=RT)
    assert sl["funding"] == pytest.approx(book["funding_pnl"], rel=1e-6)
    assert {c[2] for c in fc.calls if c[0] == "margin"} == {"CROSSED"}
    assert {c[2] for c in fc.calls if c[0] == "leverage"} == {3}
    assert not any(c[0] == "new_order" and c[1].get("type") != "MARKET" for c in fc.calls)  # no stops


def test_live_with_real_lot_steps_tracks_backtest(monkeypatch):
    fc = FakeFC(step=0.001, min_notional={"BTCUSDT": 100.0, "ETHUSDT": 20.0})
    state, st, _ = _replay_live(fc, monkeypatch)
    tr = pd.read_csv(F / "trades.csv")
    orders = [c[1] for c in fc.calls if c[0] == "new_order"]
    keys = {(o["newClientOrderId"].split("-")[1], o["symbol"], o["side"]) for o in orders}
    exp = {(pd.Timestamp(r.fill_time_utc).strftime("%Y%m%d%H"), r.symbol, r.side) for r in tr.itertuples()}
    assert len(keys & exp) >= 0.98 * len(exp)
    for o in orders:
        assert abs(float(o["quantity"]) / 0.001 - round(float(o["quantity"]) / 0.001)) < 1e-6 or "reduceOnly" in o
    px = {k: float(pd.read_csv(F / "bars.csv.gz")[f"{k}_close"].iat[-1]) for k in P.SYMS}
    e_live = L.sleeve_equity(state["u1_live"], px)
    e_bt = P.book_equity(st["books"]["lev"], px)
    assert e_live == pytest.approx(e_bt, rel=0.01)


def test_notional_cap_by_allocation_slot(monkeypatch):
    fc = FakeFC()
    state, st, gross = _replay_live(fc, monkeypatch, lift_cap=False)
    C = SLOT["notional_usdt"]
    assert max(gross) <= 3 * C * (1 + 1e-9)          # never above 3 × slot capital (at the fill price)
    tr = pd.read_csv(F / "trades.csv")
    orders = [c[1] for c in fc.calls if c[0] == "new_order"]
    # identical to the backtest until the sleeve first grows past the cap
    first = sorted(orders, key=lambda p: (p["newClientOrderId"].split("-")[1], p["symbol"]))[:6]
    exp = tr.sort_values(["fill_time_utc", "symbol"]).head(6)
    for o, (_, e) in zip(first, exp.iterrows()):
        assert float(o["quantity"]) == pytest.approx(abs(e["qty_delta"]), rel=RT)
    lim = dict(SLOT, params={"max_gross_notional_usdt": 5000.0})
    fc2 = FakeFC()
    _, _, g2 = _replay_live(fc2, monkeypatch, lift_cap=False, slot=lim)
    assert max(g2) <= 5000.0 * (1 + 1e-9)


# ---------------------------------------------------------------- gates: approval alone opens nothing
def _dec(fill_bar="2026-10-12T00:00:00+00:00"):
    return {"last_fill_decision": {"fill_bar": fill_bar, "decision_bar": "2026-10-11T23:00:00+00:00",
                                   "indicator_day": "2026-10-11", "L": 1.6, "w1x": {"BTC": 0.5, "ETH": 0.5},
                                   "targets_lev": {"BTC": 0.8, "ETH": 0.8},
                                   "fill_opens": {"BTC": 60000.0, "ETH": 2500.0}}}


NOW = pd.Timestamp("2026-10-12T00:01:30Z").to_pydatetime()


def _run(state, doc, fams, fc, live=True, allow=True, paper=None):
    fc.px = {"BTCUSDT": 60000.0, "ETHUSDT": 2500.0}
    return L.run_u1_live(state, paper or _dec(), NOW, alloc_doc=doc, approved_families=fams, live=live,
                         allow_increase=allow, fc_factory=lambda: fc, fetch_1h=None)


def test_approval_alone_opens_nothing():
    import json
    created = []

    def factory():
        created.append(1)
        return FakeFC()
    real_alloc = json.loads((Path(__file__).resolve().parents[2] / "config" / "allocation.json").read_text())
    # since 2026-10-20 the repo has the U1 USDC slot (Robin); approval alone = the repo minus that slot
    real_alloc["slots"] = [s_ for s_ in real_alloc["slots"] if s_.get("family") != P.FAMILY]
    for doc in (real_alloc, {"slots": []}, {"slots": [dict(SLOT, enabled=False)]}):
        state = {"positions": {}, "approved_families": [P.FAMILY]}
        out = L.run_u1_live(state, _dec(), NOW, alloc_doc=doc, approved_families=[P.FAMILY], live=True,
                            allow_increase=True, fc_factory=factory)
        assert out == {"active": False, "reason": "no_allocation_slot", "orders": 0}
        assert "u1_live" not in state
    assert created == []          # no exchange client is ever constructed


def test_slot_without_approval_opens_nothing_and_dry_run_opens_nothing():
    fc = FakeFC()
    state = {"positions": {}}
    assert _run(state, DOC, ["donchian_atr", "short_trend_perp"], fc)["reason"] == "family_not_approved"
    assert _run(state, DOC, [P.FAMILY], fc, live=False)["reason"] == "not_live_mode"
    assert fc.calls == [] and "u1_live" not in state


def test_approval_and_slot_trade_at_the_decision():
    fc = FakeFC()
    state = {"positions": {}}
    out = _run(state, DOC, [P.FAMILY], fc)
    assert out["active"] and out["orders"] == 2
    o = [c[1] for c in fc.calls if c[0] == "new_order"]
    # V = 10000, wt 0.8 each → 8000 notional per coin at the 00:00 open
    assert {x["symbol"]: float(x["quantity"]) for x in o} == pytest.approx({"BTCUSDT": 8000 / 60000, "ETHUSDT": 8000 / 2500})
    # rerun in the same hour: no second order (last_fill_bar + idempotent client ids)
    assert _run(state, DOC, [P.FAMILY], fc)["orders"] == 0
    assert len([c for c in fc.calls if c[0] == "new_order"]) == 2


def test_late_decision_is_not_caught_up():
    fc = FakeFC()
    state = {"positions": {}}
    out = _run(state, DOC, [P.FAMILY], fc, paper=_dec("2026-10-11T00:00:00+00:00"))
    assert out["decision"]["skipped"] == "outside_2h_window" and out["orders"] == 0


def test_one_direction_per_coin_and_shared_perp():
    fc = FakeFC()
    state = {"positions": {"s2_arb": {"status": "FILLED", "symbol": "BTCUSDT", "side": "SHORT", "venue": "futures"},
                           "c5_eth": {"status": "FILLED", "symbol": "ETHUSDT", "side": "LONG", "venue": "spot"}}}
    out = _run(state, DOC, [P.FAMILY], fc)
    o = [c[1]["symbol"] for c in fc.calls if c[0] == "new_order"]
    assert o == ["ETHUSDT"]                     # spot long on ETH is the same direction → allowed
    assert any("opposite_position:s2_arb" in s["reason"] for s in out["skips"])
    state["positions"]["a_eth"] = {"status": "FILLED", "symbol": "ETHUSDT", "side": "LONG", "venue": "futures"}
    assert L.coin_conflicts(state)["ETH"] == "shared_perp:a_eth"
    # and the main runner blocks others while U1 holds the coin
    import main
    st2 = {"positions": {}, "u1_live": {"units": {"BTC": 0.1, "ETH": 0.0}}}
    assert main.opposite_position_slot(st2, "s_x", "BTCUSDT", "SHORT") == "u1_live"
    assert main.opposite_position_slot(st2, "c_x", "BTCUSDC", "LONG") is None
    assert main.u1_perp_in_use(st2, "BTCUSDT", "futures") and not main.u1_perp_in_use(st2, "BTCUSDT", "spot")
    assert not main.u1_perp_in_use(st2, "ETHUSDT", "futures")


def test_margin_limited_by_free_balance_and_reserve():
    fc = FakeFC(avail=3000.0)
    doc = {"slots": [SLOT, {"slot": "s2_arb", "family": "short_trend_perp", "enabled": True,
                            "notional_usdt": 1000.0, "params": {"leverage": 1.0}}]}
    state = {"positions": {}}
    out = _run(state, doc, [P.FAMILY, "short_trend_perp"], fc)
    # (3000 − 1000 reserve)/1.10 × 3 = 5454.5 total added notional (instead of 16000)
    o = [c[1] for c in fc.calls if c[0] == "new_order"]
    added = sum(float(x["quantity"]) * fc.px[x["symbol"]] for x in o)
    assert added == pytest.approx(2000 / 1.10 * 3, rel=1e-9)
    assert any(s["reason"] == "margin_limited" for s in out["skips"])


def test_account_margin_ratio_guards():
    fc = FakeFC(mb=10000.0, mm=6000.0)          # 60% → no increases
    state = {"positions": {}}
    out = _run(state, DOC, [P.FAMILY], fc)
    assert out["orders"] == 0 and any(a.get("action") == "no_increases" for a in out["alerts"])
    fc = FakeFC()
    state = {"positions": {}}
    _run(state, DOC, [P.FAMILY], fc)
    fc.mm, fc.mb = 9000.0, 10000.0               # 90% → U1 closes everything (reduce-only)
    out = L.run_u1_live(state, _dec(), NOW + timedelta(hours=1), alloc_doc=DOC, approved_families=[P.FAMILY],
                        live=True, allow_increase=True, fc_factory=lambda: fc)
    closes = [c[1] for c in fc.calls if c[0] == "new_order"][2:]
    assert len(closes) == 2 and all(c["side"] == "SELL" and c["reduceOnly"] == "true" for c in closes)
    assert all(v == 0 for v in state["u1_live"]["units"].values())


def test_sleeve_liquidation_emulation_closes_all():
    fc = FakeFC()
    state = {"positions": {}}
    _run(state, DOC, [P.FAMILY], fc)
    sl = state["u1_live"]
    sl["cash"] = -15900.0      # sleeve equity ≈ 100 at the entry prices
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-12T00:00Z")])
    low = {"BTCUSDT": 59000.0, "ETHUSDT": 2450.0}
    out = L.run_u1_live(state, _dec(), NOW + timedelta(hours=1), alloc_doc=DOC, approved_families=[P.FAMILY],
                        live=True, allow_increase=True, fc_factory=lambda: fc,
                        fetch_1h=lambda s, n: pd.DataFrame({"low": [low[s]]}, index=idx))
    assert any(e["event"] == "sleeve_liquidation" for e in sl["events"])
    assert all(v == 0 for v in sl["units"].values()) and out["orders"] == 2


def test_position_mismatch_or_hedge_mode_blocks():
    fc = FakeFC()
    state = {"positions": {}}
    _run(state, DOC, [P.FAMILY], fc)
    fc.pos["BTCUSDT"] += 0.5   # someone else traded BTCUSDT
    out = L.run_u1_live(state, _dec("2026-10-13T00:00:00+00:00"), NOW + timedelta(days=1), alloc_doc=DOC,
                        approved_families=[P.FAMILY], live=True, allow_increase=True, fc_factory=lambda: fc)
    assert out["blocked"] == "position_mismatch:BTCUSDT" and out["orders"] == 0
    fc2 = FakeFC(dual=True)
    out = _run({"positions": {}}, DOC, [P.FAMILY], fc2)
    assert out["blocked"] == "hedge_mode_account" and fc2.calls == []


def test_crash_after_order_is_recovered_once():
    fc = FakeFC()
    state = {"positions": {}}
    _run(state, DOC, [P.FAMILY], fc)
    saved = state["u1_live"]
    import copy
    lost = copy.deepcopy(saved)   # simulate: orders sent, state not saved
    lost.update({"cash": 10000.0, "units": {"BTC": 0.0, "ETH": 0.0}, "fills": [], "fees": 0.0, "last_fill_bar": None})
    state2 = {"positions": {}, "u1_live": lost}
    out = _run(state2, DOC, [P.FAMILY], fc)
    assert len([c for c in fc.calls if c[0] == "new_order"]) == 2       # nothing re-sent
    assert state2["u1_live"]["units"] == pytest.approx(saved["units"])
    assert state2["u1_live"]["cash"] == pytest.approx(saved["cash"])
    assert not out.get("blocked")


def test_revoked_family_only_reduces():
    fc = FakeFC()
    state = {"positions": {}}
    _run(state, DOC, [P.FAMILY], fc)
    paper = _dec("2026-10-13T00:00:00+00:00")
    paper["last_fill_decision"]["targets_lev"] = {"BTC": 1.5, "ETH": 0.0}
    out = L.run_u1_live(state, paper, NOW + timedelta(days=1), alloc_doc={"slots": []}, approved_families=[],
                        live=True, allow_increase=True, fc_factory=lambda: fc)
    new = [c[1] for c in fc.calls if c[0] == "new_order"][2:]
    assert [(o["symbol"], o["side"]) for o in new] == [("ETHUSDT", "SELL")]
    assert any("increase_blocked" in s["reason"] for s in out["skips"])


def test_allocation_validation_for_u1_slot():
    from allocation import validate_allocation
    base = {"book_usdt": 5000, "slots": [dict(SLOT, notional_usdt=1000.0)]}
    assert validate_allocation(base, check_binance=False) == []
    bad = {"book_usdt": 5000, "slots": [dict(SLOT, notional_usdt=1000.0, params={"stop_atr_mult": 2})]}
    assert any("無停損" in e for e in validate_allocation(bad, check_binance=False))
    clash = {"book_usdt": 5000, "slots": [dict(SLOT, notional_usdt=1000.0),
                                          {"slot": "x", "family": "short_trend_perp", "strategy_id": "y", "symbol": "ETHUSDT",
                                           "timeframe": "4h", "notional_usdt": 500, "enabled": True,
                                           "params": {"ema_fast": 9, "ema_slow": 21, "stop_atr_mult": 2, "trail_atr_mult": 4}}]}
    assert any("ETHUSDT" in e for e in validate_allocation(clash, check_binance=False))


def test_generic_slot_path_never_evaluates_u1(monkeypatch):
    import strategy
    seen = []
    monkeypatch.setattr(strategy, "btc_daily_regime_series", lambda c: None)
    st = {"runtime_eval_slots": [{"id": "u1_btc_eth", "family": P.FAMILY, "symbol": "BTCUSDT", "tf": "1d"}]}
    try:
        res = strategy.evaluate_all(None, st)
    except Exception as e:  # noqa: BLE001
        seen.append(e)
    assert not seen and all(r.get("slot") != "u1_btc_eth" for r in res)
