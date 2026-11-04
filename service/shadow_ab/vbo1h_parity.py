"""Box-side trade-by-trade parity: live VBO-1h code (demo_vbo1h.signal_panel on the exact window the cloud fetches)
vs the backtest (research_lowcorr_r2_1104/r2.py + diag_vbo.W reference) on h1.pkl. Writes shadow_ab/parity_VBO1H.json
which reconcile.run_demo_gates reads (parity gate). Usage: python vbo1h_parity.py <research_dir>"""
import json, os, sys, random, time
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE))
import demo_vbo1h as V
RD = sys.argv[1] if len(sys.argv) > 1 else "/workspace/strategy-unified-3y/spec_2026-11/research_lowcorr_r2_1104"
sys.path.insert(0, RD); sys.path.insert(0, os.path.dirname(RD)); os.chdir(RD)
import r2
from common.minnot import load as mnload
t0 = time.time()
h = r2.load_1h(); H, L, Cl, QV = (h[k] for k in ["high", "low", "close", "qv"])
Cl = Cl.loc["2023-07-01":]; H, L, QV = (x.reindex_like(Cl) for x in (H, L, QV))
assert list(Cl.columns) == V.universe_symbols() or set(Cl.columns) == set(V.universe_symbols()), "universe mismatch"
# --- backtest reference: verbatim r2.vbo_h / diag_vbo.W
adv = QV.rolling(720, min_periods=240).mean(); age = Cl.notna().cumsum(); uni = adv.where(age >= 720).rank(axis=1, ascending=False) <= 50
Dh = H.resample("D").max(); Dl = L.resample("D").min(); f = lambda x: x.reindex(Cl.index.floor("D")).set_axis(Cl.index)
hh = f(Dh.rolling(55).max().shift(1)) * 1.01; ll = f(Dl.rolling(55).min().shift(1)) * 0.99
s = (((Cl > hh) & (Cl.shift(1) <= hh)) & uni).astype(float) - (((Cl < ll) & (Cl.shift(1) >= ll)) & uni).astype(float)
Wbt = r2.cap_norm(r2.hold(s, 7 * 24), 5 * 2).fillna(0)
MN = pd.Series(mnload(list(Cl.columns))[0], index=Cl.columns)
UNTR = sorted(json.load(open(os.path.join(HERE, "vbo1h_demo_untradable.json")))["symbols"])   # not TRADING on Binance Demo: live never sends
def z(X):
    X = X.copy(); X[[c for c in UNTR if c in X.columns]] = 0.0; return X
Wbt_m = Wbt.where((Wbt.abs() * r2.CAP * r2.LEV >= MN) | (Wbt == 0), 0.0)     # Sim.run mask (3000U at full 2x gross)
# --- live code, full panel
Wlive_full = V.signal_panel(H, L, Cl, QV)["W"].fillna(0)
full_maxdiff = float((Wlive_full - Wbt).abs().max().max())
# --- live code on the exact cloud window: start = floor(day(t)) - FETCH_DAYS, end = t. Per-day windows (all ops causal;
#     current-day Dh only enters via shift(1)) + true per-hour truncation on a sample incl. every trade bar.
days = pd.date_range(Cl.index[0].floor("D") + pd.Timedelta(days=V.FETCH_DAYS), Cl.index[-1].floor("D"), freq="D")
rows = []
for d in days:
    a = d - pd.Timedelta(days=V.FETCH_DAYS); b = d + pd.Timedelta(hours=23)
    w = V.signal_panel(H.loc[a:b], L.loc[a:b], Cl.loc[a:b], QV.loc[a:b])["W"].loc[d:b]; rows.append(w)
Wwin = pd.concat(rows).fillna(0); cmp_idx = Wwin.index
win_maxdiff = float((Wwin - Wbt.loc[cmp_idx]).abs().max().max())
mism_bars = int(((Wwin - Wbt.loc[cmp_idx]).abs() > 1e-12).any(axis=1).sum())
# trades = changes of the target weight per symbol
def trades(W):
    D = W.diff(); D.iloc[0] = W.iloc[0]
    st = D.stack(); st = st[st.abs() > 1e-12]
    return {(str(t), c): round(float(W.at[t, c]), 12) for (t, c) in st.index}
