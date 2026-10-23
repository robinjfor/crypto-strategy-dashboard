"""funding_arb: long spot + short USDT-M perp (equal notional), open when 7d annualized funding > 15%.
Daily UTC decision at close t, execution at open t+1. Costs 20bps/side per leg. Real Binance funding."""
import sys, json, itertools, math, numpy as np, pandas as pd
sys.path.insert(0, "/workspace/strategy-unified-3y/spec_2026-11/common")
from common import *
OUT = Path(__file__).resolve().parent
VARIANT = sys.argv[1] if len(sys.argv) > 1 else "base"
N_UNI, LIQ_WIN, MIN_HIST, ENTRY, MARGIN = 20, 30, 60, 0.15, 1/3   # perp margin = 1/3 of notional (<=3x perp leg, 1x net)
GRID = dict(exit_thr=[0.0, 0.05, 0.10], K=[3, 5, 10])
P = panel(); O, C, QV = P["open"], P["close"], P["quote_volume"]
hist = C.notna().cumsum()
SP = spot_close([s for s in C.columns])
SPO = pd.DataFrame({s: d["open"] for s, d in SP.items()}).reindex(C.index)
liq = QV.rolling(LIQ_WIN, min_periods=LIQ_WIN).mean().where(hist >= MIN_HIST).where(SPO.reindex(columns=C.columns).notna())
UNI = liq.rank(axis=1, ascending=False, method="first") <= N_UNI
syms = list(UNI.columns[UNI.any()])
FD = funding_daily(syms).reindex(C.index).reindex(columns=syms)
F7 = FD.rolling(7, min_periods=7).sum() * 365 / 7          # known at close t
F1 = FD                                                     # last 24h funding (flip check)
def oo(px): return (px.shift(-1) / px - 1).shift(-1)       # open t+1 -> open t+2, indexed at t
R_SPOT, R_PERP = oo(SPO[syms]), oo(O[syms])
FUND_NEXT = FD.shift(-1)                                    # funding settled during holding day t+1

def run(exit_thr, K):
    n = len(C); exit_thr = np.broadcast_to(np.asarray(exit_thr, float), n); K = np.broadcast_to(np.asarray(K, int), n)
    u = UNI[syms].values; f7 = F7.values; f1 = F1.values
    rs, rp, fn = R_SPOT.values, R_PERP.values, FUND_NEXT.values
    open_ = np.zeros(len(syms), bool); out = np.zeros(len(C)); npos = np.zeros(len(C))
    for i in range(len(C)):
        notional = 1.0 / K[i] / (1 + MARGIN)
        prev = open_.copy()
        keep = open_ & ~np.isnan(f7[i]) & (f7[i] >= exit_thr[i]) & ~(f1[i] < 0) & ~np.isnan(rs[i]) & ~np.isnan(rp[i])
        cand = ~keep & u[i] & (np.nan_to_num(f7[i], nan=-1) > ENTRY) & ~np.isnan(rs[i]) & ~np.isnan(rp[i]) & ~(f1[i] < 0)
        free = K[i] - keep.sum(); new = keep.copy()
        if free > 0 and cand.any():
            idx = np.where(cand)[0]; idx = idx[np.argsort(-f7[i][idx])][:free]; new[idx] = True
        # if K shrinks below open count, drop lowest-f7 positions
        if new.sum() > K[i]:
            idx = np.where(new)[0]; drop = idx[np.argsort(np.nan_to_num(f7[i][idx]))][: new.sum() - K[i]]; new[drop] = False
        trades = (new != prev).sum()  # resize of held slots when K changes at a fold boundary is not charged (documented)
        cost = trades * notional * 2 * COST                     # two legs per open/close
        h = new
        pnl = (notional * (np.nan_to_num(rs[i][h]) - np.nan_to_num(rp[i][h]) + np.nan_to_num(fn[i][h]))).sum()
        out[i] = pnl - cost; npos[i] = h.sum(); open_ = new
    idx = C.index + pd.Timedelta(days=1)
    return pd.Series(out, index=idx), pd.Series(npos, index=idx)

