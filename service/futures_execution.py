"""Futures Demo order helpers: isolated margin, capped leverage, reduce-only stops."""
from __future__ import annotations

import logging
from typing import Any

from futures_client import (
    LEVERAGE_HARD_CAP,
    FuturesDemoClient,
    clamp_leverage,
    round_price_futures,
    round_qty_futures,
)

log = logging.getLogger("trader.futures_exec")


def prepare_symbol(client: FuturesDemoClient, symbol: str, leverage: float | int, cap: int = LEVERAGE_HARD_CAP) -> dict:
    lev = clamp_leverage(leverage, cap)
    margin = client.set_margin_type(symbol, "ISOLATED")
    lev_resp = client.set_leverage(symbol, lev, cap)
    return {"leverage": lev, "margin": margin, "leverage_resp": lev_resp}


def mark_price(client: FuturesDemoClient, symbol: str) -> float:
    data = client._public("/fapi/v1/ticker/price", {"symbol": symbol})
    return float(data["price"])


def open_market(
    client: FuturesDemoClient,
    *,
    symbol: str,
    side: str,  # BUY long / SELL short
    notional_usdt: float,
    leverage: float | int,
    client_order_id: str | None = None,
    leverage_cap: int = LEVERAGE_HARD_CAP,
) -> dict[str, Any]:
    """Open MARKET. Book counts full notional (not margin). `avg_price` = exchange fill average when
    the order response carries it (else None; `mark` is the pre-order ticker price)."""
    prep = prepare_symbol(client, symbol, leverage, leverage_cap)
    info = client.exchange_info()
    mark = mark_price(client, symbol)
    raw_qty = float(notional_usdt) / mark if mark else 0.0
    qty = round_qty_futures(info, symbol, raw_qty)
    if qty <= 0:
        raise RuntimeError(f"qty too small after LOT_SIZE for {symbol} notional={notional_usdt}")
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "quantity": qty,
    }
    if client_order_id:
        params["newClientOrderId"] = str(client_order_id)[:36]
    order = client.new_order(**params)
    try:
        avg = float(order.get("avgPrice") or 0) or None
    except (TypeError, ValueError, AttributeError):
        avg = None
    return {
        "order": order,
        "qty": qty,
        "mark": mark,
        "avg_price": avg,
        "side": side.upper(),
        "leverage": prep["leverage"],
        "notional_usdt": qty * mark,
    }


def place_stop_reduce_only(
    client: FuturesDemoClient,
    *,
    symbol: str,
    is_long: bool,
    stop_price: float,
    qty: float,
    client_order_id: str | None = None,
    working_type: str = "MARK_PRICE",
) -> dict:
    """Conditional STOP via Algo Order API (classic STOP_MARKET retired from /order).
    working_type: MARK_PRICE (default, existing families) or CONTRACT_PRICE (last price; S family,
    matching the backtest's kline-High trigger)."""
    if working_type not in ("MARK_PRICE", "CONTRACT_PRICE"):
        raise ValueError(f"workingType {working_type!r}")
    info = client.exchange_info()
    stop = round_price_futures(info, symbol, stop_price)
    q = round_qty_futures(info, symbol, abs(qty))
    params: dict = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": "SELL" if is_long else "BUY",
        "type": "STOP_MARKET",
        "triggerPrice": str(stop),
        "quantity": str(q),
        "reduceOnly": "true",
        "workingType": working_type,
    }
    if client_order_id:
        params["clientAlgoId"] = str(client_order_id)[:36]
    return client.algo_order(**params)



def cancel_algo_stops(client: FuturesDemoClient, symbol: str) -> int:
    """Conditional stops live in the Algo API and are NOT removed by
    /fapi/v1/allOpenOrders — cancel them explicitly so no orphan stop remains."""
    n = 0
    try:
        rows = client.open_algo_orders(symbol) or []
        if isinstance(rows, dict):
            rows = rows.get("orders") or rows.get("rows") or []
        for o in rows:
            aid = o.get("algoId")
            if aid is None:
                continue
            try:
                client.cancel_algo_order(symbol=symbol, algoId=aid)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("futures_algo_cancel_skip symbol=%s algoId=%s err=%s", symbol, aid, e)
    except Exception as e:  # noqa: BLE001
        log.warning("futures_algo_list_skip symbol=%s err=%s", symbol, e)
    return n


def close_position_market(
    client: FuturesDemoClient,
    *,
    symbol: str,
    qty: float,
    is_long: bool,
    client_order_id: str | None = None,
) -> dict:
    try:
        client.cancel_all(symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("futures_cancel_before_close_skip symbol=%s err=%s", symbol, e)
    cancel_algo_stops(client, symbol)
    info = client.exchange_info()
    q = round_qty_futures(info, symbol, abs(qty))
    if q <= 0:
        raise RuntimeError("close qty too small")
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": "SELL" if is_long else "BUY",
        "type": "MARKET",
        "quantity": q,
        "reduceOnly": "true",
    }
    if client_order_id:
        params["newClientOrderId"] = str(client_order_id)[:36]
    return client.new_order(**params)


def list_open_futures_positions(client: FuturesDemoClient) -> list[dict]:
    """Shape compatible with homepage actualHoldings (symbol carries 多/空×lev)."""
    rows = []
    for p in client.position_risk() or []:
        amt = float(p.get("positionAmt") or 0)
        if abs(amt) < 1e-12:
            continue
        entry = float(p.get("entryPrice") or 0)
        mark = float(p.get("markPrice") or 0)
        upnl = float(p.get("unRealizedProfit") or 0)
        lev = int(float(p.get("leverage") or 1))
        side = "LONG" if amt > 0 else "SHORT"
        notional = abs(amt) * mark
        sym = p.get("symbol") or ""
        label = f"{sym} {'多' if amt > 0 else '空'}×{lev}"
        rows.append(
            {
                "symbol": label,
                "raw_symbol": sym,
                "asset": f"{sym.replace('USDT', '')} {'多' if amt > 0 else '空'}×{lev}",
                "qty": abs(amt),
                "entry": entry,
                "mark": mark,
                "unrealized_pnl": upnl,
                "unrealized": upnl,
                "market_value": notional,
                "side": side,
                "leverage": lev,
                "venue": "futures",
                "status": "FILLED",
                "tf": "",
                "slot": f"fut_{sym}_{side.lower()}",
            }
        )
    return rows
