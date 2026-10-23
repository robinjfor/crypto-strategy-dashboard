"""Ledger E-minnot (research only; NO orders; catalog/allocation/live untouched). Derived from ledger_e_min5.py.
Same E portfolio/engine (80% U1 tv0.40 x1.75 + 20% S5 L/S x1.75, band 25%, S5 buffer, breaker -10%/-40%), close decision ->
next 00:00 UTC open, 20bps/side, real funding (local data.binance.vision cache; no Binance API).
NEW RULE: per-symbol min notional MN[sym] and stepSize from ../../min_notional.json (env MIN_NOTIONAL_JSON overrides;
defaults BTC 50 / ETH 20 / others 5 USDT, step 0 = not applied):
  (0) target qty is truncated toward 0 to a multiple of stepSize (if step>0);
  (1) a non-closing adjustment order with |notional| < MN[sym] is not sent (leg keeps its position);
  (2) target 0 -> full reduce-only close regardless of size;
  (3) if an order would leave a non-zero residual with |notional| < MN[sym], the leg is closed in full instead;
  (4) sign flip = full close + new open; the open part is only sent if its notional >= MN[sym].
mode: 0 = original (no min), 1 = uniform 5U (ledger_e_min5 logic), 2 = per-symbol (this file).
Usage: python3 ledger_e_minnot.py  -> e_minnot_results.json, e_minnot_returns.csv (MINNOTIONAL.md written by hand from these)"""
import os, sys, json, math, numpy as np, pandas as pd
from pathlib import Path
from numba import njit
HERE = Path(__file__).resolve().parent; R1022 = HERE.parent
sys.path.insert(0, str(R1022)); sys.path.insert(0, str(R1022.parent / "u1_shorts"))
from lib import *                       # panel, fund(), metrics, wf_folds, OOS_START/END, COST (=0.002)
import s5_ewmac as S5
sys.path.insert(0, str(R1022.parent / "common")); import minnot
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


MN, STEP, MN_PATH = minnot.load(cols)

@njit(cache=True)
def leg(u, nu, p, mn, step, closing, st, chk):
    """Return new qty after applying min-notional rules. st: 0 sent,1 skipped,2 forced/residual closes,3 fees,5 would-be-blocked(chk)."""
    if step > 0: nu = math.trunc(nu / step) * step
    if closing or nu == 0.0:
        if u != 0.0:
            notl = abs(u) * p; st[0] += 1; st[3] += notl * COSTC
            if notl < mn: st[2] += 1
        return 0.0, abs(u) * p
    flip = u != 0.0 and (nu > 0) != (u > 0)
    if flip:
        cl = abs(u) * p; st[0] += 1
        if abs(nu) * p < mn:
            st[1] += 1; st[3] += cl * COSTC; return 0.0, cl
        st[0] += 1
        if abs(nu) * p < chk: st[5] += 1
        st[3] += (cl + abs(nu) * p) * COSTC; return nu, cl + abs(nu) * p
    d = nu - u; notl = abs(d) * p
    if notl == 0.0: return u, 0.0
    if notl < mn:
        st[1] += 1; return u, 0.0
    if abs(nu) * p < mn:           # residual below min -> full close
        notl = abs(u) * p; st[0] += 1; st[2] += 1; st[3] += notl * COSTC; return 0.0, notl
    st[0] += 1
    if notl < chk: st[5] += 1
    st[3] += notl * COSTC; return nu, notl

COSTC = COST

