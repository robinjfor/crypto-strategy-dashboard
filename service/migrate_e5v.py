"""One-shot, idempotent migration e5v_1026 (runs inside the Cloud Run reconcile job; Binance is 451 from the box).
1) APRUSDT investigation: the E5 virtual ledger holds a position the Demo exchange no longer has (no fills / open orders
   seen by the netter). Read exchangeInfo, positionRisk, forceOrders, income (all types), userTrades + allOrders and
   classify the cause; then zero the ledger leg with an audited ledger_adjustments entry (cause, close px, PnL).
2) Transfer the old E5 slot's virtual ledger (cash, book, funding_since, last_bar) into the new variant slot, so the
   per-symbol virtual net (and therefore the exchange net position) is unchanged. Old ledger -> empty. Approval of the
   old slot is NOT touched (pausing is done via /control/pause_slot; never revoke).
Everything lands in one store.mutate (atomic, generation-checked); state["migrations"]["e5v_1026"] makes it run once."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("migrate_e5v")
MIG = "e5v_1026"
OUR_PREFIX = ("e5-", "net-", "r2s-", "fa-")
LOOKBACK_MS = 30 * 86400 * 1000


def _safe(f, default=None):
    try:
        return f()
    except Exception as e:  # noqa: BLE001
        return default if default is not None else {"error": f"{type(e).__name__}: {e}"[:300]}


def investigate(fut, sym: str, q_ledger: float, known_coids: set[str] | None = None, now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000); start = now_ms - LOOKBACK_MS
    known = known_coids or set()
    info = _safe(lambda: fut.exchange_info(), {})
    meta = next((x for x in (info.get("symbols") or []) if x.get("symbol") == sym), None) if isinstance(info, dict) else None
    pos = _safe(lambda: fut.position_risk(sym), [])
    amt = sum(float(p.get("positionAmt") or 0) for p in pos) if isinstance(pos, list) else None
    inc = []
    for d0 in range(start, now_ms, 7 * 86400 * 1000):   # income endpoint: <=7d windows, all types
        r = _safe(lambda: fut._signed("GET", "/fapi/v1/income", {"symbol": sym, "startTime": d0, "endTime": min(d0 + 7 * 86400 * 1000, now_ms), "limit": 1000}), [])
        inc += r if isinstance(r, list) else []
    trades = []
    for d0 in range(start, now_ms, 7 * 86400 * 1000):
        r = _safe(lambda: fut._signed("GET", "/fapi/v1/userTrades", {"symbol": sym, "startTime": d0, "endTime": min(d0 + 7 * 86400 * 1000 - 1, now_ms), "limit": 1000}), [])
        trades += r if isinstance(r, list) else []
    orders = _safe(lambda: fut.all_orders(sym, start_ms=now_ms - 7 * 86400 * 1000, limit=1000), [])
    orders = orders if isinstance(orders, list) else []
    force = _safe(lambda: fut._signed("GET", "/fapi/v1/forceOrders", {"symbol": sym, "startTime": start, "limit": 100}), [])
    force = force if isinstance(force, list) else []
    by_oid = {o.get("orderId"): o for o in orders}
    by_oid.update({o.get("orderId"): o for o in force})
    types: dict[str, float] = {}
    for x in inc:
        types[x.get("incomeType")] = types.get(x.get("incomeType"), 0.0) + float(x.get("income") or 0)
    # closing trades: side opposite to the ledger leg (ledger short -> BUY closes)
    close_side = "BUY" if q_ledger < 0 else "SELL"
    closes = [t for t in trades if t.get("side") == close_side]
    tagged = []
    for t in closes:
        o = by_oid.get(t.get("orderId")) or {}
        coid = o.get("clientOrderId") or ""
        tagged.append({"time": t.get("time"), "qty": float(t.get("qty") or 0), "price": float(t.get("price") or 0), "realizedPnl": float(t.get("realizedPnl") or 0),
                       "commission": float(t.get("commission") or 0), "orderId": t.get("orderId"), "clientOrderId": coid, "autoCloseType": o.get("autoCloseType")})
    status = (meta or {}).get("status")
    cause, detail = "unknown", ""
    if any((o.get("autoCloseType") == "ADL") or str(o.get("clientOrderId", "")).startswith("adl_autoclose") for o in force + orders) or any(str(t["clientOrderId"]).startswith("adl_autoclose") for t in tagged):
        cause = "ADL"
    elif any((o.get("autoCloseType") == "LIQUIDATION") or str(o.get("clientOrderId", "")).startswith("autoclose-") for o in force + orders) or types.get("INSURANCE_CLEAR"):
        cause = "liquidation"
    elif meta is None or status not in (None, "TRADING") or "DELIVERED_SETTELMENT" in types or "DELIVERED_SETTLEMENT" in types:
        cause = "delist_settlement"; detail = f"status={status} deliveryDate={(meta or {}).get('deliveryDate')}"
    elif tagged and any(not t["clientOrderId"].startswith(OUR_PREFIX) for t in tagged):
        cause = "outside_close"
    elif tagged and not all(t["clientOrderId"] in known for t in tagged):
        cause = "missed_fill"
    q_close = sum(t["qty"] for t in tagged)
    px = (sum(t["qty"] * t["price"] for t in tagged) / q_close) if q_close > 0 else None
    if px is None:  # settlement / no trade: last kline close before delisting, then mark
        k = _safe(lambda: fut._public("/fapi/v1/klines", {"symbol": sym, "interval": "1h", "limit": 1}), [])
        px = float(k[-1][4]) if isinstance(k, list) and k else None
        if px is None:
            m = _safe(lambda: fut.mark_price(sym), None)
            px = m if isinstance(m, float) else None
    return {"symbol": sym, "ledger_qty": q_ledger, "exchange_position": amt, "status": status, "delivery_date": (meta or {}).get("deliveryDate"),
            "listed": meta is not None, "income_by_type": types, "n_trades_30d": len(trades), "closing_trades": tagged[-20:], "force_orders": force[-10:],
            "n_orders_7d": len(orders), "cause": cause, "detail": detail, "close_px": px,
            "exchange_realized_pnl": round(sum(t["realizedPnl"] for t in tagged), 6), "fees": round(sum(t["commission"] for t in tagged), 6)}


def apply(state: dict, inv: dict | None, new_key: str, old_key: str, new_slot: str, old_slot: str, at: str) -> dict:
    """Pure state transform (tested). Returns the audit record."""
    import netting
    mig = state.setdefault("migrations", {})
    if (mig.get(MIG) or {}).get("done"):
        return mig[MIG]
    before = netting.virtual_net(state)
    old = dict(state.get(old_key) or {}); book = dict(old.get("book") or {}); cash = float(old.get("cash") or 0.0)
    adjs = state.setdefault("ledger_adjustments", [])
    rec = {"done": True, "at": at, "from": old_slot, "to": new_slot}
    if inv and abs(float(book.get(inv["symbol"], 0.0))) > 0 and not inv.get("exchange_position"):
        sym = inv["symbol"]; q = float(book[sym]); px = inv.get("close_px")
        dcash = (q * px - float(inv.get("fees") or 0)) if px else 0.0
        last_m = float(((old.get("last_run") or {}).get("result") or {}).get("marks", {}).get(sym) or 0) or None
        adj = {"at": at, "id": f"{MIG}-{sym}", "slot": old_slot, "ledger": old_key, "symbol": sym, "qty_before": q, "qty_after": 0.0,
               "cash_delta": round(dcash, 6), "close_px": px, "cause": inv.get("cause"), "detail": inv.get("detail"),
               "exchange_realized_pnl": inv.get("exchange_realized_pnl"), "fees": inv.get("fees"),
               "pnl_vs_last_mark": round(q * (px - last_m), 6) if (px and last_m) else None, "actor": "自動交易專家", "evidence": inv}
        book.pop(sym, None); cash += dcash
        adjs.append(adj); rec["apr_adjustment"] = {k: v for k, v in adj.items() if k != "evidence"}
        from guards import log_event
        log_event(state, "ledger_adjustment", old_slot, symbol=sym, qty_before=q, cash_delta=adj["cash_delta"], cause=adj["cause"], actor="自動交易專家")
    new = {"cash": cash, "book": book, "funding_since": old.get("funding_since"), "last_bar": old.get("last_bar"), "history": [],
           "transferred_from": {"slot": old_slot, "at": at, "cash": cash, "book": dict(book)}}
    if old.get("last_run"):
        new["last_run"] = old["last_run"]
    state[new_key] = new
    state[old_key] = {**old, "cash": 0.0, "book": {}, "transferred_to": {"slot": new_slot, "at": at}}
    after = netting.virtual_net(state)
    syms = set(before) | set(after)
    diff = {s: [before.get(s, 0.0), after.get(s, 0.0)] for s in syms if abs(before.get(s, 0.0) - after.get(s, 0.0)) > 1e-12}
    rec["net_unchanged"] = set(diff) <= ({inv["symbol"]} if inv else set())
    rec["net_diff"] = diff
    adjs.append({"at": at, "id": f"{MIG}-transfer", "kind": "slot_transfer", "from": old_key, "to": new_key, "cash": cash, "book": dict(book), "actor": "自動交易專家"})
    state["ledger_adjustments"] = adjs[-200:]
    from guards import log_event
    log_event(state, "ledger_transfer", new_slot, from_slot=old_slot, cash=round(cash, 4), n_positions=len(book), actor="自動交易專家")
    mig[MIG] = rec
    return rec


def run(store, fut=None) -> dict | None:
    import demo_e5, demo_e5v
    s0 = store.load()
    if ((s0.get("migrations") or {}).get(MIG) or {}).get("done"):
        return None
    if fut is None:
        from futures_client import FuturesDemoClient
        fut = FuturesDemoClient()
    old = s0.get("demo_e5_exec") or {}
    inv = None
    q = float((old.get("book") or {}).get("APRUSDT") or 0.0)
    if q:
        inv = investigate(fut, "APRUSDT", q)
        log.info("apr_investigation %s", {k: v for k, v in inv.items() if k not in ("closing_trades", "force_orders")})
        if inv.get("exchange_position"):
            log.warning("APRUSDT still on exchange (%s); no adjustment", inv["exchange_position"])
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {}

    def mut(s):
        out["rec"] = apply(s, inv, demo_e5v.STATE_KEY, "demo_e5_exec", demo_e5v.SLOT, demo_e5.SLOT, at)
        return s
    store.mutate(mut)
    log.info("migration %s %s", MIG, out.get("rec"))
    return out.get("rec")


FIXES = {"us_1026": ("USUSDT",)}   # later one-shot fixes: same investigation + audited zeroing, on the variant ledger


def apply_symbol_fix(state: dict, inv: dict, key: str, slot: str, mig_id: str, at: str) -> dict:
    mig = state.setdefault("migrations", {})
    if (mig.get(mig_id) or {}).get("done"):
        return mig[mig_id]
    led = dict(state.get(key) or {}); book = dict(led.get("book") or {}); sym = inv["symbol"]
    q = float(book.get(sym) or 0.0); rec = {"done": True, "at": at, "symbol": sym}
    if not q or inv.get("exchange_position"):
        rec["skipped"] = "ledger flat" if not q else f"exchange still holds {inv.get('exchange_position')}"
        mig[mig_id] = rec; return rec
    px = inv.get("close_px"); dcash = (q * px - float(inv.get("fees") or 0)) if px else 0.0
    adj = {"at": at, "id": f"{mig_id}-{sym}", "slot": slot, "ledger": key, "symbol": sym, "qty_before": q, "qty_after": 0.0,
           "cash_delta": round(dcash, 6), "close_px": px, "cause": inv.get("cause"), "detail": inv.get("detail"),
           "exchange_realized_pnl": inv.get("exchange_realized_pnl"), "fees": inv.get("fees"), "actor": "自動交易專家", "evidence": inv}
    book.pop(sym, None); led["book"] = book; led["cash"] = float(led.get("cash") or 0.0) + dcash; state[key] = led
    state["ledger_adjustments"] = (list(state.get("ledger_adjustments") or []) + [adj])[-200:]
    from guards import log_event
    log_event(state, "ledger_adjustment", slot, symbol=sym, qty_before=q, cash_delta=adj["cash_delta"], cause=adj["cause"], actor="自動交易專家")
    rec["adjustment"] = {k: v for k, v in adj.items() if k != "evidence"}
    mig[mig_id] = rec
    return rec


def run_fixes(store, fut=None) -> list:
    import demo_e5v
    s0 = store.load(); out = []
    if not ((s0.get("migrations") or {}).get(MIG) or {}).get("done"):
        return out
    for mid, syms in FIXES.items():
        if ((s0.get("migrations") or {}).get(mid) or {}).get("done"):
            continue
        if fut is None:
            from futures_client import FuturesDemoClient
            fut = FuturesDemoClient()
        for sym in syms:
            q = float(((s0.get(demo_e5v.STATE_KEY) or {}).get("book") or {}).get(sym) or 0.0)
            inv = investigate(fut, sym, q) if q else {"symbol": sym, "exchange_position": None}
            log.info("fix_investigation %s %s", mid, {k: v for k, v in inv.items() if k not in ("closing_trades", "force_orders")})
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            res = {}

            def mut(s):
                res["r"] = apply_symbol_fix(s, inv, demo_e5v.STATE_KEY, demo_e5v.SLOT, mid, at); return s
            store.mutate(mut); out.append(res.get("r"))
    return out
