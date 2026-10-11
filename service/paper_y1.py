"""Y1 paper runner (模擬倉) — combo_u1_v1_equal_risk_perp (U1 + V1 equal risk, combo vol target 75%, cap 5x),
with X1 (= the analyst's V1: TradingView Super Scalper daily port on the PIT liquid perp universe, vt60 cap 3)
as a separate paper book. spec: /workspace/strategy-unified-3y/combo_u1v1_spec/spec.json (authoritative).

PAPER ONLY (Robin cleared paper simulation; not live, not approvable). Internal simulation: this module
never imports an exchange/order client and only performs public GET requests (Binance USDT-M perp
exchangeInfo / 1d+1h klines / fundingRate / 24h ticker, Binance spot 1d klines for the U1 leg).
State lives in its own GCS object (Y1_OBJECT), never in trader/state.json, and is not part of the books.

Engine = rd_regime engine._sim (bar by bar, every symbol), for five books:
  U1_1x  : U1 leg 1x weights (BTC/ETH, spot SMA200 signal) — shadow
  V1_1x  : V1 leg 1x weights elig·pos/N — shadow
  V1_lev : X1 = clip(w1x_V · L_V), L_V = min(3, 0.60/σ60(V1_1x))           (X1 paper)
  C_1x   : w1x_C = a_U·w1x_U + a_V·w1x_V (netted per symbol) — shadow
  C_lev  : Y1 = clip(w1x_C · L_C, ±3), gross ≤ 5, L_C = min(5, 0.75/σ60(C_1x))  (Y1 paper)
Per 1h bar: funding at the open (settlement bar), delist close (held, no data), fills if the previous bar was
the 23:00 UTC decision bar (25% band, 20 bps), liquidation check on Low/High (maint 0.5%), mark at close.
At the 23:00 close of day d: V1 indicators/pos and PIT eligibility for day d, the U1 leg w1x, the three
shadow-book returns → σ, a_U/a_V, L_V, L_C → targets filled at the 00:00 UTC open of d+1 (08:00 Taipei).
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

log = logging.getLogger("paper_y1")

CODE = "Y1"
FAMILY = "combo_u1_v1_equal_risk_perp"
STRATEGY_ID = "combo_U1_D4N200_plus_V1_SSCALP_eqrisk_tv75_cap5"
X_CODE = "X1"
X_FAMILY = "tradingview_popular_ports"
X_STRATEGY_ID = "tv_superscalper_atr14_rsi25v100__PIT_UNIV__1d_vt60_cap3"
LABEL_ZH = "模擬（非實盤）"
BOOKS = ("U1_1x", "V1_1x", "V1_lev", "C_1x", "C_lev")
COST, BAND, MAINT = 0.002, 0.25, 0.005
INITIAL = 10000.0
ATRLEN, MULT, R1, R2 = 14, 1.0, 25, 100
QV_MIN, QV_WIN, CNT_MIN, AGE_MIN = 5e7, 90, 45, 30
V_TV, V_CAP = 0.60, 3.0
C_TV, C_CAP = 0.75, 5.0
WCLIP, GCAP = 3.0, 5.0
WIN, MINOBS, FLOOR = 60, 20, 0.05
PREWARM_A = 0.5
# The analyst fixture stored the U1 leg weights as float32 (D4_N200_W.npy) before mixing them into the combo;
# live uses float64 (None). Parity tests set np.float32 to reproduce the fixture exactly (diff ~1e-8 rel).
U1_COMBO_CAST = None
DECISION_HOUR = 23
EXCLUDE = frozenset({"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "EUR", "BTCDOM", "DEFI", "BLUEBIRD",
                     "FOOTBALL", "XAU", "XAG", "PAXG", "BZ", "CL", "CRCL"})
U1_SYMS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
H = timedelta(hours=1)
Y1_OBJECT = os.environ.get("PAPER_Y1_OBJECT", "trader/paper/y1_state.json")
Y1_LOCAL = os.environ.get("PAPER_Y1_LOCAL_PATH", "/tmp/paper_y1_state.json")
SEED_PATH = Path(__file__).resolve().parent / "paper" / "y1_seed.json.gz"


def _ts(s) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(ts) -> str:
    return _ts(ts).isoformat()


# ================================================================ books (engine._sim, vectorised per bar)
def new_book(n: int) -> dict:
    return {"cash": INITIAL, "u": np.zeros(n), "last": np.zeros(n), "dead": False,
            "fees": 0.0, "funding": 0.0, "n_liq": 0, "eq": INITIAL, "gross": 0.0}


def grow_book(b: dict, n: int) -> None:
    for k in ("u", "last"):
        if len(b[k]) < n:
            b[k] = np.concatenate([b[k], np.zeros(n - len(b[k]))])


def open_bar(b: dict, O, ok, FR, W=None, syms=None, rec=None, meta=None) -> None:
    """Funding at the open, delist closes, then (W given = previous bar was the decision bar) band fills."""
    if b["dead"]:
        return
    u = b["u"]
    h = u != 0.0
    m = h & ok & (FR != 0.0)
    if m.any():
        f = u[m] * O[m] * FR[m]
        for x in f:                                 # sequential like _sim
            b["cash"] -= x
            b["funding"] -= x
    for k in np.where(h & ~ok)[0]:                   # no data while held -> close at last close
        tn = abs(u[k]) * b["last"][k]
        Vb = b["cash"] + float(np.sum(u * np.where(ok, O, b["last"])))
        b["cash"] += u[k] * b["last"][k] - tn * COST
        b["fees"] += tn * COST
        if rec is not None:
            rec.append({"symbol": syms[k], "side": "SELL" if u[k] > 0 else "BUY", "reason": "delist_close",
                        "target_weight": 0.0, "weight_before": None, "price": float(b["last"][k]),
                        "qty_delta": float(-u[k]), "units_after": 0.0, "notional": tn, "fee": tn * COST,
                        "equity_before": Vb, **(meta or {}), "decision_bar_open_utc": ""})
        u[k] = 0.0
    if W is None:
        return
    V = b["cash"]
    for k in np.where(u != 0.0)[0]:
        V += u[k] * O[k]
    if not V > 0:
        return
    wc = u * O / V
    to_zero = (W == 0.0) & (u != 0.0)
    entry = (W != 0.0) & (wc == 0.0)
    flip = (W != 0.0) & (wc != 0.0) & ((W > 0) != (wc > 0))
    bandb = (W != 0.0) & (wc != 0.0) & ~flip & (np.abs(W - wc) > BAND * np.abs(W))
    tr = (to_zero | entry | flip | bandb) & ok
    for k in np.where(tr)[0]:
        nu = W[k] * V / O[k]
        d = nu - u[k]
        tn = abs(d) * O[k]
        b["cash"] -= d * O[k] + tn * COST
        b["fees"] += tn * COST
        if rec is not None:
            why = "exit_to_cash" if to_zero[k] else "entry" if entry[k] else "flip" if flip[k] else "rebalance_band"
            rec.append({"symbol": syms[k], "side": "BUY" if d > 0 else "SELL", "reason": why,
                        "target_weight": float(W[k]), "weight_before": float(wc[k]), "price": float(O[k]),
                        "qty_delta": float(d), "units_after": float(nu), "notional": tn, "fee": tn * COST,
                        "equity_before": V, **(meta or {})})
        u[k] = nu


def close_bar(b: dict, ok, Hh, Ll, C) -> bool:
    """Liquidation check (Low for longs, High for shorts), then mark at close. True if liquidated."""
    if b["dead"]:
        b["eq"], b["gross"] = 0.0, 0.0
        return False
    u = b["u"]
    liq = False
    lo = np.where(u > 0, Ll, np.where(u < 0, Hh, 0.0))
    lo = np.nan_to_num(lo)
    worst = b["cash"] + float(np.sum(u * lo))
    gw = float(np.sum(np.abs(u) * lo))
    if gw > 0 and worst <= MAINT * gw:
        b["cash"] = max(worst - gw * COST, 0.0)
        b["n_liq"] += 1
        u[:] = 0.0
        liq = True
    b["last"] = np.where(ok, C, b["last"])
    V = b["cash"] + float(np.sum(u * b["last"]))
    b["eq"] = V
    b["gross"] = float(np.sum(np.abs(u * b["last"]))) / V if V > 0 else 0.0
    if V <= 0:
        b["dead"] = True
    return liq


# ================================================================ V1 indicators (Pine semantics, incremental)
def _rma_push(s: list, x: float, n: int) -> float:
    """s = [k, sum, prev]; Pine rma seeded with SMA of the first n values. Returns value or NaN."""
    if math.isnan(s[2]):
        s[0] += 1
        s[1] += x
        if s[0] == n:
            s[2] = s[1] / n
            return s[2]
        return math.nan
    s[2] = (1.0 / n) * x + (1 - 1.0 / n) * s[2]
    return s[2]


def new_coin() -> dict:
    nan = math.nan
    return {"started": False, "c": nan, "tr": [], "g1": [0, 0.0, nan], "d1": [0, 0.0, nan],
            "g2": [0, 0.0, nan], "d2": [0, 0.0, nan], "pos": 0, "age": 0, "last_day": None,
            "ind": {}}


def coin_day(cs: dict, day: str, o, h, l, c, has_data: bool) -> dict:
    """Advance one UTC day. A day without data inside the series is filled with the previous close (sim.coin_bars)."""
    if has_data:
        cs["age"] += 1
    if not cs["started"]:
        if not has_data:
            cs["last_day"] = day
            return {"pos": 0}
        cs["started"] = True
        prev = math.nan
    else:
        prev = cs["c"]
        if not has_data:
            o = h = l = c = prev
    if math.isnan(prev):
        tr = h - l
    else:
        tr = max(h - l, abs(h - prev), abs(l - prev))
    cs["tr"] = (cs["tr"] + [tr])[-ATRLEN:]
    aw = math.nan
    if len(cs["tr"]) == ATRLEN:
        aw = sum(v * (i + 1) for i, v in enumerate(cs["tr"])) / (ATRLEN * (ATRLEN + 1) / 2.0)
    r1 = r2 = math.nan
    if not math.isnan(prev):
        ch = c - prev
        g, dn = max(ch, 0.0), max(-ch, 0.0)
        up1, dn1 = _rma_push(cs["g1"], g, R1), _rma_push(cs["d1"], dn, R1)
        up2, dn2 = _rma_push(cs["g2"], g, R2), _rma_push(cs["d2"], dn, R2)

        def rsi(u_, d_):
            if math.isnan(u_) or math.isnan(d_):
                return math.nan
            return 100.0 if d_ == 0 else (0.0 if u_ == 0 else 100.0 - 100.0 / (1.0 + u_ / d_))
        r1, r2 = rsi(up1, dn1), rsi(up2, dn2)
    upper, lower = c + MULT * aw, c - MULT * aw
    buy = (o < lower) and (r1 > r2)            # NaN comparisons are False
    sell = (o > upper) and (r1 < r2)
    if buy:
        cs["pos"] = 1
    if sell:
        cs["pos"] = -1
    cs["c"] = c
    cs["last_day"] = day
    cs["ind"] = {"close": c, "wma_tr14": aw, "rsi25": r1, "rsi100": r2, "buy": int(buy), "sell": int(sell)}
    return {"pos": cs["pos"], **cs["ind"]}


def eligible(qv_prev90: list[float], age: int) -> bool:
    """elig(d) = #{k in [d-90, d-1]: qv(k) >= 50M} > 45 and days-with-data(<= d) >= 30 (missing qv = 0)."""
    cnt = sum(1 for x in qv_prev90[-QV_WIN:] if x >= QV_MIN)
    return cnt > CNT_MIN and age >= AGE_MIN


