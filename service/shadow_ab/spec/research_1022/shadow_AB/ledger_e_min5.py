"""Ledger E-min5 spec + backtest (research only; NO orders; catalog/allocation/live untouched).
E = 80% U1 tv0.40 x1.75 (BTC/ETH perps, cross, L cap 3, 25% band) + 20% S5 long/short x1.75 (EWMAC, isolated legs, buffer b),
same breaker as ledger C/E: DD<=-10% -> m=0.5 (back to 1 when DD>-5%); DD<=-40% -> flat 30d, then m=0.5 with HWM reset.
Daily decision at close, execution at the next 00:00 UTC open, 20bps/side, real funding (data already cached from data.binance.vision).
NEW RULE (min order, Binance min notional 5 USDT):
  (1) any adjustment order with |notional| < MIN_ORDER (5 USDT) is not sent (the leg keeps its current position);
  (2) if the target is 0, the leg is always closed in full (reduce-only close), even if the residual is < 5 USDT.
Dollar-based engine (equity in USDT), so results depend on starting capital. MIN_ORDER=0 reproduces the original rule.
Usage: python3 ledger_e_min5.py            # full study -> E_MINORDER.md (+ e_min5_results.json, e_min5_returns.csv)"""
import os, sys, json, math, numpy as np, pandas as pd
from pathlib import Path
from numba import njit
HERE = Path(__file__).resolve().parent; R1022 = HERE.parent
sys.path.insert(0, str(R1022)); sys.path.insert(0, str(R1022.parent / "u1_shorts"))
from lib import *                       # panel, fund(), metrics, wf_folds, OOS_START/END, COST (=0.002)
import s5_ewmac as S5
W_U1, LV, CAP, BAND, MAINT, MIN_ORDER = 0.8, 1.75, 3.0, 0.25, 0.005, 5.0
CFGS = S5.CFGS; KEYS = [S5.key(c) for c in CFGS]
# ---------- arrays
cols = list(C.columns); n = len(IDX)
Ov = O.values; Hv = H.values; Lv_ = Lo.values; Cv = C.values
Of = O.ffill().values; Cf = C.ffill().values                       # carry last price for marking delisted legs
F5 = fund().values                                                  # day d = settlements in (d, d+1]
iB, iE = cols.index("BTCUSDT"), cols.index("ETHUSDT")
SPc = spot_close(["BTCUSDT", "ETHUSDT"])
def _w1():
    out = []
    for s in ("BTCUSDT", "ETHUSDT"):
        sc = SPc[s]["close"].reindex(IDX); s20 = np.log(sc / sc.shift(1)).rolling(20, min_periods=15).std() * math.sqrt(365)
        out.append(((sc > sc.rolling(200, min_periods=200).mean()).astype(float) * 0.5 * np.minimum(1.0, 0.60 / s20)).fillna(0.0).values)
    return np.c_[out].T
W1 = _w1()
WS = np.zeros((len(CFGS), n, len(cols))); DBS = np.zeros_like(WS)
for k, c in enumerate(CFGS):
    w, sp = S5.weights(c, False); WS[k] = w.reindex(index=IDX, columns=cols).fillna(0).values; DBS[k] = (sp * c["b"]).reindex(index=IDX, columns=cols).fillna(0).values

