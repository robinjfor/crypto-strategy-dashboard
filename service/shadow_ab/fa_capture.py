"""Cloud-side: load vendored funding_arb.py (byte-identical, __main__ block not run), refresh spot 1d klines,
compare demo_fa planner vs funding_arb.run(0, 10) day by day, and build today's live targets."""
from __future__ import annotations

import json, os, sys, time, urllib.request
from pathlib import Path

import numpy as np
import pandas as pd


def fetch_spot(data_dir: Path, syms, days: int = 500) -> int:
    sp = data_dir / "sp"; sp.mkdir(parents=True, exist_ok=True); n = 0
    start = int((time.time() - days * 86400) * 1000)
    for s in syms:
        try:
            u = f"https://api.binance.com/api/v3/klines?symbol={s}&interval=1d&startTime={start}&limit=1000"
            rows = json.loads(urllib.request.urlopen(u, timeout=20).read())
        except Exception:  # noqa: BLE001  (no spot pair -> no file, same as spec exclusion)
            continue
        rows = rows[:-1]  # drop today's unfinished bar
        pd.DataFrame(rows).to_csv(sp / f"{s}_zrest.csv", header=False, index=False); n += 1
    return n


def _load_module(spec_root: Path) -> dict:
    src = (spec_root / "funding_arb" / "funding_arb.py").read_text()
    g = {"__name__": "funding_arb_vendored", "__file__": str(spec_root / "funding_arb" / "funding_arb.py")}
    argv = sys.argv; sys.argv = ["funding_arb.py", "base"]
    try:
        exec(compile(src, g["__file__"], "exec"), g)
    finally:
        sys.argv = argv
    return g


def run(spec_root: Path, data_dir: Path, parity_start: str) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import demo_fa
    os.environ["SPEC_DATA_DIR"] = str(data_dir)
    p = pd.read_pickle(data_dir / "panel_1d.pkl")
    qv = p.xs("quote_volume", axis=1, level=1)
    top = qv.iloc[-560:].rolling(30).mean().rank(axis=1, ascending=False) <= 40
    fetch_spot(data_dir, [s for s in top.columns[top.any()] if s.endswith("USDT")])
    g = _load_module(spec_root)
    led_r, led_n = g["run"](demo_fa.EXIT_THR, demo_fa.K)
    syms = g["syms"]
    u = g["UNI"][syms].values; f7 = g["F7"].values; f1 = g["F1"].values
    rs, rp, fn = g["R_SPOT"].values, g["R_PERP"].values, g["FUND_NEXT"].values
    my_r, my_n, trades, hs = demo_fa.simulate(u, f7, f1, rs, rp, fn)
    idx = g["C"].index
    si = int(np.searchsorted(idx, pd.Timestamp(parity_start, tz="UTC")))
    # compare only days whose returns are fully realised (ledger needs open t+2)
    last_real = int(np.where(~np.isnan(rp).all(axis=1))[0].max()) + 1 if (~np.isnan(rp)).any() else 0
    par = demo_fa.parity(led_r.values[:last_real], led_n.values[:last_real], my_r[:last_real], my_n[:last_real], trades[:last_real], si)
    par["from"] = parity_start
    # live decision at the latest closed bar: availability = spot & perp prices exist at t (open t+1 not known yet)
    held = hs[last_real - 1] if last_real else np.zeros(len(syms), bool)
    for i in range(last_real, len(idx)):
        avail = g["SPO"][syms].iloc[i].notna().values & g["O"][syms].iloc[i].notna().values
        held = demo_fa.decide(held, u[i], f7[i], f1[i], avail)
    liq_last = g["liq"][syms].iloc[-1]
    tgt = {"decided_bar": str(idx[-1].date()), "K": demo_fa.K, "exit_thr": demo_fa.EXIT_THR,
           "held": [syms[j] for j in np.where(held)[0]],
           "f7": {syms[j]: float(f7[-1][j]) for j in np.where(held)[0]},
           "universe": sorted(set([s for s in syms if bool(g["UNI"][s].iloc[-1])]) | {syms[j] for j in np.where(held)[0]}),
           "probe": str(liq_last.idxmax()) if liq_last.notna().any() else "BTCUSDT"}
    return {"parity": par, "targets": tgt}
