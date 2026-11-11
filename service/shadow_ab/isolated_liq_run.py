"""Replay R2S th20_h3_hv0.0_L2.0 with and without isolated liquidation.

Public GETs only (Binance klines + the published USD-M risk brackets). No keys, no orders.
Writes the comparison JSON to $LIQ_OUT (default: the agent store) and prints the table.

  python3 service/shadow_ab/isolated_liq_run.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import demo_r2s as D  # noqa: E402
import isolated_liq as LIQ  # noqa: E402

OUT = Path(os.environ.get("LIQ_OUT", "/cursor/stores/self/isolated_liq"))
CFG = "th20_h3_hv0.0_L2.0"
BRACKETS_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets"
KLINE_URL = "https://www.binance.com/fapi/v1/klines"
CAPITAL = 3000.0
COST = 0.002


def metrics(equity: np.ndarray) -> dict:
    eq = pd.Series(equity).dropna()
    r = eq.pct_change().dropna()
    if len(r) < 5 or float(eq.iloc[0]) <= 0:
        return {"sharpe": None, "cagr": None, "maxdd": None, "n_days": int(len(r))}
    sd = float(r.std())
    yrs = len(r) / 365.0
    peak = eq.cummax()
    dd = float((eq / peak - 1.0).min())
    return {
        "sharpe": float(r.mean() / sd * np.sqrt(365.0)) if sd > 0 else 0.0,
        "cagr": float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / yrs) - 1.0) if yrs > 0 else None,
        "maxdd": -dd,
        "n_days": int(len(r)),
        "end_equity": float(eq.iloc[-1]),
    }


def load_brackets() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    cache = OUT / "brackets.json"
    if cache.exists():
        return json.loads(cache.read_text())
    raw = requests.get(BRACKETS_URL, timeout=60).json()["data"]["brackets"]
    out = {}
    for row in raw:
        tiers = []
        for b in row.get("riskBrackets") or []:
            tiers.append([float(b["bracketNotionalCap"]), float(b["bracketMaintenanceMarginRate"]),
                          float(b["cumFastMaintenanceAmount"])])
        if tiers:
            out[row["symbol"]] = tiers
    cache.write_text(json.dumps({"source": BRACKETS_URL, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "symbols": out}))
    return json.loads(cache.read_text())


def _klines(sym: str) -> tuple[str, list | None]:
    try:
        r = requests.get(KLINE_URL, params={"symbol": sym, "interval": "1d", "limit": 1500}, timeout=20)
        if r.status_code != 200:
            return sym, None
        data = r.json()
        if not isinstance(data, list) or len(data) < 200:
            return sym, None
        return sym, data
    except Exception:
        return sym, None


def load_panel(symbols: list[str]) -> dict[str, pd.DataFrame]:
    cache = OUT / "daily_panel.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    rows = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(_klines, s) for s in symbols]
        for n, fut in enumerate(as_completed(futs), 1):
            sym, data = fut.result()
            if data:
                df = pd.DataFrame([(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7])) for x in data],
                                  columns=["t", "o", "h", "l", "c", "qv"])
                df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
                rows[sym] = df[~df.index.duplicated()].sort_index()
            if n % 50 == 0:
                print(f"klines {n}/{len(symbols)} kept {len(rows)}", flush=True)
    pd.to_pickle(rows, cache)
    return rows


def run() -> dict:
    brackets_doc = load_brackets()
    symbols = sorted(s for s in brackets_doc["symbols"] if s.endswith("USDT"))
    print(f"brackets {len(symbols)} USDT symbols", flush=True)
    panel = load_panel(symbols)
    cols = sorted(panel)
    idx = panel[cols[0]].index
    for s in cols[1:]:
        idx = idx.union(panel[s].index)
    idx = idx.sort_values()
    def mat(col):
        return pd.DataFrame({s: panel[s][col] for s in cols}).reindex(idx)
    C, H, L, O, QV = mat("c"), mat("h"), mat("l"), mat("o"), mat("qv")
    # drop the still-forming last bar if its close time is in the future-ish: keep all closed days returned
    print(f"panel {C.shape} {idx.min().date()} .. {idx.max().date()}", flush=True)
    I = D.indicators(C, H, L)
    elig = D.universe(C, QV).values
    W = D.weights(I, elig, D.parse_cfg(CFG))
    brackets = brackets_doc["symbols"]
    kw = dict(symbols=list(C.columns), cost=COST)
    base = LIQ.replay(O.values, H.values, L.values, C.values, W, CAPITAL, leverage=None, **kw)
    x2 = LIQ.replay(O.values, H.values, L.values, C.values, W, CAPITAL, leverage=2.0, brackets=brackets, **kw)
    x3 = LIQ.replay(O.values, H.values, L.values, C.values, W, CAPITAL, leverage=3.0, brackets=brackets, **kw)

    def pack(res):
        m = metrics(res["equity"])
        m["n_liq"] = res["n_liq"]
        m["liq_loss"] = None if res["liq_loss"] is None else round(float(res["liq_loss"]), 2)
        return m

    def half(res, which):
        eq = res["equity"]
        finite = np.where(np.isfinite(eq))[0]
        if len(finite) < 10:
            return pack(res)
        mid = finite[len(finite) // 2]
        sl = eq.copy()
        if which == "H1":
            sl[mid:] = np.nan
        else:
            sl[:mid] = np.nan
        out = pack({"equity": sl, "n_liq": None, "liq_loss": None})
        return out

    # liquidation counts are for the full path; halves report return stats only
    table = {
        "config": CFG,
        "capital": CAPITAL,
        "cost_per_side": COST,
        "liq_fee": LIQ.FEE,
        "window": [str(idx.min().date()), str(idx.max().date())],
        "n_symbols": len(cols),
        "brackets_source": BRACKETS_URL,
        "brackets_fetched_at": brackets_doc.get("fetched_at"),
        "note": (
            "baseline is this same signal path with full mark-to-market and no isolated liquidation, "
            "which is how the stored R2S ledger is built. isolated_2x is the live LEV_BY_SLOT. "
            "isolated_3x is the previous default for a slot that was not in LEV_BY_SLOT. "
            "Strategy L stays 2.0 in all three. "
            "parity_R2S.json (sharpe 0.915, cagr 0.229, maxdd 0.218) is a different walk-forward stitch "
            "of many configs, not this single live config. "
            "E5 and VBO were not re-run: their historical panels are not on this machine, and VBO is hourly."
        ),
        "full": {"baseline": pack(base), "isolated_2x": pack(x2), "isolated_3x": pack(x3)},
        "H1": {"baseline": half(base, "H1"), "isolated_2x": half(x2, "H1"), "isolated_3x": half(x3, "H1")},
        "H2": {"baseline": half(base, "H2"), "isolated_2x": half(x2, "H2"), "isolated_3x": half(x3, "H2")},
    }
    (OUT / "r2s_th20_h3_hv0_L2.json").write_text(json.dumps(table, indent=1))
    print(json.dumps(table["full"], indent=1))
    return table


if __name__ == "__main__":
    run()
