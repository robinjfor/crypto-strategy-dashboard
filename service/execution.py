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