# ================================================================ daily decision (23:00 close of day d)
def sigma60(rets: list[float]) -> float:
    r = rets[-WIN:]
    if len(r) < MINOBS:
        return math.nan
    return float(np.std(np.asarray(r, float), ddof=1) * math.sqrt(365))


def lev(rets, tv, cap) -> tuple[float, float]:
    s = sigma60(rets)
    if math.isnan(s):
        return 1.0, s
    return min(cap, tv / max(s, FLOOR)), s


def clip_gross(w: np.ndarray) -> np.ndarray:
    w = np.clip(w, -WCLIP, WCLIP)
    g = float(np.abs(w).sum())
    return w * (GCAP / g) if g > GCAP else w


def book_return(st: dict, name: str) -> None:
    e = st["books"][name]["eq"]
    prev = st["e_prev"].get(name)
    r = e / prev - 1.0 if prev else e / INITIAL - 1.0
    st["rets"][name] = (st["rets"].get(name, []) + [r])[-WIN:]
    st["e_prev"][name] = e


def decide(st: dict, day: str, w1x_U: dict, w1x_V: np.ndarray, ok_dec: np.ndarray,
           w1x_U_combo: dict | None = None, a_override: float | None = None) -> dict:
    """Day-d decision after the 23:00 close. Returns the pending targets (one array per book)."""
    idx = st["sym_index"]
    n = len(st["syms"])
    for b in ("U1_1x", "V1_1x", "C_1x"):
        book_return(st, b)
    sU = sigma60(st["rets"]["U1_1x"]); sV = sigma60(st["rets"]["V1_1x"])
    if a_override is not None:
        aU = a_override
    elif math.isnan(sU) or math.isnan(sV):
        aU = PREWARM_A
    else:
        iu, iv = 1.0 / max(sU, FLOOR), 1.0 / max(sV, FLOOR)
        aU = iu / (iu + iv)
    aV = 1.0 - aU
    wU = np.zeros(n); wUc = np.zeros(n)
    for k, sym in U1_SYMS.items():
        j = idx[sym]
        wU[j] = float(w1x_U.get(k, 0.0))
        wUc[j] = float((w1x_U_combo or w1x_U).get(k, 0.0))
    wV = np.zeros(n); wV[:len(w1x_V)] = w1x_V
    wC = aU * wUc + aV * wV
    LV, sV_raw = lev(st["rets"]["V1_1x"], V_TV, V_CAP)
    LC, sC = lev(st["rets"]["C_1x"], C_TV, C_CAP)
    tg = {"U1_1x": wU, "V1_1x": wV, "V1_lev": clip_gross(wV * LV), "C_1x": wC, "C_lev": clip_gross(wC * LC)}
    for k in tg:                                    # engine.run: W[~ok] = 0 at the decision bar
        tg[k] = np.where(ok_dec[:n], tg[k], 0.0)
    dec = {"day": day, "a_U": aU, "a_V": aV, "sigma_U": sU, "sigma_V": sV, "sigma_C": sC, "L_V": LV, "L_C": LC,
           "n_eligible": int(st.get("n_elig_today", 0)), "w1x_U": {k: float(w1x_U.get(k, 0.0)) for k in U1_SYMS}}
    return {"meta": dec, "targets": tg}


