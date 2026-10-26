"""Demo slot demo_e5_min5_x175_t05top20: E5 (demo_e5_min5_x175) with the 2026-10-26 min-notional drift fix,
exactly the backtested variant spec_2026-11/research_minnot_1026/e5_minnot_variants.py cfg (dbm=1, thr=0.005, rup=0, topn=20):
  * same U1 / S5 signals, same targets file (trader/shadow_ab/e5_targets.json), same U1 sleeve rules (demo_e5.plan_u1);
  * S5 trades only its top-20 |target weight| coins each day (ties at the cut kept), gross preserved by
    sc = sum|w| / sum|w kept|, which scales both the target and the deadband;
  * a non-closing S5 adjust is not sent if its notional < max(symbol MN, 0.5% of S5 sleeve equity)
    (below MN -> 'skip_min' = min-notional skip; between MN and the threshold -> 'skip_thr', suppressed by the rule).
Closes, flips and residual closes use the plain symbol MN (identical to the backtest `leg`).
Own virtual ledger: state['demo_e5v_exec'] (took over the old E5 slot's book + cash on 2026-10-26, see migrate_e5v.py).
Gates: same four as demo_e5 (enabled, approved, parity_pass, cloud_ordering_ok) + DEMO_ACCEPTED."""
from __future__ import annotations

import math

import numpy as np

import demo_e5
from demo_e5 import CAPITAL, LV, MIN_ORDER, U1_SYMS, W_U1, plan_lt, plan_u1  # noqa: F401  (re-exported for the executor)

SLOT = "demo_e5_min5_x175_t05top20"
STATE_KEY = "demo_e5v_exec"
OLD_SLOT = demo_e5.SLOT
DBM, THR, RUP, TOPN = 1.0, 0.005, 0.0, 20
SPEC = "research_minnot_1026/e5_minnot_variants.py cfg(dbm=1,thr=0.005,rup=0,topn=20)"


def topn_weights(ws: np.ndarray, topn: int = TOPN) -> tuple[np.ndarray, float]:
    """Backtest rule: keep |w| >= the topn-th largest |w| (only when topn < n and that value > 0); scale = gross / kept gross."""
    w = np.array(ws, dtype=float).copy(); aw = np.abs(w); sc = 1.0
    if topn > 0 and len(aw):
        srt = np.sort(aw)[::-1]
        if topn < len(aw) and srt[topn - 1] > 0:
            cut = srt[topn - 1]; gt = aw[aw >= cut].sum()
            sc = aw.sum() / gt if gt > 0 else 1.0
            w[aw < cut] = 0.0
    return w, sc


def plan_leg(u: float, nu: float, p: float, mn: float, mn_eff: float, step: float, closing: bool):
    """demo_e5.plan_leg + slot threshold mn_eff (rup=0). Kinds: order|close|residual_close|skip_min|skip_thr."""
    if step > 0:
        nu = math.trunc(nu / step) * step
    if closing or nu == 0.0:
        return 0.0, ([(-u, abs(u) * p, "close")] if u != 0.0 else [])
    if u != 0.0 and (nu > 0) != (u > 0):
        od = [(-u, abs(u) * p, "close")]
        if abs(nu) * p < mn:
            return 0.0, od + [(nu, abs(nu) * p, "skip_min")]
        return nu, od + [(nu, abs(nu) * p, "order")]
    d = nu - u; notl = abs(d) * p
    if notl == 0.0:
        return u, []
    if notl < mn_eff:
        return u, [(d, notl, "skip_min" if notl < mn else "skip_thr")]
    if abs(nu) * p < mn:
        return 0.0, [(-u, abs(u) * p, "residual_close")]
    return nu, [(d, notl, "order")]


def plan_s5(u5, ov, ws, dbs, es: float, lv: float, mm: float, mn=None, step=None, thr: float = THR, topn: int = TOPN, dbm: float = DBM):
    u5 = np.array(u5, dtype=float).copy(); orders = []
    mn = np.full(len(u5), MIN_ORDER) if mn is None else np.asarray(mn, dtype=float)
    step = np.zeros(len(u5)) if step is None else np.asarray(step, dtype=float)
    w, sc = topn_weights(ws, topn)
    for c in range(len(u5)):
        if np.isnan(ov[c]): continue
        tw = w[c] * sc * lv * mm; db = dbs[c] * sc * dbm * lv * mm
        cw = u5[c] * ov[c] / es if es > 0 else 0.0
        tr = (abs(tw - cw) > db) or (tw == 0 and cw != 0) or (np.sign(tw) != np.sign(cw))
        if not tr: continue
        nu = tw * es / ov[c] if tw != 0 else 0.0
        if nu == u5[c]: continue
        q, od = plan_leg(u5[c], nu, ov[c], float(mn[c]), max(float(mn[c]), thr * es), float(step[c]), tw == 0)
        orders += [(c, d, n, k) for d, n, k in od]; u5[c] = q
    return u5, orders


def plan_live(targets: dict, book: dict, prices: dict, equity: float, m: float = 1.0, lt: float = 1.0,
              mins: dict | None = None, steps: dict | None = None) -> list[dict]:
    mins = {**demo_e5.EXCHANGE_MIN_DEFAULT, **(mins or {})}; steps = steps or {}
    _mn = lambda ss: np.array([float(mins.get(x, MIN_ORDER)) for x in ss]); _st = lambda ss: np.array([float(steps.get(x, 0.0)) for x in ss])
    eu, es = W_U1 * equity, (1 - W_U1) * equity
    uu = np.array([float(book.get(s, 0.0)) for s in U1_SYMS]); o2 = np.array([float(prices.get(s) or np.nan) for s in U1_SYMS])
    _, od1 = plan_u1(uu, targets["w1"], lt, m, o2, eu, _mn(U1_SYMS), _st(U1_SYMS))
    out = [{"symbol": U1_SYMS[j], "delta_qty": d, "notional": n, "kind": k, "sleeve": "u1"} for j, d, n, k in od1]
    tws = targets.get("ws") or {}
    # top-N is ranked over the WHOLE target vector (as the backtest), not only the priced symbols
    allw = {s: float(v) for s, v in tws.items()}
    syms = sorted(set(allw) | {s for s in book if s not in U1_SYMS})
    w_all = np.array([allw.get(s, 0.0) for s in syms])
    u5 = np.array([float(book.get(s, 0.0)) for s in syms]); ov = np.array([float(prices.get(s) or np.nan) for s in syms])
    dbs = np.array([float((targets.get("dbs") or {}).get(s, 0.0)) for s in syms])
    _, od5 = plan_s5(u5, ov, w_all, dbs, es, LV, m, _mn(syms), _st(syms))
    out += [{"symbol": syms[c], "delta_qty": d, "notional": n, "kind": k, "sleeve": "s5"} for c, d, n, k in od5]
    return out


def active(state: dict) -> tuple[bool, str]:
    from demo_candidates import accepted
    g = (state.get("demo_candidates") or {}).get(SLOT) or {}
    for k, why in (("enabled", "未啟用"), ("approved", "未批准"), ("parity_pass", "對帳未通過"), ("cloud_ordering_ok", "下單測試未通過")):
        if not g.get(k):
            return False, why
    if not accepted(SLOT):
        return False, "未驗收"
    return True, ""