if __name__ == "__main__":
    runs = {}
    for e, k in itertools.product(*GRID.values()):
        key = f"exit{e}_K{k}"; r, n = run(e, k); runs[key] = (dict(exit_thr=e, K=k), r, n)
    folds = wf_folds(); picks = []
    ex = np.full(len(C), np.nan); kk = np.full(len(C), 0)
    for tr0, tr1, te0, te1 in folds:
        sc = {k: metrics(v[1][tr0:tr1])["sharpe"] for k, v in runs.items()}; best = max(sc, key=sc.get)
        picks.append(dict(train=[str(tr0.date()), str(tr1.date())], test=[str(te0.date()), str(te1.date())], pick=best, train_sharpe=sc[best]))
        msk = (C.index >= te0 - pd.Timedelta(days=1)) & (C.index <= te1 - pd.Timedelta(days=1))  # decisions made day before exec
        ex[msk] = runs[best][0]["exit_thr"]; kk[msk] = runs[best][0]["K"]
    first = np.argmax(~np.isnan(ex)); ex[:first] = ex[first]; kk[:first] = kk[first]
    last = len(ex) - 1 - np.argmax(~np.isnan(ex[::-1])); ex[last:] = ex[last]; kk[last:] = kk[last]
    r_all, _ = run(ex, kk)   # one continuous state machine with the WF parameter schedule (positions carry across folds)
    r_oos = r_all[OOS_START:OOS_END]
    btc = (C["BTCUSDT"] / C["BTCUSDT"].shift(1) - 1)[OOS_START:OOS_END]
    m = metrics(r_oos); fixed = {k: metrics(v[1][OOS_START:OOS_END]) for k, v in runs.items()}
    passes = lambda x: x["sharpe"] >= 0.8 and x["maxdd"] <= 0.35
    from collections import Counter
    modal = Counter(p["pick"] for p in picks).most_common(1)[0][0]; mc = runs[modal][0]; neigh = []
    for dim, vals in GRID.items():
        i = vals.index(mc[dim])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                n = dict(mc); n[dim] = vals[j]; neigh.append(f"exit{n['exit_thr']}_K{n['K']}")
    rob = sum(passes(fixed[n]) for n in neigh) / len(neigh)
    tl = OUT / "trials_log.json"; prior = json.load(open(tl)) if tl.exists() else {}
    prior[VARIANT] = {k: v["sharpe"] for k, v in fixed.items()}; json.dump(prior, open(tl, "w"), indent=1)
    all_sr = [s for vv in prior.values() for s in vv.values()]
    dsr = deflated_sharpe(r_oos, all_sr, len(all_sr))
    npos = runs[modal][2][OOS_START:OOS_END]
    res = dict(strategy="funding_arb", variant=VARIANT, oos_period=[str(OOS_START.date()), str(OOS_END.date())],
               wf="rolling 12m train / 3m test, select max train Sharpe", oos=m, btc_buy_hold=metrics(btc),
               cash=dict(sharpe=0.0, cagr=0.0, maxdd=0.0), pct_days_any_position_modal=float((npos > 0).mean()),
               avg_positions_modal=float(npos.mean()), folds=picks, modal_config=modal, neighbors=neigh,
               neighbor_pass={n: passes(fixed[n]) for n in neigh}, neighbor_robustness=rob, fixed_param_oos=fixed,
               trials_total=len(all_sr), deflated_sharpe=dsr,
               assumptions=dict(fees="20bps/side/leg (engine.COST)", funding="real Binance USDT-M settlements",
                                basis="real: spot open vs perp open daily marks (Binance spot & UM 1d klines)",
                                borrow="none (spot bought outright, no margin borrow); perp margin 1/3 notional, idle USDT yield 0",
                                capital_per_slot="1/K equity; notional = slot/(1+1/3)"),
               passed=bool(passes(m) and rob >= 0.5))
    json.dump(res, open(OUT / f"results_{VARIANT}.json", "w"), indent=1, default=float)
    pd.DataFrame({"strategy": (1 + r_oos).cumprod(), "btc_bh": (1 + btc.reindex(r_oos.index).fillna(0)).cumprod(), "cash": 1.0}).to_csv(OUT / f"equity_{VARIANT}.csv", index_label="date")
    print(json.dumps({k: res[k] for k in ["oos", "neighbor_robustness", "trials_total", "deflated_sharpe", "passed", "modal_config", "pct_days_any_position_modal", "avg_positions_modal"]}, indent=1, default=float))
    for k, v in fixed.items(): print(k, round(v["sharpe"], 2), round(v["cagr"], 3), round(v["maxdd"], 3))