# ================================================================ panel replay (seed + parity)
def daily_bars_from_hourly(O, Hh, Ll, C, V):
    """sim.bars(24): O first valid open, H max, L min, C last valid close, qv sum (NaN if no close)."""
    T, N = C.shape
    nb = T // 24
    r = lambda a: np.asarray(a[:nb * 24]).reshape(nb, 24, N)
    with np.errstate(all="ignore"):
        import warnings
        warnings.simplefilter("ignore")
        oo = r(O); o = oo[:, 0, :].copy()
        for j in range(1, 24):
            m = np.isnan(o); o[m] = oo[:, j, :][m]
        h = np.nanmax(r(Hh), axis=1); l = np.nanmin(r(Ll), axis=1)
        cc = r(C); c = cc[:, 23, :].copy()
        for j in range(22, -1, -1):
            m = np.isnan(c); c[m] = cc[:, j, :][m]
        v = np.nansum(r(V), axis=1); v[np.isnan(c)] = np.nan
    return o, h, l, c, v


def new_state(syms: list[str]) -> dict:
    n = len(syms)
    return {"syms": list(syms), "sym_index": {s: i for i, s in enumerate(syms)},
            "books": {b: new_book(n) for b in BOOKS}, "coins": {s: new_coin() for s in syms},
            "qv": {s: [] for s in syms}, "rets": {}, "e_prev": {}, "pending": None,
            "last_closed_bar": None, "open_done_bar": None, "decisions": [], "fills": {b: [] for b in BOOKS}}


def add_symbol(st: dict, sym: str) -> int:
    if sym in st["sym_index"]:
        return st["sym_index"][sym]
    st["syms"].append(sym)
    st["sym_index"][sym] = len(st["syms"]) - 1
    st["coins"].setdefault(sym, new_coin())
    st["qv"].setdefault(sym, [])
    n = len(st["syms"])
    for b in st["books"].values():
        grow_book(b, n)
    p = st.get("pending")
    if p:                                   # pending targets stay aligned with the symbol list
        for k, w in p["targets"].items():
            if len(w) < n:
                p["targets"][k] = np.concatenate([w, np.zeros(n - len(w))])
    return st["sym_index"][sym]


def advance_day(st: dict, day: str, bars: dict, qv: dict) -> np.ndarray:
    """Advance every coin by UTC day `day`. bars: {sym: (o,h,l,c) or None}; qv: {sym: quote volume of day}.
    Returns w1x_V aligned to st['syms'] (elig·pos/N) and stores n_elig_today / elig list."""
    n = len(st["syms"])
    pos = np.zeros(n); el = np.zeros(n, bool)
    for j, s in enumerate(st["syms"]):
        b = bars.get(s)
        cs = st["coins"][s]
        if b is None:
            r = coin_day(cs, day, math.nan, math.nan, math.nan, math.nan, False)
        else:
            r = coin_day(cs, day, *b, True)
        pos[j] = r["pos"]
        hist = st["qv"][s]
        el[j] = eligible(hist, cs["age"])
        x = qv.get(s)
        st["qv"][s] = (hist + [float(x) if x is not None and np.isfinite(x) else 0.0])[-QV_WIN:]
    N = int(el.sum())
    st["n_elig_today"] = N
    st["elig_today"] = [st["syms"][j] for j in np.where(el)[0]]
    st["pos_today"] = {st["syms"][j]: int(pos[j]) for j in np.where(el)[0]}
    return np.where(el, pos, 0.0) / max(N, 1)


def load_panel(data_dir="/workspace/strategy-unified-3y/rd_regime/data"):
    D = Path(data_dir)
    syms = [s + "USDT" for s in json.load(open(D / "P_syms.json"))]
    pan = {k: np.load(D / f"P_{k}.npy", mmap_mode="r") for k in "OHLCV"}
    pan["FR"] = np.load(D / "P_FR.npy", mmap_mode="r")
    pan["syms"] = syms
    pan["IDX"] = pd.date_range("2020-01-01", periods=pan["C"].shape[0], freq="1h", tz="UTC")
    qv = pd.read_pickle(D / "qv_daily.pkl")
    qv.columns = [c + "USDT" for c in qv.columns]
    pan["qv"] = qv.reindex(columns=syms)
    pan["elig_ref"] = pd.read_pickle(D / "elig_daily.pkl")
    return pan


