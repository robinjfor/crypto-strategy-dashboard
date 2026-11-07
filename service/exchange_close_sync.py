"""Book Binance-authoritative liquidation / ADL closes into shared-slot virtual ledgers.

Verified on this tree before the change:
* ``netting.net_reconcile`` compares the sum of virtual ledgers with the exchange net and
  records the diff. It does not change cash or qty.
* ``execution.reconcile_exchange_closes`` walks legacy ``state["positions"]`` only. The
  shared slots (E5 / E5v / R2S / VBO / FA) keep a virtual book, so a liquidation never
  reaches that function.
* The E5 and VBO close guards (``netting.leg_missing``) refuse a reduce when the slot's own
  leg is not on the exchange, and they leave the virtual cash and qty untouched. That
  prevents a reduce-only order from opening the other side, and it also leaves the stale
  leg in the book. Exchange flat is not the same as "this slot's leg is gone": opposing
  virtual legs can net to zero while each slot still owns its leg.

What this module does
* Reads fills, orders, and income. It never places, cancels, or amends an order.
* Books a close only when the fills are a liquidation or an ADL, the signed qty matches
  the ledger-vs-exchange gap, and exactly one allocation is possible:
  - one slot is on the side being closed and the fill does not flip it, or
  - every slot on that side is fully closed (the gap equals the sum of those legs).
* Anything else (partials across two same-side slots, outside / manual closes, a missing
  order id, a truncated history, open orders, hedge mode, a non-quote fee, two insurance
  rows, insurance on an ADL) is an alert with ``pause: false``. No qty or cash write.
* Cash delta is the book buyback ``-signed_qty * vwap - fee`` plus at most one in-window
  ``INSURANCE_CLEAR``. Funding and ``realizedPnl`` are not added. Trade ids and income
  ids are recorded so a replay is a no-op.
* Writes happen inside ``StateStore.mutate`` (generation precondition). The ledger qty is
  checked again on the locked copy. The exchange read itself is immediately before that write.

Re-entry
A forced flat is not a strategy exit. ``decide`` / ``plan_live`` are unchanged. The
executors call ``observe_target`` / ``reentry_blocked`` and skip an opening order while
``forced_flat[symbol].target`` still equals the strategy weight. A different weight clears
the marker and the normal planner runs (a fresh entry, not a replacement of the closed
leg). ``release_forced_flat`` clears the marker without sending an order, for the case
where the parent wants the current weight to be allowed to open again.

The one-off AKE / APR repair is separate so its audit actor is Robin (authorization
2026-11-07 15:00 and 15:22 Asia/Taipei). It is not attributed to 自動交易專家. The
permanent sync defers those symbols until that repair has been applied.

Rollback
``rollback_booking`` puts the qty back and subtracts the booked cash delta, but only while
the qty is still the post-booking qty. It does not restore an absolute cash balance, so a
later funding credit survives. Trade ids stay booked, so the sync will not re-apply the
close. Applying again is manual: clear that booking's ``done`` flag and the trade ids.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

import netting
from guards import log_event

log = logging.getLogger("exchange_close_sync")

TOL = Decimal("0.000001")
Q = Decimal("0.00000001")
WINDOW_MS = 7 * 86400 * 1000
SYNC_LOOKBACK_MS = 30 * 86400 * 1000
OUR_PREFIXES = ("e5-", "net-", "r2s-", "fa-", "fix-", "vb-")
FORCED = ("ADL", "liquidation")
ACTOR_ROBIN = "Robin"
SYNC_ACTOR = "binance_authoritative_sync"
AUTHORIZATION = (
    "Robin authorized reading Binance demo trade records and correcting the AKE and APR "
    "ledgers at 2026-11-07 15:00 Asia/Taipei, then said 執行改善 at 2026-11-07 15:22 "
    "Asia/Taipei after being told Binance fills are authoritative and a stale ledger must "
    "not create a replacement order."
)
REENTRY_POLICY = (
    "After a booked liquidation or ADL the executor does not open that symbol again while "
    "the strategy target weight is unchanged. A changed weight is a normal planner entry. "
    "release_forced_flat clears the marker and does not send an order."
)

# Verified demo evidence (Nov 7 15:01 Asia/Taipei). Cash is a delta from the fill, never an absolute overwrite.
REPAIRS = {
    "ake_liq_1106": {
        "slot": "demo_vbo1h_top50_1k",
        "ledger": "demo_vbo1h_exec",
        "symbol": "AKEUSDT",
        "ledger_qty": "-15002",
        "exchange_qty": "0",
        "cause": "liquidation",
        "not_before": "2026-10-20T00:00:00+08:00",
        "fill": {
            "id": "87896577",
            "orderId": "342616794",
            "side": "BUY",
            "qty": "15002",
            "price": "0.0044374",
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "-21.59496168",
            "clientOrderId": "autoclose-1793940440853088647",
            "time_ms": 1793940440000,
            "time_tolerance_ms": 5000,
        },
        "entry_trade_ids": ["87156866", "87162870", "87773350", "87810479", "87839128"],
        "entry_signed_sum": "-15002",
    },
    "apr_adl_1106": {
        "slot": "demo_e5_min5_x175_t05top20",
        "ledger": "demo_e5v_exec",
        "symbol": "APRUSDT",
        "ledger_qty": "-214",
        "exchange_qty": "0",
        "cause": "ADL",
        "not_before": "2026-10-20T00:00:00+08:00",
        "fill": {
            "id": "73620325",
            "orderId": "282105266",
            "side": "BUY",
            "qty": "214",
            "price": "0.0431",
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "1.81258",
            "clientOrderId": "adl_autoclose",
            "time_ms": 1793955589000,
            "time_tolerance_ms": 5000,
        },
        "entry": {"id": "71809638", "side": "SELL", "qty": "214", "price": "0.05157"},
        # Earlier APR liquidations already booked by named migrations. They are set aside only when
        # that migration is done and the fill is strictly before entry 71809638, which rebuilt this -214 leg.
        # Any other forced fill, including these ids if they fall after the entry, still rejects the repair.
        "prior_booked": [
            {"id": "70294043", "migration": "e5v_1026"},
            {"id": "71391559", "migration": "ab_1028"},
        ],
        "ignored_income": {"incomeType": "INSURANCE_CLEAR", "income": "-0.3589926", "note": "2026-10-27, outside this close"},
    },
}

_ALERT_KIND = {
    "ambiguous": "external_close_ambiguous",
    "unknown_outside": "external_close_unknown",
    "order_history_gap": "external_close_order_gap",
    "hedge": "external_close_hedge",
    "open_orders": "external_close_open_orders",
    "not_a_close": "external_close_not_a_close",
    "unsupported_ledger": "external_close_unsupported",
    "no_evidence": "external_close_no_evidence",
    "mixed_new_fills": "external_close_mixed",
    "insurance_ambiguous": "external_close_insurance",
    "adl_insurance": "external_close_insurance",
    "fee_asset": "external_close_fee_asset",
    "already_booked": "external_close_already_booked",
    "truncated": "external_close_history_gap",
    "unexplained": "external_close_no_evidence",
    "bad_trade": "external_close_no_evidence",
    "open_orders_unknown": "external_close_open_orders",
}


def dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x if x is not None else 0))


def num(d: Decimal) -> float:
    return float(dec(d).quantize(Q))


def _ms_iso(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def signed_qty(trade: dict) -> Decimal:
    q = dec(trade.get("qty") or 0)
    side = str(trade.get("side") or "").upper()
    if side == "BUY":
        return q
    if side == "SELL":
        return -q
    raise ValueError("trade side")


def classify_coid(coid, auto_close_type=None) -> str:
    c = str(coid or "")
    a = str(auto_close_type or "").upper()
    if a == "ADL" or c.startswith("adl_autoclose"):
        return "ADL"
    if a == "LIQUIDATION" or c.startswith("autoclose-"):
        return "liquidation"
    if c.startswith(OUR_PREFIXES):
        return "ours"
    if not c and not a:
        return "order_history_gap"
    return "outside"


def quote_fee(trade: dict, quote: str = "USDT"):
    """Quote commission, or None when a non-zero fee is in an asset we do not convert."""
    asset = str(trade.get("commissionAsset") or quote).upper()
    fee = dec(trade.get("commission") or 0)
    if fee == 0:
        return Decimal("0")
    if asset == quote.upper():
        return fee
    return None


def ledger_key(slot: str) -> str | None:
    key = netting.SLOTS.get(slot)
    if key in (None, "demo_fa_positions"):
        return None
    return key


def symbol_books(state: dict, symbol: str) -> dict[str, Decimal]:
    out = {}
    for slot, book in netting.slot_books(state).items():
        q = dec(book.get(symbol) or 0)
        if abs(q) > TOL:
            out[slot] = q
    return out


def snap_target(ex: dict, symbol: str):
    hist = ex.get("history") or []
    if hist and isinstance(hist[-1], dict) and symbol in (hist[-1].get("tg") or {}):
        return float(hist[-1]["tg"][symbol])
    targets = ex.get("targets") or {}
    if symbol in targets:
        return float(targets[symbol])
    return None


def observe_target(ex: dict, symbol: str, target_now: float) -> str | None:
    """Track the strategy weight after a forced flat. Returns snapshotted / unchanged / released / None.

    The first weight seen after the close is stored. A later different weight drops the marker
    so the existing planner can open. The same weight keeps the marker, and the executor skips the open.
    """
    ff = ex.get("forced_flat") or {}
    marker = ff.get(symbol)
    if not isinstance(marker, dict):
        return None
    if marker.get("target") is None:
        marker["target"] = float(target_now)
        marker["target_source"] = "snapshotted_on_first_bar_after_close"
        ex["forced_flat"] = ff
        return "snapshotted"
    if abs(float(target_now) - float(marker["target"])) > 1e-9:
        ff.pop(symbol, None)
        if ff:
            ex["forced_flat"] = ff
        else:
            ex.pop("forced_flat", None)
        return "released"
    return "unchanged"


def reentry_blocked(ex: dict, symbol: str) -> bool:
    return isinstance((ex.get("forced_flat") or {}).get(symbol), dict)


def release_forced_flat(state: dict, slot: str, symbol: str, *, actor: str, reason: str, at: str | None = None) -> dict:
    """Clear the re-entry marker. Does not change qty or cash and does not send an order."""
    key = ledger_key(slot)
    if not key:
        return {"ok": False, "reason": "unknown_slot"}
    ex = state.get(key) or {}
    ff = dict(ex.get("forced_flat") or {})
    if symbol not in ff:
        return {"ok": True, "skipped": "not_set"}
    marker = ff.pop(symbol)
    if ff:
        ex["forced_flat"] = ff
    else:
        ex.pop("forced_flat", None)
    state[key] = ex
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec = {"at": at, "id": f"release-{slot}-{symbol}", "kind": "forced_flat_release", "slot": slot,
           "symbol": symbol, "actor": actor, "reason": reason, "marker": marker, "orders": 0}
    state["ledger_adjustments"] = (list(state.get("ledger_adjustments") or []) + [rec])[-200:]
    log_event(state, "forced_flat_release", slot, symbol=symbol, actor=actor, reason=reason)
    return {"ok": True, "released": True}


def attribute(books: dict[str, Decimal], gap: Decimal):
    """Unique close allocation, 'unsupported', or None when the split would be a guess."""
    if abs(gap) <= TOL:
        return {}
    closed: dict[str, Decimal] = {}
    for slot, q in books.items():
        if q * gap < 0:
            closed[slot] = q
    if not closed:
        return None
    if any(ledger_key(slot) is None for slot in closed):
        return "unsupported"
    if len(closed) == 1:
        slot, q = next(iter(closed.items()))
        new = q + gap
        if q * new < -TOL or abs(new) > abs(q) + TOL:
            return None
        return {slot: gap}
    full = -sum(closed.values(), Decimal("0"))
    if abs(full - gap) <= TOL:
        return {s: -q for s, q in closed.items()}
    return None


def _share(total: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    slots = list(weights)
    denom = sum(weights.values(), Decimal("0"))
    out: dict[str, Decimal] = {}
    acc = Decimal("0")
    for i, slot in enumerate(slots):
        if i == len(slots) - 1:
            out[slot] = total - acc
        else:
            part = (total * weights[slot] / denom).quantize(Q)
            out[slot] = part
            acc += part
    return out


def _dedupe_trades(trades: list) -> list:
    seen, out = set(), []
    for t in trades or []:
        tid = str(t.get("id") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append(t)
    return out


def _closing_events(snapshot: dict, gap: Decimal, booked: set[str]) -> dict:
    if snapshot.get("truncated"):
        return {"status": "truncated"}
    orders = {str(o.get("orderId")): o for o in (snapshot.get("orders") or []) if o.get("orderId") is not None}
    grouped: dict[str, list] = {}
    unresolved = False
    seen: set[str] = set()
    for t in snapshot.get("trades") or []:
        tid = str(t.get("id") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            signed = signed_qty(t)
        except ValueError:
            return {"status": "bad_trade"}
        if signed == 0 or signed * gap <= 0:
            continue
        oid = str(t.get("orderId") or "")
        order = orders.get(oid) or {}
        coid = str(t.get("clientOrderId") or order.get("clientOrderId") or "")
        auto = order.get("autoCloseType")
        if not coid and not auto:
            unresolved = True
            klass = "order_history_gap"
        else:
            klass = classify_coid(coid, auto)
        fee = quote_fee(t, str(snapshot.get("quote") or "USDT"))
        if fee is None:
            return {"status": "fee_asset"}
        grouped.setdefault(oid, []).append({
            "id": tid, "signed": signed, "qty": abs(signed), "price": dec(t.get("price") or 0),
            "fee": fee, "time": int(t.get("time") or 0), "klass": klass, "orderId": oid, "coid": coid,
            "realized": dec(t.get("realizedPnl") or 0), "booked": tid in booked,
        })
    if unresolved:
        return {"status": "order_history_gap"}
    events = []
    for oid, rows in grouped.items():
        klasses = {r["klass"] for r in rows}
        qty = sum((r["qty"] for r in rows), Decimal("0"))
        events.append({
            "orderId": oid,
            "signed": sum((r["signed"] for r in rows), Decimal("0")),
            "qty": qty,
            "notional": sum((r["qty"] * r["price"] for r in rows), Decimal("0")),
            "fee": sum((r["fee"] for r in rows), Decimal("0")),
            "time": max(r["time"] for r in rows),
            "klass": next(iter(klasses)) if len(klasses) == 1 else "mixed",
            "fills": rows,
            "booked": all(r["booked"] for r in rows),
        })
    return {"status": "ok", "events": events}


def _select(events: list[dict], gap: Decimal) -> dict:
    unbooked = [e for e in events if not e["booked"]]
    booked_ev = [e for e in events if e["booked"]]
    forced = sorted((e for e in unbooked if e["klass"] in FORCED), key=lambda e: e["time"], reverse=True)
    other = [e for e in unbooked if e["klass"] not in FORCED]
    acc = Decimal("0")
    pick = []
    for ev in forced:
        if abs(acc - gap) <= TOL:
            break
        nxt = acc + ev["signed"]
        if abs(nxt) > abs(gap) + TOL and abs(nxt - gap) > TOL:
            return {"status": "ambiguous"}
        pick.append(ev)
        acc = nxt
    if abs(acc - gap) > TOL:
        if any(e["klass"] == "outside" for e in other):
            return {"status": "unknown_outside"}
        if booked_ev and not forced:
            return {"status": "already_booked"}
        if not events:
            return {"status": "no_evidence"}
        return {"status": "unexplained"}
    causes = {e["klass"] for e in pick}
    if causes not in ({"ADL"}, {"liquidation"}):
        return {"status": "ambiguous"}
    if pick and any(e["time"] >= min(p["time"] for p in pick) for e in other):
        return {"status": "mixed_new_fills"}
    return {"status": "book", "events": pick, "cause": next(iter(causes))}


def _insurance(snapshot: dict, t0: int, t1: int, booked: set[str], cause: str) -> dict:
    rows = []
    seen: set[str] = set()
    quote = str(snapshot.get("quote") or "USDT").upper()
    for r in snapshot.get("income") or []:
        if str(r.get("incomeType") or "") != "INSURANCE_CLEAR":
            continue
        ts = int(r.get("time") or 0)
        if ts < t0 or ts > t1:
            continue
        iid = str(r.get("tranId") or r.get("id") or "")
        if not iid or iid in seen or iid in booked:
            continue
        seen.add(iid)
        asset = str(r.get("asset") or quote).upper()
        amt = dec(r.get("income") or 0)
        if amt != 0 and asset != quote:
            return {"status": "fee_asset"}
        rows.append({"id": iid, "amt": amt})
    if cause == "ADL" and rows:
        return {"status": "adl_insurance"}
    if len(rows) > 1:
        return {"status": "insurance_ambiguous"}
    amt = rows[0]["amt"] if rows else Decimal("0")
    return {"status": "ok", "rows": rows, "amt": amt}


def _alert(state: dict, kind: str, symbol: str, *, slots: list[str], at: str, detail: str, trade_ids: list[str] | None = None) -> None:
    key = f"{kind}|{symbol}|{','.join(trade_ids or [])}|{detail[:80]}"
    seen = state.setdefault("external_close_alert_keys", [])
    if key in seen:
        return
    seen.append(key)
    state["external_close_alert_keys"] = seen[-2000:]
    state.setdefault("reconcile_alerts", []).append({
        "at": at, "key": key, "venue": "futures", "quote": "USDT", "symbol": symbol,
        "kind": kind, "slots": slots, "pause": False, "detail": detail[:300],
    })
    state["reconcile_alerts"] = state["reconcile_alerts"][-200:]


def _guard(snapshot: dict) -> str | None:
    if snapshot.get("error"):
        return "no_evidence"
    if snapshot.get("truncated"):
        return "truncated"
    if snapshot.get("hedge") or snapshot.get("dual_side"):
        return "hedge"
    if "open_orders" not in snapshot or "open_algo_orders" not in snapshot:
        return "open_orders_unknown"
    if snapshot.get("open_orders") or snapshot.get("open_algo_orders"):
        return "open_orders"
    if snapshot.get("exchange_qty") is None:
        return "no_evidence"
    return None


def plan_sync(state: dict, snapshot: dict, *, at: str | None = None) -> dict:
    """Pure. ``status == 'book'`` is the only plan that changes a ledger."""
    symbol = str(snapshot.get("symbol") or "")
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    books = symbol_books(state, symbol)
    slots = sorted(books)
    base = {"symbol": symbol, "at": at, "slots": slots, "pause": False}
    if snapshot.get("error") or snapshot.get("exchange_qty") is None:
        return {"status": "no_evidence", **base}
    if symbol in netting.U1_SYMBOLS:
        # Same exclusion as net_reconcile. U1 is not a shared-slot book; do not alert or write it.
        return {"status": "u1_sleeve", **base}
    if not books:
        # Flat and unowned is already consistent. A leftover exchange position is net_reconcile's alert.
        if abs(dec(snapshot.get("exchange_qty"))) <= TOL:
            return {"status": "in_sync", **base}
        return {"status": "unowned", **base}
    gap = dec(snapshot.get("exchange_qty")) - sum(books.values(), Decimal("0"))
    if abs(gap) <= TOL:
        return {"status": "in_sync", **base}
    blocked = _guard(snapshot)
    if blocked:
        return {"status": blocked, "gap": num(gap), **base}
    alloc = attribute(books, gap)
    if alloc == "unsupported":
        return {"status": "unsupported_ledger", "symbol": symbol, "at": at, "slots": slots, "gap": num(gap), "pause": False}
    if not alloc:
        closed = [q for q in books.values() if q * gap < 0]
        return {"status": "ambiguous" if len(closed) > 1 else "not_a_close", "gap": num(gap), **base}
    booked = set((state.get("booked_external_trades") or {}))
    ev = _closing_events(snapshot, gap, booked)
    if ev["status"] != "ok":
        return {"status": ev["status"], "symbol": symbol, "at": at, "slots": slots, "gap": num(gap), "pause": False}
    picked = _select(ev["events"], gap)
    if picked["status"] != "book":
        return {"status": picked["status"], "symbol": symbol, "at": at, "slots": slots, "gap": num(gap), "pause": False}
    t0 = min(e["time"] for e in picked["events"]) - 60_000
    t1 = max(e["time"] for e in picked["events"]) + 60_000
    income_booked = set(state.get("booked_external_income") or {})
    ins = _insurance(snapshot, t0, t1, income_booked, picked["cause"])
    if ins["status"] != "ok":
        return {"status": ins["status"], "symbol": symbol, "at": at, "slots": slots, "gap": num(gap), "pause": False}
    qty = sum((e["qty"] for e in picked["events"]), Decimal("0"))
    notional = sum((e["notional"] for e in picked["events"]), Decimal("0"))
    fee = sum((e["fee"] for e in picked["events"]), Decimal("0"))
    vwap = notional / qty
    weights = {slot: abs(delta) for slot, delta in alloc.items()}
    fee_parts = _share(fee, weights)
    ins_parts = _share(ins["amt"], weights)
    trade_ids = [f["id"] for e in picked["events"] for f in e["fills"]]
    legs = []
    for slot, delta in alloc.items():
        key = ledger_key(slot)
        old = books[slot]
        cash_delta = (-delta * vwap - fee_parts[slot] + ins_parts[slot]).quantize(Q)
        new = old + delta
        legs.append({
            "slot": slot, "ledger": key, "symbol": symbol,
            "qty_before": num(old), "qty_after": 0.0 if abs(new) <= TOL else num(new),
            "delta": num(delta), "cash_delta": num(cash_delta), "fee": num(fee_parts[slot]),
            "insurance": num(ins_parts[slot]),
        })
    return {
        "status": "book", "symbol": symbol, "at": at, "slots": slots, "gap": num(gap), "pause": False,
        "cause": picked["cause"], "vwap": num(vwap), "fee": num(fee), "insurance": num(ins["amt"]),
        "trade_ids": trade_ids, "income_ids": [r["id"] for r in ins["rows"]],
        "order_ids": [e["orderId"] for e in picked["events"]],
        "client_order_ids": sorted({f["coid"] for e in picked["events"] for f in e["fills"] if f["coid"]}),
        "realized_pnl": num(sum((f["realized"] for e in picked["events"] for f in e["fills"]), Decimal("0"))),
        "legs": legs, "actor": SYNC_ACTOR,
        "booking_id": "xclose-" + symbol + "-" + "-".join(sorted(trade_ids)[:4]),
    }


def apply_plan(state: dict, plan: dict) -> dict:
    """Apply a ``plan_sync`` result. A non-book plan only alerts. Idempotent by trade id."""
    status = plan.get("status")
    symbol = plan.get("symbol")
    at = plan.get("at")
    if status in ("in_sync", "deferred_repair", "unowned", "u1_sleeve"):
        return plan
    if status != "book":
        kind = _ALERT_KIND.get(status, "external_close_no_evidence")
        _alert(state, kind, symbol, slots=plan.get("slots") or [], at=at, detail=status, trade_ids=plan.get("trade_ids"))
        return plan
    booked = state.setdefault("booked_external_trades", {})
    if any(tid in booked for tid in plan["trade_ids"]):
        _alert(state, "external_close_already_booked", symbol, slots=plan.get("slots") or [], at=at,
               detail="trade id already booked", trade_ids=plan["trade_ids"])
        return {**plan, "status": "already_booked", "applied": False}
    for leg in plan["legs"]:
        ex = state.get(leg["ledger"]) or {}
        have = dec((ex.get("book") or {}).get(symbol) or 0)
        if abs(have - dec(leg["qty_before"])) > TOL:
            _alert(state, "external_close_ambiguous", symbol, slots=plan.get("slots") or [], at=at,
                   detail="ledger qty changed before write", trade_ids=plan["trade_ids"])
            return {**plan, "status": "ledger_changed", "applied": False}
    for leg in plan["legs"]:
        ex = dict(state.get(leg["ledger"]) or {})
        book = dict(ex.get("book") or {})
        if abs(dec(leg["qty_after"])) <= TOL:
            book.pop(symbol, None)
        else:
            book[symbol] = leg["qty_after"]
        ex["book"] = book
        ex["cash"] = num(dec(str(ex.get("cash") or 0)) + dec(leg["cash_delta"]))
        ff = dict(ex.get("forced_flat") or {})
        ff[symbol] = {"at": at, "cause": plan["cause"], "target": snap_target(ex, symbol),
                      "qty_before": leg["qty_before"], "trade_ids": plan["trade_ids"], "booking": plan["booking_id"]}
        ex["forced_flat"] = ff
        state[leg["ledger"]] = ex
        log_event(state, "external_close_booked", leg["slot"], symbol=symbol, qty_before=leg["qty_before"],
                  qty_after=leg["qty_after"], cash_delta=leg["cash_delta"], cause=plan["cause"], actor=plan["actor"])
    for tid in plan["trade_ids"]:
        booked[tid] = {"booking": plan["booking_id"], "symbol": symbol, "at": at}
    if plan.get("income_ids"):
        inc = list(state.get("booked_external_income") or [])
        inc.extend(plan["income_ids"])
        state["booked_external_income"] = inc
    rec = {k: v for k, v in plan.items() if k != "status"}
    rec.update(done=True, rolled_back=False, kind="external_close", orders=0, reentry=REENTRY_POLICY)
    state.setdefault("external_closes", {})[plan["booking_id"]] = rec
    adj = {"at": at, "id": plan["booking_id"], "kind": "external_close", "symbol": symbol, "cause": plan["cause"],
           "actor": plan["actor"], "legs": plan["legs"], "trade_ids": plan["trade_ids"], "orders": 0,
           "realized_pnl_not_booked": plan.get("realized_pnl"), "funding_booked": False}
    state["ledger_adjustments"] = (list(state.get("ledger_adjustments") or []) + [adj])[-200:]
    _alert(state, "external_close_booked", symbol, slots=[leg["slot"] for leg in plan["legs"]], at=at,
           detail=plan["cause"], trade_ids=plan["trade_ids"])
    return {**plan, "applied": True}


def _find_trade(snapshot: dict, tid: str):
    for t in _dedupe_trades(snapshot.get("trades") or []):
        if str(t.get("id")) == str(tid):
            orders = {str(o.get("orderId")): o for o in (snapshot.get("orders") or [])}
            order = orders.get(str(t.get("orderId"))) or {}
            return t, order
    return None, None


def _fill_matches(trade: dict, order: dict, spec: dict) -> str | None:
    f = spec["fill"]
    if str(trade.get("orderId")) != str(f["orderId"]):
        return "order_id"
    if str(trade.get("side") or "").upper() != f["side"]:
        return "side"
    if abs(dec(trade.get("qty")) - dec(f["qty"])) > TOL:
        return "qty"
    if abs(dec(trade.get("price")) - dec(f["price"])) > TOL:
        return "price"
    if abs(dec(trade.get("commission") or 0) - dec(f["commission"])) > TOL:
        return "fee"
    asset = str(trade.get("commissionAsset") or "USDT").upper()
    if dec(trade.get("commission") or 0) != 0 and asset != str(f.get("commissionAsset") or "USDT").upper():
        return "fee_asset"
    if abs(dec(trade.get("realizedPnl") or 0) - dec(f["realizedPnl"])) > TOL:
        return "realized_pnl"
    coid = str(trade.get("clientOrderId") or order.get("clientOrderId") or "")
    if coid != f["clientOrderId"]:
        return "client_order_id"
    if abs(int(trade.get("time") or 0) - int(f["time_ms"])) > int(f["time_tolerance_ms"]):
        return "time"
    klass = classify_coid(coid, order.get("autoCloseType"))
    if klass != spec["cause"]:
        return "cause"
    return None


def _entry_check(snapshot: dict, spec: dict, books: dict[str, Decimal]) -> dict:
    """Re-check ownership when the fresh window still has the entry trades. A short window is noted, not guessed."""
    if spec.get("entry_trade_ids"):
        want = [str(i) for i in spec["entry_trade_ids"]]
        found = []
        for tid in want:
            t, _ = _find_trade(snapshot, tid)
            if t is not None:
                found.append(t)
        if len(found) != len(want):
            return {"ok": True, "entry_check": "not_in_fresh_window"}
        total = sum((signed_qty(t) for t in found), Decimal("0"))
        if abs(total - dec(spec["entry_signed_sum"])) > TOL:
            return {"ok": False, "reason": "entry_sum_mismatch", "entry_check": "sum_mismatch"}
        return {"ok": True, "entry_check": "sum_ok"}
    ent = spec.get("entry")
    if not ent:
        return {"ok": True, "entry_check": "not_required"}
    t, _ = _find_trade(snapshot, ent["id"])
    if t is None:
        return {"ok": True, "entry_check": "not_in_fresh_window"}
    if str(t.get("side") or "").upper() != ent["side"]:
        return {"ok": False, "reason": "entry_mismatch"}
    if abs(dec(t.get("qty")) - dec(ent["qty"])) > TOL or abs(dec(t.get("price")) - dec(ent["price"])) > TOL:
        return {"ok": False, "reason": "entry_mismatch"}
    return {"ok": True, "entry_check": "entry_ok"}


def _set_aside_prior_lifecycle(state: dict, snapshot: dict, spec: dict, target: dict, events: dict, have: Decimal):
    """Drop forced fills that closed an earlier leg, or return a skip reason.

    A fill is set aside only when its id is named on the spec, the migration that booked that
    earlier close is done, and the fill is strictly before the entry that rebuilt the current qty.
    Every other forced fill still rejects the repair. No cash is computed here.
    """
    fill_id = str(spec["fill"]["id"])
    forced = [f for e in events.get("events") or [] if e["klass"] in FORCED for f in e["fills"]]
    others = [f for f in forced if f["id"] != fill_id]
    if not others:
        return []
    prior = spec.get("prior_booked") or []
    proven = {str(row["id"]): row.get("migration") for row in prior}
    if not prior or not spec.get("entry"):
        return None
    entry, _ = _find_trade(snapshot, spec["entry"]["id"])
    if entry is None:
        return None
    ent = spec["entry"]
    if str(entry.get("side") or "").upper() != ent["side"]:
        return None
    if abs(dec(entry.get("qty")) - dec(ent["qty"])) > TOL or abs(dec(entry.get("price")) - dec(ent["price"])) > TOL:
        return None
    if abs(signed_qty(entry) - have) > TOL:
        return None
    entry_time = int(entry.get("time") or 0)
    if int(target.get("time") or 0) <= entry_time:
        return None
    migs = state.get("migrations") or {}
    kept = []
    for f in others:
        mid_name = proven.get(f["id"])
        booked = bool(mid_name) and bool((migs.get(mid_name) or {}).get("done")) and int(f.get("time") or 0) < entry_time
        if not booked:
            return None
        kept.append({"id": f["id"], "migration": mid_name})
    return kept


def commit_repair(state: dict, snapshot: dict | None, spec: dict, mid: str, *, at: str | None = None) -> dict:
    """Precondition-checked one-off. Applies the fill's cash delta or changes nothing."""
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    mig = (state.get("migrations") or {}).get(mid) or {}
    if mig.get("done"):
        return {"id": mid, "skipped": "done", "rolled_back": bool(mig.get("rolled_back"))}
    symbol = spec["symbol"]
    if not snapshot or snapshot.get("error"):
        return {"id": mid, "skipped": (snapshot or {}).get("error") or "no_snapshot"}
    blocked = _guard(snapshot)
    if blocked:
        return {"id": mid, "skipped": blocked}
    if str(snapshot.get("symbol") or symbol) != symbol:
        return {"id": mid, "skipped": "symbol"}
    if abs(dec(snapshot.get("exchange_qty")) - dec(spec["exchange_qty"])) > TOL:
        return {"id": mid, "skipped": "exchange_qty", "exchange_qty": snapshot.get("exchange_qty")}
    ex = dict(state.get(spec["ledger"]) or {})
    book = dict(ex.get("book") or {})
    have = dec(book.get(symbol) or 0)
    fill_id = str(spec["fill"]["id"])
    booked = state.setdefault("booked_external_trades", {})
    if abs(have) <= TOL and fill_id in booked:
        state.setdefault("migrations", {})[mid] = {**mig, "done": True, "at": at, "skipped": "already_applied"}
        return {"id": mid, "skipped": "already_applied"}
    if abs(have) <= TOL:
        _alert(state, "external_close_no_evidence", symbol, slots=[spec["slot"]], at=at, detail="ledger flat but close not booked")
        return {"id": mid, "skipped": "ledger_flat_unbooked"}
    if abs(have - dec(spec["ledger_qty"])) > TOL:
        return {"id": mid, "skipped": "ledger_qty", "ledger_qty": num(have)}
    books = symbol_books(state, symbol)
    if list(books) != [spec["slot"]]:
        _alert(state, "external_close_ambiguous", symbol, slots=sorted(books), at=at, detail="repair is sole-holder only")
        return {"id": mid, "skipped": "not_sole_holder", "slots": sorted(books)}
    trade, order = _find_trade(snapshot, fill_id)
    if trade is None:
        return {"id": mid, "skipped": "fill_missing"}
    bad = _fill_matches(trade, order, spec)
    if bad:
        return {"id": mid, "skipped": "fill_mismatch", "field": bad}
    # Do not mix this verified fill with any other forced close of the same leg.
    gap = -have
    ev = _closing_events(snapshot, gap, set(booked))
    if ev["status"] != "ok":
        return {"id": mid, "skipped": ev["status"]}
    prior = _set_aside_prior_lifecycle(state, snapshot, spec, trade, ev, have)
    if prior is None:
        forced_ids = sorted(f["id"] for e in ev["events"] if e["klass"] in FORCED for f in e["fills"])
        _alert(state, "external_close_mixed", symbol, slots=[spec["slot"]], at=at, detail="extra forced fill", trade_ids=forced_ids)
        return {"id": mid, "skipped": "extra_forced_fill", "trade_ids": forced_ids}
    verified_time = int(trade.get("time") or 0)
    newer = [e for e in ev["events"] if not e["booked"] and e["klass"] not in FORCED and e["time"] >= verified_time - int(spec["fill"]["time_tolerance_ms"])]
    if newer:
        _alert(state, "external_close_mixed", symbol, slots=[spec["slot"]], at=at, detail="newer non-forced fill", trade_ids=[f["id"] for e in newer for f in e["fills"]])
        return {"id": mid, "skipped": "mixed_new_fills"}
    entry = _entry_check(snapshot, spec, books)
    if not entry["ok"]:
        return {"id": mid, "skipped": entry["reason"], "entry_check": entry.get("entry_check")}
    t_close = int(trade.get("time") or spec["fill"]["time_ms"])
    ins = _insurance(snapshot, t_close - 60_000, t_close + 60_000, set(state.get("booked_external_income") or {}), spec["cause"])
    if ins["status"] != "ok" or ins["rows"]:
        _alert(state, "external_close_insurance", symbol, slots=[spec["slot"]], at=at, detail="in-window insurance not in the verified evidence")
        return {"id": mid, "skipped": "unexpected_insurance"}
    fee = quote_fee(trade) or Decimal("0")
    signed = signed_qty(trade)
    if abs(signed + have) > TOL:
        return {"id": mid, "skipped": "qty_mismatch"}
    price = dec(trade.get("price"))
    cash_delta = (-signed * price - fee).quantize(Q)
    expected = (-dec(spec["fill"]["qty"]) * dec(spec["fill"]["price"]) - dec(spec["fill"]["commission"])).quantize(Q)
    if cash_delta != expected:
        return {"id": mid, "skipped": "cash_delta_mismatch"}
    # realizedPnl is evidence only. Funding rows are not in cash_delta.
    qty_before = num(have)
    book.pop(symbol, None)
    cash_before = dec(str(ex.get("cash") or 0))
    ex["book"] = book
    ex["cash"] = num(cash_before + cash_delta)
    ff = dict(ex.get("forced_flat") or {})
    ff[symbol] = {"at": at, "cause": spec["cause"], "target": snap_target(ex, symbol),
                  "qty_before": qty_before, "trade_ids": [fill_id], "booking": mid}
    ex["forced_flat"] = ff
    state[spec["ledger"]] = ex
    booked[fill_id] = {"booking": mid, "symbol": symbol, "at": at, "rolled_back": False}
    leg = {"slot": spec["slot"], "ledger": spec["ledger"], "symbol": symbol, "qty_before": qty_before,
           "qty_after": 0.0, "delta": num(signed), "cash_delta": num(cash_delta), "cash_before": num(cash_before),
           "fee": num(fee), "insurance": 0.0}
    rec = {"done": True, "rolled_back": False, "at": at, "id": mid, "symbol": symbol, "cause": spec["cause"],
           "actor": ACTOR_ROBIN, "authorization": AUTHORIZATION, "orders": 0, "entry_check": entry["entry_check"],
           "trade_ids": [fill_id], "income_ids": [], "legs": [leg], "realized_pnl_not_booked": spec["fill"]["realizedPnl"],
           "funding_booked": False, "reentry": REENTRY_POLICY,
           "prior_lifecycle": prior,
           "fill": {"id": fill_id, "orderId": spec["fill"]["orderId"], "clientOrderId": spec["fill"]["clientOrderId"],
                    "qty": spec["fill"]["qty"], "price": spec["fill"]["price"], "commission": spec["fill"]["commission"]}}
    state.setdefault("migrations", {})[mid] = rec
    state.setdefault("external_closes", {})[mid] = rec
    adj = {"at": at, "id": f"{mid}-{symbol}", "kind": "external_close_repair", "slot": spec["slot"],
           "ledger": spec["ledger"], "symbol": symbol, "qty_before": qty_before, "qty_after": 0.0,
           "cash_delta": num(cash_delta), "cause": spec["cause"], "actor": ACTOR_ROBIN,
           "authorization": AUTHORIZATION, "orders": 0, "trade_ids": [fill_id],
           "realized_pnl_not_booked": spec["fill"]["realizedPnl"], "funding_booked": False,
           "entry_check": entry["entry_check"]}
    state["ledger_adjustments"] = (list(state.get("ledger_adjustments") or []) + [adj])[-200:]
    log_event(state, "external_close_booked", spec["slot"], symbol=symbol, qty_before=qty_before, qty_after=0.0,
              cash_delta=num(cash_delta), cause=spec["cause"], actor=ACTOR_ROBIN)
    _alert(state, "external_close_booked", symbol, slots=[spec["slot"]], at=at, detail=spec["cause"], trade_ids=[fill_id])
    return {"id": mid, "applied": True, "cash_delta": num(cash_delta), "entry_check": entry["entry_check"], "actor": ACTOR_ROBIN}


