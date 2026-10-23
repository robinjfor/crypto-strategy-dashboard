import sys, math, json, itertools, numpy as np, pandas as pd
sys.path.insert(0, "/workspace/strategy-unified-3y/spec_2026-11/common")
from common import *
P = panel(); C, O, H, Lo, QV = P["close"], P["open"], P["high"], P["low"], P["quote_volume"]
IDX = C.index
_F = None
def fund():
    global _F
    if _F is None: _F = funding_daily(list(C.columns)).reindex(IDX).reindex(columns=C.columns).fillna(0.0)
    return _F
RET_OO = (O.shift(-1).fillna(C) / O - 1).shift(-1)   # weight decided at close t earns open t+1 -> open t+2
def pnl(W, deadband_rel=0.0, deadband_abs=0.0, cost=None, return_held=False):
    """W: target weights (decided at close t, signed). Trades at open t+1. Rel deadband: skip if |dw| < rel*|w_tgt|.
    Funding: long pays, short receives (r = -w*F). Returns daily returns at execution day t+1, plus turnover."""
    cost = COST if cost is None else cost
    cols = W.columns
    if isinstance(deadband_abs, pd.DataFrame): deadband_abs = deadband_abs.reindex(index=W.index, columns=cols).fillna(0).values
    Wv = W.fillna(0).values; R = RET_OO[cols].reindex(W.index).fillna(0).values
    F = fund()[cols].shift(-1).reindex(W.index).fillna(0).values
    held = np.zeros(len(cols)); out = np.zeros(len(W)); to = np.zeros(len(W)); HM = np.zeros_like(Wv) if return_held else None
    for i in range(len(W)):
        t = Wv[i]; d = t - held
        dba = deadband_abs[i] if np.ndim(deadband_abs) == 2 else deadband_abs
        tr = (np.abs(d) > np.maximum(deadband_rel * np.abs(t), dba)) | ((t == 0) & (held != 0)) | (np.sign(t) != np.sign(held))
        new = np.where(tr, t, held); c = np.abs(new - held).sum() * cost
        if return_held: HM[i] = new
        g = (new * R[i]).sum(); out[i] = g - (new * F[i]).sum() - c; to[i] = np.abs(new - held).sum()
        held = new * (1 + R[i]) / (1 + g) if 1 + g > 0 else new * 0
    ix = W.index + pd.Timedelta(days=1)
    if return_held: return pd.Series(out, index=ix), pd.Series(to, index=ix), pd.DataFrame(HM, index=W.index, columns=cols)
    return pd.Series(out, index=ix), pd.Series(to, index=ix)
passes = lambda m: m["sharpe"] >= 0.8 and m["maxdd"] <= 0.35
def neighbors(c, grid, key):
    out = []
    for dim, vals in grid.items():
        i = vals.index(c[dim])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals): n = dict(c); n[dim] = vals[j]; out.append(key(n))
    return out
def wf_select(runs, score=lambda r: metrics(r)["sharpe"]):
    picks = []
    for tr0, tr1, te0, te1 in wf_folds():
        sc = {k: score(v[tr0:tr1]) for k, v in runs.items()}; b = max(sc, key=sc.get); picks.append((te0, te1, b))
    return picks
def stitch_weights(wfun, picks):
    parts = [wfun(b).loc[te0 - pd.Timedelta(days=1): te1 - pd.Timedelta(days=1)] for te0, te1, b in picks]
    W = pd.concat(parts); return W[~W.index.duplicated()]
def half(r):
    mid = pd.Timestamp("2025-04-01", tz="UTC")
    return dict(H1=metrics(r[:mid - pd.Timedelta(days=1)]), H2=metrics(r[mid:]))
BTC = C["BTCUSDT"].pct_change()[OOS_START:OOS_END]
def summarize(name, r_oos, runs_fixed, modal_cfg, grid, key, ntrials_all, extra=None):
    fixed = {k: metrics(v[OOS_START:OOS_END]) for k, v in runs_fixed.items()}
    nb = neighbors(modal_cfg, grid, key); rob = sum(passes(fixed[n]) for n in nb) / max(len(nb), 1)
    m = metrics(r_oos); d = dict(name=name, oos=m, halves=half(r_oos), corr_btc=float(r_oos.corr(BTC.reindex(r_oos.index))), ref_config=key(modal_cfg),
        neighbor_robustness=rob, neighbors={n: [round(fixed[n]["sharpe"], 2), round(fixed[n]["maxdd"], 3)] for n in nb},
        trials=ntrials_all, deflated_sharpe=deflated_sharpe(r_oos, [v["sharpe"] for v in fixed.values()], ntrials_all), passed=bool(passes(m) and rob >= 0.5))
    if extra: d.update(extra)
    return d