def replay_panel(pan: dict, spot: dict, start="2020-06-24", end="2026-10-11", a_override: dict | None = None,
                 u1_combo_cast=None, record=None, log_fills=True) -> dict:
    """Replay the analyst panel hour by hour through open_bar/close_bar/advance_day/decide (the functions used live).
    spot: {BTC/ETH: daily spot close Series} for the U1 leg. a_override: {day: a_U} for the fixture's bfilled
    warm-up days (σ needs 20 returns). u1_combo_cast: dtype applied to the U1 leg inside the combo (fixture: float32)."""
    import paper_u1 as U
    syms = pan["syms"]; IDX = pan["IDX"]
    st = new_state(syms)
    O = pan["O"]; Hh = pan["H"]; Ll = pan["L"]; C = pan["C"]; FR = pan["FR"]
    o_d, h_d, l_d, c_d, _ = daily_bars_from_hourly(O, Hh, Ll, C, pan["V"])
    days = pd.date_range(IDX[0].floor("D"), periods=c_d.shape[0], freq="1D", tz="UTC")
    qvm = pan["qv"].reindex(days).fillna(0.0).to_numpy(float)
    ind = {k: U.daily_indicators(spot[k]) for k in U1_SYMS}
    s0 = int(IDX.searchsorted(pd.Timestamp(start, tz="UTC"))); e0 = int(IDX.searchsorted(pd.Timestamp(end, tz="UTC")))

    def day_bars(di):
        out, q = {}, {}
        for j, s in enumerate(syms):
            if np.isfinite(c_d[di, j]):
                out[s] = (float(o_d[di, j]), float(h_d[di, j]), float(l_d[di, j]), float(c_d[di, j]))
            q[s] = qvm[di, j]
        return out, q

    for di in range(s0 // 24):
        advance_day(st, str(days[di].date()), *day_bars(di))
    pending = None
    for t in range(s0, e0):
        ts = IDX[t]
        Ot = np.nan_to_num(np.asarray(O[t], float)); Ct = np.asarray(C[t], float)
        ok = np.isfinite(Ct) & np.isfinite(np.asarray(O[t], float))
        FRt = np.asarray(FR[t], float)
        for b in BOOKS:
            meta = {"fill_time_utc": ts.isoformat(), "decision_bar_open_utc": pending["meta"]["decision_bar"]} if pending else {"fill_time_utc": ts.isoformat()}
            open_bar(st["books"][b], Ot, ok, FRt, pending["targets"][b] if pending else None, syms,
                     st["fills"][b] if log_fills else None, meta)
        pending = None
        Hv = np.nan_to_num(np.asarray(Hh[t], float)); Lv = np.nan_to_num(np.asarray(Ll[t], float)); Cv = np.nan_to_num(Ct)
        for b in BOOKS:
            close_bar(st["books"][b], ok, Hv, Lv, Cv)
        if ts.hour == DECISION_HOUR:
            di = t // 24; day = days[di]
            w1x_V = advance_day(st, str(day.date()), *day_bars(di))
            w1x_U = {k: (float(ind[k].loc[day, "w1x"]) if day in ind[k].index else 0.0) for k in U1_SYMS}
            w_c = {k: float(np.asarray(v, u1_combo_cast)) for k, v in w1x_U.items()} if u1_combo_cast else None
            ao = (a_override or {}).get(str(day.date()))
            pending = decide(st, str(day.date()), w1x_U, w1x_V, ok, w_c, ao)
            pending["meta"]["decision_bar"] = ts.isoformat()
            st["decisions"].append(pending["meta"])
        st["last_closed_bar"] = ts.isoformat()
        if record:
            record(t, ts, st)
    st["pending"] = pending
    return st


# ================================================================ serialisation
def _clean(x):
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating,)):
        return _clean(float(x))
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.ndarray):
        return _clean(x.tolist())
    return x


def _nan(v):
    return math.nan if v is None else v


def to_json(st: dict) -> dict:
    out = {k: v for k, v in st.items() if k not in ("books", "coins", "pending", "sym_index")}
    out["books"] = {b: {**{k: v for k, v in bk.items() if k not in ("u", "last")},
                        "u": bk["u"].tolist(), "last": bk["last"].tolist()} for b, bk in st["books"].items()}
    out["coins"] = st["coins"]
    p = st.get("pending")
    if p:
        out["pending"] = {"meta": p["meta"], "targets": {b: {st["syms"][j]: float(w[j]) for j in np.where(w != 0)[0]}
                                                         for b, w in p["targets"].items()}}
    else:
        out["pending"] = None
    return _clean(out)


def from_json(d: dict) -> dict:
    st = dict(d)
    st["sym_index"] = {s: i for i, s in enumerate(st["syms"])}
    n = len(st["syms"])
    st["books"] = {}
    for b, bk in d["books"].items():
        x = dict(bk)
        x["u"] = np.zeros(n); x["u"][:len(bk["u"])] = bk["u"]
        x["last"] = np.zeros(n); x["last"][:len(bk["last"])] = [(_nan(v) if v is not None else 0.0) for v in bk["last"]]
        st["books"][b] = x
    for cs in st["coins"].values():
        cs["c"] = _nan(cs.get("c"))
        for k in ("g1", "d1", "g2", "d2"):
            cs[k][2] = _nan(cs[k][2])
    p = d.get("pending")
    if p:
        tg = {}
        for b, m in p["targets"].items():
            w = np.zeros(n)
            for s, v in m.items():
                w[st["sym_index"][s]] = v
            tg[b] = w
        st["pending"] = {"meta": p["meta"], "targets": tg}
    return st


def seed_from_replay(st: dict) -> dict:
    """Engine state after the last fixture bar (2026-10-10 23:00 close) with the 10-11 00:00 pending targets."""
    keep = {k: st[k] for k in ("syms", "books", "coins", "qv", "rets", "e_prev", "pending", "last_closed_bar")}
    keep["v1_day"] = st["decisions"][-1]["day"]
    if keep["pending"]:
        keep["pending"]["meta"]["fill_bar"] = _iso(_ts(keep["pending"]["meta"]["decision_bar"]) + H)
    keep["decisions"] = st["decisions"][-5:]
    keep["fills"] = {b: [] for b in BOOKS}
    keep["sym_index"] = st["sym_index"]
    return to_json(keep)


def load_seed(path: Path = SEED_PATH) -> dict:
    import gzip
    with gzip.open(path, "rt") as f:
        return from_json(json.load(f))


# ================================================================ public market data (GET only, weight-aware)
FAPI_BASE = os.environ.get("PERP_MARKET_BASE_URL", "https://fapi.binance.com").rstrip("/")
DAY0 = pd.Timestamp("2020-01-01", tz="UTC")