def rollback_booking(state: dict, booking_id: str, *, actor: str, at: str | None = None) -> dict:
    """Reverse qty and the cash delta if the qty is still the post-booking qty. Keeps trade ids booked."""
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec = (state.get("migrations") or {}).get(booking_id) or (state.get("external_closes") or {}).get(booking_id)
    if not rec:
        return {"ok": False, "reason": "unknown"}
    if rec.get("rolled_back"):
        return {"ok": True, "skipped": "already_rolled_back"}
    if not rec.get("done"):
        return {"ok": False, "reason": "not_applied"}
    for leg in rec.get("legs") or []:
        ex = state.get(leg["ledger"]) or {}
        have = dec((ex.get("book") or {}).get(leg["symbol"]) or 0)
        if abs(have - dec(leg["qty_after"])) > TOL:
            return {"ok": False, "reason": "qty_moved", "symbol": leg["symbol"]}
    for leg in rec["legs"]:
        ex = dict(state.get(leg["ledger"]) or {})
        book = dict(ex.get("book") or {})
        if abs(dec(leg["qty_before"])) <= TOL:
            book.pop(leg["symbol"], None)
        else:
            book[leg["symbol"]] = leg["qty_before"]
        ex["book"] = book
        ex["cash"] = num(dec(str(ex.get("cash") or 0)) - dec(leg["cash_delta"]))
        ff = dict(ex.get("forced_flat") or {})
        ff.pop(leg["symbol"], None)
        if ff:
            ex["forced_flat"] = ff
        else:
            ex.pop("forced_flat", None)
        state[leg["ledger"]] = ex
    rec["rolled_back"] = True
    rec["rolled_back_at"] = at
    rec["rolled_back_by"] = actor
    for tid in rec.get("trade_ids") or []:
        row = (state.get("booked_external_trades") or {}).get(tid)
        if isinstance(row, dict):
            row["rolled_back"] = True
    state["ledger_adjustments"] = (list(state.get("ledger_adjustments") or []) + [{
        "at": at, "id": f"rollback-{booking_id}", "kind": "external_close_rollback", "actor": actor,
        "booking": booking_id, "orders": 0, "legs": rec.get("legs"),
    }])[-200:]
    log_event(state, "external_close_rollback", None, booking=booking_id, actor=actor)
    return {"ok": True, "rolled_back": True}


