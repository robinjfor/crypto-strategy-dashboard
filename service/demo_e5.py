"""Demo slot E-min5 (rule-finalized E): 80% U1 tv0.40 x1.75 (BTCUSDT/ETHUSDT perps) + 20% S5 long/short x1.75,
5,000 USDT on Binance futures Demo. DISABLED (enabled=False), NOT approved, NOT in DEMO_ACCEPTED.
Spec: research_1022/shadow_AB/ledger_e_min5.py + E_MINORDER.md (MIN_ORDER=5U: an adjustment < 5U is not sent and
the leg keeps its position; target 0 -> always close in full, reduce-only, even if the residual is < 5U).
Live-only extra rule: the exchange per-symbol MIN_NOTIONAL (e.g. BTCUSDT 100U, ETHUSDT 20U) is enforced on top;
a non-closing order below it is BLOCKED and logged (event e5_order_blocked_min). The ledger does NOT model this
(it only uses 5U) -> see parity()["exchange_min_divergence"]; live will diverge from the backtest on those orders.
The planner functions below are the decision logic; parity() checks them trade-by-trade vs the ledger capture
(shadow_ab/e5_capture.py). Gates (state["demo_candidates"][SLOT]): cloud_ordering_ok, parity_pass, DEMO_ACCEPTED."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger("demo_e5")
SLOT = "demo_e5_min5_x175"
CAPITAL = 5000.0
W_U1, LV, CAP, BAND, MIN_ORDER = 0.8, 1.75, 3.0, 0.25, 5.0
U1_SYMS = ("BTCUSDT", "ETHUSDT")
# fallback when exchangeInfo is unavailable; live always reads the real filter
EXCHANGE_MIN_DEFAULT = {"BTCUSDT": 100.0, "ETHUSDT": 20.0}


# ---------------- planner (must equal ledger_e_min5.sim rebalance rules)
def plan_lt(r: np.ndarray, m: float, lv: float = LV, cap: float = CAP) -> float:
    if len(r) >= 20:
        mu = r.mean(); sd = math.sqrt(((r - mu) ** 2).sum() / (len(r) - 1)) * math.sqrt(365)
        return min(cap, 0.40 * lv * m / max(sd, 0.05))
    return 1.0


def plan_u1(uu, w1, lt: float, m: float, o2, eu: float, minord: float = MIN_ORDER):
    """Returns (new units, orders[(j, delta_units, notional, kind)]) kind: order|skip_min|close."""
    uu = np.array(uu, dtype=float).copy(); orders = []
    for j in range(2):
        wt = w1[j] * lt if m > 0 else 0.0
        wc = uu[j] * o2[j] / eu if eu > 0 else 0.0
        tr = (uu[j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > BAND * abs(wt))
        if tr and eu > 0:
            nu = wt * eu / o2[j]; d = nu - uu[j]; notl = abs(d) * o2[j]
            if wt != 0 and notl < minord:
                orders.append((j, d, notl, "skip_min")); continue
            if notl > 0:
                orders.append((j, d, notl, "close" if wt == 0 else "order"))
            uu[j] = nu
    return uu, orders


def plan_s5(u5, ov, ws, dbs, es: float, lv: float, mm: float, minord: float = MIN_ORDER):
    u5 = np.array(u5, dtype=float).copy(); orders = []
    for c in range(len(u5)):
        if np.isnan(ov[c]): continue
        tw = ws[c] * lv * mm; db = dbs[c] * lv * mm
        cw = u5[c] * ov[c] / es if es > 0 else 0.0
        tr = (abs(tw - cw) > db) or (tw == 0 and cw != 0) or (np.sign(tw) != np.sign(cw))
        if not tr: continue
        nu = tw * es / ov[c] if tw != 0 else 0.0; d = nu - u5[c]; notl = abs(d) * ov[c]
        if notl == 0: continue
        if tw != 0 and notl < minord:
            orders.append((c, d, notl, "skip_min")); continue
        orders.append((c, d, notl, "close" if tw == 0 else "order")); u5[c] = nu
    return u5, orders


# ---------------- exchange per-symbol min notional (live only; not in the ledger)
def exchange_min(info: dict | None, sym: str) -> float:
    if info:
        for s in info.get("symbols") or []:
            if s.get("symbol") == sym:
                for f in s.get("filters") or []:
                    if f.get("filterType") == "MIN_NOTIONAL":
                        return float(f.get("notional") or f.get("minNotional") or 5.0)
    return EXCHANGE_MIN_DEFAULT.get(sym, MIN_ORDER)


def exchange_filter(orders: list[dict], mins: dict) -> tuple[list[dict], list[dict]]:
    """orders: {symbol, delta_qty, notional, kind}. Full closes go reduce-only (exempt from MIN_NOTIONAL);
    any other order below the symbol min is blocked."""
    send, blocked = [], []
    for o in orders:
        if o["kind"] == "skip_min":
            continue
        mn = float(mins.get(o["symbol"], MIN_ORDER))
        if o["kind"] != "close" and o["notional"] < mn:
            blocked.append({**o, "min": mn}); continue
        send.append({**o, "reduce_only": o["kind"] == "close"})
    return send, blocked


def log_blocked(state: dict, blocked: list[dict], bar: str | None) -> None:
    from guards import log_event
    for b in blocked:
        log_event(state, "e5_order_blocked_min", SLOT, symbol=b["symbol"], notional=round(b["notional"], 4), min=b["min"], bar=bar)
        log.warning("demo_e5 blocked %s %.2fU < min %.0fU", b["symbol"], b["notional"], b["min"])


# ---------------- trade-by-trade parity vs ledger capture
def parity(cap: dict, mins: dict | None = None) -> dict:
    cols, idx = cap["cols"], cap["idx"]; iB, iE = cap["iB"], cap["iE"]
    days = orders = bad = skips = closes = 0
    first_bad = None; pend = None
    div = {"n": 0, "by_symbol": {}, "examples": []}
    mins = mins or {}
    for r in cap["rec"]:
        t = r[0]
        if t == "u1":
            _, i, rw, m, uu, o2, eu, w1, mo = r
            lt = plan_lt(rw, m); got, od = plan_u1(uu, w1, lt, m, o2, eu, mo); pend = ("u1", i, uu, got, od, lt, [cols[iB], cols[iE]])
        elif t == "u1post":
            _, i, exp, lt_exp = r
            _, i0, pre, got, od, lt, syms = pend
            days += 1
            if not (np.array_equal(got, exp) and lt == lt_exp):
                bad += 1; first_bad = first_bad or (idx[i], "u1")
            orders, skips, closes = _acc(od, syms, pre, exp, orders, skips, closes, mins, div, idx[i])
        elif t == "s5":
            _, i, u5, es, k, mm, ov, ws, dbs, lv, mo = r
            got, od = plan_s5(u5, ov, ws, dbs, es, lv, mm, mo); pend = ("s5", i, u5, got, od, None, cols)
        elif t == "s5post":
            _, i, exp = r
            _, i0, pre, got, od, _, syms = pend
            if not np.array_equal(got, exp):
                bad += 1; first_bad = first_bad or (idx[i], "s5")
            orders, skips, closes = _acc(od, syms, pre, exp, orders, skips, closes, mins, div, idx[i])
    led_orders = int(cap["stats"][0])
    return {"days": days, "orders": orders, "ledger_orders": led_orders, "skipped_lt5": skips, "full_closes": closes,
            "mismatches": bad, "first_mismatch": first_bad, "capture_vs_jit_max_rel": cap.get("capture_vs_jit_max_rel"),
            "exchange_min_divergence": div,
            "pass": days > 0 and bad == 0 and orders == led_orders and (cap.get("capture_vs_jit_max_rel") or 0) < 1e-9,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _acc(od, syms, pre, exp, orders, skips, closes, mins, div, day):
    for c, d, notl, kind in od:
        if kind == "skip_min":
            skips += 1; continue
        orders += 1
        if kind == "close":
            closes += 1; continue
        mn = float(mins.get(syms[c], MIN_ORDER))
        if notl < mn:
            div["n"] += 1; div["by_symbol"][syms[c]] = div["by_symbol"].get(syms[c], 0) + 1
            if len(div["examples"]) < 10:
                div["examples"].append(f"{day} {syms[c]} {notl:.2f}U<{mn:g}U")
    return orders, skips, closes


# ---------------- live order plan from targets + real book (used once enabled)
def plan_live(targets: dict, book: dict, prices: dict, equity: float, m: float = 1.0, lt: float = 1.0) -> list[dict]:
    cols = list(targets.get("cols") or [])
    eu, es = W_U1 * equity, (1 - W_U1) * equity
    uu = np.array([float(book.get(s, 0.0)) for s in U1_SYMS]); o2 = np.array([float(prices.get(s) or np.nan) for s in U1_SYMS])
    _, od1 = plan_u1(uu, targets["w1"], lt, m, o2, eu)
    out = [{"symbol": U1_SYMS[j], "delta_qty": d, "notional": n, "kind": k, "sleeve": "u1"} for j, d, n, k in od1]
    syms = sorted(set(targets.get("ws") or {}) | {s for s in book if s not in U1_SYMS})
    u5 = np.array([float(book.get(s, 0.0)) for s in syms]); ov = np.array([float(prices.get(s) or np.nan) for s in syms])
    ws = np.array([float((targets.get("ws") or {}).get(s, 0.0)) for s in syms]); dbs = np.array([float((targets.get("dbs") or {}).get(s, 0.0)) for s in syms])
    _, od5 = plan_s5(u5, ov, ws, dbs, es, LV, m)
    out += [{"symbol": syms[c], "delta_qty": d, "notional": n, "kind": k, "sleeve": "s5"} for c, d, n, k in od5]
    _ = cols
    return out


def active(state: dict) -> tuple[bool, str]:
    from demo_candidates import accepted
    g = (state.get("demo_candidates") or {}).get(SLOT) or {}
    for k, why in (("enabled", "未啟用"), ("approved", "未批准"), ("parity_pass", "對帳未通過"), ("cloud_ordering_ok", "下單測試未通過")):
        if not g.get(k):
            return False, why
    if not accepted(SLOT):
        return False, "未驗收"
    return True, ""


def send_orders(fut, info, orders: list[dict], tag: str) -> list[dict]:
    from futures_client import round_qty_futures
    res = []
    for o in orders:
        q = round_qty_futures(info, o["symbol"], abs(o["delta_qty"]))
        if q <= 0:
            res.append({**o, "status": "below_step"}); continue
        kw = {"reduceOnly": "true"} if o.get("reduce_only") else {}
        coid = f"e5-{o['symbol'][:12]}-{tag}"[:36]
        r = fut.new_order(symbol=o["symbol"], side="BUY" if o["delta_qty"] > 0 else "SELL", type="MARKET", quantity=q, newClientOrderId=coid, **kw)
        res.append({**o, "qty": q, "status": r.get("status"), "orderId": r.get("orderId"), "coid": coid})
    return res


# ---------------- cloud order test: small real Demo fills, then close all, confirm flat
TEST_SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT")


def order_test(fut, state: dict | None = None) -> dict:
    """Per symbol (skipped if the Demo account already holds it, so other slots are never touched):
    1) planned 6U order -> must be BLOCKED by the exchange-min filter when the symbol min > 6U (logged);
    2) open the smallest valid order (max(symbol min, 5U) x1.2) through send_orders; read back the fill;
    3) close it with the slot's full-close path (reduce-only); 4) confirm positionAmt == 0."""
    import time
    from futures_client import round_qty_futures
    info = fut.exchange_info()
    held = {r["symbol"]: float(r.get("positionAmt") or 0) for r in fut.position_risk() if float(r.get("positionAmt") or 0) != 0}
    out = {"symbols": {}, "orders": 0, "filled": 0, "blocked_min": 0, "errors": []}
    st = state if state is not None else {}
    tag = datetime.now(timezone.utc).strftime("%m%d%H%M")
    for sym in TEST_SYMS:
        rec = {}
        if sym in held:
            rec["skipped"] = f"account already holds {held[sym]}"; out["symbols"][sym] = rec; continue
        try:
            px = float(fut.mark_price(sym)); mn = exchange_min(info, sym); rec["min"] = mn
            _, blk = exchange_filter([{"symbol": sym, "delta_qty": 6.0 / px, "notional": 6.0, "kind": "order"}], {sym: mn})
            rec["blocked_6U"] = bool(blk)
            if blk:
                out["blocked_min"] += 1; log_blocked(st, blk, "order_test")
            notl = max(mn, MIN_ORDER) * 1.2
            q = 0.0
            for mult in (1.2, 1.5, 2.0, 3.0):
                q = round_qty_futures(info, sym, max(mn, MIN_ORDER) * mult / px)
                if q * px >= max(mn, MIN_ORDER) * 1.05:
                    break
            if q * px < max(mn, MIN_ORDER) or q * px > 400:
                raise RuntimeError(f"no valid small test qty ({q})")
            send, blk2 = exchange_filter([{"symbol": sym, "delta_qty": q, "notional": q * px, "kind": "order"}], {sym: mn})
            r1 = send_orders(fut, info, send, tag + "o"); out["orders"] += len(r1)
            time.sleep(1.0)
            amt = float(next((p.get("positionAmt") for p in fut.position_risk() if p["symbol"] == sym), 0) or 0)
            rec["open"] = {"qty": q, "notional": round(q * px, 2), "status": r1[0]["status"] if r1 else None, "pos_after": amt}
            if amt > 0: out["filled"] += 1
            cl, _ = exchange_filter([{"symbol": sym, "delta_qty": -amt, "notional": abs(amt) * px, "kind": "close"}], {sym: mn})
            r2 = send_orders(fut, info, cl, tag + "c") if amt else []; out["orders"] += len(r2)
            time.sleep(1.0)
            amt2 = float(next((p.get("positionAmt") for p in fut.position_risk() if p["symbol"] == sym), 0) or 0)
            if r2 and amt2 == 0: out["filled"] += 1
            rec["close"] = {"status": r2[0]["status"] if r2 else None, "pos_after": amt2}
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{sym}: {str(e)[:120]}")
        out["symbols"][sym] = rec
    fin = {r["symbol"]: float(r.get("positionAmt") or 0) for r in fut.position_risk() if r["symbol"] in TEST_SYMS and r["symbol"] not in held}
    out["blocked_events"] = st.get("control_log") or []
    out["flat"] = all(v == 0 for v in fin.values())
    out["pass"] = out["flat"] and not out["errors"] and out["filled"] == out["orders"] and out["orders"] > 0
    out["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out
