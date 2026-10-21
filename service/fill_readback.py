"""Read back actual fills from Binance after every order (spec 3.1 steps 2-3).

Binance is the source of truth: the price/qty/commission recorded in
state.positions / state.closed_trades come from GET order + myTrades/userTrades
filtered by orderId, not from the local estimate.
"""
from __future__ import annotations

import logging
import time

from execution import base_asset, fee_usdt_from_rows

log = logging.getLogger(__name__)

READBACK_TIMEOUT_S = 10.0
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


def _poll_order(get, timeout_s: float | None, sleep=time.sleep) -> dict | None:
    timeout_s = READBACK_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        try:
            last = get() or last
        except Exception as e:  # noqa: BLE001
            log.warning("fill_readback_order_fail err=%s", e)
        if last and str(last.get("status") or "").upper() in TERMINAL:
            return last
        if time.monotonic() >= deadline:
            return last
        sleep(0.5)


def summarize_trades(rows: list, symbol: str) -> dict:
    """Aggregate exchange trade rows into the fill record stored on trades."""
    qty = sum(float(r.get("qty") or 0) for r in rows)
    notional = sum(float(r.get("price") or 0) * float(r.get("qty") or 0) for r in rows)
    by_asset: dict[str, float] = {}
    for r in rows:
        a = str(r.get("commissionAsset") or "").upper()
        if not a:
            continue
        by_asset[a] = by_asset.get(a, 0.0) + float(r.get("commission") or 0)
    base = base_asset(symbol)
    return {
        "price": round(notional / qty, 10) if qty else None,
        "qty": round(qty, 10),
        "quote_qty": round(notional, 8),
        "commissions": [{"asset": a, "amount": round(v, 10)} for a, v in sorted(by_asset.items())],
        "commission_quote": fee_usdt_from_rows(rows, symbol),
        "commission_base_qty": round(by_asset.get(base, 0.0), 10),
        "trade_ids": [r.get("id") for r in rows],
        "n_trades": len(rows),
    }


def _finish(order: dict | None, rows: list, symbol: str, venue: str) -> dict:
    fb = summarize_trades(rows, symbol) if rows else {
        "price": None, "qty": 0.0, "quote_qty": 0.0, "commissions": [], "commission_quote": None,
        "commission_base_qty": 0.0, "trade_ids": [], "n_trades": 0}
    fb.update({"venue": venue, "symbol": symbol, "order_id": (order or {}).get("orderId"),
               "order_status": (order or {}).get("status"), "source": "binance",
               "complete": bool(rows)})
    return fb


def readback_spot(client, symbol: str, order: dict, *, timeout_s: float | None = None, sleep=time.sleep) -> dict:
    oid = order.get("orderId")
    final = order
    if oid is not None and order.get("status") and str(order["status"]).upper() not in TERMINAL:
        final = _poll_order(lambda: client.get_order(symbol, oid), timeout_s, sleep) or order
    rows: list = []
    try:
        rows = [t for t in (client.my_trades(symbol, limit=50) or []) if str(t.get("orderId")) == str(oid)]
    except Exception as e:  # noqa: BLE001
        log.warning("fill_readback_trades_fail symbol=%s err=%s", symbol, e)
    if not rows:  # fall back to the fills Binance returned on the order response
        rows = [dict(f, orderId=oid) for f in (order.get("fills") or [])]
    return _finish(final, rows, symbol, "spot")


def readback_futures(fc, symbol: str, order: dict, *, coid: str | None = None, timeout_s: float | None = None, sleep=time.sleep) -> dict:
    oid = order.get("orderId")
    coid = order.get("clientOrderId") or coid
    final = order
    if coid and order.get("status") and str(order["status"]).upper() not in TERMINAL:
        final = _poll_order(lambda: fc.get_order(symbol, coid), timeout_s, sleep) or order
        oid = final.get("orderId", oid)
    rows: list = []
    try:
        rows = [t for t in (fc.user_trades(symbol, limit=50) or []) if str(t.get("orderId")) == str(oid)]
    except Exception as e:  # noqa: BLE001
        log.warning("fill_readback_trades_fail symbol=%s err=%s", symbol, e)
    return _finish(final, rows, symbol, "futures")


def apply_entry_fill(pos: dict, fb: dict) -> dict:
    pos["entry_fill"] = fb
    if fb.get("complete") and fb.get("price"):
        pos["entry"] = fb["price"]
        pos["entry_commissions"] = fb["commissions"]
        pos["entry_fee_usdt"] = fb["commission_quote"]
    return pos


def apply_exit_fill(closed: dict, fb: dict) -> dict:
    """Overwrite exit price / qty / gross pnl with the exchange fill (before finalize_closed_pnl)."""
    closed["exit_fill"] = fb
    if not (fb.get("complete") and fb.get("price")):
        return closed
    closed["exit"] = fb["price"]
    closed["qty"] = fb["qty"]
    closed["exit_commissions"] = fb["commissions"]
    entry = float(closed.get("entry") or 0)
    if entry:
        short = str(closed.get("side") or "LONG").upper() == "SHORT"
        d = (entry - fb["price"]) if short else (fb["price"] - entry)
        closed["pnl_usdt"] = round(d * fb["qty"], 4)
    return closed


def safe(fn, *a, **k) -> dict | None:
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001
        log.warning("fill_readback_fail err=%s", e)
        return None