@njit(cache=True)
def sim(K, MNv, STv, CHK, sched, start, wu1, lv, s5only, Ov, Hv, Lv_, Cv, Of, Cf, F5, W1, WS, DBS, iB, iE, cost, cap, band, maint):
    n, m_ = Ov.shape; u5 = np.zeros(m_); uu = np.zeros(2); ui = np.array([iB, iE])
    cash5 = K * (1 - wu1) if s5only == 0 else K; cashu = K * wu1 if s5only == 0 else 0.0
    eq = np.full(n, np.nan); m = 1.0; hwm = K; flat = 0
    x1c = 1.0; x1u = np.zeros(2); x1prev = 1.0; x1r = np.zeros(n); nx1 = 0
    st = np.zeros(8)  # 0 orders sent,1 skipped<min,2 forced small/residual closes,3 fees,4 funding,5 sent opens/adjusts < CHK (would be rejected live),6 leg liq,7 u1 liq
    for i in range(1, n):
        o2 = np.array([Of[i, iB], Of[i, iE]])
        x1c -= (x1u * o2 * np.array([F5[i-1, iB], F5[i-1, iE]])).sum()
        V = x1c + (x1u * o2).sum()
        for j in range(2):
            wt = W1[i-1, j]; wc = x1u[j] * o2[j] / V if V > 0 else 0.0
            tr = (x1u[j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > band * abs(wt))
            if tr and V > 0:
                nu = wt * V / o2[j]; d = nu - x1u[j]; x1c -= d * o2[j] + abs(d) * o2[j] * cost; x1u[j] = nu
        if i >= start:
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
                        nu = wt * eu / o2[j]; c0 = ui[j]
                        q, _ = leg(uu[j], nu, o2[j], MNv[c0], STv[c0], wt == 0, st, CHK[c0])
                        d = q - uu[j]; cashu -= d * o2[j] + abs(d) * o2[j] * cost; uu[j] = q
            k = sched[i-1]; mm = m if s5only == 0 else 1.0
            for c in range(m_):
                if np.isnan(Ov[i, c]): continue
                tw = WS[k, i-1, c] * lv * mm; db = DBS[k, i-1, c] * lv * mm
                cw = u5[c] * Ov[i, c] / es if es > 0 else 0.0
                tr = (abs(tw - cw) > db) or (tw == 0 and cw != 0) or (np.sign(tw) != np.sign(cw))
                if not tr: continue
                nu = tw * es / Ov[i, c] if tw != 0 else 0.0
                if nu == u5[c]: continue
                q, _ = leg(u5[c], nu, Ov[i, c], MNv[c], STv[c], tw == 0, st, CHK[c])
                d = q - u5[c]; cash5 -= d * Ov[i, c] + abs(d) * Ov[i, c] * cost; u5[c] = q
            if s5only == 0:
                gw = uu[0] * Lv_[i, iB] + uu[1] * Lv_[i, iE]; worst = cashu + gw
                if gw > 0 and worst <= maint * gw:
                    cashu = max(worst - gw * cost, 0.0); uu[:] = 0; st[7] += 1
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
        E1 = x1c + x1u[0] * Cf[i, iB] + x1u[1] * Cf[i, iE]
        x1r[nx1] = E1 / x1prev - 1; nx1 += 1; x1prev = E1
    return eq, st

def rule(mode):
    if mode == 0: return np.zeros(len(cols)), np.zeros(len(cols))
    if mode == 1: return np.full(len(cols), 5.0), np.zeros(len(cols))
    return MN.copy(), STEP.copy()

def run(K, mode, sched, start, s5only=0, wu1=W_U1, lv=LV):
    mn, sp = rule(mode)
    eq, st = sim(float(K), mn, sp, MN, sched.astype(np.int64), int(start), wu1, lv, s5only, Ov, Hv, Lv_, Cv, Of, Cf, F5, W1, WS, DBS, iB, iE, COST, CAP, BAND, MAINT)
    e = pd.Series(eq, index=IDX).dropna(); r = (e / e.shift(1).fillna(K) - 1)
    return r, dict(orders=int(st[0]), skipped=int(st[1]), forced_small_close=int(st[2]), fees=float(st[3]), funding=float(st[4]), sent_below_real_min=int(st[5]), s5_leg_liq=int(st[6]), u1_liq=int(st[7]))

def wf_sched(K, mode):
    runs = {k: run((1 - W_U1) * K, mode, np.full(n, k), 1, s5only=1)[0] for k in range(len(CFGS))}
    sched = np.zeros(n, np.int64); picks = []
    for tr0, tr1, te0, te1 in wf_folds():
        sc = {k: metrics(v[tr0:tr1])["sharpe"] for k, v in runs.items()}; b = max(sc, key=sc.get); picks.append(KEYS[b])
        msk = (IDX >= te0 - pd.Timedelta(days=1)) & (IDX <= te1 - pd.Timedelta(days=1)); sched[msk] = b
    first = (IDX >= wf_folds()[0][2] - pd.Timedelta(days=1)).argmax(); sched[:first] = sched[first]; sched[(IDX > OOS_END - pd.Timedelta(days=1))] = sched[(IDX <= OOS_END - pd.Timedelta(days=1))][-1]
    return sched, picks

if __name__ == "__main__":
    K = float(os.environ.get("E_CAPITAL", 5000)); start = int(np.searchsorted(IDX, OOS_START)); OOS = slice(OOS_START, OOS_END)
    out = dict(capital=K, min_notional_file=MN_PATH, min_notional_nondefault={c: float(MN[i]) for i, c in enumerate(cols) if MN[i] != 5.0}); rets = {}
    for mode, tag in ((0, "orig"), (1, "min5"), (2, "minnot")):
        sch, pk = wf_sched(K, mode); r, s = run(K, mode, sch, start); r = r[OOS]; rets[tag] = r
        out[tag] = dict(wf=metrics(r), stats=s, picks=pk)
        print(tag, {x: round(out[tag]["wf"][x], 4) for x in ("sharpe", "cagr", "maxdd")}, s, flush=True)
    R = pd.DataFrame(rets); R.to_csv(HERE / "e_minnot_returns.csv", index_label="date")
    for t in ("orig", "min5"):
        d = R["minnot"] - R[t]; out["minnot"]["vs_" + t] = dict(te_ann=float(d.std() * math.sqrt(365)), corr=float(R["minnot"].corr(R[t])), final_eq_ratio=float((1 + R["minnot"]).prod() / (1 + R[t]).prod()))
    json.dump(out, open(HERE / "e_minnot_results.json", "w"), indent=1, default=float)