def _pending_repair_symbols(state: dict) -> set[str]:
    mig = state.get("migrations") or {}
    return {spec["symbol"] for mid, spec in REPAIRS.items() if not (mig.get(mid) or {}).get("done")}


def run_repairs(store, fut=None, snapshots: dict | None = None, *, at: str | None = None) -> list:
    """Generation-checked repair. ``snapshots`` injects fetch results (tests). No order methods are called.

    A missing client is a failed result, not an empty success. ``run_once`` resolves one demo
    futures client and passes it here; this guard is for any other caller.
    """
    if snapshots is None and fut is None:
        return [{"ok": False, "applied": False, "skipped": "futures_client_unavailable"}]
    want = [mid for mid in REPAIRS if not ((store.load().get("migrations") or {}).get(mid) or {}).get("done")]
    if not want:
        return []
    fresh: dict[str, dict | None] = {}
    now_ms = int(time.time() * 1000)
    for mid in want:
        if snapshots is not None and mid in snapshots:
            fresh[mid] = snapshots[mid]
        elif fut is not None:
            spec = REPAIRS[mid]
            fresh[mid] = fetch_symbol(fut, spec["symbol"], start_ms=_ms_iso(spec["not_before"]), end_ms=now_ms)
        else:
            fresh[mid] = None
    out: dict = {}

    def mut(s):
        out["results"] = [commit_repair(s, fresh[mid], REPAIRS[mid], mid, at=at) for mid in want]
        return s

    store.mutate(mut)
    return out.get("results") or []


