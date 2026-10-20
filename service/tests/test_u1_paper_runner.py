"""U1 paper runner: live hourly path == replay (incl. the 00:01 UTC daily fill), catch-up, unpublished funding (deferred open phase),
isolation from live trader state/books, no exchange orders, not approvable."""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import paper_u1 as P  # noqa: E402

F = Path(__file__).resolve().parent / "fixtures" / "u1"
SNAP = pd.Timestamp("2026-09-07 20:00", tz="UTC")       # before the 2026-09-09 00:00 ETH rebalance fill
END = pd.Timestamp("2026-09-10 02:00", tz="UTC")


@pytest.fixture(scope="module")
def fx():
    bars, spot = P.load_fixture(F)
    ts = pd.to_datetime(bars["bar_open_utc"], utc=True)
    snap = {}
    ref = {}

    def rec(i, t, st):
        if t == SNAP:
            snap["st"] = copy.deepcopy(st)
        if SNAP < t <= END:
            ref[t] = (P.book_equity(st["books"]["lev"]), P.book_equity(st["books"]["x1"]),
                      dict(st["books"]["lev"]["units"]), len(st["fills"]))
    full = P.replay_bars(bars[ts <= END + pd.Timedelta(hours=1)].reset_index(drop=True), spot, record=rec)
    return bars.assign(ts=ts), spot, snap["st"], ref, full


def fetchers(bars, spot, *, funding_lag=False, missing_spot_day=None):
    by = {k: bars.set_index("ts")[[f"{k}_{c}" for c in ("open", "high", "low", "close", "funding_rate")]] for k in P.SYMS}
    calls = {"spot": 0}

    def perp(sym, limit, now_ref=[None]):
        raise AssertionError("replaced per run")

    def make(now):
        cur = pd.Timestamp(now).floor("h")

        def perp_1h(sym, limit):
            d = by[sym][by[sym].index <= cur].tail(limit)
            out = pd.DataFrame({"Open": d[f"{sym}_open"], "High": d[f"{sym}_high"], "Low": d[f"{sym}_low"], "Close": d[f"{sym}_close"]})
            out.loc[cur, ["High", "Low", "Close"]] = out.loc[cur, "Open"]   # forming bar: only its open is known
            return out

        def funding(sym):
            r = by[sym][f"{sym}_funding_rate"]
            r = r[(r != 0) & (r.index <= cur)]
            if funding_lag:
                r = r[r.index < cur]
            return r

        def spot_daily(sym, now_):
            calls["spot"] += 1
            s = spot[sym]
            s = s[s.index + pd.Timedelta(days=1) <= pd.Timestamp(now_)]
            if missing_spot_day is not None:
                s = s[s.index != missing_spot_day]
            return s
        return {"perp_1h": perp_1h, "funding": funding, "spot_daily": spot_daily}
    return make, calls


def _runs(st, bars, spot, hours, **kw):
    make, _ = fetchers(bars, spot, **kw)
    out = {}
    for now in hours:
        out[now] = P.step_live(st, now.to_pydatetime(), make(now))
    return out


def _hourly(start, end):
    return list(pd.date_range(start, end, freq="1h") + pd.Timedelta(minutes=1))


def _check_against_replay(st, ref, upto):
    eq, eq1, units, nf = ref[upto]
    assert P.book_equity(st["books"]["lev"]) == pytest.approx(eq, rel=1e-12)
    assert P.book_equity(st["books"]["x1"]) == pytest.approx(eq1, rel=1e-12)
    for k in P.SYMS:
        assert st["books"]["lev"]["units"][k] == pytest.approx(units[k], rel=1e-12)
    assert len(st["fills"]) == nf


def test_hourly_live_runs_equal_replay_and_fill_at_0001(fx):
    bars, spot, snap, ref, full = fx
    st = copy.deepcopy(snap)
    runs = _runs(st, bars, spot, _hourly(SNAP + pd.Timedelta(hours=1), END + pd.Timedelta(hours=1)))
    _check_against_replay(st, ref, END)
    fill_run = runs[pd.Timestamp("2026-09-09 00:01", tz="UTC")]
    assert fill_run["open_phase"] == {"bar": "2026-09-09T00:00:00+00:00", "funding_source": "history"}
    assert [(f["symbol"], f["reason"], f["fill_time_utc"]) for f in fill_run["fills"]] == [
        ("ETHUSDT", "rebalance_band", "2026-09-09T00:00:00+00:00")]
    tr = pd.read_csv(F / "trades.csv")
    e = tr[tr.fill_time_utc == "2026-09-09T00:00:00+00:00"].iloc[0]
    assert fill_run["fills"][0]["price"] == e["price"] and fill_run["fills"][0]["target_weight"] == pytest.approx(e["target_weight"], rel=1e-8)
    # decisions happen at the 00:01 run (23:00 bar close) and are filled in that same run
    assert fill_run["decisions"] and fill_run["decisions"][0]["indicator_day"] == "2026-09-08"
    assert all(r["stopped"] is None for r in runs.values())


