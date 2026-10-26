"""Box-side trade-by-trade parity: service planner (demo_e5v.plan_s5 + demo_e5.plan_u1, the live code) vs the backtest
ledger of research_minnot_1026/e5_minnot_variants.py for cfg (dbm=1, thr=0.005, rup=0, topn=20), E_CAPITAL=5000.
The ledger (numba sim) logs every trade; the replay holds its OWN book and is fed only the ledger's per-day sleeve
equity / Lt / m, prices, and the same targets. Per day the set of (coin, delta, sleeve) must match exactly.
Liquidation events (ledger kinds 2/3) are not orders and are applied from the ledger. Writes shadow_ab/parity_E5V.json.
Usage: python shadow_ab/e5v_parity.py   (needs /workspace/strategy-unified-3y data; ~2 min)"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RESEARCH = Path(os.environ.get("E5V_RESEARCH", "/workspace/strategy-unified-3y/spec_2026-11/research_minnot_1026"))
CFG = (1, 0.005, 0, 20)


def replay(V, cfg, sched, start, TL, REC, plan_u1, plan_s5, tail_days: int = 10) -> dict:
    ncol = len(V.cols); u5 = np.zeros(ncol); uu = np.zeros(2); ui = (V.iB, V.iE)
    led: dict = {}
    for i, c, d, s in TL:
        led.setdefault(int(i), []).append((int(c), float(d), int(s)))
    days = mism = sent = skip_min = skip_thr = live_trades = 0; first = None; tail = []
    for i in range(start, V.n):
        eu, es, Lt, m = REC[i, :4]
        if not eu and not es:
            continue
        days += 1; got = []; kinds = {}
        o2 = np.array([V.Of[i, V.iB], V.Of[i, V.iE]])
        nuu, od1 = plan_u1(uu, V.W1[i - 1], Lt, m, o2, eu, V.MN[[V.iB, V.iE]], V.STEP[[V.iB, V.iE]])
        for j in range(2):
            if nuu[j] != uu[j]:
                got.append((ui[j], nuu[j] - uu[j], 0))
        uu = nuu
        k = sched[i - 1]
        nu5, od5 = plan_s5(u5, V.Ov[i], V.WS[k, i - 1], V.DBS[k, i - 1] * cfg[0], es, V.LV, m, V.MN, V.STEP, thr=cfg[1], topn=cfg[3], dbm=1.0)
        for c in range(ncol):
            if nu5[c] != u5[c]:
                got.append((c, nu5[c] - u5[c], 1))
        u5 = nu5
        for od in (od1, od5):
            for _, _, _, kd in od:
                kinds[kd] = kinds.get(kd, 0) + 1
        sent += sum(v for kd, v in kinds.items() if not kd.startswith("skip")); skip_min += kinds.get("skip_min", 0); skip_thr += kinds.get("skip_thr", 0)
        exp = [(c, d, s) for c, d, s in led.get(i, []) if s < 2]
        live_trades += len(got)
        ok = len(exp) == len(got) and all(a[0] == b[0] and a[2] == b[2] and abs(a[1] - b[1]) <= 1e-9 * max(1.0, abs(b[1])) for a, b in zip(sorted(got), sorted(exp)))
        if not ok:
            mism += 1; first = first or str(V.IDX[i].date())
        for c, d, s in led.get(i, []):
            if s == 2: uu[ui.index(c)] = 0.0
            if s == 3: u5[c] = 0.0
        tail.append({"day": str(V.IDX[i].date()), "trades": len(exp), "sent": sum(v for kd, v in kinds.items() if not kd.startswith("skip")),
                     "skip_min": kinds.get("skip_min", 0), "skip_thr": kinds.get("skip_thr", 0), "match": ok})
    ledger_trades = int(sum(1 for t in TL if t[3] < 2))
    return {"days": days, "live_trades": live_trades, "ledger_trades": ledger_trades, "day_mismatches": mism, "first_mismatch": first,
            "orders_sent": sent, "skipped_lt_min": skip_min, "skipped_thr": skip_thr,
            "skip_rate": round(skip_min / max(sent + skip_min, 1), 4), "tail": tail[-tail_days:],
            "pass": days > 0 and mism == 0 and live_trades == ledger_trades}


def main() -> dict:
    sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(RESEARCH))
    cwd = os.getcwd(); os.chdir(RESEARCH)
    try:
        import e5_minnot_variants as V
    finally:
        os.chdir(cwd)
    import demo_e5, demo_e5v
    K = 5000.0; start = int(np.searchsorted(V.IDX, V.OOS_START))
    sched, _ = V.wf_sched(K, 2)
    r, st, TL, REC = V.run(CFG, sched, start, K)
    par = replay(V, CFG, sched, start, TL, REC, demo_e5.plan_u1, demo_e5v.plan_s5)
    ref = V.live_replay(CFG, sched, start, TL, REC)
    par.update(spec=demo_e5v.SPEC, slot=demo_e5v.SLOT, capital=K, ledger_orders=int(st[0]), ledger_skipped_min=int(st[1]), ledger_thr_suppressed=int(st[4]),
               ledger_skip_rate=round(float(st[1] / max(st[0] + st[1], 1)), 4), research_replay=ref,
               metrics=V.mets(r[V.OOS_START:V.OOS_END]), checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    par["pass"] = bool(par["pass"] and par["orders_sent"] == par["ledger_orders"] and par["skipped_lt_min"] == par["ledger_skipped_min"]
                       and par["skipped_thr"] == par["ledger_thr_suppressed"])
    json.dump(par, open(HERE / "parity_E5V.json", "w"), indent=1, default=str)
    return par


if __name__ == "__main__":
    p = main()
    print(json.dumps({k: v for k, v in p.items() if k != "tail"}, indent=1, default=str))
