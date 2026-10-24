"""Daily order path for demo_r2s_top10_3k (pattern: demo_e5_exec / demo_fa_exec). Runs from reconcile.
Active ONLY when the slot is enabled AND approved AND accepted AND both gates pass (demo_r2s.active); otherwise a no-op.
Signal: demo_r2s.indicators/universe/weights (re-verified vs the WF ledger target weights) on 1,000 real-fapi daily bars
of every TRADING USDT perp (box check: 1,000 bars x all symbols reproduces full-history targets exactly), cfg = the WF
pick for the quarter (LIVE_PICKS, 12m train / 3m test, re-selected on the box each quarter; missing pick -> no trading).
Decision per coin = demo_r2s.decide_minnot (the function that passed trade-by-trade parity) with p = today's daily open,
E = the slot's own virtual equity (3,000U + fills - fees + funding on its own book), per-symbol MIN_NOTIONAL + stepSize
from the real Demo exchangeInfo. Decided bar d (UTC) trades only in the window 00:00-02:00 UTC of d+1 (open t+1).
Orders are virtual-ledger intents netted per symbol with the other Demo slots (netting.py; no "held by another slot"
skip; exchange min notional/stepSize on the net). Opens/increases are skipped while the account/slot is paused, a guard
entry block is active, the symbol is TradFi (crypto perps only), or it isn't listed on Demo."""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import demo_r2s as D
import netting
import tradfi
from guards import log_event

log = logging.getLogger("demo_r2s_exec")
LEVERAGE = 3            # isolated per symbol (each position <= 0.2 E notional)
FEE = 0.0005            # taker estimate when the fill has no commission
BARS = 1000
WINDOW_H = 2            # trade only within the first 2h (UTC) after the decided bar closes
FAPI = os.environ.get("PERP_MARKET_BASE_URL", "https://fapi.binance.com").rstrip("/")
# WF picks (r2s_minnot mode 2, 3,000U): first test day of the quarter -> cfg. Decided bar d uses the pick for d+1.
LIVE_PICKS = {"2026-10-01": "th20_h3_hv0.0_L2.0"}   # train 2025-10-01..2026-09-30 (box, 2026-10-24)