tb = trades(z(Wbt_m.loc[cmp_idx])); E = V.CAPITAL
Wl_m = z(Wwin.where((Wwin.abs() * V.GROSS * E >= MN) | (Wwin == 0), 0.0))            # live mask (1000U, 0.5x actual notional)
tl = trades(Wl_m)
Wbt_real = z(Wbt.loc[cmp_idx].where((Wbt.loc[cmp_idx].abs() * V.GROSS * E >= MN) | (Wbt.loc[cmp_idx] == 0), 0.0))   # vbo1h_realmn.py backtest
tb_real = trades(Wbt_real); same_real = tb_real == tl
tl_specmask = trades(z(Wwin.where((Wwin.abs() * r2.CAP * r2.LEV >= MN) | (Wwin == 0), 0.0)))
same_spec = tl_specmask == tb
only_bt = sorted(set(tb) - set(tl)); only_live = sorted(set(tl) - set(tb))
val_mis = [k for k in set(tb) & set(tl) if abs(tb[k] - tl[k]) > 1e-12]
trade_bars = sorted({pd.Timestamp(k[0]) for k in tb})
random.seed(7); smp = sorted(set(random.sample(list(cmp_idx), 300)) | set(random.sample(trade_bars, min(400, len(trade_bars)))))
hour_bad = 0
for t in smp:
    a = t.floor("D") - pd.Timedelta(days=V.FETCH_DAYS)
    _, tg = V.last_targets(H.loc[a:t], L.loc[a:t], Cl.loc[a:t], QV.loc[a:t])
    ref = {c: float(x) for c, x in Wbt.loc[t].items() if x != 0}
    if set(tg) != set(ref) or any(abs(tg[c] - ref[c]) > 1e-12 for c in ref):
        hour_bad += 1
# execution: ledger replay with decide() at 1000U -> an order exactly at every live target change
book, prev, orders, kinds = {}, {}, set(), {}
P = Cl.loc[cmp_idx].ffill()
for t in cmp_idx[:]:
    tgt = Wl_m.loc[t]
    for c in set(book) | set(tgt[tgt != 0].index) | set(prev):
        tt = float(tgt.get(c, 0.0)) * V.GROSS; pt = prev.get(c, 0.0); px = float(P.at[t, c]) if not np.isnan(P.at[t, c]) else None
        if px is None:
            continue
        q, k = V.decide(pt, tt, book.get(c, 0.0), px, E, 0.0, float(MN[c]))
        if k != "hold":
            orders.add((str(t), c)); kinds[k] = kinds.get(k, 0) + 1
        book[c] = q; prev[c] = tt
        if q == 0: book.pop(c, None)
exec_extra = sorted(orders - set(tl)); exec_missing = sorted(set(tl) - orders)
mn_syms = {c for c in MN.index if MN[c] > 5.0}
mn_only = all(k[1] in mn_syms for k in only_bt + val_mis) and not only_live
ok = mn_only and full_maxdiff < 1e-12 and win_maxdiff < 1e-12 and hour_bad == 0 and same_spec and same_real and not exec_extra and not exec_missing
out = {"slot": V.SLOT, "spec": V.SPEC, "pass": bool(ok), "generated_at": V.checked_at(), "data": "h1.pkl 2023-07-01..2026-09-30 (1h)",
       "bars_compared": len(cmp_idx), "first_bar": str(cmp_idx[0]), "last_bar": str(cmp_idx[-1]),
       "full_panel_max_abs_weight_diff": full_maxdiff, "window_max_abs_weight_diff": win_maxdiff, "window_mismatch_bars": mism_bars,
       "per_hour_truncated_checks": len(smp), "per_hour_mismatches": hour_bad,
       "backtest_trades": len(tb), "live_trades_specmask": len(tl_specmask), "trade_by_trade_identical_spec_mask": bool(same_spec),
       "demo_untradable_skipped": UNTR, "backtest_trades_real_1000U": len(tb_real), "trade_by_trade_identical_real_1000U": bool(same_real), "live_trades_1000U_mask": len(tl), "min_notional_divergence": {"only_backtest": len(only_bt), "only_live": len(only_live),
           "examples": [list(x) for x in (only_bt + only_live)[:10]],
           "note": "backtest masks at 3000U x full 2x gross (select.py); live at the real 50U/leg vs Binance MIN_NOTIONAL; only BTC/ETH (50/20U) can differ"},
       "trade_value_mismatches": len(val_mis), "min_notional_divergence_only_btc_eth": bool(mn_only), "exec_decisions": len(orders), "exec_kinds": kinds,
       "exec_note": "skip_min = cap_norm re-weight increase below MIN_NOTIONAL; live keeps the old qty (backtest rebalances continuously)",
       "exec_orders_without_target_change": len(exec_extra), "target_changes_without_order": len(exec_missing),
       "runtime_s": round(time.time() - t0, 1)}
json.dump(out, open(os.path.join(HERE, "parity_VBO1H.json"), "w"), indent=1); print(json.dumps(out, indent=1))