@njit(cache=True)
def sim(K, minord, sched, start, wu1, lv, s5only, Ov, Hv, Lv_, Cv, Of, Cf, F5, W1, WS, DBS, iB, iE, cost, cap, band, maint):
    """sched[i] = S5 config index used for decision at close i. Portfolio is all-cash until open `start`.
    s5only=1: S5 sleeve alone (capital K, x lv, no breaker) for WF selection. Returns equity at close, stats."""
    n, m_ = Ov.shape; u5 = np.zeros(m_); uu = np.zeros(2); ui = np.array([iB, iE])
    cash5 = K * (1 - wu1) if s5only == 0 else K; cashu = K * wu1 if s5only == 0 else 0.0
    eq = np.full(n, np.nan); m = 1.0; hwm = K; flat = 0
    x1c = 1.0; x1u = np.zeros(2); x1prev = 1.0; x1r = np.zeros(n); nx1 = 0
    st = np.zeros(8)  # 0 orders sent,1 skipped<min,2 forced small closes,3 fees,4 funding,5 orders<min that orig would send(=1),6 leg liq,7 u1 liq
    for i in range(1, n):
        # ---- x1 (unlevered U1 book, for L) : same band rule, fractional
        o2 = np.array([Of[i, iB], Of[i, iE]])
        if i >= start or True:
            x1c -= (x1u * o2 * np.array([F5[i-1, iB], F5[i-1, iE]])).sum()
            V = x1c + (x1u * o2).sum()
            for j in range(2):
                wt = W1[i-1, j]; wc = x1u[j] * o2[j] / V if V > 0 else 0.0
                tr = (x1u[j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > band * abs(wt))
                if tr and V > 0:
                    nu = wt * V / o2[j]; d = nu - x1u[j]; x1c -= d * o2[j] + abs(d) * o2[j] * cost; x1u[j] = nu
        if i >= start:
            # funding on positions held since previous open (paid over bar i-1)
            for j in range(2):
                f = uu[j] * o2[j] * F5[i-1, ui[j]]; cashu -= f; st[4] += f
            for c in range(m_):
                if u5[c] != 0:
                    f = u5[c] * Of[i, c] * F5[i-1, c]; cash5 -= f; st[4] += f
            eu = cashu + (uu * o2).sum(); es = cash5
            for c in range(m_):
                if u5[c] != 0: es += u5[c] * Of[i, c]
            if s5only == 0:
                E = eu + es; cashu += wu1 * E - eu; cash5 += (1 - wu1) * E - es; eu = wu1 * E; es = (1 - wu1) * E
            # ---- U1 rebalance (targets from close i-1)
            if s5only == 0:
                r = x1r[max(0, nx1-60):nx1]
                if nx1 >= 20:
                    mu = r.mean(); sd = math.sqrt(((r - mu) ** 2).sum() / (len(r) - 1)) * math.sqrt(365); Lt = min(cap, 0.40 * lv * m / max(sd, 0.05))
                else:
                    Lt = 1.0
                for j in range(2):
                    wt = W1[i-1, j] * Lt if m > 0 else 0.0; wc = uu[j] * o2[j] / eu if eu > 0 else 0.0
                    tr = (uu[j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > band * abs(wt))
                    if tr and eu > 0:
                        nu = wt * eu / o2[j]; d = nu - uu[j]; notl = abs(d) * o2[j]
                        if wt != 0 and notl < minord:
                            st[1] += 1; continue
                        if wt == 0 and notl < minord: st[2] += 1
                        if notl > 0: st[0] += 1
                        if 0 < notl < 5.0: st[5] += 1
                        fee = notl * cost; cashu -= d * o2[j] + fee; uu[j] = nu; st[3] += fee
            # ---- S5 rebalance with buffer (targets from close i-1)
            k = sched[i-1]; mm = m if s5only == 0 else 1.0
            for c in range(m_):
                if np.isnan(Ov[i, c]): continue
                tw = WS[k, i-1, c] * lv * mm; db = DBS[k, i-1, c] * lv * mm
                cw = u5[c] * Ov[i, c] / es if es > 0 else 0.0
                tr = (abs(tw - cw) > db) or (tw == 0 and cw != 0) or (np.sign(tw) != np.sign(cw))
                if not tr: continue
                nu = tw * es / Ov[i, c] if tw != 0 else 0.0; d = nu - u5[c]; notl = abs(d) * Ov[i, c]
                if notl == 0: continue
                if notl < 5.0: st[5] += 1
                if tw != 0 and notl < minord:
                    st[1] += 1; continue
                if tw == 0 and notl < minord: st[2] += 1
                st[0] += 1; fee = notl * cost; cash5 -= d * Ov[i, c] + fee; u5[c] = nu; st[3] += fee
            # ---- liquidations: U1 cross on Lows
            if s5only == 0:
                gw = uu[0] * Lv_[i, iB] + uu[1] * Lv_[i, iE]; worst = cashu + gw
                if gw > 0 and worst <= maint * gw:
                    cashu = max(worst - gw * cost, 0.0); uu[:] = 0; st[7] += 1
            # S5 isolated per leg
            G = 0.0
            for c in range(m_):
                if u5[c] != 0: G += abs(u5[c]) * Of[i, c]
            G = G / es if es > 0 else 0.0
            if G > 0:
                for c in range(m_):
                    if u5[c] == 0 or np.isnan(Ov[i, c]): continue
                    adv = 1 - Lv_[i, c] / Ov[i, c] if u5[c] > 0 else Hv[i, c] / Ov[i, c] - 1
                    if adv >= 1 / G - maint:
                        notional = abs(u5[c]) * Ov[i, c]; cash5 += u5[c] * Ov[i, c] - notional / G - notional * cost; u5[c] = 0; st[6] += 1
            # ---- mark at close, breaker
            E = cashu + uu[0] * Cf[i, iB] + uu[1] * Cf[i, iE] + cash5
            for c in range(m_):
                if u5[c] != 0: E += u5[c] * Cf[i, c]
            eq[i] = E
            if s5only == 0:
                hwm = max(hwm, E); dd = E / hwm - 1
                if m == 0:
                    flat -= 1
                    if flat <= 0: m = 0.5; hwm = E
                elif dd <= -0.40: m = 0.0; flat = 30
                elif m == 1 and dd <= -0.10: m = 0.5
                elif m == 0.5 and dd > -0.05: m = 1.0
        # x1 mark at close
        E1 = x1c + x1u[0] * Cf[i, iB] + x1u[1] * Cf[i, iE]
        if i >= 1: x1r[nx1] = E1 / x1prev - 1; nx1 += 1
        x1prev = E1
    return eq, st

def run(K, minord, sched, start, s5only=0, wu1=W_U1, lv=LV):
    eq, st = sim(float(K), float(minord), sched.astype(np.int64), int(start), wu1, lv, s5only, Ov, Hv, Lv_, Cv, Of, Cf, F5, W1, WS, DBS, iB, iE, COST, CAP, BAND, MAINT)
    e = pd.Series(eq, index=IDX).dropna(); r = (e / e.shift(1).fillna(K) - 1)
    return r, dict(orders=int(st[0]), skipped=int(st[1]), forced_small_close=int(st[2]), fees=float(st[3]), funding=float(st[4]), orders_lt5=int(st[5]), s5_leg_liq=int(st[6]), u1_liq=int(st[7]))

def wf_sched(K, minord):
    """Rolling WF (train 12m / test 3m): pick S5 config by train Sharpe of the S5 sleeve alone at its real size (0.2*K x Lv)."""
    runs = {}
    for k in range(len(CFGS)):
        r, _ = run((1 - W_U1) * K, minord, np.full(n, k), 1, s5only=1); runs[k] = r
    sched = np.zeros(n, np.int64); picks = []
    for tr0, tr1, te0, te1 in wf_folds():
        sc = {k: metrics(v[tr0:tr1])["sharpe"] for k, v in runs.items()}; b = max(sc, key=sc.get); picks.append(KEYS[b])
        msk = (IDX >= te0 - pd.Timedelta(days=1)) & (IDX <= te1 - pd.Timedelta(days=1)); sched[msk] = b
    first = (IDX >= wf_folds()[0][2] - pd.Timedelta(days=1)).argmax(); sched[:first] = sched[first]; sched[(IDX > OOS_END - pd.Timedelta(days=1))] = sched[(IDX <= OOS_END - pd.Timedelta(days=1))][-1]
    return sched, picks

if __name__ == "__main__":
    start = int(np.searchsorted(IDX, OOS_START)); OOS = slice(OOS_START, OOS_END); out = {}; rets = {}
    # original WF picks (capital-free, from research_1022 runs_S5_longshort.pkl) for reference
    picks0 = [p[2] for p in wf_select(pd.read_pickle(R1022 / "runs_S5_longshort.pkl"))]
    sched0 = np.zeros(n, np.int64)
    for (tr0, tr1, te0, te1), k in zip(wf_folds(), picks0): sched0[(IDX >= te0 - pd.Timedelta(days=1)) & (IDX <= te1 - pd.Timedelta(days=1))] = KEYS.index(k)
    sched0[:start] = sched0[start - 1 if sched0[start-1] else start]; sched0[IDX > OOS_END - pd.Timedelta(days=1)] = KEYS.index(picks0[-1])
    for K in (1000, 3000, 10000):
        for mo in (0.0, MIN_ORDER):
            sch, pk = wf_sched(K, mo)
            r, s = run(K, mo, sch, start); r = r[OOS]
            rf, sf = run(K, mo, sched0, start); rf = rf[OOS]          # same rule, original fixed WF picks
            tag = f"K{K}_{'min5' if mo else 'orig'}"; rets[tag] = r; rets[tag + "_picks0"] = rf
            out[tag] = dict(K=K, min_order=mo, wf=metrics(r), stats=s, picks=pk, with_orig_picks=metrics(rf), stats_orig_picks=sf)
            print(tag, {x: round(out[tag]["wf"][x], 3) for x in ("sharpe", "cagr", "maxdd")}, s, "| orig picks", {x: round(out[tag]["with_orig_picks"][x], 3) for x in ("sharpe", "cagr", "maxdd")}, flush=True)
    R = pd.DataFrame(rets); R.to_csv(HERE / "e_min5_returns.csv", index_label="date")
    for K in (1000, 3000, 10000):
        for t in ("", "_picks0"):
            a, b = R[f"K{K}_min5{t}"], R[f"K{K}_orig{t}"]; d = a - b
            out[f"K{K}_min5"]["te" + t] = dict(te_ann=float(d.std() * math.sqrt(365)), corr=float(a.corr(b)), final_eq_ratio=float((1 + a).prod() / (1 + b).prod()))
    out["picks0"] = picks0
    json.dump(out, open(HERE / "e_min5_results.json", "w"), indent=1, default=float)
