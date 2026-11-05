"""Hourly order path for demo_vbo1h_top50_1k (pattern: demo_r2s_exec). Runs from reconcile (every :08/:23/:38/:53 UTC).
Active ONLY when enabled AND approved AND accepted AND both gates pass (demo_vbo1h.active); otherwise a no-op.
Decided bar = the 1h bar that closed at HH:00 UTC; it trades only in HH:00-HH:50 (first reconcile tick after the close,
i.e. at the open of bar t+1, same as the backtest's close(t)->close(t+1) return). Per-bar claim in reconcile (slot_claim).
Signal: demo_vbo1h.signal_panel (trade-by-trade parity vs r2.py, shadow_ab/parity_VBO1H.json) on FETCH_DAYS of real-fapi
1h bars for the 98-symbol research universe (+ anything still in the book). Per symbol demo_vbo1h.decide: trade only when the
backtest target weight changes; size = w * 0.5 * E with E = the slot's own virtual equity (1,000U ledger + fills - fees +
funding); Binance MIN_NOTIONAL/stepSize from the Demo exchangeInfo. Orders are virtual-ledger intents netted per symbol with
the other Demo slots (netting.Netter, isolated 2x via LEV_BY_SLOT). A close/reduce of a leg the exchange does not hold is
never sent. Opens/increases are skipped while paused, guard-blocked, TradFi, or not listed on Demo.
Daily shadow (bar 00 UTC): recompute the previous 24 bars' backtest targets from the same panel and compare with what the
ledger recorded + the backtest-model return vs the ledger's equity change -> ex["shadow"] (review_date 2026-12-02)."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import demo_vbo1h as V
import netting
import tradfi
from guards import log_event

log = logging.getLogger("demo_vbo1h_exec")
KEY = V.STATE_KEY
WINDOW_MIN = 50
FAPI = os.environ.get("PERP_MARKET_BASE_URL", "https://fapi.binance.com").rstrip("/")


def in_window(now: datetime) -> tuple[bool, str]:
    h = now.replace(minute=0, second=0, microsecond=0)
    return now.minute < WINDOW_MIN, (h - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00Z")


def _get(path: str, params: dict):
    import requests
    for k in range(4):
        r = requests.get(FAPI + path, params=params, timeout=20)
        if r.status_code in (418, 429):
            time.sleep(5 * (k + 1)); continue
        r.raise_for_status(); return r.json()
    r.raise_for_status()


def fetch_panel(syms, now: datetime, get=_get, pace: float = 0.05) -> dict:
    """1h klines from floor(day(now)) - FETCH_DAYS to now (incl. the forming bar). 2 requests/symbol (weight 5 + <=5)."""
    start = pd.Timestamp(now).tz_convert("UTC").floor("D") - pd.Timedelta(days=V.FETCH_DAYS)
    end_ms = int(pd.Timestamp(now).timestamp() * 1000)
    fr = {}
    for s in syms:
        rows, t = [], int(start.timestamp() * 1000)
        try:
            while t <= end_ms:
                k = get("/fapi/v1/klines", {"symbol": s, "interval": "1h", "startTime": t, "limit": 1000})
                if not k:
                    break
                rows += k; t = int(k[-1][0]) + 3600_000
                if len(k) < 1000:
                    break
                time.sleep(pace)
        except Exception as e:  # noqa: BLE001
            log.warning("vbo1h klines %s %s", s, e); continue
        if rows:
            df = pd.DataFrame([(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7])) for x in rows],
                              columns=["t", "o", "h", "l", "c", "qv"])
            df.index = pd.to_datetime(df["t"], unit="ms", utc=True); fr[s] = df[~df.index.duplicated()]
        time.sleep(pace)
    return fr


def build(fr: dict, bar: pd.Timestamp, cols: list[str]):
    """-> H,L,C,QV on a contiguous hourly index [floor(day(bar)) - FETCH_DAYS, bar] + O1 (open of bar+1 per symbol)."""
    idx = pd.date_range(bar.floor("D") - pd.Timedelta(days=V.FETCH_DAYS), bar, freq="h", tz="UTC")
    pick = lambda col: pd.DataFrame({s: (fr[s][col] if s in fr else pd.Series(dtype=float)) for s in cols}).reindex(idx)
    H, L, C, QV = (pick(x) for x in ("h", "l", "c", "qv"))
    b1 = bar + pd.Timedelta(hours=1)
    O1 = {s: float(f.loc[b1, "o"]) for s, f in fr.items() if b1 in f.index}
    last = {s: f.index.max() for s, f in fr.items()}
    return H, L, C, QV, O1, last


def equity(ex: dict, prices: dict) -> float:
    return float(ex.get("cash", V.CAPITAL)) + sum(q * float(prices.get(s) or 0) for s, q in (ex.get("book") or {}).items())


def entry_block(state: dict) -> str | None:
    import guards
    from main import slot_paused
    if state.get("paused"):
        return "帳戶暫停"
    if slot_paused(state, V.SLOT):
        return "本倉暫停"
    b, why = guards.entry_blocked(state)
    return f"風控封鎖：{why}" if b else None


def _funding(fut, ex: dict, now_ms: int, state: dict) -> float:
    since = int(ex.get("funding_since") or now_ms)
    tot = netting.funding_share(fut, state, V.SLOT, ex.get("book") or {}, since, now_ms)
    ex["funding_since"] = now_ms
    return tot


def shadow_day(ex: dict, W: pd.DataFrame, C: pd.DataFrame, bar: pd.Timestamp) -> dict | None:
    """Previous UTC day (24 bars ending at bar-1h... bar = 00:00 -> day = bar-1d): recomputed backtest targets vs the
    ledger's recorded targets per bar + backtest-model return (w*0.5x on close->close) vs ledger equity change."""
    day0 = bar.floor("D") - pd.Timedelta(days=1); bars = pd.date_range(day0, day0 + pd.Timedelta(hours=23), freq="h", tz="UTC")
    hist = {h["bar"]: h for h in ex.get("history") or [] if isinstance(h, dict)}
    bt = V.notional_frac(W.reindex(bars).fillna(0))
    R = C.pct_change(fill_method=None).shift(-1).reindex(bars).fillna(0)
    model = float(((bt * R).sum(axis=1)).add(1).prod() - 1)
    rec, bad, eqs = 0, [], []
    for t in bars:
        h = hist.get(t.strftime("%Y-%m-%dT%H:00Z"))
        if not h:
            continue
        rec += 1; eqs.append(h.get("equity_before"))
        ref = {s: round(float(x), 6) for s, x in bt.loc[t].items() if x != 0}
        if (h.get("tg") or {}) != ref:
            bad.append(t.strftime("%H"))
    e_end = (hist.get(bar.strftime("%Y-%m-%dT%H:00Z")) or {}).get("equity_before")
    led = (e_end / eqs[0] - 1) if (eqs and eqs[0] and e_end) else None
    return {"day": str(day0.date()), "bars_recorded": rec, "target_mismatch_bars": bad, "model_ret": round(model, 5),
            "ledger_ret": None if led is None else round(led, 5), "gap": None if led is None else round(led - model, 5),
            "review_date": V.REVIEW_DATE}


