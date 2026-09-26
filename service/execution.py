"""Live order helpers: idempotent IDs, LOT rounding, hard stops, manual close."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from binance_client import BinanceClient
from slots import MAX_NOTIONAL_USDT

log = logging.getLogger("trader.exec")
TZ = ZoneInfo("Asia/Taipei")


def now_iso_taipei() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def client_order_id(slot_id: str, kind: str, bar_ts: str | None = None) -> str:
    """Deterministic <=36 char id: slot-kind-bar."""
    bar = (bar_ts or "").replace(":", "").replace("+", "").replace("-", "")[:12]
    raw = f"{slot_id}-{kind}-{bar}" if bar else f"{slot_id}-{kind}"
    raw = re.sub(r"[^A-Za-z0-9\-_]", "", raw)
    return raw[:36]


def base_asset(symbol: str) -> str:
    s = str(symbol or "").upper()
    for q in ("USDT", "USDC", "FDUSD", "BUSD"):
        if s.endswith(q):
            return s[: -len(q)]
    return s


def free_balance(client: BinanceClient, asset: str) -> float:
    for b in client.nonzero_balances():
        if b.get("asset") == asset:
            return float(b.get("free") or 0)
    return 0.0


def net_filled_qty(order: dict, symbol: str) -> float:
    """executedQty minus commission charged in the base asset (spot BUY).

    Binance takes the 0.1% fee out of the bought coin unless paid in BNB, so
    the sellable balance is below executedQty. Using gross qty made every
    later SELL / STOP_LOSS_LIMIT fail with insufficient balance."""
    qty = float(order.get("executedQty") or 0)
    base = base_asset(symbol)
    fee = 0.0
    for f in order.get("fills") or []:
        if str(f.get("commissionAsset") or "").upper() == base:
            fee += float(f.get("commission") or 0)
    if not qty:
        qty = sum(float(f.get("qty") or 0) for f in order.get("fills") or [])
    return max(0.0, qty - fee)


def cancel_symbol_orders(client: BinanceClient, symbol: str) -> None:
    try:
        client.cancel_open_orders(symbol)
    except Exception as e:  # noqa: BLE001  (-2011 "Unknown order" when none open)
        log.info("cancel_open_orders_skip symbol=%s err=%s", symbol, e)


def sellable_qty(client: BinanceClient, symbol: str, want_qty: float | None = None) -> float:
    """Free base balance (after cancels), capped at want_qty, floored to stepSize.
    Returns 0 when below minQty/minNotional."""
    free = free_balance(client, base_asset(symbol))
    qty = min(free, float(want_qty)) if want_qty else free
    try:
        px = float(client.ticker_price(symbol))
    except Exception:  # noqa: BLE001
        px = 0.0
    if px > 0:
        return client.round_qty(symbol, qty, px)
    f = client.load_filters(symbol)
    return client.round_step(qty, f["stepSize"])


def spot_market_exit(client: BinanceClient, symbol: str, pos_qty: float | None, coid: str) -> tuple[dict, float]:
    """Exit a spot position: cancel resting stop(s) FIRST (they lock the
    balance), then market-sell free balance (≤ position qty) rounded DOWN to
    stepSize. Raises when nothing sellable."""
    cancel_symbol_orders(client, symbol)
    q = sellable_qty(client, symbol, pos_qty)
    if q <= 0:
        raise RuntimeError(f"no sellable {base_asset(symbol)} after cancel (pos_qty={pos_qty})")
    order = client.market_sell(symbol, q, coid)
    return order, q


def place_hard_stop(client: BinanceClient, symbol: str, qty: float, stop: float, slot_id: str) -> dict:
    """Cancel existing opens for symbol then place STOP_LOSS_LIMIT on the free
    balance (≤ qty) — never more than the account actually holds."""
    cancel_symbol_orders(client, symbol)
    try:
        q = sellable_qty(client, symbol, qty)
    except Exception as e:  # noqa: BLE001
        log.warning("stop_qty_lookup_skip symbol=%s err=%s", symbol, e)
        q = qty
    if q <= 0:
        raise RuntimeError(f"stop qty 0 for {symbol}")
    coid = client_order_id(slot_id, "stop")
    limit = stop * 0.995
    order = client.stop_loss_limit(symbol, q, stop, limit, coid)
    log.info("hard_stop_placed symbol=%s qty=%s stop=%s orderId=%s", symbol, q, stop, order.get("orderId"))
    return order


def replace_trail_stop(client: BinanceClient, pos: dict, new_stop: float, slot_id: str) -> dict:
    """Ratchet: only raise stop; cancel+replace exchange stop."""
    old = float(pos.get("stop") or 0)
    if new_stop <= old:
        return {"skipped": "not_higher", "stop": old}
    symbol = pos["symbol"]
    qty = float(pos["qty"])
    order = place_hard_stop(client, symbol, qty, new_stop, slot_id)
    pos["stop"] = new_stop
    pos["stop_order_id"] = order.get("orderId")
    pos["stop_client_order_id"] = order.get("clientOrderId")
    pos["stop_updated_at"] = now_iso_taipei()
    return order


def market_close_slot(
    client: BinanceClient,
    state: dict,
    slot_id: str,
    reason: str = "manual",
) -> dict:
    """Cancel stops, market-sell full free qty (LOT_SIZE), record closed trade."""
    positions = state.setdefault("positions", {})
    pos = positions.get(slot_id)
    if not pos or float(pos.get("qty") or 0) <= 0:
        return {"ok": False, "error": "此槽位目前沒有持倉", "slot": slot_id}

    symbol = pos["symbol"]
    if pos.get("venue") == "futures":
        from futures_client import FuturesDemoClient
        from futures_execution import close_position_market
        fc = FuturesDemoClient()
        is_long = str(pos.get("side") or "LONG").upper() == "LONG"
        qty = float(pos["qty"])
        coid = client_order_id(slot_id, "mclose")
        order = close_position_market(fc, symbol=symbol, qty=qty, is_long=is_long, client_order_id=coid)
        entry = float(pos.get("entry") or 0)
        fill_px = entry
        pnl = None
        closed = {
            "slot": slot_id, "symbol": symbol, "qty": qty, "entry": entry, "exit": fill_px,
            "pnl_usdt": pnl, "reason": reason, "side": pos.get("side"), "venue": "futures",
            "closed_at": now_iso_taipei(), "order_id": order.get("orderId"),
        }
        state.setdefault("closed_trades", []).append(closed)
        state["closed_trades"] = state["closed_trades"][-200:]
        positions.pop(slot_id, None)
        return {"ok": True, "closed": closed}
    # Cancel resting stop FIRST (it locks the balance), then sell free qty.
    try:
        px = client.ticker_price(symbol)
    except Exception:  # noqa: BLE001
        px = float(pos.get("entry") or 0)
    cancel_symbol_orders(client, symbol)
    q = sellable_qty(client, symbol, float(pos["qty"]))
    if q <= 0:
        return {"ok": False, "error": "數量低於 LOT_SIZE / MIN_NOTIONAL", "slot": slot_id}

    coid = client_order_id(slot_id, "mclose", datetime.now(timezone.utc).isoformat())
    order = client.market_sell(symbol, q, coid)
    entry = float(pos.get("entry") or 0)
    fill_px = px
    fills = order.get("fills") or []
    if fills:
        notional = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        qty2 = sum(float(f["qty"]) for f in fills)
        fill_px = notional / qty2 if qty2 else px
    pnl = (fill_px - entry) * q if entry else None
    closed = {
        "slot": slot_id,
        "symbol": symbol,
        "qty": q,
        "entry": entry,
        "exit": fill_px,
        "pnl_usdt": round(pnl, 4) if pnl is not None else None,
        "reason": reason,
        "closed_at": now_iso_taipei(),
        "order_id": order.get("orderId"),
    }
    state.setdefault("closed_trades", []).append(closed)
    state["closed_trades"] = state["closed_trades"][-200:]
    positions.pop(slot_id, None)
    meta = state.setdefault("slots", {}).setdefault(slot_id, {})
    meta["last_exit_bar_ts"] = closed["closed_at"]
    meta["last_exit_reason"] = reason
    # Re-arm: satellites stay armed; SOL needs reset latch
    if slot_id == "core_sol":
        meta["needs_reset_latch"] = True
    log.info("manual_close slot=%s qty=%s px=%s reason=%s", slot_id, q, fill_px, reason)
    return {"ok": True, "closed": closed}


def capped_quote(quote: float) -> float:
    return min(float(quote), float(MAX_NOTIONAL_USDT))


# --- Detect positions closed on the exchange (stop filled / balance gone) ---

def _ms_to_taipei(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(TZ).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return None


def _bar_floor_iso(ms, tf: str | None) -> str | None:
    try:
        from indicators import interval_ms
        step = interval_ms(tf or "1h")
        t = (int(ms) // step) * step
        return datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _entry_ms(pos: dict) -> int:
    for k in ("filled_at", "entry_time"):
        v = pos.get(k)
        if v:
            try:
                return int(datetime.fromisoformat(str(v)).timestamp() * 1000)
            except Exception:  # noqa: BLE001
                pass
    return 0


def _record_exchange_close(state: dict, slot_id: str, pos: dict, *, qty: float, price: float | None,
                           time_ms, order_id, reason: str, venue: str, tf: str | None) -> dict:
    entry = float(pos.get("entry") or 0)
    is_long = str(pos.get("side") or "LONG").upper() == "LONG"
    pnl = None
    if entry and price:
        pnl = round(((price - entry) if is_long else (entry - price)) * qty, 4)
    closed = {
        "slot": slot_id, "symbol": pos.get("symbol"), "qty": qty, "entry": entry,
        "exit": price, "pnl_usdt": pnl, "reason": reason, "venue": venue,
        "closed_at": _ms_to_taipei(time_ms) or now_iso_taipei(),
        "order_id": order_id, "strategy_id": pos.get("strategy_id"),
        "detected_at": now_iso_taipei(), "source": "exchange_reconcile",
    }
    if venue == "futures":
        closed["side"] = pos.get("side")
    state.setdefault("closed_trades", []).append(closed)
    state["closed_trades"] = state["closed_trades"][-200:]
    state.setdefault("positions", {}).pop(slot_id, None)
    meta = state.setdefault("slots", {}).setdefault(slot_id, {})
    bar = _bar_floor_iso(time_ms, tf) if time_ms else None
    meta["last_exit_bar_ts"] = bar or meta.get("last_exit_bar_ts")
    meta["last_acted_bar_ts"] = bar or meta.get("last_acted_bar_ts")
    meta["last_exit_reason"] = reason
    log.info("exchange_close_recorded slot=%s qty=%s px=%s reason=%s order=%s", slot_id, qty, price, reason, order_id)
    return closed


def _spot_closed_info(client, slot_id: str, pos: dict) -> dict | None:
    symbol = pos["symbol"]
    sid = pos.get("stop_order_id")
    if sid:
        try:
            o = client.get_order(symbol, int(sid))
            if str(o.get("status")) == "FILLED" and float(o.get("executedQty") or 0) > 0:
                q = float(o["executedQty"])
                quote = float(o.get("cummulativeQuoteQty") or 0)
                return {"qty": q, "price": (quote / q) if quote and q else float(o.get("price") or 0) or None,
                        "time_ms": o.get("updateTime") or o.get("time"), "order_id": o.get("orderId"),
                        "reason": "stop"}
        except Exception as e:  # noqa: BLE001
            log.warning("stop_order_lookup_skip slot=%s err=%s", slot_id, e)
    # Balance gone? (free+locked below tradable size)
    base = base_asset(symbol)
    held = 0.0
    for b in client.nonzero_balances():
        if b.get("asset") == base:
            held = float(b.get("free") or 0) + float(b.get("locked") or 0)
            break
    f = client.load_filters(symbol)
    try:
        px = float(client.ticker_price(symbol))
    except Exception:  # noqa: BLE001
        px = float(pos.get("entry") or 0)
    if held >= float(f.get("minQty") or 0) and held * px >= float(f.get("minNotional") or 5.0) and held > 0:
        return None
    # Position gone: find the SELL(s) since entry, most recent order
    since = _entry_ms(pos)
    sells = [t for t in (client.my_trades(symbol, limit=100) or [])
             if not t.get("isBuyer") and int(t.get("time") or 0) >= since]
    if sells:
        last_oid = sells[-1].get("orderId")
        rows = [t for t in sells if t.get("orderId") == last_oid]
        q = sum(float(t["qty"]) for t in rows)
        quote = sum(float(t.get("quoteQty") or float(t["qty"]) * float(t["price"])) for t in rows)
        reason = "stop" if (sid and str(last_oid) == str(sid)) or not sid else "external_sell"
        return {"qty": q, "price": quote / q if q else None, "time_ms": rows[-1].get("time"),
                "order_id": last_oid, "reason": reason}
    return {"qty": float(pos.get("qty") or 0), "price": None, "time_ms": None,
            "order_id": None, "reason": "position_gone"}


def _futures_closed_info(fc, slot_id: str, pos: dict) -> dict | None:
    symbol = pos["symbol"]
    rows = fc.position_risk(symbol) or []
    amt = sum(abs(float(r.get("positionAmt") or 0)) for r in rows if r.get("symbol") == symbol)
    if amt > 1e-12:
        return None
    since = _entry_ms(pos)
    is_long = str(pos.get("side") or "LONG").upper() == "LONG"
    close_side = "SELL" if is_long else "BUY"
    trades = [t for t in (fc.user_trades(symbol, limit=100) or [])
              if int(t.get("time") or 0) >= since and str(t.get("side")) == close_side]
    if trades:
        last_oid = trades[-1].get("orderId")
        rs = [t for t in trades if t.get("orderId") == last_oid]
        q = sum(float(t["qty"]) for t in rs)
        quote = sum(float(t.get("quoteQty") or float(t["qty"]) * float(t["price"])) for t in rs)
        return {"qty": q, "price": quote / q if q else None, "time_ms": rs[-1].get("time"),
                "order_id": last_oid, "reason": "stop"}
    return {"qty": float(pos.get("qty") or 0), "price": None, "time_ms": None,
            "order_id": None, "reason": "position_gone"}


def reconcile_exchange_closes(client, state: dict, *, only_slots: set | None = None, tf_by_slot: dict | None = None) -> list[dict]:
    """Close state positions the exchange already closed (stop filled or
    balance/positionAmt ≈ 0). Read-only on the exchange except cancelling
    leftover futures algo stops — never sends a sell/buy."""
    out = []
    for slot_id, pos in list((state.get("positions") or {}).items()):
        if not isinstance(pos, dict) or pos.get("status") != "FILLED":
            continue
        if only_slots and slot_id not in only_slots:
            continue
        venue = str(pos.get("venue") or "spot")
        tf = (tf_by_slot or {}).get(slot_id) or pos.get("tf")
        try:
            if venue == "futures":
                from futures_client import FuturesDemoClient
                from futures_execution import cancel_algo_stops
                fc = FuturesDemoClient()
                info = _futures_closed_info(fc, slot_id, pos)
                if info:
                    cancel_algo_stops(fc, pos["symbol"])
            else:
                info = _spot_closed_info(client, slot_id, pos)
            if info:
                out.append(_record_exchange_close(state, slot_id, pos, venue=venue, tf=tf, **info))
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile_close_skip slot=%s err=%s", slot_id, e)
    return out