def test_catch_up_single_run_equals_hourly(fx):
    bars, spot, snap, ref, _ = fx
    st = copy.deepcopy(snap)
    _runs(st, bars, spot, [END + pd.Timedelta(hours=1, minutes=1)])
    _check_against_replay(st, ref, END)


def test_unpublished_funding_defers_open_phase_same_result(fx):
    bars, spot, snap, ref, _ = fx
    st = copy.deepcopy(snap)
    runs = _runs(st, bars, spot, _hourly(SNAP + pd.Timedelta(hours=1), END + pd.Timedelta(hours=1)), funding_lag=True)
    r0 = runs[pd.Timestamp("2026-09-09 00:01", tz="UTC")]
    assert r0["open_phase"]["deferred"] and not r0["fills"]
    r1 = runs[pd.Timestamp("2026-09-09 01:01", tz="UTC")]
    assert [(f["symbol"], f["fill_time_utc"]) for f in r1["fills"]] == [("ETHUSDT", "2026-09-09T00:00:00+00:00")]
    _check_against_replay(st, ref, END)


def test_missing_spot_day_defers_decision(fx):
    bars, spot, snap, ref, _ = fx
    st = copy.deepcopy(snap)
    day = pd.Timestamp("2026-09-08", tz="UTC")
    make_missing, _ = fetchers(bars, spot, missing_spot_day=day)
    s = P.step_live(st, datetime(2026, 9, 9, 0, 1, tzinfo=timezone.utc), make_missing(pd.Timestamp("2026-09-09 00:01", tz="UTC")))
    assert s["stopped"] and "2026-09-08" in s["stopped"] and not s["fills"]
    assert st["last_closed_bar"] == "2026-09-08T22:00:00+00:00"
    _runs(st, bars, spot, [END + pd.Timedelta(hours=1, minutes=1)])
    _check_against_replay(st, ref, END)


def test_live_from_seed_first_paper_decision(monkeypatch, tmp_path):
    """Seeded paper book continues the backtest: the 2026-10-11 00:00 band check (day 10-10 decision)
    is within band (no trade); the first paper decision that can trade fills 2026-10-12 00:00 UTC (08:00 TPT)."""
    st = P.initial_paper_state()
    eq0 = P.book_equity(st["books"]["lev"])
    o = {"BTC": 83000.0, "ETH": 2505.0}
    s = {k: dict(st["books"]["lev"]["units"]) for k in ("u",)}
    fills = P.open_phase(st, "2026-10-11T00:00:00+00:00", o, {"BTC": 0.0001, "ETH": 0.0001})
    assert fills == []                                     # weights 0.97 / 0.85 vs target 0.798: inside 25% band
    assert st["books"]["lev"]["units"] == s["u"]
    assert P.book_equity(st["books"]["lev"], o) < eq0 * 1.01
    v = P.public_view(st)
    assert v["next_fill_utc"] == "2026-10-12T00:00:00+00:00" and v["label_zh"] == "模擬（非實盤）" and v["in_books"] is False


def test_paper_store_isolated_from_trader_state(tmp_path, monkeypatch):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    trader = tmp_path / "trader_state.json"
    trader.write_text(json.dumps({"approved": {"x": 1}, "positions": {"p": 1}}))
    before = trader.read_text()
    monkeypatch.setenv("STATE_LOCAL_PATH", str(trader))
    store = P.PaperStore(local_path=str(tmp_path / "paper.json"))
    assert store._s.object_name == "trader/paper/u1_state.json" != "trader/state.json"
    bars, spot = P.load_fixture(F)
    make, _ = fetchers(bars.assign(ts=pd.to_datetime(bars["bar_open_utc"], utc=True)), spot)
    # seeded at 2026-10-10 23:00; fixture ends there, so ask for a run at 23:30 (no new bars) — exercises save/load only
    s = P.run_paper(datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc), make(pd.Timestamp("2026-10-10 23:30", tz="UTC")), store)
    assert s["seeded"] is True and s["ok"] is True
    assert trader.read_text() == before
    saved = json.loads((tmp_path / "paper.json").read_text())
    assert saved["mode"] == "paper_only" and saved["code"] == "U1"
    s2 = P.run_paper(datetime(2026, 10, 10, 23, 40, tzinfo=timezone.utc), make(pd.Timestamp("2026-10-10 23:40", tz="UTC")), store)
    assert s2["seeded"] is False
    assert P.PAPER_LOCAL != "/tmp/trader_state.json"


