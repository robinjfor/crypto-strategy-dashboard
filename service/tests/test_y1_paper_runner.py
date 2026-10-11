"""Y1/X1 paper runner: the hourly live path (step_live) == the panel replay == the fixture trades; paper-only
safety (public GETs only, own GCS object, not approvable, failures isolated); fapi weight guard; clean deferral."""
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper_u1 as U  # noqa: E402
import paper_y1 as Y  # noqa: E402

SPEC = Path("/workspace/strategy-unified-3y/combo_u1v1_spec")
PANEL = Path("/workspace/strategy-unified-3y/rd_regime/data")
U1FIX = Path(__file__).resolve().parent / "fixtures" / "u1"
SERVICE = Path(__file__).resolve().parents[1]
need = pytest.mark.skipif(not (PANEL / "P_C.npy").is_file() or not (SPEC / "combo" / "trades.csv").is_file(),
                          reason="analyst panel / fixtures not on this machine")


class _Stats:
    def stats(self):
        return {"requests": 0}


class FakeMarket:
    """Serves the analyst panel through the Market interface (what fapi would return at time `now`)."""

    def __init__(self, pan, spot, hide_funding=None):
        self.p = pan; self.spot = spot; self.now = None; self.http = _Stats(); self.calls = []
        self.IDX = pan["IDX"]; self.si = {s: j for j, s in enumerate(pan["syms"])}
        self.C = np.asarray(pan["C"]); self.O = np.asarray(pan["O"]); self.H = np.asarray(pan["H"]); self.L = np.asarray(pan["L"])
        self.FR = np.asarray(pan["FR"])
        o, h, l, c, _ = Y.daily_bars_from_hourly(self.O, self.H, self.L, self.C, pan["V"])
        self.d = (o, h, l, c)
        self.days = pd.date_range("2020-01-01", periods=c.shape[0], freq="1D", tz="UTC")
        self.qv = pan["qv"].reindex(self.days).fillna(0.0).to_numpy()
        self.hide_funding = set(hide_funding or [])

    def _t(self, ts):
        return int(self.IDX.searchsorted(ts))

    def exchange_info(self):
        cur = self._t(pd.Timestamp(self.now).floor("h"))
        lo = max(0, cur - 3)
        out = []
        for s, j in self.si.items():
            alive = np.isfinite(self.C[lo:cur + 1, j]).any()
            out.append({"symbol": s, "base": s[:-4], "quote": "USDT", "status": "TRADING" if alive else "SETTLING",
                        "contractType": "PERPETUAL", "underlyingType": "COIN", "onboardDate": None})
        return out

    def funding_intervals(self):
        return {}

    def funding_hour(self, t):
        self.calls.append(("funding", t))
        if t in self.hide_funding:
            self.hide_funding.discard(t)
            return {}
        i = self._t(t)
        ok = np.isfinite(self.C[i]) & np.isfinite(self.O[i])
        return {s: float(self.FR[i, j]) for s, j in self.si.items() if ok[j] and (self.FR[i, j] != 0 or t.hour % 8 == 0)}

    def klines(self, sym, interval, start, limit):
        j = self.si[sym]
        cur = pd.Timestamp(self.now).floor("h")
        rows = []
        if interval == "1h":
            a = self._t(start)
            for i in range(a, min(a + limit, len(self.IDX))):
                ts = self.IDX[i]
                if ts > cur:
                    break
                if np.isfinite(self.C[i, j]) and np.isfinite(self.O[i, j]):
                    ms = int(ts.timestamp() * 1000)
                    rows.append([ms, self.O[i, j], np.nan_to_num(self.H[i, j]), np.nan_to_num(self.L[i, j]), self.C[i, j], 0, ms + 3_599_999, 0])
        else:
            a = int(self.days.searchsorted(start))
            o, h, l, c = self.d
            for di in range(a, min(a + limit, len(self.days))):
                ts = self.days[di]
                if ts > cur:
                    break
                if np.isfinite(c[di, j]):
                    ms = int(ts.timestamp() * 1000)
                    rows.append([ms, o[di, j], h[di, j], l[di, j], c[di, j], 0, ms + 86_399_999, self.qv[di, j]])
        return rows

    def spot_daily(self, k, now):
        s = self.spot[k]
        return s[s.index < pd.Timestamp(now).floor("D")]

    def pmap(self, fn, items):
        return {x: fn(x) for x in items}


