"""U1 paper runner (模擬倉) — family btc_eth_trend_voltarget_perp, strategy d4_trend_N200_tv60_cap3__BTC_ETH__1d.

PAPER ONLY (Robin cleared U1 for paper trading, NOT live). This module is an internal simulation:
it never imports an exchange/order client and only performs public GET requests
(Binance spot 1d klines for the signal, USDT-M perp 1h klines + fundingRate for fills/marks/funding).
State lives in its own GCS object (PAPER_OBJECT), never in trader/state.json, and is not part of
the USDT/USDC books.

Engine = rd_regime engine._sim restricted to BTC/ETH (spec.json authoritative), bar by bar:
  open phase : funding on held units at the open (settlement bars 00/08/16 UTC), then — if the previous
               bar was the daily decision bar (23:00 UTC) — 25% band rebalance at this bar's open.
  close phase: cross-margin liquidation check on Low (longs) / High (shorts), mark at close; on the
               23:00 bar: day-d spot signal → w1x, shadow 1x equity close → sigma60 → L, targets.
Both phases run for two books: the shadow 1x book (w1x) and the levered book (w1x × L, cap 3).
The hourly job (01 * * * * UTC) runs the open phase of the 00:00 bar at 00:01 UTC, so the daily
paper decision fills at 00:00 UTC (08:00 Taipei) exactly like the backtest.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("paper_u1")

CODE = "U1"
FAMILY = "btc_eth_trend_voltarget_perp"
STRATEGY_ID = "d4_trend_N200_tv60_cap3__BTC_ETH__1d"
LABEL_ZH = "模擬（非實盤）"
SYMS = ("BTC", "ETH")
N_SMA = 200
ASSET_TV = 0.60
SLEEVE = 0.5
ACCT_TV = 0.60
LEV_CAP = 3.0
VOL_FLOOR = 0.05
SIG60_WIN, SIG60_MIN = 60, 20
SIG20_WIN, SIG20_MIN = 20, 15
POS_CAP, GROSS_CAP = 3.0, 5.0
BAND = 0.25
COST = 0.002
MAINT = 0.005
INITIAL = 10000.0
DECISION_HOUR = 23
PAPER_OBJECT = os.environ.get("PAPER_U1_OBJECT", "trader/paper/u1_state.json")
PAPER_LOCAL = os.environ.get("PAPER_U1_LOCAL_PATH", "/tmp/paper_u1_state.json")
SEED_PATH = Path(__file__).resolve().parent / "paper" / "u1_seed.json"
VISION_BASE = os.environ.get("VISION_BASE_URL", "https://data-api.binance.vision").rstrip("/")
H = timedelta(hours=1)
KEEP_HOURLY = 24 * 120


def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat() if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts, tz="UTC").isoformat()


def _ts(s) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# ---------------------------------------------------------------- indicators (spec §2–4)
def daily_indicators(spot_close: pd.Series) -> pd.DataFrame:
    """spot_close: daily spot closes indexed by UTC day (00:00). Same formulas as designs_old.d4_daily_weights."""
    sc = spot_close.astype(float).sort_index()
    sma = sc.rolling(N_SMA, min_periods=N_SMA).mean()
    vol = np.log(sc / sc.shift(1)).rolling(SIG20_WIN, min_periods=SIG20_MIN).std() * np.sqrt(365)
    sig = (sc > sma).astype(int)
    w = (sig.astype(float) * SLEEVE * np.minimum(1.0, ASSET_TV / vol)).fillna(0.0)
    return pd.DataFrame({"spot_close": sc, "sma200": sma, "sigma20": vol, "signal": sig, "w1x": w})


def leverage_from_returns(returns: list[float]) -> tuple[float, float | None]:
    """L(d) = min(3, 0.60/max(sigma60,0.05)); sigma60 = std(last 60 daily 1x returns, ddof=1, min 20)·√365."""
    r = np.asarray([x for x in returns[-SIG60_WIN:] if x is not None and np.isfinite(x)], float)
    if len(r) < SIG60_MIN:
        return 1.0, None
    s = float(np.std(r, ddof=1) * math.sqrt(365))
    return float(min(LEV_CAP, ACCT_TV / max(s, VOL_FLOOR))), s


def lev_targets(w1x: dict, L: float) -> dict:
    u = {k: float(np.clip(w1x[k] * L, -POS_CAP, POS_CAP)) for k in SYMS}
    g = sum(abs(v) for v in u.values())
    if g > GROSS_CAP:
        u = {k: v * GROSS_CAP / max(g, 1e-12) for k, v in u.items()}
    return u


# ---------------------------------------------------------------- books
def new_book(cash: float = INITIAL) -> dict:
    return {"cash": float(cash), "units": {k: 0.0 for k in SYMS}, "last": {k: 0.0 for k in SYMS},
            "funding_pnl": 0.0, "fees": 0.0, "n_liq": 0, "dead": False}


def book_equity(book: dict, px: dict | None = None) -> float:
    px = px or book["last"]
    return book["cash"] + sum(book["units"][k] * px[k] for k in SYMS)


def apply_funding(book: dict, opens: dict, rates: dict) -> dict:
    out = {}
    if book["dead"]:
        return out
    for k in SYMS:
        u = book["units"][k]
        r = float(rates.get(k) or 0.0)
        if u != 0.0 and r != 0.0:
            f = u * opens[k] * r
            book["cash"] -= f
            book["funding_pnl"] -= f
            out[k] = -f
    return out


def rebalance(book: dict, targets: dict, opens: dict) -> list[dict]:
    """25% band; trades go exactly to target units = wt·V/O. Returns fills (engine order BTC, ETH)."""
    fills = []
    if book["dead"]:
        return fills
    V = book["cash"] + sum(book["units"][k] * opens[k] for k in SYMS)
    if not V > 0:
        return fills
    for k in SYMS:
        wt = float(targets[k]); u = book["units"][k]; o = opens[k]
        wc = u * o / V
        if wt == 0.0:
            tr, why = u != 0.0, "exit_to_cash"
        elif wc == 0.0:
            tr, why = True, "entry"
        elif (wt > 0) != (wc > 0):
            tr, why = True, "flip"
        elif abs(wt - wc) > BAND * abs(wt):
            tr, why = True, "rebalance_band"
        else:
            tr, why = False, ""
        if tr:
            nu = wt * V / o; d = nu - u; tn = abs(d) * o; fee = tn * COST
            book["cash"] -= d * o + fee
            book["fees"] += fee
            book["units"][k] = nu
            fills.append({"symbol": k + "USDT", "side": "BUY" if d > 0 else "SELL", "reason": why,
                          "target_weight": wt, "weight_before": wc, "price": o, "qty_delta": d,
                          "units_after": nu, "notional": tn, "fee": fee, "equity_before": V})
    return fills


def liquidation_and_mark(book: dict, lows: dict, highs: dict, closes: dict) -> bool:
    """Cross-margin worst-price check (maint 0.5%), then mark at close. True if liquidated this bar."""
    if book["dead"]:
        return False
    worst = book["cash"]; gw = 0.0
    for k in SYMS:
        u = book["units"][k]
        if u > 0:
            worst += u * lows[k]; gw += u * lows[k]
        elif u < 0:
            worst += u * highs[k]; gw -= u * highs[k]
    liq = False
    if gw > 0 and worst <= MAINT * gw:
        book["cash"] = max(worst - gw * COST, 0.0)
        book["n_liq"] += 1
        book["units"] = {k: 0.0 for k in SYMS}
        liq = True
    for k in SYMS:
        book["last"][k] = closes[k]
    if book_equity(book) <= 0:
        book["dead"] = True
    return liq


# ---------------------------------------------------------------- engine state machine (shared by parity + live)
def new_engine_state() -> dict:
    return {"books": {"x1": new_book(), "lev": new_book()}, "pending": None, "last_closed_bar": None,
            "open_done_bar": None, "open_funding_used": None, "e1x_closes": [], "e1x_prev": INITIAL,
            "returns_1x": [], "fills": [], "fills_1x": [], "decisions": [], "first_bar": None}


def open_phase(st: dict, ts, opens: dict, rates: dict) -> list[dict]:
    """Funding at the open, then (if previous bar was the decision bar) band rebalance at the open."""
    ts = _ts(ts)
    if st["first_bar"] is None:
        st["first_bar"] = _iso(ts)
    fl = {b: apply_funding(st["books"][b], opens, rates) for b in ("x1", "lev")}
    st["open_funding_used"] = {"bar": _iso(ts), "rates": {k: float(rates.get(k) or 0.0) for k in SYMS},
                               "units_before": {b: dict(st["books"][b]["units"]) for b in ("x1", "lev")},
                               "opens": dict(opens), "funding": fl}
    out = []
    p = st.get("pending")
    if p and _ts(p["fill_bar"]) == ts:
        for b, key, log_key in (("x1", "targets_1x", "fills_1x"), ("lev", "targets_lev", "fills")):
            fills = rebalance(st["books"][b], p[key], opens)
            for f in fills:
                f.update({"fill_time_utc": _iso(ts), "decision_bar_open_utc": p["decision_bar"],
                          "indicator_day_utc": p["indicator_day"], "w1x": p["w1x"][f["symbol"][:-4]],
                          "leverage": p["L"] if b == "lev" else 1.0, "book": b})
                st[log_key].append(f)
                if b == "lev":
                    out.append(f)
        st["pending"] = None
    st["open_done_bar"] = _iso(ts)
    return out


def close_phase(st: dict, ts, lows: dict, highs: dict, closes: dict, ind_day: dict | None = None) -> dict | None:
    """Liquidation check + mark; on the 23:00 bar compute next targets. ind_day: {sym: row of daily_indicators for day d}."""
    ts = _ts(ts)
    liq = {b: liquidation_and_mark(st["books"][b], lows, highs, closes) for b in ("x1", "lev")}
    if any(liq.values()):
        st.setdefault("liquidations", []).append({"bar": _iso(ts), **liq})
    st["last_closed_bar"] = _iso(ts)
    st["open_done_bar"] = None
    st["open_funding_used"] = None
    dec = None
    if ts.hour == DECISION_HOUR:
        if ind_day is None:
            raise ValueError("decision bar needs day-d indicators")
        day = ts.floor("D")
        e1x = book_equity(st["books"]["x1"])
        prev = st["e1x_prev"]
        r = e1x / prev - 1.0
        st["returns_1x"] = (st["returns_1x"] + [r])[-SIG60_WIN:]
        st["e1x_prev"] = e1x
        st["e1x_closes"] = (st["e1x_closes"] + [[str(day.date()), e1x]])[-(SIG60_WIN + 5):]
        L, s60 = leverage_from_returns(st["returns_1x"])
        w1x = {k: float(ind_day[k]["w1x"]) for k in SYMS}
        tl = lev_targets(w1x, L)
        dec = {"decision_bar": _iso(ts), "fill_bar": _iso(ts + H), "indicator_day": str(day.date()),
               "w1x": w1x, "L": L, "sigma60_1x": s60, "targets_1x": dict(w1x), "targets_lev": tl,
               "e1x_close": e1x, "ret_1x": r, "equity_close": book_equity(st["books"]["lev"]),
               "indicators": {k: {c: (None if pd.isna(ind_day[k][c]) else float(ind_day[k][c]))
                                  for c in ("spot_close", "sma200", "sigma20", "signal")} for k in SYMS}}
        st["pending"] = dec
        st["decisions"] = (st["decisions"] + [dec])[-400:]
    return dec


# ---------------------------------------------------------------- seed (continue the backtest book)
def load_fixture(fixture_dir: str | Path):
    F = Path(fixture_dir)
    bars = pd.read_csv(F / "bars.csv.gz")
    spot = {}
    for k in SYMS:
        d = pd.read_csv(F / f"spot1d_{k}.csv")
        spot[k] = pd.Series(d["Close"].to_numpy(float), index=pd.to_datetime(d["ts"], utc=True).dt.floor("D"))
    return bars, spot


def build_seed(fixture_dir: str | Path) -> dict:
    """Engine state after the last fixture bar (2026-10-10 23:00 UTC), from a full-precision replay of the
    whole fixture (verified bar-by-bar against bars.csv.gz). Funding/fee counters reset for the paper period."""
    bars, spot = load_fixture(fixture_dir)
    st = replay_bars(bars, spot)
    assert _ts(st["last_closed_bar"]).hour == DECISION_HOUR and st["pending"]
    for b in ("x1", "lev"):
        st["books"][b].update({"funding_pnl": 0.0, "fees": 0.0})
    st["fills"] = st["fills"][-30:]; st["fills_1x"] = st["fills_1x"][-30:]; st["decisions"] = st["decisions"][-5:]
    return st


def initial_paper_state() -> dict:
    seed = json.loads(SEED_PATH.read_text())
    st = seed["engine"]
    st.update({"code": CODE, "family": FAMILY, "strategy_id": STRATEGY_ID, "mode": "paper_only",
               "label_zh": LABEL_ZH, "orders": "none (internal simulation; no exchange orders, not even Demo)",
               "paper_start": {"bar": st["last_closed_bar"], "equity": book_equity(st["books"]["lev"]),
                               "equity_1x_shadow": book_equity(st["books"]["x1"]), "seed": seed.get("source")},
               "equity_hourly": [], "runs": [], "errors": []})
    return st


# ---------------------------------------------------------------- live data (public GETs only)
def _get_json(url: str, params: dict):
    import requests
    r = requests.get(url, params=params, headers={"User-Agent": "crypto-trader-paper/1.0"}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_spot_daily(sym: str, now: datetime, limit: int = 400) -> pd.Series:
    raw = _get_json(f"{VISION_BASE}/api/v3/klines", {"symbol": sym + "USDT", "interval": "1d", "limit": limit})
    nowms = int(now.timestamp() * 1000)
    rows = [(int(k[0]), float(k[4])) for k in raw if int(k[6]) < nowms]   # closed days only
    idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
    return pd.Series([r[1] for r in rows], index=idx)


def fetch_perp_1h(sym: str, limit: int) -> pd.DataFrame:
    from perp_market import fetch_perp_klines
    return fetch_perp_klines(sym + "USDT", "1h", limit=limit)


def fetch_funding_hist(sym: str) -> pd.Series:
    from perp_market import fetch_funding
    s = fetch_funding(sym + "USDT", limit=1000)
    if s.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    s.index = s.index.floor("h")
    return s[~s.index.duplicated(keep="last")]


DEFAULT_FETCHERS = {"spot_daily": fetch_spot_daily, "perp_1h": fetch_perp_1h, "funding": fetch_funding_hist}

FUNDING_HOURS = (0, 8, 16)


def step_live(st: dict, now: datetime, fetchers: dict | None = None) -> dict:
    """Advance the paper engine to `now` (UTC): every fully closed 1h bar, then the open phase of the
    current bar (so the 00:00 UTC daily fill happens in the 00:01 run). Mutates st; returns a run summary."""
    f = {**DEFAULT_FETCHERS, **(fetchers or {})}
    now = now.astimezone(timezone.utc)
    cur = pd.Timestamp(now).floor("h")
    last = _ts(st["last_closed_bar"])
    hours = int((cur - last) / H) + 2
    if hours > 1450:
        raise RuntimeError(f"paper gap too large to catch up ({hours} h)")
    kl = {k: f["perp_1h"](k, hours) for k in SYMS}
    fr = {k: f["funding"](k) for k in SYMS}
    ind = None
    summary = {"now_utc": now.isoformat(), "bars_closed": 0, "open_phase": None, "fills": [], "decisions": [],
               "stopped": None}

    def _rates(t):
        return {k: (float(fr[k].get(t, 0.0)) if t.hour in FUNDING_HOURS else 0.0) for k in SYMS}

    def _have_rate(t):
        return t.hour not in FUNDING_HOURS or all(t in fr[k].index for k in SYMS)

    t = last + H
    while t + H <= now:
        if not all(t in kl[k].index for k in SYMS):
            summary["stopped"] = f"missing perp kline {_iso(t)}"
            break
        row = {k: kl[k].loc[t] for k in SYMS}
        if st.get("open_done_bar") != _iso(t):
            if not _have_rate(t):
                summary["stopped"] = f"funding not published for {_iso(t)}"
                break
            summary["fills"] += open_phase(st, t, {k: float(row[k]["Open"]) for k in SYMS}, _rates(t))
        ind_day = None
        if t.hour == DECISION_HOUR:
            if ind is None:
                ind = {k: daily_indicators(f["spot_daily"](k, now)) for k in SYMS}
            day = t.floor("D")
            if not all(day in ind[k].index for k in SYMS):
                summary["stopped"] = f"spot daily close for {day.date()} not available"
                break
            ind_day = {k: ind[k].loc[day] for k in SYMS}
        dec = close_phase(st, t, {k: float(row[k]["Low"]) for k in SYMS}, {k: float(row[k]["High"]) for k in SYMS},
                          {k: float(row[k]["Close"]) for k in SYMS}, ind_day)
        if dec:
            summary["decisions"].append({x: dec[x] for x in ("indicator_day", "w1x", "L", "targets_lev", "fill_bar")})
        st.setdefault("equity_hourly", []).append([_iso(t), book_equity(st["books"]["lev"]), book_equity(st["books"]["x1"])])
        summary["bars_closed"] += 1
        t = t + H
    st["equity_hourly"] = st.get("equity_hourly", [])[-KEEP_HOURLY:]
    # Open phase of the bar that is forming now (daily fill at 00:00 UTC happens here, in the 00:01 run).
    # Needs the settled funding rate of this bar (00/08/16 UTC); if Binance hasn't published it yet the open
    # phase is deferred to the next run — fills still use this bar's open, so the result is identical.
    if summary["stopped"] is None and t == cur and st.get("open_done_bar") != _iso(cur) and all(cur in kl[k].index for k in SYMS):
        if _have_rate(cur):
            summary["fills"] += open_phase(st, cur, {k: float(kl[k].loc[cur]["Open"]) for k in SYMS}, _rates(cur))
            summary["open_phase"] = {"bar": _iso(cur), "funding_source": "history"}
        else:
            summary["open_phase"] = {"bar": _iso(cur), "deferred": "funding rate not yet published"}
    return summary


# ---------------------------------------------------------------- persistence (own GCS object; never trader/state.json)
class PaperStore:
    def __init__(self, bucket: str | None = None, object_name: str = PAPER_OBJECT, local_path: str = PAPER_LOCAL):
        from state_store import StateStore
        assert object_name != "trader/state.json"
        self._s = StateStore(bucket=bucket, object_name=object_name, local_path=local_path)

    def load(self) -> dict | None:
        s = self._s
        if s.bucket:
            blob = s._gcs().bucket(s.bucket).blob(s.object_name)
            if not blob.exists():
                s._generation = 0
                return None
            blob.reload()
            s._generation = blob.generation
            return json.loads(blob.download_as_text())
        p = Path(s.local_path)
        return json.loads(p.read_text()) if p.is_file() else None

    def save(self, st: dict) -> None:
        self._s.save(st, if_generation_match=self._s._generation)


def run_paper(now: datetime | None = None, fetchers: dict | None = None, store: PaperStore | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    store = store or PaperStore()
    st = store.load()
    seeded = st is None
    if seeded:
        st = initial_paper_state()
    try:
        summ = step_live(st, now, fetchers)
        summ["ok"] = summ["stopped"] is None
    except Exception as e:  # noqa: BLE001
        log.exception("paper_u1_step_fail")
        summ = {"ok": False, "now_utc": now.isoformat(), "error": str(e)[:300]}
        st["errors"] = (st.get("errors", []) + [summ])[-50:]
    summ["seeded"] = seeded
    st["updated_at_utc"] = now.isoformat()
    st["runner_version"] = os.environ.get("RUNNER_VERSION") or "unknown"
    st["runs"] = (st.get("runs", []) + [{k: summ.get(k) for k in ("now_utc", "ok", "bars_closed", "open_phase", "stopped", "error")}
                                        | {"n_fills": len(summ.get("fills") or [])}])[-200:]
    store.save(st)
    log.info("paper_u1 ok=%s bars=%s fills=%s decisions=%s stopped=%s", summ.get("ok"), summ.get("bars_closed"),
             len(summ.get("fills") or []), len(summ.get("decisions") or []), summ.get("stopped"))
    return summ


# ---------------------------------------------------------------- read-only public view (API /status "paper")
def public_view(st: dict | None) -> dict | None:
    if not st:
        return None
    lev, x1 = st["books"]["lev"], st["books"]["x1"]
    eq = book_equity(lev)
    last_px = lev["last"]
    weights = {k: (lev["units"][k] * last_px[k] / eq if eq > 0 else 0.0) for k in SYMS}
    start = st.get("paper_start") or {}
    last_dec = (st.get("decisions") or [None])[-1]
    nxt = None
    if st.get("last_closed_bar"):
        lc = _ts(st["last_closed_bar"])
        d = lc.floor("D") + timedelta(hours=DECISION_HOUR)
        if d <= lc:
            d += timedelta(days=1)
        nxt = _iso(d + H)
    paper_fills = [f for f in st.get("fills", []) if not start.get("bar") or _ts(f["fill_time_utc"]) > _ts(start["bar"])]
    return {
        "code": CODE, "family": FAMILY, "strategy_id": STRATEGY_ID, "mode": "paper_only", "label_zh": LABEL_ZH,
        "not_live": True, "orders": "none", "in_books": False,
        "equity": eq, "equity_1x_shadow": book_equity(x1),
        "paper_start": start, "return_since_paper_start_pct": ((eq / start["equity"] - 1) * 100) if start.get("equity") else None,
        "units": dict(lev["units"]), "weights": weights, "gross": sum(abs(v) for v in weights.values()),
        "mark": dict(last_px), "cash": lev["cash"], "funding_pnl": lev["funding_pnl"], "fees": lev["fees"], "n_liq": lev["n_liq"],
        "last_closed_bar_utc": st.get("last_closed_bar"), "open_done_bar_utc": st.get("open_done_bar"),
        "pending": ({k: st["pending"][k] for k in ("indicator_day", "fill_bar", "w1x", "L", "targets_lev")} if st.get("pending") else None),
        "last_decision": ({k: last_dec.get(k) for k in ("indicator_day", "decision_bar", "fill_bar", "w1x", "L", "sigma60_1x",
                                                         "targets_lev", "indicators")} if last_dec else None),
        "next_fill_utc": nxt, "paper_fills": paper_fills[-20:], "n_paper_fills": len(paper_fills),
        "updated_at_utc": st.get("updated_at_utc"), "runner_version": st.get("runner_version"),
        "last_run": (st.get("runs") or [None])[-1], "errors_recent": (st.get("errors") or [])[-3:],
    }


# ---------------------------------------------------------------- historical replay (parity + seed)
def replay_bars(bars: pd.DataFrame, spot: dict, st: dict | None = None, record=None) -> dict:
    """Replay 1h bars (columns <SYM>_open/high/low/close/funding_rate, bar_open_utc) through the same
    open_phase/close_phase used live. spot: {sym: daily close Series}. record(ts, st) called after each bar."""
    st = st or new_engine_state()
    ind = {k: daily_indicators(spot[k]) for k in SYMS}
    zero = {"w1x": 0.0, "spot_close": np.nan, "sma200": np.nan, "sigma20": np.nan, "signal": 0}
    ts_all = pd.to_datetime(bars["bar_open_utc"], utc=True)
    cols = {k: {c: bars[f"{k}_{c}"].to_numpy(float) for c in ("open", "high", "low", "close", "funding_rate")} for k in SYMS}
    for i, ts in enumerate(ts_all):
        open_phase(st, ts, {k: cols[k]["open"][i] for k in SYMS}, {k: cols[k]["funding_rate"][i] for k in SYMS})
        ind_day = None
        if ts.hour == DECISION_HOUR:
            day = ts.floor("D")
            ind_day = {k: (ind[k].loc[day] if day in ind[k].index else pd.Series(zero)) for k in SYMS}
        close_phase(st, ts, {k: cols[k]["low"][i] for k in SYMS}, {k: cols[k]["high"][i] for k in SYMS},
                    {k: cols[k]["close"][i] for k in SYMS}, ind_day)
        if record:
            record(i, ts, st)
    return st
