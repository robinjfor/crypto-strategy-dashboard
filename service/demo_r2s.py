"""Demo slot R2S (top-10 perp trend long/short, smoothed + ADX hysteresis, <5U rule), 1,000 USDT x1 on Binance futures Demo.
DISABLED (enabled=False), NOT approved, NOT in DEMO_ACCEPTED, not in demo_candidates.CANDIDATES.
Spec: strategy-unified-3y/spec_2026-11/research_regime_v2/SPEC.md (== research_v2.r2s_W + pnl(deadband_rel=0.25) + min5_check K=1000).
This module re-implements the signal (universe, ATR/ADX/slope/vpct, hysteresis regime, 9 sub-engines, weights) and the
rebalance rule independently; parity() compares it trade-by-trade with the backtest ledger (shadow_ab/r2s_ledger.py).
Rebalance (SPEC 7+9, as coded in lib.pnl): trade iff |dw| > max(0.25|w|, 5U/equity) OR target 0 (full close, reduce-only)
OR sign(target) != sign(held) (includes opening from 0).
Live-only extra rule: exchange per-symbol MIN_NOTIONAL (BTCUSDT 100U, ETHUSDT 20U, ...): non-closing orders below it are
BLOCKED and logged (event r2s_order_blocked_min). The backtest does not model it -> parity()["exchange_min_divergence"]."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

log = logging.getLogger("demo_r2s")
SLOT = "demo_r2s_top10_1x"
CAPITAL, LEVERAGE = 1000.0, 1.0
BAND, MIN_ORDER, SLOTS, SEPS = 0.25, 5.0, 10, 0.002
INNER = [(N, k) for N in (20, 30, 40) for k in (2.0, 2.5, 3.0)]
EXCHANGE_MIN_DEFAULT = {"BTCUSDT": 100.0, "ETHUSDT": 20.0}


def parse_cfg(key: str) -> dict:
    a = key.split("_")
    return dict(th=float(a[0][2:]), h=float(a[1][1:]), hv=float(a[2][2:]), L=float(a[3][1:]))


# ---------------- signal
def universe(C: pd.DataFrame, QV: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    age = C.notna().cumsum()
    liq = QV.rolling(30, min_periods=30).mean().where(age >= 60)
    return liq.rank(axis=1, ascending=False, method="first") <= n


def indicators(c: pd.DataFrame, h: pd.DataFrame, l: pd.DataFrame) -> dict:
    pc = c.shift(1)
    tr = pd.DataFrame(np.maximum(np.maximum((h - l).values, (h - pc).abs().values), (l - pc).abs().values), index=c.index, columns=c.columns)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0); mdm = dn.where((dn > up) & (dn > 0), 0.0)
    pdi = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / atr; mdi = 100 * mdm.ewm(alpha=1 / 14, adjust=False).mean() / atr
    adx = (100 * (pdi - mdi).abs() / (pdi + mdi)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    sma = c.rolling(50).mean()
    vpct = np.log(c / pc).rolling(20).std().rolling(250, min_periods=120).rank(pct=True)
    return dict(c=c.values, h=h, l=l, atr=atr.values, adx=adx.values, slope=(sma / sma.shift(10) - 1).values, hv=(vpct > 0.9).values)


def regime(adx: np.ndarray, slope: np.ndarray, th: float, h: float) -> np.ndarray:
    T, n = adx.shape; out = np.zeros((T, n), dtype=np.int64)
    for j in range(n):
        on, d = False, 0
        for i in range(T):
            a, s = adx[i, j], slope[i, j]
            if np.isnan(a) or np.isnan(s):
                on, d = False, 0; continue
            if on and a < th - h: on = False
            elif not on and a > th + h: on = True
            if s > SEPS: d = 1
            elif s < -SEPS: d = -1
            elif d == 0: d = 1 if s > 0 else -1
            out[i, j] = d if on else 0
    return out


def sub_engine(c, atr, reg, hv, hi, lo, elig, k: float, hvp: float) -> np.ndarray:
    T, n = c.shape; out = np.zeros((T, n))
    for j in range(n):
        pos, frac, ereg, ext, ent, ea = 0, 0.0, 0, 0.0, 0.0, 1.0
        for i in range(T):
            x, a = c[i, j], atr[i, j]
            if np.isnan(x) or np.isnan(a):
                pos = 0; continue
            if pos:
                if not elig[i, j]:
                    pos = 0
                else:
                    ext = max(ext, x) if pos > 0 else min(ext, x)
                    stop = ext - pos * (k if reg[i, j] == ereg else 0.5 * k) * a
                    gain = (x - ent) * pos / ea
                    if (x - stop) * pos <= 0: pos = 0
                    elif gain >= 4.0: frac = min(frac, 0.25)
                    elif gain >= 2.0: frac = min(frac, 0.5)
            if pos == 0 and elig[i, j] and not (hv[i, j] and hvp == 0):
                r = reg[i, j]
                if r == 1 and x > hi[i, j]: pos = 1
                elif r == -1 and x < lo[i, j]: pos = -1
                if pos:
                    frac, ereg, ext, ent, ea = 1.0, r, x, x, a
            out[i, j] = pos * frac
    return out


def weights(I: dict, elig: np.ndarray, cfg: dict) -> np.ndarray:
    reg = regime(I["adx"], I["slope"], cfg["th"], cfg["h"]); acc = 0.0
    for N, k in INNER:
        hi = I["h"].rolling(N).max().shift(1).values; lo = I["l"].rolling(N).min().shift(1).values
        acc = acc + sub_engine(I["c"], I["atr"], reg, I["hv"], hi, lo, elig, k, cfg["hv"])
    return acc / len(INNER) * np.where(I["hv"], cfg["hv"], 1.0) / SLOTS * cfg["L"] * LEVERAGE


# ---------------- rebalance planner (notional space)
def plan(target_w: dict, pos_notional: dict, equity: float, minord: float = MIN_ORDER) -> list[dict]:
    """target_w: symbol -> signed weight; pos_notional: symbol -> signed current notional (USDT).
    Returns orders {symbol, notional(signed delta), abs_notional, kind: order|close|skip_min}."""
    out = []
    for s in sorted(set(target_w) | set(pos_notional)):
        t = float(target_w.get(s, 0.0)); cur = float(pos_notional.get(s, 0.0))
        tn = t * equity; d = tn - cur
        if t == 0 and cur != 0:
            out.append({"symbol": s, "notional": d, "abs_notional": abs(d), "kind": "close"}); continue
        if np.sign(t) != np.sign(cur):
            out.append({"symbol": s, "notional": d, "abs_notional": abs(d), "kind": "order"}); continue
        if t == 0 or abs(d) <= BAND * abs(tn):
            continue
        if abs(d) <= minord:
            out.append({"symbol": s, "notional": d, "abs_notional": abs(d), "kind": "skip_min"}); continue
        out.append({"symbol": s, "notional": d, "abs_notional": abs(d), "kind": "order"})
    return out


# ---------------- exchange per-symbol min notional (live only)
def exchange_min(info: dict | None, sym: str) -> float:
    for s in (info or {}).get("symbols") or []:
        if s.get("symbol") == sym:
            for f in s.get("filters") or []:
                if f.get("filterType") == "MIN_NOTIONAL":
                    return float(f.get("notional") or f.get("minNotional") or MIN_ORDER)
    return EXCHANGE_MIN_DEFAULT.get(sym, MIN_ORDER)


def exchange_filter(orders: list[dict], mins: dict) -> tuple[list[dict], list[dict]]:
    send, blocked = [], []
    for o in orders:
        if o["kind"] == "skip_min":
            continue
        mn = float(mins.get(o["symbol"], MIN_ORDER))
        if o["kind"] != "close" and o["abs_notional"] < mn:
            blocked.append({**o, "min": mn}); continue
        send.append({**o, "reduce_only": o["kind"] == "close"})
    return send, blocked


def log_blocked(state: dict, blocked: list[dict], bar: str | None) -> None:
    from guards import log_event
    for b in blocked:
        log_event(state, "r2s_order_blocked_min", SLOT, symbol=b["symbol"], notional=round(b["abs_notional"], 4), min=b["min"], bar=bar)
        log.warning("demo_r2s blocked %s %.2fU < min %.0fU", b["symbol"], b["abs_notional"], b["min"])


def min_divergence(ledger_orders, mins: dict) -> dict:
    """Backtest orders (non-closing) with 5U <= |notional| < exchange symbol min -> sent in backtest, blocked live."""
    d = {"n": 0, "lt5_opens": 0, "by_symbol": {}, "examples": []}
    for day, sym, new_w, pre_w, notl in ledger_orders:
        if new_w == 0:
            continue
        mn = float(mins.get(sym, MIN_ORDER)); a = abs(notl)
        if a < MIN_ORDER:
            d["lt5_opens"] += 1
        if a < mn:
            if a >= MIN_ORDER:
                d["n"] += 1
            d["by_symbol"][sym] = d["by_symbol"].get(sym, 0) + 1
            if len(d["examples"]) < 10:
                d["examples"].append(f"{day} {sym} {a:.2f}U<{mn:g}U")
    d["blocked_total"] = sum(d["by_symbol"].values())
    return d


# ---------------- trade-by-trade parity vs the backtest ledger
def parity(led: dict, C: pd.DataFrame, H: pd.DataFrame, L: pd.DataFrame, QV: pd.DataFrame, tol: float = 1e-9) -> dict:
    elig_all = universe(C, QV)
    ev = [c for c in C.columns if elig_all[c].any()]
    I = indicators(C[ev], H[ev], L[ev]); elig = elig_all[ev].values
    cidx = [str(d.date()) for d in C.index]; pos = {d: i for i, d in enumerate(cidx)}
    cache = {}
    W = np.zeros((len(led["idx"]), len(led["cols"]))); colpos = {c: j for j, c in enumerate(led["cols"])}
    for a, e, key in led["picks"]:
        if key not in cache: cache[key] = weights(I, elig, parse_cfg(key))
        for r, d in enumerate(led["idx"]):
            dt = pd.Timestamp(d) + pd.Timedelta(days=1)
            if pd.Timestamp(a) <= dt <= pd.Timestamp(e) and not W[r].any():
                row = cache[key][pos[d]]
                for j, s in enumerate(ev): W[r, colpos[s]] = row[j]
    sig_bad = int((np.abs(W - led["W"]) > 1e-12).any(axis=1).sum())
    exp = {(o[0], o[1]): o for o in led["orders"]}
    got = {}; skips = 0; bad = []
    for r, d in enumerate(led["idx"]):
        eq = float(led["eqo"][r]); day = str((pd.Timestamp(d) + pd.Timedelta(days=1)).date())
        tw = {s: W[r, j] for j, s in enumerate(led["cols"]) if W[r, j] != 0}
        cur = {s: led["pre"][r, j] * eq for j, s in enumerate(led["cols"]) if led["pre"][r, j] != 0}
        for o in plan(tw, cur, eq, MIN_ORDER):
            if o["kind"] == "skip_min": skips += 1; continue
            got[(day, o["symbol"])] = o
    for k in set(exp) | set(got):
        if k not in exp or k not in got or abs(got[k]["notional"] - exp[k][4]) > tol * max(1.0, abs(exp[k][4])) \
                or (got[k]["kind"] == "close") != (exp[k][2] == 0):
            bad.append(k)
    return {"days": len(led["idx"]), "ledger_orders": len(exp), "orders": len(got), "signal_day_mismatches": sig_bad,
            "skipped_lt5": skips, "full_closes": sum(1 for o in got.values() if o["kind"] == "close"),
            "mismatches": len(bad), "first_mismatch": sorted(bad)[:3], "ledger_sharpe": led.get("sharpe"),
            "pass": len(bad) == 0 and sig_bad == 0 and len(got) == len(exp) > 0,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ---------------- gates
def active(state: dict) -> tuple[bool, str]:
    from demo_candidates import accepted
    g = (state.get("demo_candidates") or {}).get(SLOT) or {}
    for k, why in (("enabled", "未啟用"), ("approved", "未批准"), ("parity_pass", "對帳未通過"), ("cloud_ordering_ok", "下單測試未通過")):
        if not g.get(k):
            return False, why
    if not accepted(SLOT):
        return False, "未驗收"
    return True, ""


def send_orders(fut, info, orders: list[dict], prices: dict, tag: str) -> list[dict]:
    from futures_client import round_qty_futures
    res = []
    for o in orders:
        px = float(prices[o["symbol"]])
        qty = abs(o["delta_qty"]) if o.get("delta_qty") is not None else abs(o["notional"]) / px
        q = round_qty_futures(info, o["symbol"], qty)
        if q <= 0:
            res.append({**o, "status": "below_step"}); continue
        kw = {"reduceOnly": "true"} if o.get("reduce_only") else {}
        coid = f"r2s-{o['symbol'][:12]}-{tag}"[:36]
        r = fut.new_order(symbol=o["symbol"], side="BUY" if o["notional"] > 0 else "SELL", type="MARKET", quantity=q, newClientOrderId=coid, **kw)
        res.append({**o, "qty": q, "status": r.get("status"), "orderId": r.get("orderId"), "coid": coid})
    return res


# ---------------- cloud order test: tiny real Demo fills, close all, confirm flat
BLOCK_SYMS = ("BTCUSDT", "ETHUSDT")            # 6U order must be blocked (min 100U / 20U); nothing is sent
TEST_SYMS = ("XRPUSDT", "ADAUSDT", "LINKUSDT")  # disjoint from demo_e5's test symbols


def _amt(fut, sym):
    return float(next((p.get("positionAmt") for p in fut.position_risk() if p["symbol"] == sym), 0) or 0)


def order_test(fut, state: dict | None = None) -> dict:
    import time
    st = state if state is not None else {}
    info = fut.exchange_info(); tag = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    out = {"orders": 0, "filled": 0, "blocked_min": 0, "errors": [], "symbols": {}}
    held = {p["symbol"] for p in fut.position_risk() if float(p.get("positionAmt") or 0) != 0}
    for sym in BLOCK_SYMS:
        mn = exchange_min(info, sym); px = float(fut.mark_price(sym))
        send, blk = exchange_filter([{"symbol": sym, "notional": 6.0, "abs_notional": 6.0, "kind": "order"}], {sym: mn})
        log_blocked(st, blk, "order_test"); out["blocked_min"] += len(blk)
        out["symbols"][sym] = {"min": mn, "planned_6U": "blocked" if blk and not send else "NOT_BLOCKED"}
        if send: out["errors"].append(f"{sym}: 6U not blocked (min {mn})")
    for sym in TEST_SYMS:
        rec = {}
        if sym in held:
            out["symbols"][sym] = {"skipped": "account already holds it"}; continue
        try:
            mn = exchange_min(info, sym); px = float(fut.mark_price(sym)); tgt = max(mn, MIN_ORDER) * 1.3
            if tgt > 40: raise RuntimeError(f"min {mn} too large for a small test")
            o = plan({sym: tgt / CAPITAL}, {}, CAPITAL)
            send, blk = exchange_filter(o, {sym: mn})
            r1 = send_orders(fut, info, send, {sym: px}, tag + "o"); out["orders"] += len(r1)
            time.sleep(1.0); amt = _amt(fut, sym)
            rec["open"] = {"notional": round(tgt, 2), "qty": r1[0].get("qty") if r1 else None, "status": r1[0]["status"] if r1 else None, "pos_after": amt}
            if amt > 0: out["filled"] += 1
            cl = plan({}, {sym: amt * px}, CAPITAL)
            cs, _ = exchange_filter(cl, {sym: mn})
            r2 = send_orders(fut, info, [{**c, "delta_qty": -amt} for c in cs], {sym: px}, tag + "c") if amt else []
            out["orders"] += len(r2); time.sleep(1.0); amt2 = _amt(fut, sym)
            if r2 and amt2 == 0: out["filled"] += 1
            rec["close"] = {"status": r2[0]["status"] if r2 else None, "reduce_only": bool(cs and cs[0]["reduce_only"]), "pos_after": amt2}
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{sym}: {str(e)[:120]}")
        out["symbols"][sym] = rec
    fin = {r["symbol"]: float(r.get("positionAmt") or 0) for r in fut.position_risk() if r["symbol"] in TEST_SYMS and r["symbol"] not in held}
    out["final_positions"] = fin
    out["blocked_events"] = [e for e in (st.get("control_log") or []) if e.get("action") == "r2s_order_blocked_min"]
    out["flat"] = all(v == 0 for v in fin.values())
    out["pass"] = out["flat"] and not out["errors"] and out["filled"] == out["orders"] and out["orders"] > 0 and out["blocked_min"] == len(BLOCK_SYMS)
    out["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out
