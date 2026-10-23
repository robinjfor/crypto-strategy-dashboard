"""Demo slot: funding-rate arb (long spot + short USDT-M perp, net 1x, 1,000 USDT). DISABLED (enabled=False).
Spec: funding_arb/SPEC.md + research_1023/REPORT.md, fixed WF pick exit_thr=0, K=10. Live must equal funding_arb.py.
The planner here is checked day-by-day (held set, trades, daily P&L) against funding_arb.run(0, 10) in the cloud
shadow job (trader/shadow_ab/parity_FA.json). Ordering gate: every first-day order validated on Demo with
/api/v3/order/test (spot BUY) and /fapi/v1/order/test (perp SELL); no fills. Weekly deviation >0.5% -> pause."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

SLOT = "demo_funding_arb_1x"
CAPITAL = 1000.0
K = 10
EXIT_THR = 0.0
ENTRY = 0.15
MARGIN = 1 / 3
COST = 0.002


def decide(prev, u, f7, f1, avail, k=K, exit_thr=EXIT_THR):
    """One decision at close t. prev/u/avail bool arrays, f7/f1 float arrays (nan = missing). Mirrors funding_arb.run."""
    keep = prev & ~np.isnan(f7) & (f7 >= exit_thr) & ~(f1 < 0) & avail
    cand = ~keep & u & (np.nan_to_num(f7, nan=-1) > ENTRY) & avail & ~(f1 < 0)
    free = k - keep.sum(); new = keep.copy()
    if free > 0 and cand.any():
        idx = np.where(cand)[0]; idx = idx[np.argsort(-f7[idx])][:free]; new[idx] = True
    if new.sum() > k:
        idx = np.where(new)[0]; drop = idx[np.argsort(np.nan_to_num(f7[idx]))][: new.sum() - k]; new[drop] = False
    return new


def simulate(u, f7, f1, rs, rp, fn, k=K, exit_thr=EXIT_THR):
    n, m = f7.shape
    held = np.zeros(m, bool); pnl = np.zeros(n); npos = np.zeros(n); trades = np.zeros(n, int); hs = []
    notional = 1.0 / k / (1 + MARGIN)
    for i in range(n):
        avail = ~np.isnan(rs[i]) & ~np.isnan(rp[i])
        new = decide(held, u[i], f7[i], f1[i], avail, k, exit_thr)
        trades[i] = (new != held).sum()
        pnl[i] = (notional * (np.nan_to_num(rs[i][new]) - np.nan_to_num(rp[i][new]) + np.nan_to_num(fn[i][new]))).sum() - trades[i] * notional * 2 * COST
        npos[i] = new.sum(); held = new; hs.append(new.copy())
    return pnl, npos, trades, hs


def parity(led_r, led_n, my_r, my_n, trades, start_i: int, tol: float = 1e-12) -> dict:
    a, b = np.asarray(led_r)[start_i:], np.asarray(my_r)[start_i:]
    na, nb = np.asarray(led_n)[start_i:], np.asarray(my_n)[start_i:]
    bad = int(((np.abs(a - b) > tol) | (na != nb)).sum())
    return {"days": int(len(a)), "trades": int(np.asarray(trades)[start_i:].sum()), "mismatch_days": bad,
            "max_abs_err": float(np.max(np.abs(a - b))) if len(a) else 0.0, "pass": len(a) > 0 and bad == 0}


def first_day_orders(targets: dict, equity: float = CAPITAL) -> list[dict]:
    notional = equity / int(targets.get("K", K)) / (1 + MARGIN)
    out = []
    # nothing held today -> validate both legs on a probe symbol (top liquidity in universe) so the gate still covers spot+perp
    for sym in (targets.get("held") or ([targets["probe"]] if targets.get("probe") else [])):
        out.append({"symbol": sym, "leg": "spot", "side": "BUY", "notional": notional})
        out.append({"symbol": sym, "leg": "perp", "side": "SELL", "notional": notional})
    return out


def ordering_check(spot, fut, targets: dict) -> dict:
    """spot: BinanceClient (Demo spot), fut: FuturesDemoClient. Only */order/test endpoints are called."""
    from futures_client import round_qty_futures, _filters
    info = fut.exchange_info()
    orders = first_day_orders(targets)
    ok = rej = below = 0; errs: list[str] = []
    for o in orders:
        s = o["symbol"]
        try:
            if o["leg"] == "spot":
                px = spot.ticker_price(s); q = spot.round_qty(s, o["notional"] / px, px)
                if q <= 0:
                    below += 1; errs.append(f"{s} 現貨低於最小量"); continue
                spot.test_order(symbol=s, side="BUY", type="MARKET", quantity=q)
            else:
                px = fut.mark_price(s); q = round_qty_futures(info, s, o["notional"] / px)
                mnf = _filters(info, s).get("MIN_NOTIONAL") or {}
                if q <= 0 or q * px < float(mnf.get("notional") or 5.0):
                    below += 1; errs.append(f"{s} 永續低於最小金額"); continue
                fut.test_order(symbol=s, side="SELL", type="MARKET", quantity=q)
            ok += 1
        except Exception as e:  # noqa: BLE001
            rej += 1; errs.append(f"{s} {o['leg']}: {str(e)[:80]}")
    return {"orders": len(orders), "accepted": ok, "rejected": rej, "below_min": below, "errors": errs[:10],
            "held": targets.get("held"), "decided_bar": targets.get("decided_bar"),
            "probe": None if targets.get("held") else targets.get("probe"),
            "pass": len(orders) > 0 and rej == 0 and below == 0,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