@pytest.fixture(autouse=True)
def _fixture_cast(monkeypatch):
    monkeypatch.setattr(Y, "U1_COMBO_CAST", np.float32)


@pytest.fixture(scope="module")
def world():
    pan = Y.load_panel(PANEL)
    _, spot = U.load_fixture(U1FIX)
    cd = pd.read_csv(SPEC / "combo" / "daily.csv")
    ao = {r.date_utc: r.a_U1 for r in cd.itertuples() if not np.isfinite(r.sigma60_U1_1x_raw) or not np.isfinite(r.sigma60_V1_1x_raw)}
    st = Y.replay_panel(pan, spot, end="2026-09-28", a_override=ao, u1_combo_cast=np.float32, log_fills=False)
    st["v1_day"] = st["decisions"][-1]["day"]
    st["pending"]["meta"]["fill_bar"] = Y._iso(Y._ts(st["pending"]["meta"]["decision_bar"]) + Y.H)
    st["fills"] = {b: [] for b in Y.BOOKS}
    return {"pan": pan, "spot": spot, "start": Y.to_json(st)}


def _hourly_run(world, until, hide=None, start_json=None):
    st = Y.from_json(json.loads(json.dumps(start_json or world["start"])))   # JSON round trip like GCS
    M = FakeMarket(world["pan"], world["spot"], hide)
    now = datetime(2026, 9, 28, 0, 1, tzinfo=timezone.utc)
    stops = []
    while now <= until:
        M.now = now
        s = Y.step_live(st, now, M)
        if s["stopped"]:
            stops.append((now, s["stopped"]))
        st = Y.from_json(json.loads(json.dumps(Y.to_json(st))))
        now += timedelta(hours=1)
    return st, stops, M


@need
def test_hourly_live_path_equals_replay_and_fixture_trades(world):
    """13 days of hourly runs (JSON state round-trip each run) end exactly where the full replay ends (the seed),
    and every live-path fill equals combo/trades.csv and v1/trades.csv trade by trade."""
    st, stops, _ = _hourly_run(world, datetime(2026, 10, 11, 0, 1, tzinfo=timezone.utc))
    seed = Y.load_seed()
    assert st["last_closed_bar"] == "2026-10-10T23:00:00+00:00"
    assert stops[-1][1].startswith("missing 1h kline")       # panel ends: the 10-11 00:00 bar isn't published -> clean stop
    for b in Y.BOOKS:
        assert st["books"][b]["eq"] == pytest.approx(seed["books"][b]["eq"], rel=1e-10), b
        assert st["books"][b]["cash"] == pytest.approx(seed["books"][b]["cash"], rel=1e-9), b
        np.testing.assert_allclose(st["books"][b]["u"][:241], seed["books"][b]["u"], rtol=1e-9, atol=1e-12)
    for b in ("C_lev", "V1_lev"):
        np.testing.assert_allclose(st["pending"]["targets"][b][:241], seed["pending"]["targets"][b], rtol=1e-9, atol=1e-14)
    for b, f in (("C_lev", "combo/trades.csv"), ("V1_lev", "v1/trades.csv"), ("C_1x", "combo/trades_1x_shadow.csv"), ("V1_1x", "v1/trades_1x_shadow.csv")):
        ref = pd.read_csv(SPEC / f)
        ref = ref[ref.fill_time_utc >= "2026-09-28T00:00:00+00:00"].reset_index(drop=True)
        mine = pd.DataFrame(st["fills"][b])
        assert len(mine) == len(ref) > 0, (b, len(mine), len(ref))
        for c in ("fill_time_utc", "symbol", "side", "reason"):
            assert (mine[c].to_numpy() == ref[c].to_numpy()).all(), (b, c)
        for c in ("target_weight", "price", "qty_delta", "units_after"):
            np.testing.assert_allclose(mine[c].to_numpy(float), ref[c].to_numpy(float), rtol=1e-8, atol=1e-9, err_msg=f"{b} {c}")
    d = st["decisions"][-1]
    assert d["day"] == "2026-10-10" and d["n_eligible"] == 27
    assert d["fill_bar"] == "2026-10-11T00:00:00+00:00"