def kline_weight(limit: int) -> int:
    return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10


class PublicHttp:
    """Public GETs to fapi with an IP-weight guard: reads X-MBX-USED-WEIGHT-1M and pauses when above `budget`
    (Binance limit 2400/min); honours 429/418 Retry-After. Thread-safe counters for the run report."""

    def __init__(self, budget: int | None = None, session=None):
        import threading
        self.budget = int(budget or os.environ.get("PAPER_Y1_WEIGHT_BUDGET", "1200"))
        self.lock = threading.Lock()
        self.session = session
        self.n = 0
        self.weight = 0
        self.max_used = 0
        self.sleeps = 0.0
        self.errors = 0
        self.used = 0

    def get(self, path: str, params: dict | None = None, weight: int = 1, base: str | None = None):
        import time
        import requests
        sess = self.session or requests
        for attempt in range(4):
            with self.lock:
                hot = self.used + weight > self.budget
            if hot:
                wait = 61 - (time.time() % 60)
                time.sleep(wait)
                with self.lock:
                    self.sleeps += wait
                    self.used = 0
            r = sess.get(f"{base or FAPI_BASE}{path}", params=params or {}, timeout=30,
                         headers={"User-Agent": "crypto-trader-paper-y1/1.0"})
            with self.lock:
                self.n += 1
                self.weight += weight
                uw = r.headers.get("X-MBX-USED-WEIGHT-1M") or r.headers.get("x-mbx-used-weight-1m")
                if uw is not None:
                    self.used = int(uw)
                    self.max_used = max(self.max_used, int(uw))
            if r.status_code in (418, 429):
                with self.lock:
                    self.errors += 1
                ra = float(r.headers.get("Retry-After") or 30)
                if ra > 120:
                    raise RuntimeError(f"fapi rate limited (retry-after {ra}s)")
                time.sleep(ra)
                with self.lock:
                    self.sleeps += ra
                continue
            if r.status_code >= 500:
                with self.lock:
                    self.errors += 1
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"fapi GET failed after retries: {path}")

    def stats(self) -> dict:
        return {"requests": self.n, "weight_est": self.weight, "max_used_weight_1m": self.max_used,
                "throttle_sleep_s": round(self.sleeps, 1), "http_errors": self.errors, "budget_1m": self.budget}


class Market:
    """All market data the live paper step needs. Every call is a public GET; tests replace this object."""

    def __init__(self, http: PublicHttp | None = None, workers: int | None = None):
        self.http = http or PublicHttp()
        self.workers = int(workers or os.environ.get("PAPER_Y1_WORKERS", "8"))

    def exchange_info(self) -> list[dict]:
        d = self.http.get("/fapi/v1/exchangeInfo", weight=1)
        return [{"symbol": s["symbol"], "base": s.get("baseAsset"), "quote": s.get("quoteAsset"),
                 "status": s.get("status"), "contractType": s.get("contractType"),
                 "underlyingType": s.get("underlyingType"), "onboardDate": s.get("onboardDate")}
                for s in d.get("symbols", [])]

    def funding_intervals(self) -> dict:
        d = self.http.get("/fapi/v1/fundingInfo", weight=1) or []
        return {x["symbol"]: int(x.get("fundingIntervalHours") or 8) for x in d}

    def funding_hour(self, t: pd.Timestamp) -> dict:
        """All settlements whose fundingTime falls in bar [t, t+1h) -> {symbol: rate}."""
        a = int(t.timestamp() * 1000)
        out, start = {}, a
        for _ in range(5):
            rows = self.http.get("/fapi/v1/fundingRate", {"startTime": start, "endTime": a + 3_599_999, "limit": 1000}, weight=1) or []
            for r in rows:
                out[r["symbol"]] = float(r["fundingRate"])
            if len(rows) < 1000:
                break
            start = int(rows[-1]["fundingTime"]) + 1
        return out

    def klines(self, sym: str, interval: str, start: pd.Timestamp, limit: int) -> list:
        limit = max(1, min(int(limit), 1500))
        return self.http.get("/fapi/v1/klines", {"symbol": sym, "interval": interval, "limit": limit,
                                                 "startTime": int(start.timestamp() * 1000)}, weight=kline_weight(limit)) or []

    def spot_daily(self, k: str, now: datetime) -> pd.Series:
        import paper_u1
        return paper_u1.fetch_spot_daily(k, now)

    def pmap(self, fn, items):
        from concurrent.futures import ThreadPoolExecutor
        items = list(items)
        if not items:
            return {}
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            return dict(zip(items, ex.map(fn, items)))


def candidate_pool(info: list[dict]) -> dict:
    """USDT-M PERPETUAL, quote USDT, TRADING, crypto underlyings only (no stock/commodity/index), minus the exclusion list."""
    out = {}
    for s in info:
        if s.get("contractType") != "PERPETUAL" or s.get("quote") != "USDT" or s.get("status") != "TRADING":
            continue
        if (s.get("underlyingType") or "COIN") != "COIN":
            continue
        if (s.get("base") or "") in EXCLUDE:
            continue
        out[s["symbol"]] = s
    return out


def _bars_by_open(rows: list, closed_before_ms: int | None = None) -> dict:
    out = {}
    for k in rows:
        if closed_before_ms is not None and int(k[6]) >= closed_before_ms:
            continue
        out[pd.Timestamp(int(k[0]), unit="ms", tz="UTC")] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7]))
    return out


# ================================================================ live paper step (hourly job)
KEEP_HOURLY = 24 * 120
KEEP_FILLS = 400
D1 = timedelta(days=1)


class Stop(Exception):
    """Input not available yet: stop at a clean bar boundary; the next run resumes from here (exact catch-up)."""


def bootstrap_coin(st: dict, sym: str, rows: dict) -> None:
    """Rebuild a coin's SAR/indicator state and qv window from its full perp 1d history (2020-01-01 .. v1_day)."""
    add_symbol(st, sym)
    cs, q = new_coin(), []
    d, end = DAY0, _ts(st["v1_day"])
    while d <= end:
        r = rows.get(d)
        if r is None:
            coin_day(cs, str(d.date()), math.nan, math.nan, math.nan, math.nan, False)
        else:
            coin_day(cs, str(d.date()), r[0], r[1], r[2], r[3], True)
        q = (q + [r[4] if r else 0.0])[-QV_WIN:]
        d += D1
    st["coins"][sym] = cs
    st["qv"][sym] = q
    st.setdefault("ready", {})[sym] = True


