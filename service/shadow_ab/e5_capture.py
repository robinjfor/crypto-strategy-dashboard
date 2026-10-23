"""Run the analyst's ledger_e_minnot.py (vendored, unchanged) and capture, per decision day, the exact inputs and
outputs of its two rebalance loops (U1 BTC/ETH band rule, S5 buffer rule, both with per-symbol min notional / stepSize, mode 2).
The `sim` source is taken verbatim from the module; only recording lines are inserted around the two loops and
it is run as plain Python (numba off for this copy only). Used for (a) trade-by-trade parity of service/demo_e5.py
vs the backtest and (b) next-open targets for the Demo slot. No orders, no API keys."""
from __future__ import annotations

import inspect
import os
import sys
import textwrap
from pathlib import Path

import numpy as np

SHADOW = Path("/workspace/strategy-unified-3y/spec_2026-11/research_1022/shadow_AB")
K_DEMO = 5000.0

U1_PRE = "                for j in range(2):\n                    wt = W1[i-1, j] * Lt"
U1_POST = "            k = sched[i-1]; mm = m if s5only == 0 else 1.0"
S5_PRE = "            for c in range(m_):\n                if np.isnan(Ov[i, c]): continue"
S5_POST = "            if s5only == 0:\n                gw = uu[0] * Lv_[i, iB]"


def _patched_sim(mod):
    src = inspect.getsource(mod.sim.py_func if hasattr(mod.sim, "py_func") else mod.sim)
    src = textwrap.dedent(src)
    src = "\n".join(l for l in src.splitlines() if not l.startswith("@njit"))
    for a in (U1_PRE, U1_POST, S5_PRE, S5_POST):
        assert src.count(a) == 1, a
    src = src.replace(U1_PRE, "                _R.append(('u1', i, r.copy(), m, uu.copy(), o2.copy(), eu, W1[i-1].copy()))\n" + U1_PRE)
    src = src.replace(U1_POST, "            if s5only == 0: _R.append(('u1post', i, uu.copy(), Lt))\n" + U1_POST)
    src = src.replace(S5_PRE, "            _R.append(('s5', i, u5.copy(), es, k, mm, Ov[i].copy(), WS[k, i-1].copy(), DBS[k, i-1].copy(), lv))\n" + S5_PRE)
    src = src.replace(S5_POST, "            _R.append(('s5post', i, u5.copy()))\n" + S5_POST)
    g = dict(mod.__dict__)
    rec: list = []
    g["_R"] = rec
    exec(compile(src, "ledger_e_minnot_sim_capture", "exec"), g)
    return g["sim"], rec


def load(shadow_dir: Path = SHADOW, data_dir: Path | None = None):
    if data_dir is not None:  # common.py reads SPEC_DATA_DIR at import; other ledgers may have imported it already
        os.environ["SPEC_DATA_DIR"] = str(data_dir)
        for m in [m for m in sys.modules if m in ("lib", "common", "minnot", "ledger_e_minnot", "ledger_e_min5") or m.startswith("s5_")]:
            del sys.modules[m]
    sys.path.insert(0, str(shadow_dir))
    cwd = os.getcwd(); os.chdir(shadow_dir)
    try:
        import ledger_e_minnot as L
    finally:
        os.chdir(cwd)
    return L


def run(K: float = K_DEMO, shadow_dir: Path = SHADOW, data_dir: Path | None = None) -> dict:
    L = load(shadow_dir, data_dir)
    start = int(np.searchsorted(L.IDX, L.OOS_START))
    sched, picks = L.wf_sched(K, 2)              # jitted, same as the study
    sim, rec = _patched_sim(L)
    eq, st = sim(float(K), L.MN.copy(), L.STEP.copy(), L.MN, sched.astype(np.int64), int(start), L.W_U1, L.LV, 0, L.Ov, L.Hv, L.Lv_, L.Cv,
                 L.Of, L.Cf, L.F5, L.W1, L.WS, L.DBS, L.iB, L.iE, L.COST, L.CAP, L.BAND, L.MAINT)
    # cross-check: the instrumented copy reproduces the jitted ledger
    eq_j, st_j = L.sim(float(K), L.MN.copy(), L.STEP.copy(), L.MN, sched.astype(np.int64), int(start), L.W_U1, L.LV, 0, L.Ov, L.Hv, L.Lv_, L.Cv,
                       L.Of, L.Cf, L.F5, L.W1, L.WS, L.DBS, L.iB, L.iE, L.COST, L.CAP, L.BAND, L.MAINT)
    ok = np.isfinite(eq_j)
    same = float(np.nanmax(np.abs(eq[ok] / eq_j[ok] - 1))) if ok.any() else 0.0
    last = len(L.IDX) - 1
    k = int(sched[last])
    tgt = {"decided_bar": str(L.IDX[last].date()), "cols": L.cols, "s5_cfg": L.KEYS[k],
           "w1": [float(x) for x in L.W1[last]], "ws": {c: float(v) for c, v in zip(L.cols, L.WS[k, last]) if v != 0},
           "dbs": {c: float(v) for c, v in zip(L.cols, L.DBS[k, last]) if v != 0}, "lv": L.LV, "w_u1": L.W_U1,
           "min_notional": {c: float(L.MN[j]) for j, c in enumerate(L.cols)}, "step": {c: float(L.STEP[j]) for j, c in enumerate(L.cols) if L.STEP[j]}, "K": K}
    return {"rec": rec, "cols": L.cols, "idx": [str(d.date()) for d in L.IDX], "stats": st, "stats_jit": st_j,
            "capture_vs_jit_max_rel": same, "targets": tgt, "iB": L.iB, "iE": L.iE, "mn": L.MN.copy(), "step": L.STEP.copy(), "mn_path": L.MN_PATH}