@need
def test_unpublished_funding_defers_cleanly_and_resumes_identically(world):
    t = pd.Timestamp("2026-09-29 00:00", tz="UTC")
    until = datetime(2026, 9, 30, 2, 1, tzinfo=timezone.utc)
    a, _, _ = _hourly_run(world, until)
    b, stops, _ = _hourly_run(world, until, hide={t})
    assert any("funding not published" in s for _, s in stops)
    for k in Y.BOOKS:
        assert b["books"][k]["eq"] == a["books"][k]["eq"]
        assert len(b["fills"][k]) == len(a["fills"][k])
    assert b["fills"]["C_lev"][-1] == a["fills"]["C_lev"][-1]


@need
def test_bootstrap_coin_from_history_equals_incremental_state(world):
    """A coin first seen live is rebuilt from its full 1d history; result == the coin's replayed state."""
    seed = Y.load_seed()
    M = FakeMarket(world["pan"], world["spot"]); M.now = datetime(2026, 10, 11, 0, 30, tzinfo=timezone.utc)
    for sym in ("SOLUSDT", "1000PEPEUSDT", "WLDUSDT"):
        st = Y.new_state(["BTCUSDT", "ETHUSDT"]); st["v1_day"] = "2026-10-10"
        rows = Y.fetch_daily_history(M, sym, None, pd.Timestamp("2026-10-10", tz="UTC"))
        Y.bootstrap_coin(st, sym, rows)
        a, b = st["coins"][sym], seed["coins"][sym]
        assert a["pos"] == b["pos"] and a["age"] == b["age"]
        np.testing.assert_allclose(a["tr"], b["tr"], rtol=1e-12)
        for k in ("g1", "d1", "g2", "d2"):
            np.testing.assert_allclose(a[k][1:], b[k][1:], rtol=1e-12)
        np.testing.assert_allclose(st["qv"][sym], seed["qv"][sym], rtol=1e-12)


def test_live_default_uses_float64_u1_leg():
    import importlib
    assert importlib.reload(Y).U1_COMBO_CAST is None


def test_candidate_pool_filters():
    info = [dict(symbol="BTCUSDT", base="BTC", quote="USDT", status="TRADING", contractType="PERPETUAL", underlyingType="COIN"),
            dict(symbol="XAUUSDT", base="XAU", quote="USDT", status="TRADING", contractType="PERPETUAL", underlyingType="COMMODITY"),
            dict(symbol="TSLAUSDT", base="TSLA", quote="USDT", status="TRADING", contractType="PERPETUAL", underlyingType="EQUITY"),
            dict(symbol="BTCDOMUSDT", base="BTCDOM", quote="USDT", status="TRADING", contractType="PERPETUAL", underlyingType="COIN"),
            dict(symbol="USDCUSDT", base="USDC", quote="USDT", status="TRADING", contractType="PERPETUAL", underlyingType="COIN"),
            dict(symbol="ETHUSDT_261225", base="ETH", quote="USDT", status="TRADING", contractType="CURRENT_QUARTER", underlyingType="COIN"),
            dict(symbol="ETHUSDC", base="ETH", quote="USDC", status="TRADING", contractType="PERPETUAL", underlyingType="COIN"),
            dict(symbol="OLDUSDT", base="OLD", quote="USDT", status="SETTLING", contractType="PERPETUAL", underlyingType="COIN"),
            dict(symbol="SOLUSDT", base="SOL", quote="USDT", status="TRADING", contractType="PERPETUAL", underlyingType="COIN")]
    assert set(Y.candidate_pool(info)) == {"BTCUSDT", "SOLUSDT"}


