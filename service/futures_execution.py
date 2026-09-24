"""Futures Demo order helpers: isolated margin, capped leverage, reduce-only stops."""
from __future__ import annotations

import logging
from typing import Any

from futures_client import (
    FuturesDemoClient,
    clamp_leverage,
    round_price_futures,
    round_qty_futures,
)

log = logging.getLogger("trader.futures_exec")


def prepare_symbol(client: FuturesDemoClient, symbol: str, leverage: float | int) -> dict:
    lev = clamp_leverage(leverage)
    margin = client.set_margin_type(symbol, "ISOLATED")
    lev_resp = client.set_leverage(symbol, lev)
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
) -> dict[str, Any]:
    """Open MARKET. Book counts full notional (not margin)."""
    prep = prepare_symbol(client, symbol, leverage)
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
    return {
        "order": order,
        "qty": qty,
        "mark": mark,
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
) -> dict:
    info = client.exchange_info()
    stop = round_price_futures(info, symbol, stop_price)
    q = round_qty_futures(info, symbol, abs(qty))
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": "SELL" if is_long else "BUY",
        "type": "STOP_MARKET",
        "stopPrice": stop,
        "quantity": q,
        "reduceOnly": "true",
        "workingType": "MARK_PRICE",
    }
    if client_order_id:
        params["newClientOrderId"] = str(client_order_id)[:36]
    return client.new_order(**params)


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