def cfg_for(bar: str) -> str | None:
    d1 = pd.Timestamp(bar) + pd.Timedelta(days=1)
    q0 = pd.Timestamp(year=d1.year, month=3 * ((d1.month - 1) // 3) + 1, day=1)
    return LIVE_PICKS.get(str(q0.date()))


def pick_for(bar: str) -> tuple[str | None, str | None, bool]:
    """(cfg, quarter_used, fallback). No pick for the trading quarter -> latest earlier quarter's pick."""
    d1 = pd.Timestamp(bar) + pd.Timedelta(days=1)
    q = str(pd.Timestamp(year=d1.year, month=3 * ((d1.month - 1) // 3) + 1, day=1).date())
    if q in LIVE_PICKS:
        return LIVE_PICKS[q], q, False
    prev = [k for k in LIVE_PICKS if k < q]
    if not prev:
        return None, None, False
    k = max(prev)
    return LIVE_PICKS[k], k, True


def in_window(now: datetime) -> tuple[bool, str]:
    bar = str((now - timedelta(days=1)).date())
    return now.hour < WINDOW_H, bar


# ---------------- market data (real fapi, public)
def _get(path: str, params: dict):
    import requests
    for k in range(4):
        r = requests.get(FAPI + path, params=params, timeout=20)
        if r.status_code in (418, 429):
            time.sleep(5 * (k + 1)); continue
        r.raise_for_status(); return r.json()
    r.raise_for_status()


def perp_symbols(get=_get) -> list[str]:
    info = get("/fapi/v1/exchangeInfo", {})
    return sorted(s["symbol"] for s in info.get("symbols") or [] if s.get("quoteAsset") == "USDT"
                  and tradfi.is_crypto_perp(s) and s.get("status") == "TRADING")


def fetch_panel(syms, get=_get, bars: int = BARS, pace: float = 0.15) -> dict:
    """-> C,H,L,QV (rows = closed UTC days) + O_today (open of the current, still-forming day)."""
    rows = {}
    for s in syms:
        try:
            k = get("/fapi/v1/klines", {"symbol": s, "interval": "1d", "limit": bars})   # 1000 = weight 5 (>1000 = 10)
        except Exception as e:  # noqa: BLE001
            log.warning("r2s klines %s %s", s, e); continue
        rows[s] = k; time.sleep(pace)
    fr = {}
    for s, k in rows.items():
        if not k:
            continue
        df = pd.DataFrame([(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7])) for x in k],
                          columns=["t", "o", "h", "l", "c", "qv"])
        df.index = pd.to_datetime(df["t"], unit="ms", utc=True); fr[s] = df
    return fr


def build(fr: dict, bar: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    d = pd.Timestamp(bar, tz="UTC"); d1 = d + pd.Timedelta(days=1)
    pick = lambda col: pd.DataFrame({s: f[col] for s, f in fr.items()}).sort_index()
    C, H, L, QV = (pick(x).loc[:d] for x in ("c", "h", "l", "qv"))
    C = C.loc[:, C.notna().any()]; H, L, QV = H[C.columns], L[C.columns], QV[C.columns]
    O1 = {s: float(f.loc[d1, "o"]) for s, f in fr.items() if d1 in f.index}
    return C, H, L, QV, O1


def targets(C, H, L, QV, cfg_key: str) -> dict:
    I = D.indicators(C, H, L); el = D.universe(C, QV).values
    w = D.weights(I, el, D.parse_cfg(cfg_key))[-1]
    return {s: float(x) for s, x in zip(C.columns, w) if x != 0 and not np.isnan(x)}


# ---------------- book helpers
def equity(ex: dict, prices: dict) -> float:
    return float(ex.get("cash", D.CAPITAL)) + sum(q * float(prices.get(s) or 0) for s, q in (ex.get("book") or {}).items())


def entry_block(state: dict) -> str | None:
    import guards
    from main import slot_paused
    if state.get("paused"):
        return "帳戶暫停"
    if slot_paused(state, D.SLOT):
        return "本倉暫停"
    b, why = guards.entry_blocked(state)
    return f"風控封鎖：{why}" if b else None


def _fill(fut, sym, order, coid) -> tuple[float, float, float | None]:
    o = order
    for _ in range(6):
        if o.get("status") == "FILLED" and float(o.get("executedQty") or 0) > 0:
            break
        time.sleep(0.5); o = fut.get_order(sym, coid) or o
    return float(o.get("executedQty") or 0), float(o.get("avgPrice") or 0), None


def _funding(fut, ex: dict, now_ms: int, state: dict | None = None) -> float:
    since = int(ex.get("funding_since") or now_ms)
    tot = netting.funding_share(fut, state if state is not None else {"demo_r2s_exec": ex}, D.SLOT, ex.get("book") or {}, since, now_ms)
    ex["funding_since"] = now_ms
    return tot


def _round(q: float, step: float) -> float:
    if step <= 0:
        return q
    n = math.floor(abs(q) / step + 1e-9); dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return float(f"{n * step:.{dec}f}")


def execute(state: dict, fut, now: datetime | None = None, panel_fn=None, netter=None) -> dict:
    ok, why = D.active(state)
    if not ok:
        return {"skipped": why}
    now = now or datetime.now(timezone.utc)
    win, bar = in_window(now)
    ex = state.get("demo_r2s_exec") or {}
    state["demo_r2s_exec"] = ex
    if ex.get("last_bar") == bar:
        return {"skipped": "already executed", "bar": bar}
    if not win:
        return {"skipped": "outside 00:00-02:00 UTC window", "bar": bar}
    ck, q_used, fb = pick_for(bar)
    if not ck:
        log_event(state, "r2s_no_wf_pick", D.SLOT, bar=bar)
        return {"skipped": "no WF pick for this or any earlier quarter", "bar": bar}
    if fb:
        log_event(state, "r2s_wf_pick_fallback", D.SLOT, bar=bar, cfg=ck, from_quarter=q_used)
    ex.setdefault("cash", D.CAPITAL); book = ex.setdefault("book", {})
    if panel_fn is None:
        panel_fn = lambda: fetch_panel(sorted(set(perp_symbols()) | set(book)))
    C, H, L, QV, O1 = build(panel_fn(), bar)
    if str(C.index[-1].date()) != bar:
        return {"skipped": f"data ends {C.index[-1].date()} (want {bar})", "bar": bar}
    tw = targets(C, H, L, QV, ck)
    info = fut.exchange_info()
    lst = {s["symbol"] for s in info.get("symbols") or [] if s.get("status", "TRADING") == "TRADING"}
    syms = sorted(set(tw) | set(book))
    mn, st = D.load_mins(info, syms)
    now_ms = int(now.timestamp() * 1000)
    fnd = _funding(fut, ex, now_ms, state); ex["cash"] = float(ex["cash"]) + fnd
    prices = {s: O1[s] for s in syms if s in O1}
    for s in book:
        if s not in prices:
            prices[s] = float(fut.mark_price(s))
    eq = equity(ex, prices)
    blk = entry_block(state)
    out = {"bar": bar, "cfg": ck, "equity": round(eq, 2), "targets": {s: round(w, 4) for s, w in tw.items()},
           "funding": round(fnd, 4), "sent": [], "skipped": [], "kinds": {}}
    tag = bar.replace("-", "")[2:]
    plan = []
    for s in syms:
        if s not in prices:
            out["skipped"].append(f"{s} no price"); continue
        u = float(book.get(s, 0.0))
        q, kind = D.decide_minnot(tw.get(s, 0.0), u, prices[s], eq, mn[s], st[s])
        out["kinds"][kind] = out["kinds"].get(kind, 0) + 1
        if kind == "skip_min":
            log_event(state, "r2s_skip_min", D.SLOT, symbol=s, delta_notional=round(abs(tw.get(s, 0) * eq - u * prices[s]), 2), min=mn[s], bar=bar)
        if kind in ("hold", "skip_min") or q == u:
            continue
        plan.append((s, u, q, kind))
    own = netter is None
    netter = netter or netting.Netter(fut, info, tag=f"r2s{tag}")
    for s, u, q, kind in plan:
        reducing = q == 0 or (abs(q) < abs(u) and np.sign(q) == np.sign(u))
        if not reducing:
            why = ("not listed on Demo" if s not in lst else blk or ("TradFi (crypto only)" if tradfi.is_tradfi(s, info, state) else None))
            if why:
                out["skipped"].append(f"{s} {why}"); log_event(state, "r2s_entry_skip", D.SLOT, symbol=s, why=why, bar=bar); continue
        d = q - u
        if abs(d) <= 0:
            continue

        def logf(fq, px, fee, s=s, kind=kind):
            if fq:
                side = "BUY" if fq > 0 else "SELL"
                log_event(state, "r2s_order", D.SLOT, symbol=s, side=side, qty=abs(fq), px=px, kind=kind, bar=bar)
                out["sent"].append({"symbol": s, "side": side, "qty": abs(fq), "px": px, "kind": kind})
            else:
                out["skipped"].append(f"{s} net not filled")
        netter.add(state, D.SLOT, s, d, prices.get(s, 0.0), netting.ledger_cb(ex, s, on_fill=logf), kind=kind)

    def finish():
        out["equity_after"] = round(equity(ex, prices), 2)
        ex.update(last_bar=bar, last=out)
        ex["history"] = (list(ex.get("history") or []) + [{"bar": bar, "equity": out["equity_after"], "sent": len(out["sent"])}])[-400:]
    netter.after.append(finish)
    if own:
        netter.flush()
    return out