def fetch_daily_history(M: Market, sym: str, onboard_ms, end_day: pd.Timestamp) -> dict:
    start = DAY0
    if onboard_ms:
        start = max(DAY0, pd.Timestamp(int(onboard_ms), unit="ms", tz="UTC").floor("D"))
    rows = {}
    while start <= end_day:
        lim = min((end_day - start).days + 1, 1500)
        raw = M.klines(sym, "1d", start, lim)
        if not raw:
            break
        got = _bars_by_open(raw)
        rows.update(got)
        nxt = max(got) + D1
        if nxt <= start:
            break
        start = nxt               # page until end_day (a page can be short when it spans pre-listing days)
    return {k: v for k, v in rows.items() if k <= end_day}


def _held(st) -> set:
    out = set()
    for bk in st["books"].values():
        for j in np.where(bk["u"] != 0)[0]:
            out.add(st["syms"][j])
    return out


def _pending_syms(st) -> set:
    p = st.get("pending")
    if not p:
        return set()
    return {st["syms"][j] for w in p["targets"].values() for j in np.where(w != 0)[0]}


def step_live(st: dict, now: datetime, M: Market | None = None, budget_s: float | None = None) -> dict:
    import time
    t_run = time.time()
    budget = float(budget_s or os.environ.get("PAPER_Y1_BUDGET_S", "240"))
    M = M or Market()
    now = now.astimezone(timezone.utc)
    cur = pd.Timestamp(now).floor("h")
    summ = {"now_utc": now.isoformat(), "bars_closed": 0, "open_phase": None, "fills": 0, "decisions": [],
            "stopped": None, "bootstrapped": 0, "funding_assumed_zero": []}
    timing = {}

    def left():
        return budget - (time.time() - t_run)

    # ---- universe (exchangeInfo once per UTC day, and on first run)
    uni = st.get("universe") or {}
    t0 = time.time()
    if not uni.get("updated_utc") or _ts(uni["updated_utc"]).floor("D") < cur.floor("D") or now - _ts(uni["updated_utc"]) > timedelta(hours=6):
        info = M.exchange_info()
        pool = candidate_pool(info)
        from collections import Counter
        st["universe"] = {"updated_utc": now.isoformat(), "pool": {s: v.get("onboardDate") for s, v in pool.items()},
                          "trading": sorted(s["symbol"] for s in info if s.get("status") == "TRADING"),
                          "n_usdt_perp_trading": sum(1 for s in info if s.get("contractType") == "PERPETUAL" and s.get("quote") == "USDT" and s.get("status") == "TRADING"),
                          "underlying_types": dict(Counter(str(s.get("underlyingType")) for s in info if s.get("contractType") == "PERPETUAL" and s.get("quote") == "USDT" and s.get("status") == "TRADING"))}
        try:
            st["funding_iv"] = M.funding_intervals()
        except Exception as e:  # noqa: BLE001
            log.warning("y1_funding_info_skip %s", e)
    pool = st["universe"]["pool"]
    trading = set(st["universe"]["trading"])
    timing["universe_s"] = round(time.time() - t0, 2)

    # ---- bootstrap coins new to the tracked set (full 1d history -> indicator/SAR state + qv window)
    t0 = time.time()
    ready = st.setdefault("ready", {s: True for s in st["syms"]})
    todo = sorted(s for s in pool if not ready.get(s))
    if todo:
        end_day = _ts(st["v1_day"])
        batch = []
        for s in todo:
            if left() < budget * 0.45:
                break
            batch.append(s)
            if len(batch) >= 40:
                got = M.pmap(lambda x: fetch_daily_history(M, x, pool.get(x), end_day), batch)
                for x in batch:
                    bootstrap_coin(st, x, got[x]); summ["bootstrapped"] += 1
                batch = []
        if batch:
            got = M.pmap(lambda x: fetch_daily_history(M, x, pool.get(x), end_day), batch)
            for x in batch:
                bootstrap_coin(st, x, got[x]); summ["bootstrapped"] += 1
    summ["bootstrap_pending"] = sum(1 for s in pool if not ready.get(s))
    timing["bootstrap_s"] = round(time.time() - t0, 2)

    # ---- bar loop
    t0 = time.time()
    last = _ts(st["last_closed_bar"])
    p = st.get("pending")
    first = last + H
    if p:
        first = min(first, _ts(p["meta"]["decision_bar"]))
    hours = int((cur - first) / H) + 2
    if hours > 1450:
        raise RuntimeError(f"paper gap too large to catch up ({hours} h)")
    cache: dict = {}
    cms = int((cur + H).timestamp() * 1000)

    def need(syms):
        miss = [s for s in syms if s not in cache]
        if miss:
            got = M.pmap(lambda s: _bars_by_open(M.klines(s, "1h", first, hours)), miss)
            cache.update(got)

    fund: dict = {}
    daily_cache: dict = {}
    spot_ind: dict = {}

    def bar_inputs(t, syms):
        """O/ok arrays for the bar; Stop when a TRADING symbol's bar isn't published yet."""
        n = len(st["syms"])
        O = np.zeros(n); Hh = np.zeros(n); Ll = np.zeros(n); C = np.zeros(n); ok = np.zeros(n, bool)
        for s in syms:
            j = st["sym_index"][s]
            r = cache[s].get(t)
            if r is None:
                later = any(k > t for k in cache[s])
                if s in trading and not later:
                    raise Stop(f"missing 1h kline {s} {_iso(t)}")
                continue                               # delisted / data gap -> no data (engine closes at last close)
            O[j], Hh[j], Ll[j], C[j] = r[0], r[1], r[2], r[3]
            ok[j] = True
        return O, Hh, Ll, C, ok

    def rates_for(t, ok_syms):
        if not ok_syms:
            return {}
        if t not in fund:
            fund[t] = M.funding_hour(t)
        fr = fund[t]
        iv = st.get("funding_iv") or {}
        for s in ok_syms:
            if t.hour % int(iv.get(s, 8)) == 0 and s not in fr:
                if cur - t >= timedelta(hours=3):
                    summ["funding_assumed_zero"].append(f"{s}@{_iso(t)}")
                else:
                    raise Stop(f"funding not published {s} {_iso(t)}")
        return fr

    def do_open(t):
        p = st.get("pending")
        fill = bool(p) and _ts(p["meta"]["fill_bar"]) == t
        held = _held(st)
        syms = held | (_pending_syms(st) if fill else set())
        need(syms)
        O, _, _, _, ok = bar_inputs(t, syms)
        fr = rates_for(t, [s for s in held if ok[st["sym_index"][s]]])
        FRa = np.zeros(len(st["syms"]))
        for s in held:
            FRa[st["sym_index"][s]] = fr.get(s, 0.0)
        W = None
        if fill:
            dbar = _ts(p["meta"]["decision_bar"])
            ok_dec = np.zeros(len(st["syms"]), bool)
            for s in syms:
                ok_dec[st["sym_index"][s]] = cache[s].get(dbar) is not None
            W = {b: np.where(ok_dec, w, 0.0) for b, w in p["targets"].items()}
        nf = 0
        for b in BOOKS:
            rec = []
            open_bar(st["books"][b], O, ok, FRa, W[b] if W else None, st["syms"], rec,
                     {"fill_time_utc": _iso(t), "decision_bar_open_utc": p["meta"]["decision_bar"] if fill else ""})
            st["fills"][b] = (st["fills"].get(b, []) + rec)[-KEEP_FILLS:]
            st.setdefault("n_fills", {}).setdefault(b, 0)
            st["n_fills"][b] += len(rec)
            nf += len(rec)
        if fill:
            st["pending"] = None
        st["open_done_bar"] = _iso(t)
        summ["fills"] += nf

    def decision_inputs(t):
        day = t.floor("D")
        live = [s for s in st["syms"] if s in pool and ready.get(s)]
        miss = [s for s in live if s not in daily_cache]
        if miss:
            nd = max(1, (cur.floor("D") - day).days)
            got = M.pmap(lambda s: _bars_by_open(M.klines(s, "1d", day, min(nd, 99)), closed_before_ms=cms), miss)
            daily_cache.update(got)
        bars, qv = {}, {}
        for s in live:
            r = daily_cache[s].get(day)
            if r is not None:
                bars[s] = r[:4]; qv[s] = r[4]
        if not spot_ind:
            import paper_u1
            for k in U1_SYMS:
                spot_ind[k] = paper_u1.daily_indicators(M.spot_daily(k, now))
        if not all(day in spot_ind[k].index for k in U1_SYMS):
            raise Stop(f"spot daily close for {day.date()} not available")
        w1x_U = {k: float(spot_ind[k].loc[day, "w1x"]) for k in U1_SYMS}
        return day, bars, qv, w1x_U

    try:
        t = last + H
        while t + H <= now:
            if left() < 10:
                raise Stop("time budget")
            dec_in = None
            if t.hour == DECISION_HOUR:
                if summ["bootstrap_pending"]:
                    raise Stop(f"bootstrap pending ({summ['bootstrap_pending']} coins)")
                dec_in = decision_inputs(t)
            if st.get("open_done_bar") != _iso(t):
                do_open(t)
            held = _held(st)
            need(held)
            _, Hh, Ll, C, ok = bar_inputs(t, held)
            for b in BOOKS:
                if close_bar(st["books"][b], ok, Hh, Ll, C):
                    st.setdefault("liquidations", []).append({"book": b, "bar": _iso(t)})
            if dec_in:
                day, bars, qv, w1x_U = dec_in
                w1x_V = advance_day(st, str(day.date()), bars, qv)
                st["v1_day"] = str(day.date())
                w_c = {k: float(np.asarray(v, U1_COMBO_CAST)) for k, v in w1x_U.items()} if U1_COMBO_CAST else None
                pend = decide(st, str(day.date()), w1x_U, w1x_V, np.ones(len(st["syms"]), bool), w_c)
                pend["meta"].update({"decision_bar": _iso(t), "fill_bar": _iso(t + H),
                                     "n_long_V": int((w1x_V > 0).sum()), "n_short_V": int((w1x_V < 0).sum()),
                                     "gross_target_C": float(np.abs(pend["targets"]["C_lev"]).sum()),
                                     "net_target_C": float(pend["targets"]["C_lev"].sum()),
                                     "equity": {b: st["books"][b]["eq"] for b in BOOKS}})
                st["pending"] = pend
                st["decisions"] = (st.get("decisions", []) + [pend["meta"]])[-60:]
                summ["decisions"].append({k: pend["meta"][k] for k in ("day", "a_U", "L_C", "L_V", "n_eligible", "gross_target_C")})
            st["last_closed_bar"] = _iso(t)
            st.setdefault("equity_hourly", []).append([_iso(t), st["books"]["C_lev"]["eq"], st["books"]["V1_lev"]["eq"]])
            summ["bars_closed"] += 1
            t = t + H
        if t == cur and st.get("open_done_bar") != _iso(cur):
            do_open(cur)
            summ["open_phase"] = {"bar": _iso(cur)}
    except Stop as e:
        summ["stopped"] = str(e)
    st["equity_hourly"] = st.get("equity_hourly", [])[-KEEP_HOURLY:]
    timing["bars_s"] = round(time.time() - t0, 2)
    timing["total_s"] = round(time.time() - t_run, 2)
    summ["timing"] = timing
    summ["http"] = M.http.stats()
    return summ


