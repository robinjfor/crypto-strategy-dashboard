"""S5 whole-market EWMAC trend CTA (after OctopusTakopi/crypto-trend-following), Binance USDT-M perps incl. delisted.
Eligible: >=120 daily bars and 30d median quote volume >= X. Signal per (f,s) in {(8,32),(16,64),(32,128),(64,256)}:
x=(EWMA_f(logP)-EWMA_s(logP))/(sigma_daily*sqrt(s)); pos=tanh(x); average -> [-1,1] (long-only: clip at 0).
Cluster risk parity: quarterly hierarchical clustering of 90d return correlation (avg linkage, 10 clusters), equal risk per
cluster, equal risk per coin within cluster (w ~ 1/sigma). Portfolio vol target sigma* using trailing 60d realized vol of the
unscaled book; per-coin cap 5%, gross <= 3. Buffer: trade only if |target-held| > b x standard position (pos=1 weight).
Daily decision at close, execution next 00:00 UTC open. 20bps/side, real funding."""
from lib import *
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform
OUT = Path(__file__).resolve().parent
PAIRS = [(8, 32), (16, 64), (32, 128), (64, 256)]
GRID = dict(X=[5e6, 20e6, 50e6], tv=[0.2, 0.3, 0.4], b=[0.1, 0.25, 0.5])
key = lambda c: f"X{int(c['X']/1e6)}M_tv{c['tv']}_b{c['b']}"
CFGS = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
lp = np.log(C); dr = lp.diff(); sd = dr.ewm(span=36, min_periods=20).std()
SIG = sum(np.tanh((lp.ewm(span=f, adjust=False).mean() - lp.ewm(span=s, adjust=False).mean()) / (sd * math.sqrt(s))) for f, s in PAIRS) / len(PAIRS)
hist = C.notna().cumsum(); medv = QV.rolling(30, min_periods=20).median()
ELIG = {X: (hist >= 120) & (medv >= X) & C.notna() for X in GRID["X"]}
qstarts = [d for d in IDX if d.day == 1 and d.month in (1, 4, 7, 10)]
def clusters(X):
    out = pd.DataFrame(np.nan, index=IDX, columns=C.columns)
    for q in qstarts:
        el = ELIG[X].loc[q]; cols = el.index[el.values]
        if len(cols) < 3: continue
        R = dr.loc[:q].iloc[-90:][cols].dropna(axis=1, thresh=60); cols = R.columns
        cm = R.corr().fillna(0).values.copy(); np.fill_diagonal(cm, 1); dist = np.clip(1 - cm, 0, 2)
        lab = fcluster(linkage(squareform(dist, checks=False), "average"), t=min(10, len(cols)), criterion="maxclust")
        out.loc[q, cols] = lab
    return out.ffill()
CL = {X: clusters(X) for X in GRID["X"]}
def base_weights(X, longonly):
    s = SIG.clip(lower=0) if longonly else SIG
    el = ELIG[X]; cl = CL[X].where(el)
    vol = sd * math.sqrt(365)
    unit = (1 / vol).where(el & cl.notna())                    # equal risk per coin
    # equal risk per cluster: divide by cluster size, then by number of clusters
    ncl = cl.nunique(axis=1)
    size = cl.apply(lambda row: row.map(row.value_counts()), axis=1)
    unit = unit / size; unit = unit.div(ncl, axis=0)
    return (s * unit).fillna(0.0), unit.fillna(0.0)
_cache = {}
def weights(c, longonly):
    k = (c["X"], c["tv"], longonly)
    if k in _cache: return _cache[k]
    w0, unit = base_weights(c["X"], longonly)
    r0 = (w0.shift(1) * dr).sum(axis=1)                          # unscaled book close-to-close returns (causal)
    rv = r0.rolling(60, min_periods=30).std() * math.sqrt(365)
    sc = (c["tv"] / rv).replace([np.inf], np.nan).fillna(0.0).clip(upper=50)
    w = w0.mul(sc, axis=0).clip(-0.05, 0.05); g = w.abs().sum(axis=1); w = w.div(np.maximum(g / 3.0, 1.0), axis=0)
    std_pos = unit.mul(sc, axis=0).clip(upper=0.05)
    _cache[k] = (w, std_pos); return _cache[k]
