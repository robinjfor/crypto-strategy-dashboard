"""Live risk controls for demo_funding_arb_1x (SPEC.md "must implement"). Active ONLY when the slot is enabled.
(1) Squeeze: perp leg mark loss > 50% of its isolated margin -> top up margin back to notional/3 from free
    futures balance; if the balance cannot cover it, reduce BOTH legs by 50% (stays delta-neutral).
(2) Drift: |spot notional - perp notional| > 5% of the position -> trim the larger leg down to the smaller one.
Never increases exposure. Every action -> control_log actor=guard. Positions come from state["demo_fa_positions"]
({sym: {"spot_qty": q, "perp_qty": q}}, perp_qty positive = short size) kept by the slot executor."""
from __future__ import annotations

import logging

import demo_fa
from guards import log_event

log = logging.getLogger("demo_fa_guard")
SQUEEZE_FRAC = 0.5
DRIFT_LIMIT = 0.05
REDUCE_FRAC = 0.5


def run(state: dict, spot, fut) -> list[dict]:
    g = (state.get("demo_candidates") or {}).get(demo_fa.SLOT) or {}
    if not g.get("enabled") or (state.get("paused_slots") or {}).get(demo_fa.SLOT):
        return []
    from futures_client import round_qty_futures
    book = state.setdefault("demo_fa_positions", {})
    if not book:
        return []
    info = fut.exchange_info()
    risk = {r["symbol"]: r for r in fut.position_risk() if float(r.get("positionAmt") or 0) != 0}
    free = float((fut.account() or {}).get("availableBalance") or 0)
    acts = []

    def act(action, sym, **kw):
        log_event(state, action, demo_fa.SLOT, symbol=sym, **kw); acts.append({"action": action, "symbol": sym, **kw})

    for sym, pos in list(book.items()):
        try:
            sq, pq = float(pos.get("spot_qty") or 0), float(pos.get("perp_qty") or 0)
            r = risk.get(sym)
            if not r or sq <= 0 or pq <= 0:
                continue
            mark = float(r["markPrice"]); upl = float(r.get("unRealizedProfit") or 0)
            margin = float(r.get("isolatedMargin") or r.get("isolatedWallet") or 0)
            loss = -upl
            if margin > 0 and loss > SQUEEZE_FRAC * margin:
                need = pq * mark * demo_fa.MARGIN - (margin - loss)
                if 0 < need <= free:
                    fut.add_isolated_margin(sym, need); free -= need
                    act("fa_margin_topup", sym, amount=round(need, 2), loss=round(loss, 2), margin=round(margin, 2))
                else:
                    q_p = round_qty_futures(info, sym, pq * REDUCE_FRAC)
                    q_s = spot.round_step(sq * REDUCE_FRAC, spot.load_filters(sym)["stepSize"])
                    if q_p > 0:
                        fut.new_order(symbol=sym, side="BUY", type="MARKET", quantity=q_p, reduceOnly="true",
                                      newClientOrderId=f"faG-red-{sym[:14]}")
                        pq -= q_p
                    if q_s > 0:
                        spot.market_sell(sym, q_s, f"faG-red-{sym[:14]}"); sq -= q_s
                    book[sym] = {"spot_qty": sq, "perp_qty": pq}
                    act("fa_reduce_squeeze", sym, perp_qty=q_p, spot_qty=q_s, loss=round(loss, 2), margin=round(margin, 2),
                        reason="可用保證金不足，兩腳各減半")
                    continue
            sp_px = spot.ticker_price(sym)
            ns, np_ = sq * sp_px, pq * mark
            base = (ns + np_) / 2
            if base > 0 and abs(ns - np_) > DRIFT_LIMIT * base:
                if ns > np_:
                    q = spot.round_step((ns - np_) / sp_px, spot.load_filters(sym)["stepSize"])
                    if q > 0:
                        spot.market_sell(sym, q, f"faG-reb-{sym[:14]}"); book[sym]["spot_qty"] = sq - q
                        act("fa_rebalance", sym, leg="spot", qty=q, drift=round((ns - np_) / base, 4))
                else:
                    q = round_qty_futures(info, sym, (np_ - ns) / mark)
                    if q > 0:
                        fut.new_order(symbol=sym, side="BUY", type="MARKET", quantity=q, reduceOnly="true",
                                      newClientOrderId=f"faG-reb-{sym[:14]}")
                        book[sym]["perp_qty"] = pq - q
                        act("fa_rebalance", sym, leg="perp", qty=q, drift=round((np_ - ns) / base, 4))
        except Exception as e:  # noqa: BLE001
            log.warning("fa_guard_fail sym=%s err=%s", sym, e)
            act("fa_guard_error", sym, error=str(e)[:120])
    return acts
