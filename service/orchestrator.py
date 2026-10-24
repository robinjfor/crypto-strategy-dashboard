"""DEMO-only portfolio orchestrator (research_orchestrator 'regime' variant, spec_2026-11/research_orchestrator).

Scales slot *sizing capital* only; never touches entry/exit signals, stops, approvals or ledgers.
Rules (identical to orchestrator.py run(mode='regime')):
  base weights on total portfolio equity P, 1x:  E(demo_e5) 60% / U1 25% / R2S 15% / FA 0%
  bear   = BTC daily close < 200d SMA (last CLOSED bar)  -> E, U1 halved; freed capital 50/50 to R2S and FA
  hi-vol = BTC 30d realized vol (log, ann. sqrt365) > expanding 66.7th pct of its own history (min 365 obs,
           history from 2022-06-01 = the backtest panel start)  -> every target x0.7 (rest stays cash)
  NO drawdown breaker. DD from peak > 35% only raises an alert flag (log + /status), no trading action.
Quotes: the 10kU book is USDT (E5 5000 + R2S 3000 + FA 1000) + USDC (U1 1000). P = sum of the four virtual
slot ledger equities with USDC counted 1:1 as USDT; U1's target is in USDC and sized on its USDC ledger,
the other three in USDT. Every executor keeps its own wallet/margin checks, so a target the wallet cannot fund
is simply capped by those existing checks (no extra transfers).
Runs as Cloud Run job crypto-orchestrator-daily at 00:05 UTC (08:05 Taipei): after the 08:00 bar closes,
before the 08:08 reconcile (E5/R2S/FA executors) and the U1 hourly run that consume state['orchestrator'].
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

log = logging.getLogger("orchestrator")

SLOTS = {"E": "demo_e5_min5_x175", "U": "u1_live", "R": "demo_r2s_top10_3k", "F": "demo_funding_arb_1x"}
BASE_W = {"E": 0.60, "U": 0.25, "R": 0.15, "F": 0.0}
DEFAULT_CAP = {"E": 5000.0, "U": 1000.0, "R": 3000.0, "F": 1000.0}
QUOTE = {"E": "USDT", "U": "USDC", "R": "USDT", "F": "USDT"}
HIVOL_X = 0.7
DD_ALERT = 0.35
MA_N, VOL_N, VOL_MIN_HIST, VOL_Q = 200, 30, 365, 2 / 3
HIST_START = "2022-06-01"
FAPI = os.environ.get("FAPI_BASE_URL", "https://fapi.binance.com")
GCS_PREFIX = "research/orchestrator"
MAX_AGE = timedelta(hours=30)   # a plan older than this is ignored (executors fall back to their fixed capital)


# ---------------- pure rules (tested against the backtest code)
def regime(close: pd.Series) -> dict:
    """close: BTC daily closes (closed bars only, ascending). Decision for the next day uses the last value."""
    close = close.dropna().astype(float)
    ma = close.rolling(MA_N).mean()
    vol = np.log(close).diff().rolling(VOL_N).std() * np.sqrt(365)
    thr = vol.expanding(VOL_MIN_HIST).quantile(VOL_Q)
    c, m, v, t = (float(x.iloc[-1]) if not pd.isna(x.iloc[-1]) else None for x in (close, ma, vol, thr))
    return {"bar": str(close.index[-1].date()), "btc_close": c, "ma200": m, "vol30": v, "vol_thr": t,
            "bear": bool(m is not None and c < m), "hivol": bool(v is not None and t is not None and v > t),
            "vol_hist_n": int(vol.notna().sum())}


def weights(bear: bool, hivol: bool) -> tuple[dict, float]:
    w = dict(BASE_W)
    if bear:
        fr = (w["E"] + w["U"]) / 2; w["E"] /= 2; w["U"] /= 2; w["R"] += fr / 2; w["F"] += fr / 2
    return w, (HIVOL_X if hivol else 1.0)


def plan(close: pd.Series, equities: dict, prev: dict | None, now: datetime) -> dict:
    rg = regime(close)
    w, x = weights(rg["bear"], rg["hivol"])
    P = float(sum(equities.values()))
    peak = max(P, float((prev or {}).get("peak") or 0.0))
    dd = 1 - P / peak if peak > 0 else 0.0
    tgt = {k: round(P * w[k] * x, 2) for k in w}
    prev_t = (prev or {}).get("targets") or {}
    actions = []
    for k in w:
        a, b = prev_t.get(SLOTS[k]), tgt[k]
        if a is None:
            actions.append(f"{SLOTS[k]}: set sizing capital {b:.2f} {QUOTE[k]} (was fixed {DEFAULT_CAP[k]:.0f})")
        elif abs(a - b) >= 0.01:
            actions.append(f"{SLOTS[k]}: sizing capital {a:.2f} -> {b:.2f} {QUOTE[k]}")
    alert = dd > DD_ALERT
    if alert:
        actions.append(f"ALERT: portfolio drawdown {dd:.1%} > {DD_ALERT:.0%} (alert only, no trading action)")
    return {"schema": "orchestrator_daily_v1", "generated_at": now.isoformat(), "decided_bar": rg["bar"],
            "regime": rg, "weights": {SLOTS[k]: round(w[k], 4) for k in w}, "exposure_mult": x,
            "targets": {SLOTS[k]: tgt[k] for k in w}, "target_quote": {SLOTS[k]: QUOTE[k] for k in w},
            "equity": {SLOTS[k]: round(float(equities[k]), 2) for k in equities}, "portfolio_equity": round(P, 2),
            "peak": round(peak, 2), "drawdown": round(dd, 4), "dd_alert": alert, "dd_alert_threshold": DD_ALERT,
            "actions": actions,
            "notes": "USDC counted 1:1 as USDT in P; U1 target in USDC on its USDC ledger. No DD breaker."}


# ---------------- consumer side (executors). Never raises; None/stale plan => unchanged fixed sizing.
_ACTIVE: dict | None = None


def load(state: dict | None) -> dict | None:
    global _ACTIVE
    _ACTIVE = (state or {}).get("orchestrator") or None
    return _ACTIVE


def sizing_capital(slot_id: str, default: float, now: datetime | None = None, plan_doc: dict | None = None) -> float:
    p = plan_doc if plan_doc is not None else _ACTIVE
    try:
        if not p or os.environ.get("ORCHESTRATOR_ENABLED", "1") != "1":
            return default
        at = datetime.fromisoformat(str(p["generated_at"]))
        if (now or datetime.now(timezone.utc)) - at > MAX_AGE:
            log.warning("orchestrator_plan_stale at=%s -> fixed capital for %s", p.get("generated_at"), slot_id)
            return default
        v = (p.get("targets") or {}).get(slot_id)
        return default if v is None else max(0.0, float(v))
    except Exception as e:  # noqa: BLE001
        log.warning("orchestrator_sizing_fail %s", e)
        return default


# ---------------- job side (Cloud Run only: Binance calls)
def btc_closes(now: datetime) -> pd.Series:
    import requests
    rows, start = [], int(pd.Timestamp(HIST_START, tz="UTC").timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    while True:
        r = requests.get(FAPI + "/fapi/v1/klines", params={"symbol": "BTCUSDT", "interval": "1d", "startTime": start, "limit": 1500}, timeout=20)
        r.raise_for_status(); k = r.json()
        rows += [x for x in k if int(x[6]) < end_ms]   # closed bars only
        if len(k) < 1500:
            break
        start = int(k[-1][0]) + 1
    s = pd.Series({pd.Timestamp(int(x[0]), unit="ms", tz="UTC"): float(x[4]) for x in rows}).sort_index()
    return s[~s.index.duplicated()]


def _book_eq(ex: dict | None, default: float, px) -> float:
    if not ex:
        return default
    return float(ex.get("cash", default)) + sum(float(q) * px(s) for s, q in (ex.get("book") or {}).items() if q)


def slot_equities(state: dict, fut) -> dict:
    cache: dict = {}

    def px(s):
        if s not in cache:
            cache[s] = float(fut.mark_price(s))
        return cache[s]
    eq = {"E": _book_eq(state.get("demo_e5_exec"), DEFAULT_CAP["E"], px),
          "R": _book_eq(state.get("demo_r2s_exec"), DEFAULT_CAP["R"], px)}
    fa = DEFAULT_CAP["F"]   # FA has no cash ledger: capital + unrealized spot/perp legs (funding not tracked)
    for s, p in (state.get("demo_fa_positions") or {}).items():
        try:
            m = px(s); fa += float(p.get("spot_qty") or 0) * (m - float(p.get("spot_px") or m)) \
                + float(p.get("perp_qty") or 0) * (float(p.get("perp_px") or m) - m)
        except Exception:  # noqa: BLE001
            pass
    eq["F"] = fa
    sl = state.get("u1_live")
    if sl:
        import u1_live
        pm = u1_live.perp_map(sl.get("quote") or "USDC")
        eq["U"] = u1_live.sleeve_equity(sl, {k: (px(pm[k]) if float((sl.get("units") or {}).get(k) or 0) else 0.0) for k in u1_live.SYMS})
    else:
        eq["U"] = DEFAULT_CAP["U"]
    return eq


def run(dry_run: bool = False, now: datetime | None = None) -> dict:
    from futures_client import FuturesDemoClient
    from state_store import StateStore
    now = now or datetime.now(timezone.utc)
    store = StateStore(); state = store.load()
    prev = state.get("orchestrator")
    doc = plan(btc_closes(now), slot_equities(state, FuturesDemoClient()), prev, now)
    doc["dry_run"] = bool(dry_run)
    if doc["dd_alert"]:
        log.error("ORCHESTRATOR_DD_ALERT dd=%.4f peak=%.2f equity=%.2f", doc["drawdown"], doc["peak"], doc["portfolio_equity"])
    bucket = os.environ.get("GCS_BUCKET")
    name = f"{GCS_PREFIX}/{now:%Y-%m-%d}.json"
    body = json.dumps(doc, indent=1, default=str)
    if bucket:
        from google.cloud import storage
        storage.Client().bucket(bucket).blob(name).upload_from_string(body, content_type="application/json")
        print(f"wrote gs://{bucket}/{name}")
    if not dry_run:
        def mut(s):
            s["orchestrator"] = {**doc, "history": ((s.get("orchestrator") or {}).get("history") or [])[-399:]
                                 + [{"bar": doc["decided_bar"], "P": doc["portfolio_equity"], "dd": doc["drawdown"],
                                     "bear": doc["regime"]["bear"], "hivol": doc["regime"]["hivol"]}]}
            return s
        store.mutate(mut)
    print(body)
    return doc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dry = "--dry-run" in sys.argv or os.environ.get("ORCH_DRY_RUN") == "1"
    run(dry_run=dry)
    sys.exit(0)