def run_sync(store, fut=None, snapshots: dict | None = None, *, at: str | None = None) -> list:
    """Book unequivocal forced closes. Defers symbols that still have a pending one-off repair."""
    if snapshots is None:
        if fut is None:
            return [{"ok": False, "applied": False, "status": "futures_client_unavailable", "pause": False}]
        snapshots = snapshots_for_gaps(store.load(), fut, int(time.time() * 1000))
    out: dict = {}

    def mut(s):
        results = []
        for sym in sorted(snapshots):
            if sym in _pending_repair_symbols(s):
                results.append({"symbol": sym, "status": "deferred_repair", "pause": False})
                continue
            plan = plan_sync(s, snapshots[sym], at=at)
            results.append(apply_plan(s, plan))
        out["results"] = results
        return s

    store.mutate(mut)
    return out.get("results") or []


def snapshots_for_gaps(state: dict, fut, now_ms: int) -> dict[str, dict]:
    rows = fut.position_risk() or []
    exch: dict[str, Decimal] = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        exch[sym] = exch.get(sym, Decimal("0")) + dec(r.get("positionAmt") or 0)
    vn = {s: dec(q) for s, q in netting.virtual_net(state).items()}
    out = {}
    for sym in sorted(set(vn) | set(exch)):
        if abs(exch.get(sym, Decimal("0")) - vn.get(sym, Decimal("0"))) <= TOL:
            continue
        if sym in _pending_repair_symbols(state) or sym in netting.U1_SYMBOLS:
            continue
        out[sym] = fetch_symbol(fut, sym, start_ms=now_ms - SYNC_LOOKBACK_MS, end_ms=now_ms)
    return out