def run(c, longonly, cost=None, W=None, SP=None):
    w, sp = weights(c, longonly) if W is None else (W, SP)
    return pnl(w, deadband_abs=sp * c["b"], cost=cost)
sys.path.insert(0, "/workspace/strategy-unified-3y/spec_2026-11/u1_shorts")
if __name__ == "__main__":
    import u1
    u1_live = u1.sim(200, 0.6, 3.0)[0][OOS_START:OOS_END]; u1_04 = u1.sim(200, 0.4, 3.0)[0][OOS_START:OOS_END]
    pd.DataFrame({"u1_live": u1_live, "u1_tv04": u1_04}).to_csv(OUT / "u1_returns.csv", index_label="date")
    res = {}; eq = {"btc_bh": BTC}; NT = 2 * len(CFGS)
    from collections import Counter
    for longonly in (False, True):
        name = "S5_longonly" if longonly else "S5_longshort"
        runs = {key(c): run(c, longonly)[0] for c in CFGS}
        picks = wf_select(runs)
        Wp, Sp, Bp = [], [], []
        for te0, te1, k in picks:
            c = next(x for x in CFGS if key(x) == k); w, sp = weights(c, longonly); sl = slice(te0 - pd.Timedelta(days=1), te1 - pd.Timedelta(days=1))
            Wp.append(w.loc[sl]); Sp.append((sp * c["b"]).loc[sl])
        W = pd.concat(Wp); W = W[~W.index.duplicated()]; DB = pd.concat(Sp); DB = DB[~DB.index.duplicated()]
        r, to = pnl(W, deadband_abs=DB); r = r[OOS_START:OOS_END]; r2, _ = pnl(W, deadband_abs=DB, cost=2 * COST); r2 = r2[OOS_START:OOS_END]
        modal = Counter(p[2] for p in picks).most_common(1)[0][0]; mc = next(x for x in CFGS if key(x) == modal)
        pre = r[:pd.Timestamp("2023-12-31", tz="UTC")]; post = r[pd.Timestamp("2024-01-01", tz="UTC"):]
        res[name] = summarize(name, r, runs, mc, GRID, key, NT, dict(picks=[p[2] for p in picks], oos_double_cost=metrics(r2),
            corr_u1_live=float(r.corr(u1_live)), corr_u1_tv04=float(r.corr(u1_04)), pre2024=metrics(pre), post2024=metrics(post),
            avg_gross=float(W.abs().sum(axis=1)[OOS_START:].mean()), avg_turnover=float(to[OOS_START:].mean())))
        eq[name] = r; pd.to_pickle(runs, OUT / f"runs_{name}.pkl"); r.to_frame("r").to_csv(OUT / f"returns_{name}.csv", index_label="date")
    res["btc_buy_hold"] = metrics(BTC); res["u1_live"] = metrics(u1_live); res["u1_tv04"] = metrics(u1_04); res["trials_total"] = NT
    json.dump(res, open(OUT / "results_s5.json", "w"), indent=1, default=float)
    pd.DataFrame({k: (1 + x).cumprod() for k, x in eq.items()}).assign(cash=1.0).to_csv(OUT / "equity_s5.csv", index_label="date")
    for k in ("S5_longshort", "S5_longonly"):
        d = res[k]; print(k, {x: round(d["oos"][x], 3) for x in ("sharpe", "cagr", "maxdd")}, "H1/H2", round(d["halves"]["H1"]["sharpe"], 2), round(d["halves"]["H2"]["sharpe"], 2),
              "pre/post2024", round(d["pre2024"]["sharpe"], 2), round(d["post2024"]["sharpe"], 2), "rob", round(d["neighbor_robustness"], 2), "dsr", round(d["deflated_sharpe"]["dsr"], 3),
              "2x", round(d["oos_double_cost"]["sharpe"], 2), "corrU1", round(d["corr_u1_live"], 2), round(d["corr_u1_tv04"], 2), "gross", round(d["avg_gross"], 2), d["ref_config"], d["passed"])
    for k in ("S5_longshort", "S5_longonly"):
        runs = pd.read_pickle(OUT / f"runs_{k}.pkl"); print(k, " | ".join(f"{n} {metrics(v[OOS_START:OOS_END])['sharpe']:.2f}/{metrics(v[OOS_START:OOS_END])['maxdd']:.2f}" for n, v in runs.items()))