# ================================================================ persistence (own GCS objects; never trader/state.json)
PUBLIC_OBJECT = os.environ.get("PAPER_Y1_PUBLIC_OBJECT", "trader/paper/y1_public.json")


class Y1Store:
    def __init__(self, bucket: str | None = None, object_name: str = Y1_OBJECT, local_path: str = Y1_LOCAL,
                 public_object: str = PUBLIC_OBJECT):
        from state_store import StateStore
        assert object_name != "trader/state.json" and public_object != "trader/state.json"
        self._s = StateStore(bucket=bucket, object_name=object_name, local_path=local_path)
        self._p = StateStore(bucket=bucket, object_name=public_object, local_path=local_path + ".public.json")

    def _load(self, s):
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

    def load(self) -> dict | None:
        return self._load(self._s)

    def load_public(self) -> dict | None:
        return self._load(self._p)

    def save(self, st_json: dict, public: dict) -> None:
        self._s.save(st_json, if_generation_match=self._s._generation)
        self._p.save(public)


def initial_state() -> dict:
    st = load_seed()
    st["paper_start"] = {"bar": st["last_closed_bar"],
                         "equity": {b: st["books"][b]["eq"] for b in BOOKS},
                         "note": "seed = analyst fixture state after the 2026-10-10 23:00 UTC close (backtest started at 10,000 on 2020-06-24)"}
    st["ready"] = {s: True for s in st["syms"]}
    return st