def fetch_symbol(fut, symbol: str, *, start_ms: int, end_ms: int) -> dict:
    """Read-only. Does not call ``new_order`` or any cancel/amend endpoint."""
    try:
        dual = bool(fut.position_mode_dual())
        rows = list(fut.position_risk(symbol) or [])
        oo = list(fut.open_orders(symbol) or [])
        oao = list(fut.open_algo_orders(symbol) or [])
    except Exception as e:  # noqa: BLE001
        return {"symbol": symbol, "error": f"{type(e).__name__}: {e}"[:200]}
    hedge = dual or any(str(r.get("positionSide") or "BOTH").upper() in ("LONG", "SHORT") for r in rows)
    qty = sum((dec(r.get("positionAmt") or 0) for r in rows), Decimal("0"))
    trades: list = []
    income: list = []
    truncated = False
    t = int(start_ms)
    while t <= end_ms:
        end = min(t + WINDOW_MS - 1, int(end_ms))
        try:
            batch = fut._signed("GET", "/fapi/v1/userTrades",
                                {"symbol": symbol, "startTime": t, "endTime": end, "limit": 1000})
            inc = fut.income(symbol, "INSURANCE_CLEAR", t, end)
        except Exception as e:  # noqa: BLE001
            return {"symbol": symbol, "error": f"{type(e).__name__}: {e}"[:200]}
        batch = batch if isinstance(batch, list) else []
        if len(batch) >= 1000:
            truncated = True
        trades.extend(batch)
        income.extend(inc or [])
        t = end + 1
    try:
        orders = list(fut.all_orders(symbol, start_ms=max(int(end_ms) - WINDOW_MS, int(start_ms)), limit=1000) or [])
        if len(orders) >= 1000:
            truncated = True
        force = fut._signed("GET", "/fapi/v1/forceOrders", {"symbol": symbol, "startTime": int(start_ms), "limit": 100})
    except Exception as e:  # noqa: BLE001
        return {"symbol": symbol, "error": f"{type(e).__name__}: {e}"[:200]}
    if isinstance(force, list):
        orders = orders + force
    by = {str(o.get("orderId")): o for o in orders if o.get("orderId") is not None}
    missing = []
    for tr in trades:
        oid = str(tr.get("orderId") or "")
        if oid and oid not in by and not tr.get("clientOrderId"):
            missing.append(oid)
    missing = list(dict.fromkeys(missing))
    if len(missing) > 30:
        truncated = True
    elif hasattr(fut, "get_order_by_id"):
        for oid in missing:
            try:
                found = fut.get_order_by_id(symbol, oid)
            except Exception:  # noqa: BLE001
                found = None
            if found:
                by[str(found.get("orderId") or oid)] = found
    return {"symbol": symbol, "exchange_qty": num(qty), "dual_side": dual, "hedge": hedge,
            "open_orders": oo, "open_algo_orders": oao, "trades": _dedupe_trades(trades),
            "orders": list(by.values()), "income": income, "truncated": truncated, "quote": "USDT"}