def test_no_exchange_order_paths():
    src = Path(P.__file__).read_text()
    for bad in ("binance_client", "futures_client", "futures_execution", "import execution", "BinanceClient", "_signed",
                ".post(", "requests.post", "/fapi/v1/order", "/api/v3/order", "newOrder", "api_key", "X-MBX-APIKEY"):
        assert bad not in src, bad
    import ast
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert mods <= {"__future__", "json", "logging", "math", "os", "datetime", "pathlib", "numpy", "pandas", "requests", "perp_market", "state_store"}


def test_live_run_makes_only_public_gets(monkeypatch, tmp_path, fx):
    import requests
    seen = []

    class R:
        status_code = 200

        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    def fake_get(url, params=None, **kw):
        seen.append(url)
        assert url.startswith(("https://fapi.binance.com/", "https://data-api.binance.vision/")), url
        if "klines" in url and "fapi" in url:
            return R([[1, "1", "1", "1", "1", "0", 2, "0", 0, "0", "0", "0"]])
        if "fundingRate" in url:
            return R([])
        if "klines" in url:
            return R([])
        raise AssertionError(url)
    for m in ("post", "put", "delete", "patch"):
        monkeypatch.setattr(requests, m, lambda *a, **k: (_ for _ in ()).throw(AssertionError("non-GET request")))
    monkeypatch.setattr(requests, "get", fake_get)
    import perp_market
    monkeypatch.setattr(perp_market.requests, "get", fake_get)
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    s = P.run_paper(datetime(2026, 10, 11, 9, 1, tzinfo=timezone.utc), None, P.PaperStore(local_path=str(tmp_path / "p.json")))
    assert seen and s["ok"] is False and s["stopped"]  # fake data has no bars → stops safely, no orders anywhere
    assert {u.split("/")[2] for u in seen} <= {"fapi.binance.com", "data-api.binance.vision"}


def test_u1_approvable_by_exception_but_not_in_allocation():
    # Robin 2026-10-11: U1 in 待審核 by exception (approvable); the allocation still has no U1 slot,
    # so approval alone trades nothing (see test_u1_live_parity.py).
    from slots import SUPPORTED_FAMILIES, approve_family
    from allocation import RUNNER_FAMILIES
    assert P.FAMILY in SUPPORTED_FAMILIES and P.FAMILY in RUNNER_FAMILIES
    st = {}
    approve_family(st, P.FAMILY, at="2026-10-11T16:00:00+08:00")
    assert P.FAMILY in st["approved_families"]
    alloc = json.loads((Path(__file__).resolve().parents[2] / "config" / "allocation.json").read_text())
    # Robin 2026-10-20: exactly one U1 slot, on the USDC book (BTCUSDC+ETHUSDC), 1000 USDC
    u1 = [s for s in alloc["slots"] if s.get("family") == P.FAMILY or "BTC_ETH" in str(s.get("strategy_id"))]
    assert len(u1) == 1 and u1[0]["slot"] == "u1_btc_eth_usdc" and u1[0]["symbol"] == "BTCUSDC"
    assert u1[0]["quote_currency"] == "USDC" and u1[0]["notional_usdc"] == 1000 and "notional_usdt" not in u1[0]


def test_main_paper_failure_never_breaks_live(monkeypatch):
    import main
    monkeypatch.setattr(P, "run_paper", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out, st = main._run_paper_u1()
    assert out == {"ok": False, "error": "boom"} and st is None
    monkeypatch.setenv("PAPER_U1_ENABLED", "false")
    assert main._run_paper_u1() == (None, None)


def test_api_paper_endpoint_read_only(monkeypatch, tmp_path):
    import api
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.setattr(api, "_check_auth", lambda allow_readonly=False: (True, "", 200))
    p = tmp_path / "paper.json"
    st = P.initial_paper_state()
    p.write_text(json.dumps(st))
    monkeypatch.setattr(P, "PAPER_LOCAL", str(p))
    monkeypatch.setattr(P.PaperStore.__init__, "__defaults__", (None, P.PAPER_OBJECT, str(p)))
    with api.app.test_client() as c:
        v = c.get("/paper").get_json()
        assert c.post("/paper").status_code == 405
    u1 = v["paper"]["U1"]
    assert v["label_zh"] == "模擬（非實盤）" and u1["mode"] == "paper_only" and u1["orders"] == "none" and u1["not_live"]
    assert u1["equity"] == pytest.approx(342930.6738671098, rel=1e-12)
    assert json.loads(p.read_text()) == json.loads(json.dumps(st))   # GET never writes
