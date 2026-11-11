"""Alert-only distance from mark to the exchange liquidation price.

distance = |mark - liquidationPrice| / mark. Under 15% appends one reconcile alert
(symbol, distance, liq, mark, side, holders). It never pauses a slot and never sends an order.
The same symbol+side is not alerted again until the distance recovers to 15% or more.
"""
from __future__ import annotations

THRESHOLD = 0.15
_FIELDS = ("liquidationPrice", "isolatedMargin", "isolatedWallet", "notional", "maintMargin")


def _f(row: dict, *names: str) -> float | None:
    for n in names:
        raw = row.get(n)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def distance(mark: float | None, liq: float | None) -> float | None:
    if mark is None or liq is None or mark <= 0 or liq <= 0:
        return None
    return abs(mark - liq) / mark


def side_of(qty: float) -> str:
    if qty > 0:
        return "LONG"
    if qty < 0:
        return "SHORT"
    return "FLAT"


def scan(rows: list | None, holders: dict | None = None) -> dict:
    """One row per open position. `near` is the subset closer than THRESHOLD."""
    holders = holders or {}
    positions = []
    near = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            qty = float(row.get("positionAmt") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        sym = str(row.get("symbol") or "")
        mark = _f(row, "markPrice")
        liq = _f(row, "liquidationPrice")
        dist = distance(mark, liq)
        rec = {
            "symbol": sym,
            "side": side_of(qty),
            "qty": qty,
            "mark": mark,
            "liq": liq,
            "distance": None if dist is None else round(dist, 6),
            "isolatedMargin": _f(row, "isolatedMargin"),
            "isolatedWallet": _f(row, "isolatedWallet"),
            "notional": _f(row, "notional") if row.get("notional") not in (None, "") else (
                None if mark is None else qty * mark),
            "maintMargin": _f(row, "maintMargin"),
            "leverage": row.get("leverage"),
            "marginType": row.get("marginType"),
            "holders": list(holders.get(sym) or []),
        }
        positions.append(rec)
        if dist is not None and dist < THRESHOLD:
            near.append(rec)
    return {"threshold": THRESHOLD, "positions": positions, "near": near}


def snapshot_position(row: dict) -> dict:
    """Fields kept on the read-only account snapshot. No orders."""
    keep = ("symbol", "positionAmt", "entryPrice", "markPrice", "unRealizedProfit",
            "leverage", "marginType", "positionSide", *_FIELDS)
    return {k: row.get(k) for k in keep}


def apply(state: dict, rows: list | None, at: str, holders: dict | None = None) -> dict:
    """Write state['liq_distance'] and append a deduped reconcile alert. Never trades."""
    if holders is None:
        try:
            import netting
            holders = netting.holders(state)
        except Exception:  # noqa: BLE001
            holders = {}
    view = scan(rows, holders)
    view["at"] = at
    view["orders"] = 0
    view["pause"] = False
    open_keys = set((state.get("liq_distance") or {}).get("open") or [])
    still = set()
    alerts = list(state.get("reconcile_alerts") or [])
    for rec in view["near"]:
        key = f"liq_distance|{rec['symbol']}|{rec['side']}"
        still.add(key)
        if key in open_keys:
            continue  # still inside the same breach; one alert until it recovers
        alerts.append({
            "at": at, "key": key, "kind": "liq_distance", "venue": "futures",
            "symbol": rec["symbol"], "side": rec["side"], "distance": rec["distance"],
            "liq": rec["liq"], "mark": rec["mark"], "holders": rec["holders"],
            "pause": False, "orders": 0,
        })
    view["open"] = sorted(still)
    view["n_near"] = len(view["near"])
    state["liq_distance"] = view
    state["reconcile_alerts"] = alerts[-200:]
    return view
