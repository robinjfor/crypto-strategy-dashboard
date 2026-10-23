"""R2S min-notional backtest ledger (box-side; local data cache). Runs research_regime_v2/r2s_minnot.py verbatim up to its WF loop,
re-selects WF picks in mode 2 at R2S_CAPITAL (default 3000) exactly as the study, then replays the stitched weights through a
plain-Python copy of its `ledger` (source taken verbatim, @njit stripped, only recording lines inserted) and cross-checks it
against the jitted ledger. Dumps every per-coin decision (inputs + resulting qty). No orders, no keys."""
import os, sys, pickle, inspect, textwrap
os.environ.setdefault("R2S_CAPITAL", "3000")
OUT = os.environ.get("R2S_OUT", "/workspace/r2s")
from pathlib import Path
HERE0 = Path("/workspace/strategy-unified-3y/spec_2026-11/research_regime_v2"); __file__ = str(HERE0 / "r2s_minnot.py")
_src = open(HERE0 / "r2s_minnot.py").read()
_head = _src.split("cfgs = [dict(zip(GS")[0]
exec(compile(_head.replace("@njit(cache=True)\ndef ledger", "@njit\ndef ledger"), str(HERE0 / "r2s_minnot.py"), "exec"))  # cache=True needs a file locator
cfgs = [dict(zip(GS, v)) for v in itertools.product(*GS.values())]; key = lambda c: "_".join(f"{k}{c[k]}" for k in GS)
WS_ = {key(c): r2s_W(c) for c in cfgs}
runs = {k: run(W, 2, IDX[0])[0] for k, W in WS_.items()}
picks = wf_select({k: v[:OOS_END] for k, v in runs.items()})
W = pd.concat([WS_[b_].loc[te0 - pd.Timedelta(days=1): te1 - pd.Timedelta(days=1)] for te0, te1, b_ in picks]); W = W[~W.index.duplicated()]
r_j, s_j = run(W, 2, OOS_START - pd.Timedelta(days=1))
# instrumented plain-python copy of ledger()
ls = textwrap.dedent(_src.split("@njit(cache=True)\ndef ledger")[1].split("\ndef run(")[0]); ls = "def ledger" + ls
A = "            d = new - u\n"; assert ls.count(A) == 1
ls = ls.replace(A, "            _R.append((i, c, t, u, p, E, new))\n" + A)
B = "            if not tr: continue\n"; assert ls.count(B) == 1
ls = ls.replace(B, "            if not tr:\n                _R.append((i, c, t, q[c], p, E, q[c])); continue\n")
C1 = "                    st[1] += 1; continue\n"; assert ls.count(C1) == 1
ls = ls.replace(C1, "                    st[1] += 1; _R.append((i, c, t, u, p, E, u)); continue\n")
g = dict(globals()); REC = []; g["_R"] = REC; exec(compile(ls, "r2s_minnot_ledger_capture", "exec"), g)
Wv = W.reindex(index=IDX, columns=cols).fillna(0.0).values; s0 = int(np.searchsorted(IDX, OOS_START - pd.Timedelta(days=1)))
eq, st = g["ledger"](Wv, K, MN, STEP, MN, BAND, COST, Ov, Of, Cf, Fv, s0)
eq_j, _ = ledger(Wv, K, MN, STEP, MN, BAND, COST, Ov, Of, Cf, Fv, s0)
ok = np.isfinite(eq_j); rel = float(np.nanmax(np.abs(eq[ok] / eq_j[ok] - 1)))
r = pd.Series(eq, index=IDX).dropna(); r = (r.shift(-1) / r - 1).dropna(); r.index = r.index + pd.Timedelta(days=1)
m = metrics(r[OOS_START:OOS_END])
pickle.dump(dict(rec=REC, cols=cols, idx=[str(d.date()) for d in IDX], W=Wv, s0=s0, mn=MN, step=STEP, mn_path=MN_PATH, K=K,
                 picks=[(str(a.date()), str(e.date()), b) for a, e, b in picks], stats=dict(orders=int(st[0]), skipped=int(st[1]), forced_small_close=int(st[2])),
                 stats_jit=s_j, capture_vs_jit_max_rel=rel, sharpe=m["sharpe"], cagr=m["cagr"], maxdd=m["maxdd"]), open(OUT + "/r2s_mn_ledger.pkl", "wb"))
pd.to_pickle(dict(C=C, H=H, L=Lo, QV=QV), OUT + "/r2s_panel.pkl")
print("orders", int(st[0]), "jit", s_j, "rel", rel, "sharpe", m["sharpe"], "picks", [p[2] for p in picks])