def test_kline_weights_and_weight_guard(monkeypatch):
    assert [Y.kline_weight(x) for x in (1, 99, 100, 499, 500, 1000, 1500)] == [1, 1, 2, 2, 5, 5, 10]
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    class R:
        def __init__(self, code, used, ra=None):
            self.status_code = code; self.headers = {"X-MBX-USED-WEIGHT-1M": str(used)}
            if ra:
                self.headers["Retry-After"] = str(ra)

        def raise_for_status(self):
            pass

        def json(self):
            return []
    seq = [R(200, 1190), R(200, 20), R(429, 2400, ra=7), R(200, 5)]
    calls = []

    class S:
        def get(self, url, **k):
            calls.append(url)
            return seq.pop(0)
    h = Y.PublicHttp(budget=1200, session=S())
    h.get("/fapi/v1/klines", {}, weight=1)          # used 1190
    h.get("/fapi/v1/klines", {}, weight=20)         # 1190+20 > 1200 -> waits for the next minute first
    assert slept and slept[0] > 0
    h.get("/fapi/v1/klines", {}, weight=1)          # 429 -> Retry-After 7, then retried
    assert 7.0 in slept and len(calls) == 4
    st = h.stats()
    assert st["max_used_weight_1m"] == 2400 and st["http_errors"] == 1 and st["requests"] == 4


def test_paper_only_static_safety():
    src = (SERVICE / "paper_y1.py").read_text()
    for bad in ("futures_client", "binance_client", "BinanceClient", "FuturesDemoClient", "u1_live", ".post(", ".delete(",
                ".put(", "_signed", "X-MBX-APIKEY", "api_key", "allocation", "approve"):
        assert bad not in src, bad
    assert Y.Y1_OBJECT == "trader/paper/y1_state.json" and Y.PUBLIC_OBJECT == "trader/paper/y1_public.json"
    assert Y.Y1_OBJECT not in ("trader/state.json", U.PAPER_OBJECT)
    with pytest.raises(AssertionError):
        Y.Y1Store(object_name="trader/state.json")


def test_not_approvable_and_not_in_books():
    import allocation
    import slots
    for fam in (Y.FAMILY, Y.X_FAMILY):
        assert fam not in allocation.RUNNER_FAMILIES and fam not in slots.SUPPORTED_FAMILIES
        assert fam not in getattr(allocation, "FUTURES_FAMILIES", ()) and fam not in getattr(slots, "FUTURES_FAMILIES", ())
    js = (SERVICE.parent / "backtest.js").read_text()
    i = js.index("var RUNNER_FAMILIES")
    block = js[i: js.index("};", i)]
    assert Y.FAMILY not in block and Y.X_FAMILY not in block
    assert f"{Y.FAMILY}: null" in js and f"{Y.X_FAMILY}: null" in js     # catalog -> runner map: hidden, not approvable
    alloc = json.loads((SERVICE.parent / "config" / "allocation.json").read_text()) if (SERVICE.parent / "config" / "allocation.json").is_file() else {}
    assert Y.FAMILY not in json.dumps(alloc) and Y.X_STRATEGY_ID not in json.dumps(alloc)


def test_main_failure_isolated(monkeypatch):
    import main
    import paper_y1

    def boom(*a, **k):
        raise RuntimeError("fapi down")
    monkeypatch.setattr(paper_y1, "run_paper", boom)
    r = main._run_paper_y1()
    assert r["ok"] is False and "fapi down" in r["error"]
    monkeypatch.setenv("PAPER_Y1_ENABLED", "0")
    assert main._run_paper_y1() is None
    src = (SERVICE / "main.py").read_text()
    i_save, i_feed, i_y1 = src.index("store.save(state, if_generation_match"), src.index("url = store.save_feed(feed)"), src.index("paper_y1_summary = _run_paper_y1()")
    assert i_save < i_feed < i_y1          # Y1 runs only after the trader state + feed are saved


def test_run_paper_seeds_rolls_back_on_error_and_public_is_strict_json(tmp_path, monkeypatch):
    store = Y.Y1Store(bucket=None, local_path=str(tmp_path / "y1.json"))
    monkeypatch.setattr(Y.Y1Store, "__init__", Y.Y1Store.__init__)

    class Broken:
        http = _Stats()

        def exchange_info(self):
            raise RuntimeError("boom")
    s = Y.run_paper(datetime(2026, 10, 11, 15, 1, tzinfo=timezone.utc), Broken(), store)
    assert s["ok"] is False and s["seeded"] is True
    saved = json.loads((tmp_path / "y1.json").read_text())
    assert saved["last_closed_bar"] == "2026-10-10T23:00:00+00:00" and saved["errors"]
    pub = json.loads((tmp_path / "y1.json.public.json").read_text())
    json.dumps(pub, allow_nan=False)
    y = pub["Y1"]
    assert y["label_zh"] == "模擬（非實盤）" and y["orders"] == "none" and y["in_books"] is False and y["approvable"] is False
    assert y["code"] == "Y1" and pub["X1"]["code"] == "X1"
    assert y["n_positions"] == 27 and y["next_fill_utc"] == "2026-10-11T00:00:00+00:00"
    assert y["last_decision"]["n_eligible"] == 27 and y["pending_targets"]["BTCUSDT"] == pytest.approx(0.5555, abs=1e-4)
    assert 15 < y["liq_distance_pct"] < 80