def run_paper(now: datetime | None = None, market: Market | None = None, store: Y1Store | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    store = store or Y1Store()
    raw = store.load()
    seeded = raw is None
    st = initial_state() if seeded else from_json(raw)
    snapshot = to_json(st)
    try:
        summ = step_live(st, now, market)
        summ["ok"] = True          # a Stop is a clean deferral (the next run resumes exactly); see "stopped"
        out = to_json(st)
    except Exception as e:  # noqa: BLE001  (failures never leave a half-applied bar; state rolls back)
        log.exception("paper_y1_step_fail")
        summ = {"ok": False, "now_utc": now.isoformat(), "error": str(e)[:300]}
        out = snapshot
        out["errors"] = (out.get("errors", []) + [summ])[-50:]
    summ["seeded"] = seeded
    out["updated_at_utc"] = now.isoformat()
    out["runner_version"] = os.environ.get("RUNNER_VERSION") or "unknown"
    out["runs"] = (out.get("runs", []) + [{k: summ.get(k) for k in ("now_utc", "ok", "bars_closed", "open_phase", "stopped", "error",
                                                                   "fills", "bootstrapped", "bootstrap_pending", "timing", "http")}])[-200:]
    pub = public_view(out)
    store.save(out, pub)
    log.info("paper_y1 ok=%s bars=%s fills=%s stopped=%s timing=%s http=%s", summ.get("ok"), summ.get("bars_closed"),
             summ.get("fills"), summ.get("stopped"), summ.get("timing"), summ.get("http"))
    return summ


# ================================================================ read-only public view (API /status "paper")
WARN_Y1 = ["tv75 為事後選出（看過 tv45/60/75/90 後才選）", "V1 鄰居換入組合後 4/8 的 MaxDD 超過 -60%",
           "回測 MaxDD -57.7%（離 60% 門檻僅 2.3 個百分點）", "組合層槓桿上限 5x", "全倉（CROSS）", "無停損"]


def _book_view(st_json: dict, b: str) -> dict:
    bk = st_json["books"][b]
    syms = st_json["syms"]
    eq = bk.get("eq") or 0.0
    pos = []
    gross_n = net_n = 0.0
    for j, u in enumerate(bk["u"]):
        if u:
            px = bk["last"][j] or 0.0
            nt = u * px
            gross_n += abs(nt); net_n += nt
            pos.append({"symbol": syms[j], "units": u, "mark": px, "notional": nt, "weight": nt / eq if eq > 0 else None})
    pos.sort(key=lambda x: -abs(x["notional"]))
    start = (st_json.get("paper_start") or {}).get("equity", {}).get(b)
    g = gross_n / eq if eq > 0 else None
    return {"equity": eq, "cash": bk.get("cash"), "gross": g, "net": net_n / eq if eq > 0 else None,
            "n_positions": len(pos), "n_long": sum(1 for x in pos if x["units"] > 0), "n_short": sum(1 for x in pos if x["units"] < 0),
            # uniform adverse move (longs down / shorts up) that reaches maintenance 0.5% of gross: E/G - 0.5%
            "liq_distance_pct": ((eq / gross_n - MAINT) / (1 - MAINT) * 100) if gross_n > 0 and eq > 0 else None,
            "return_since_paper_start_pct": ((eq / start - 1) * 100) if start else None,
            "funding_pnl": bk.get("funding"), "fees": bk.get("fees"), "n_liq": bk.get("n_liq"),
            "positions": pos[:40]}


def public_view(st_json: dict | None) -> dict | None:
    if not st_json:
        return None
    last = (st_json.get("decisions") or [None])[-1] or {}
    p = st_json.get("pending")
    nxt = None
    if st_json.get("last_closed_bar"):
        lc = _ts(st_json["last_closed_bar"])
        d = lc.floor("D") + timedelta(hours=DECISION_HOUR)
        if d <= lc:
            d += D1
        nxt = _iso(d + H)
    if p and (p.get("meta") or {}).get("fill_bar"):
        nxt = p["meta"]["fill_bar"]
    common = {"mode": "paper_only", "label_zh": LABEL_ZH, "not_live": True, "orders": "none", "in_books": False,
              "approvable": False, "last_closed_bar_utc": st_json.get("last_closed_bar"),
              "open_done_bar_utc": st_json.get("open_done_bar"), "next_fill_utc": nxt,
              "paper_start": st_json.get("paper_start"), "updated_at_utc": st_json.get("updated_at_utc"),
              "runner_version": st_json.get("runner_version"), "last_run": (st_json.get("runs") or [None])[-1],
              "errors_recent": (st_json.get("errors") or [])[-3:]}
    dec_keys = ("day", "decision_bar", "fill_bar", "a_U", "a_V", "sigma_U", "sigma_V", "sigma_C", "L_C", "L_V",
                "n_eligible", "n_long_V", "n_short_V", "gross_target_C", "net_target_C", "w1x_U")
    y1 = {"code": CODE, "family": FAMILY, "strategy_id": STRATEGY_ID, "status_zh": "PAPER（Robin 核可模擬，積極版 tv75）",
          "warnings_zh": WARN_Y1, **common, **_book_view(st_json, "C_lev"),
          "shadow_1x_equity": {k: st_json["books"][k].get("eq") for k in ("U1_1x", "V1_1x", "C_1x")},
          "last_decision": {k: last.get(k) for k in dec_keys} if last else None,
          "pending_targets": (dict(sorted(p["targets"]["C_lev"].items(), key=lambda kv: -abs(kv[1]))) if p else None),
          "recent_fills": (st_json.get("fills") or {}).get("C_lev", [])[-20:],
          "n_fills_live": (st_json.get("n_fills") or {}).get("C_lev", 0),
          "universe": {k: (st_json.get("universe") or {}).get(k) for k in ("updated_utc", "n_usdt_perp_trading", "underlying_types")}
                      | {"n_pool": len((st_json.get("universe") or {}).get("pool") or {})}}
    x1 = {"code": X_CODE, "family": X_FAMILY, "strategy_id": X_STRATEGY_ID,
          "status_zh": "PAPER（Y1 的 V1 腿單獨運行，vt60 上限 3x）", **common, **_book_view(st_json, "V1_lev"),
          "last_decision": {k: last.get(k) for k in ("day", "fill_bar", "L_V", "sigma_V", "n_eligible", "n_long_V", "n_short_V")} if last else None,
          "pending_targets": (dict(sorted(p["targets"]["V1_lev"].items(), key=lambda kv: -abs(kv[1]))) if p else None),
          "recent_fills": (st_json.get("fills") or {}).get("V1_lev", [])[-20:],
          "n_fills_live": (st_json.get("n_fills") or {}).get("V1_lev", 0)}
    return _clean({"Y1": y1, "X1": x1})