def execute(state: dict, fut, now: datetime | None = None, panel_fn=None, netter=None) -> dict:
    ok, why = V.active(state)
    if not ok:
        return {"skipped": why}
    now = now or datetime.now(timezone.utc)
    win, bar_s = in_window(now)
    ex = state.get(KEY) or {}
    state[KEY] = ex
    if ex.get("last_bar") == bar_s:
        return {"skipped": "already executed", "bar": bar_s}
    if not win:
        return {"skipped": f"outside HH:00-HH:{WINDOW_MIN} window", "bar": bar_s}
    bar = pd.Timestamp(bar_s.replace("Z", ""), tz="UTC")
    ex.setdefault("cash", V.CAPITAL); book = ex.setdefault("book", {}); prev = dict(ex.get("targets") or {})
    cols = V.universe_symbols()
    syms_fetch = sorted(set(cols) | set(book))
    fr = panel_fn() if panel_fn else fetch_panel(syms_fetch, now)
    H, L, C, QV, O1, last = build(fr, bar, cols)
    if C.loc[bar].notna().sum() < 0.8 * len(cols):
        return {"skipped": f"bar {bar_s} incomplete ({int(C.loc[bar].notna().sum())}/{len(cols)} symbols)", "bar": bar_s}
    sp = V.signal_panel(H, L, C, QV); W = sp["W"].fillna(0)
    tw = {s: float(x) * V.GROSS for s, x in W.loc[bar].items() if x != 0}
    info = fut.exchange_info()
    lst = {s["symbol"] for s in info.get("symbols") or [] if s.get("status", "TRADING") == "TRADING"}
    import demo_r2s
    syms = sorted(set(tw) | set(book) | set(prev))
    mn, st = demo_r2s.load_mins(info, syms)
    now_ms = int(now.timestamp() * 1000)
    fnd = _funding(fut, ex, now_ms, state); ex["cash"] = float(ex["cash"]) + fnd
    prices = {s: O1[s] for s in syms if s in O1}
    for s in syms:
        if s not in prices:
            try:
                prices[s] = float(fut.mark_price(s))
            except Exception:  # noqa: BLE001
                pass
    eq = equity(ex, prices)
    keep, dropped = V.apply_min(tw, eq, mn)
    blk = entry_block(state)
    out = {"bar": bar_s, "slot": V.SLOT, "equity": round(eq, 2), "n": len(tw), "targets": {s: round(w, 5) for s, w in keep.items()},
           "min_masked": dropped, "funding": round(fnd, 4), "sent": [], "skipped": [], "kinds": {}}
    try:
        xpos = {r["symbol"]: float(r.get("positionAmt") or 0) for r in fut.position_risk()}
    except Exception:  # noqa: BLE001
        xpos = None
    tag = bar.strftime("%y%m%d%H")
    own = netter is None
    netter = netter or netting.Netter(fut, info, tag=f"vb{tag}")
    for s in syms:
        if s not in prices:
            out["skipped"].append(f"{s} no price"); continue
        u = float(book.get(s, 0.0))
        q, kind = V.decide(float(prev.get(s, 0.0)), float(keep.get(s, 0.0)), u, prices[s], eq, st.get(s, 0.0), mn.get(s, 5.0))
        out["kinds"][kind] = out["kinds"].get(kind, 0) + 1
        if kind == "skip_min":
            log_event(state, "vbo1h_skip_min", V.SLOT, symbol=s, min=mn.get(s), bar=bar_s)
        if kind in ("hold", "skip_min") or q == u:
            continue
        reducing = q == 0 or (abs(q) < abs(u) and np.sign(q) == np.sign(u))
        if u and xpos is not None and (reducing or kind == "flip"):
            x = xpos.get(s, 0.0)
            if netting.leg_missing(state, V.SLOT, s, x, u):   # vs exchange net minus other slots' legs (netting)   # closing a leg the exchange doesn't hold would OPEN one
                out["skipped"].append(f"{s} leg missing on exchange")
                log_event(state, "vbo1h_skip_missing_leg", V.SLOT, symbol=s, ledger=u, exchange=x, bar=bar_s); continue
        if not reducing:
            why = ("not listed on Demo" if s not in lst else blk or ("TradFi (crypto only)" if tradfi.is_tradfi(s, info, state) else None))
            if why:
                out["skipped"].append(f"{s} {why}"); log_event(state, "vbo1h_entry_skip", V.SLOT, symbol=s, why=why, bar=bar_s); continue

        def logf(fq, px, fee, s=s, kind=kind):
            if fq:
                side = "BUY" if fq > 0 else "SELL"
                log_event(state, "vbo1h_order", V.SLOT, symbol=s, side=side, qty=abs(fq), px=px, kind=kind, bar=bar_s)
                out["sent"].append({"symbol": s, "side": side, "qty": abs(fq), "px": px, "kind": kind})
            else:
                out["skipped"].append(f"{s} net not filled")
        netter.add(state, V.SLOT, s, q - u, prices[s], netting.ledger_cb(ex, s, on_fill=logf), kind=kind)
    sh = shadow_day(ex, W, C, bar) if bar.hour == 0 else None

    def finish():
        out["equity_after"] = round(equity(ex, prices), 2)
        ex.update(last_bar=bar_s, last=out, targets=keep, review_date=V.REVIEW_DATE, spec=V.SPEC, capital=V.CAPITAL)
        ex["history"] = (list(ex.get("history") or []) + [{"bar": bar_s, "equity_before": round(eq, 4), "equity": out["equity_after"],
                         "sent": len(out["sent"]), "tg": {s: round(w, 6) for s, w in tw.items()}}])[-24 * 14:]
        if sh:
            ex["shadow"] = (list(ex.get("shadow") or []) + [sh])[-60:]
    netter.after.append(finish)
    if own:
        netter.flush()
    return out