def test_new_symbol_keeps_pending_and_books_aligned():
    """Regression (first cloud run): bootstrapping new coins grew syms 241 -> 566 but not the pending targets."""
    st = Y.load_seed()
    n0 = len(st["syms"])
    tg0 = {k: v.copy() for k, v in st["pending"]["targets"].items()}
    for s in ("NEWAUSDT", "NEWBUSDT"):
        Y.add_symbol(st, s)
    for k, w in st["pending"]["targets"].items():
        assert len(w) == n0 + 2 and (w[:n0] == tg0[k]).all() and (w[n0:] == 0).all()
    for b in st["books"].values():
        assert len(b["u"]) == len(b["last"]) == n0 + 2
    st2 = Y.from_json(json.loads(json.dumps(Y.to_json(st))))
    assert len(st2["pending"]["targets"]["C_lev"]) == n0 + 2
    ok = np.ones(n0 + 2, bool)
    O = np.r_[np.where(st["books"]["C_lev"]["last"][:n0] > 0, st["books"]["C_lev"]["last"][:n0], 1.0), [1.0, 1.0]]
    Y.open_bar(st["books"]["C_lev"], O, ok, np.zeros(n0 + 2), np.where(ok, st["pending"]["targets"]["C_lev"], 0.0), st["syms"], [], {})


@need
def test_live_path_with_coins_bootstrapped_mid_life(world):
    """Like the first cloud run: the exchange lists perps the state has never seen -> bootstrap, then normal bars and
    a daily decision with the larger symbol list (clones of SOL/DOGE data under new names)."""
    class More(FakeMarket):
        EXTRA = {"SOLCLONEUSDT": "SOLUSDT", "DOGECLONEUSDT": "DOGEUSDT"}

        def exchange_info(self):
            out = super().exchange_info()
            for e, src in self.EXTRA.items():
                out.append({**next(x for x in out if x["symbol"] == src), "symbol": e, "base": e[:-4]})
            return out

        def klines(self, sym, interval, start, limit):
            return super().klines(self.EXTRA.get(sym, sym), interval, start, limit)

        def funding_hour(self, t):
            fr = super().funding_hour(t)
            fr.update({e: fr[src] for e, src in self.EXTRA.items() if src in fr})
            return fr
    st = Y.from_json(json.loads(json.dumps(world["start"])))
    st.pop("ready", None)
    M = More(world["pan"], world["spot"])
    now = datetime(2026, 9, 28, 0, 1, tzinfo=timezone.utc)
    while now <= datetime(2026, 9, 30, 1, 1, tzinfo=timezone.utc):
        M.now = now
        s = Y.step_live(st, now, M)
        assert not (s["stopped"] or "").startswith("bootstrap"), s["stopped"]
        st = Y.from_json(json.loads(json.dumps(Y.to_json(st))))
        now += timedelta(hours=1)
    assert "SOLCLONEUSDT" in st["syms"] and st["coins"]["SOLCLONEUSDT"]["pos"] == st["coins"]["SOLUSDT"]["pos"]
    assert st["v1_day"] == "2026-09-29" and st["last_closed_bar"] == "2026-09-30T00:00:00+00:00"
    d = st["decisions"][-1]
    sol_elig = "SOLUSDT" in st["elig_today"]
    assert ("SOLCLONEUSDT" in st["elig_today"]) == sol_elig
    assert d["n_eligible"] == len(st["elig_today"])
    n = len(st["syms"])
    for b in st["books"].values():
        assert len(b["u"]) == n
