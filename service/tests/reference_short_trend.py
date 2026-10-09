"""Reference for the S family (short_trend_perp) — verbatim port of the analyst's engine.

Sources (vendored logic, pure Python; records trades):
  strategy-unified-3y/rd_short_range/code/engine_sr.py   (bt, Data, sma_atr, daily_to_bars, btc_daily)
  strategy-unified-3y/rd_short_range/code/families.py    (sema / sdon signals, filters, PAIRS)
  strategy-unified-3y/rd_p118v2/code/{eng,scan}.py        (2h/6h/12h data, MAXHOLD 2h=120 6h=40 12h=20)
Rules: signal at bar close i -> fill at bar i+1 OPEN; stops checked intrabar from the fill bar on,
short stop fills at max(open, stop); 20 bps per side; daily info (BTC / coin) on intraday bars uses
the previous day's close; funding affects P&L only (not decisions)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

FIX = Path(__file__).resolve().parent / "fixtures" / "short_trend"
START = pd.Timestamp("2023-09-24", tz="UTC")
COST = 0.002
MAINT = 0.005
MAXHOLD = {"1d": 40, "4h": 60, "1h": 240, "2h": 120, "6h": 40, "12h": 20}
PAIRS = [(9, 21), (12, 26), (20, 50), (30, 80), (50, 100), (50, 200)]


def load(sym, tf):
    df = pd.read_csv(FIX / f"{sym}_{tf}.csv.gz", parse_dates=["ts"], index_col="ts")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.astype(float)


def load_funding(sym):
    d = pd.read_csv(FIX / f"funding_{sym}.csv.gz")
    idx = pd.to_datetime(d.ts, format="mixed", utc=True).dt.floor("min")
    s = pd.Series(d.rate.values.astype(float), index=pd.DatetimeIndex(idx)).sort_index()
    return s[~s.index.duplicated(keep="last")]


def sma_atr(df, n=14):
    pc = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def daily_to_bars(daily_flag, idx, tf):
    s = daily_flag.astype(float)
    if tf != "1d":
        s = s.shift(1)
    return s.reindex(idx, method="ffill").fillna(0).values.astype(bool)


def prev(x):
    y = np.empty_like(x); y[0] = x[0]; y[1:] = x[:-1]; return y


class Data:
    def __init__(self, sym, tf):
        self.sym, self.tf = sym, tf
        df = load(sym, tf); self.df = df; idx = df.index; self.idx = idx
        self.O, self.H, self.L, self.C = (df[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        self.A = sma_atr(df).fillna(0).values
        self.s0 = int(np.searchsorted(idx.values, START.to_datetime64()))
        f = load_funding(sym)
        fr = np.zeros(len(idx)); pos = idx.searchsorted(f.index, side="left")
        for r_, j in zip(f.values, pos):
            if 0 <= j < len(fr):
                fr[j] += r_
        self.FR = fr
        favg = f.rolling(9).mean()
        self.favg = favg.reindex(idx.union(favg.index)).ffill().reindex(idx).fillna(0).values
        b = load("BTC", "1d").Close
        self.btc200 = daily_to_bars(b < b.rolling(200).mean(), idx, tf)
        self.btc50 = daily_to_bars(b < b.ewm(span=50, adjust=False).mean(), idx, tf)
        d1 = load(sym, "1d").Close
        self.coin50 = daily_to_bars(d1 < d1.rolling(50).mean(), idx, tf)


def filt(D, name):
    n = len(D.C)
    return {"none": np.ones(n, bool), "btc200": D.btc200, "btc50": D.btc50, "coin50": D.coin50,
            "fpos": D.favg > 0, "btc50_fpos": D.btc50 & (D.favg > 0)}[name]


def signals(D, fam, p):
    C = D.C; n = len(C); Z = np.zeros(n, bool); ent = np.zeros(n, np.int8)
    with np.errstate(invalid="ignore"):
        if fam == "sdon":
            lo = pd.Series(D.L).rolling(p["n"]).min().shift(1).values
            hi = pd.Series(D.H).rolling(max(p["n"] // 2, 5)).max().shift(1).values
            br = C < lo; e = br & ~prev(br) & filt(D, p["filt"]); ent[e] = -2
            return ent, Z, Z, Z, C > hi
        if fam == "sema":
            f, s = PAIRS[p["pair"]]
            ef = pd.Series(C).ewm(span=f, adjust=False).mean().values
            es = pd.Series(C).ewm(span=s, adjust=False).mean().values
            st = (ef < es) & filt(D, p["filt"]); ent[st & ~prev(st)] = -2
            return ent, Z, Z, Z, ef > es
    raise ValueError(fam)


def bt(D, ent, x, stop_m, trail_m, max_hold, lev=1.0, cost=COST, maint=MAINT):
    """engine_sr.bt with trade records (times are bar open timestamps)."""
    O, H, L, C, A, FR = D.O, D.H, D.L, D.C, D.A, D.FR
    x1l, x1s, x2l, x2s = x
    n = len(C); s0 = D.s0; idx = D.idx
    cash = 10000.0; sh = 0.0; kind = 0; stop = 0.0; ei = 0
    pend_ent = 0; pend_ex = False; pend_a = 0.0; entry_px = 0.0
    trades = []; cur = None
    def close(i, px, reason):
        nonlocal cur
        cur.update(exit=idx[i], exit_px=px, reason=reason); trades.append(cur); cur = None
    for i in range(s0, n):
        o = O[i]
        if sh != 0.0 and FR[i] != 0.0:
            cash -= sh * o * FR[i]
        if pend_ex and sh != 0.0:
            px = o * (1 - cost) if sh > 0 else o * (1 + cost)
            cash += sh * px; sh = 0.0; kind = 0; close(i, px, pend_reason)
        pend_ex = False
        if pend_ent != 0 and sh == 0.0 and cash > 0:
            d = 1 if pend_ent > 0 else -1
            px = o * (1 + cost) if d > 0 else o * (1 - cost)
            sh = d * cash * lev / px; cash -= sh * px; entry_px = px; ei = i; kind = abs(pend_ent)
            if stop_m >= 50:
                stop = -1e18 if d > 0 else 1e18
            else:
                stop = px - stop_m * pend_a if d > 0 else px + stop_m * pend_a
            cur = dict(entry=idx[i], entry_px=px, side=d, init_stop=stop)
        pend_ent = 0
        c = C[i]
        if sh > 0:
            if L[i] <= stop:
                px = min(o, stop) * (1 - cost); cash += sh * px; sh = 0.0; kind = 0; close(i, px, "stop_loss")
            elif cash + sh * L[i] <= maint * sh * L[i]:
                cash = max(cash + sh * L[i] * (1 - cost), 0.0); sh = 0.0; kind = 0; close(i, L[i], "liquidation")
            else:
                if kind == 2 and 0 < trail_m < 50:
                    t = c - trail_m * A[i]
                    if t > stop: stop = t
                ex = x1l[i] if kind == 1 else x2l[i]
                if ex or (i - ei) >= max_hold:
                    pend_ex = True; pend_reason = "signal_exit" if ex else "max_hold"
                elif ent[i] < 0 and i + 1 < n:
                    pend_ex = True; pend_reason = "reverse"; pend_ent = ent[i]; pend_a = A[i]
        elif sh < 0:
            if H[i] >= stop:
                px = max(o, stop) * (1 + cost); cash += sh * px; sh = 0.0; kind = 0; close(i, px, "stop_loss")
            elif cash + sh * H[i] <= maint * (-sh) * H[i]:
                cash = max(cash + sh * H[i] * (1 + cost), 0.0); sh = 0.0; kind = 0; close(i, H[i], "liquidation")
            else:
                if kind == 2 and 0 < trail_m < 50:
                    t = c + trail_m * A[i]
                    if t < stop: stop = t
                ex = x1s[i] if kind == 1 else x2s[i]
                if ex or (i - ei) >= max_hold:
                    pend_ex = True; pend_reason = "signal_exit" if ex else "max_hold"
                elif ent[i] > 0 and i + 1 < n:
                    pend_ex = True; pend_reason = "reverse"; pend_ent = ent[i]; pend_a = A[i]
        if sh == 0.0 and pend_ent == 0 and ent[i] != 0 and i + 1 < n and A[i] > 0:
            pend_ent = ent[i]; pend_a = A[i]
    final = cash
    if sh != 0.0:
        px = C[n - 1] * (1 - cost) if sh > 0 else C[n - 1] * (1 + cost)
        final = cash + sh * px; close(n - 1, px, "eod_flat")
    return trades, final


def run_row(params):
    """params = catalog row params for an S row."""
    fam = params["family_engine"]; tf = params["tf"]
    p = {"filt": params["filt"]}
    if fam == "sema":
        p["pair"] = int(params["pair"])
    else:
        p["n"] = int(params["n"])
    D = Data(params["symbol"], tf)
    ent, *x = signals(D, fam, p)
    return D, bt(D, ent, tuple(x), float(params["stop_m"]), float(params["trail_m"]), MAXHOLD[tf])
