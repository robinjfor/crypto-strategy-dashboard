"""Isolated USD-M liquidation on one net position per symbol.

Mirrors the live netter: one exchange position, leverage = min(LEV_BY_SLOT) of the
slots in that flush, isolated wallet = |qty| * entry / leverage. Maintenance comes
from the Binance USD-M risk brackets.

Source of the tier table (fetched, not typed by hand):
  GET https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets
  fields bracketNotionalCap, bracketMaintenanceMarginRate, cumFastMaintenanceAmount.
  Snapshot saved next to the research output. Unknown symbols use a 1% tier
  (cum 0), which is at least as strict as the first tier of the small-alt contracts
  in that table.

Binance isolated balance (one position, no extra wallet top-up):
  long  liq = (entry * (1 - 1/lev) - cum/qty) / (1 - mmr)
  short liq = (entry * (1 + 1/lev) + cum/|qty|) / (1 + mmr)
A long liquidates if the bar low (or a gap open) prints at or below liq.
A short liquidates if the bar high (or a gap open) prints at or above liq.
The book loses the isolated margin plus a taker fee on the liquidation notional
(0.05%, the live FEE). It does not lose more than that: the rest of the account
is not the isolated wallet.
"""
from __future__ import annotations

from typing import Iterable

FEE = 0.0005
DEFAULT_TIER = ((1e18, 0.01, 0.0),)  # cap, maintenance rate, cum amount


def bracket_for(brackets: dict | None, symbol: str, notional: float) -> tuple[float, float]:
    tiers: Iterable = (brackets or {}).get(symbol) or (brackets or {}).get("_default") or DEFAULT_TIER
    n = abs(float(notional))
    last = (0.01, 0.0)
    for row in tiers:
        cap, mmr, cum = float(row[0]), float(row[1]), float(row[2])
        last = (mmr, cum)
        if n <= cap:
            return mmr, cum
    return last


def liquidation_price(entry: float, qty: float, leverage: float, mmr: float, cum: float) -> float | None:
    if entry <= 0 or qty == 0 or leverage <= 0 or mmr >= 1:
        return None
    if qty > 0:
        denom = 1.0 - mmr
        if denom <= 0:
            return None
        return (entry * (1.0 - 1.0 / leverage) - cum / qty) / denom
    q = abs(qty)
    return (entry * (1.0 + 1.0 / leverage) + cum / q) / (1.0 + mmr)


def breached(side_qty: float, liq: float | None, adverse: float) -> bool:
    if liq is None or adverse <= 0:
        return False
    if side_qty > 0:
        return adverse <= liq
    if side_qty < 0:
        return adverse >= liq
    return False


def liquidation_loss(entry: float, qty: float, leverage: float, liq: float, fee: float = FEE) -> float:
    """Isolated margin plus the taker fee on the liquidation notional. Always >= 0."""
    margin = abs(qty) * entry / leverage
    return margin + fee * abs(qty) * liq


def settle_liquidation(cash: float, entry: float, qty: float, leverage: float, liq: float, fee: float = FEE) -> tuple[float, float]:
    """Full-notional cash book: drop the position and leave only the isolated loss."""
    loss = liquidation_loss(entry, qty, leverage, liq, fee)
    cash = cash + qty * entry - loss
    return cash, 0.0


def _liq_one(cash, qty, entry, leverage, adverse, brackets, symbol, fee):
    if qty == 0 or entry <= 0:
        return cash, qty, entry, 0.0, False
    mmr, cum = bracket_for(brackets, symbol, qty * entry)
    liq = liquidation_price(entry, qty, leverage, mmr, cum)
    if not breached(qty, liq, adverse):
        return cash, qty, entry, 0.0, False
    loss = liquidation_loss(entry, qty, leverage, liq, fee)
    cash, qty = settle_liquidation(cash, entry, qty, leverage, liq, fee)
    return cash, qty, 0.0, loss, True


def replay(opens, highs, lows, closes, weights, equity0: float, *, leverage: float | None,
           brackets: dict | None = None, symbols: list | None = None,
           cost: float = 0.002, fee: float = FEE, min_order: float = 5.0):
    """Close-to-close book. Weight row t-1 fills at the open of t.

    leverage is None for the current backtest (full mark-to-market, no isolated stop).
    Otherwise each symbol is an isolated position at that leverage on its own qty
    (the net, when the book is one strategy). A gap through the open liquidates
    before the rebalance. After the fill, the bar high (short) or low (long) can
    liquidate the new position.
    """
    import demo_r2s as D
    import numpy as np
    O, H, L, C, W = (np.asarray(x, dtype=float) for x in (opens, highs, lows, closes, weights))
    T, N = C.shape
    cash = float(equity0)
    qty = np.zeros(N)
    ent = np.zeros(N)
    eq = np.full(T, np.nan)
    eq[0] = equity0
    n_liq = 0
    loss = 0.0
    syms = list(symbols) if symbols is not None else [str(i) for i in range(N)]

    def mtm(px):
        return cash + float(np.nansum(qty * px))

    for t in range(1, T):
        px_o, px_h, px_l, px_c = O[t], H[t], L[t], C[t]
        if leverage:
            for j in range(N):
                if qty[j] == 0 or not np.isfinite(px_o[j]):
                    continue
                cash, q2, e2, lost, hit = _liq_one(cash, qty[j], ent[j], leverage, px_o[j], brackets, syms[j], fee)
                if hit:
                    qty[j], ent[j] = q2, e2
                    n_liq += 1
                    loss += lost
        finite = np.isfinite(px_o)
        equity = cash + float(np.nansum(qty * np.where(finite, px_o, 0.0)))
        w = W[t - 1]
        for j in range(N):
            p = px_o[j]
            if not np.isfinite(p) or p <= 0:
                continue
            new, _kind = D.decide_minnot(float(w[j]) if np.isfinite(w[j]) else 0.0, float(qty[j]), float(p), equity, min_order, 0.0)
            dq = new - qty[j]
            if dq == 0:
                continue
            cash -= dq * p + abs(dq) * p * cost
            if new == 0:
                ent[j] = 0.0
            elif qty[j] == 0 or (new > 0) != (qty[j] > 0):
                ent[j] = p
            elif abs(new) > abs(qty[j]):
                ent[j] = (ent[j] * qty[j] + p * dq) / new
            qty[j] = new
        if leverage:
            for j in range(N):
                if qty[j] == 0:
                    continue
                adverse = px_l[j] if qty[j] > 0 else px_h[j]
                if not np.isfinite(adverse):
                    continue
                cash, q2, e2, lost, hit = _liq_one(cash, qty[j], ent[j], leverage, float(adverse), brackets, syms[j], fee)
                if hit:
                    qty[j], ent[j] = q2, e2
                    n_liq += 1
                    loss += lost
        mark = np.where(np.isfinite(px_c), px_c, 0.0)
        eq[t] = cash + float(np.nansum(qty * mark))
    return {"equity": eq, "n_liq": n_liq, "liq_loss": loss}
