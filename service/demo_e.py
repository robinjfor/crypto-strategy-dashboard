"""Demo slot E (80% U1 tv0.40 / 20% S5, x1.75, 1,000 USDT) on Binance futures Demo. DISABLED (enabled=False).
Spec: research_1022/shadow_AB/ledger_e.py (+ ledger_c.py engine). The planner below is the order logic; it is
checked trade-by-trade against the analyst engine (shadow job writes trader/shadow_ab/parity_E.json).
Gates (state["demo_candidates"][SLOT]): cloud_ordering_ok (every planned first-day order accepted by Demo
/fapi/v1/order/test, no fills) and parity_pass. Until Robin approves, nothing here sends a real (filling) order.
Once enabled: daily reconcile vs ledger E; weekly |demo/ledger - 1| > 0.5% -> pause this slot only."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

log = logging.getLogger("demo_e")
SLOT = "demo_e_8020_x175"
CAPITAL = 1000.0
BAND = 0.25
WEEKLY_DEV_LIMIT = 0.005
U1_SYMS = ("BTCUSDC", "ETHUSDC")


# ---------------- planner (must equal ledger_c.py rebalance rules)
def plan_u1(u_prev, w_lev, uo, eu):
    out = np.array(u_prev, dtype=float).copy()
    for j in range(len(out)):
        wt = w_lev[j]
        wc = out[j] * uo[j] / eu if eu > 0 else 0
        if (out[j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > BAND * abs(wt)):
            out[j] = wt * eu / uo[j]
    return out


def plan_s5(cur_u: pd.Series, tw: pd.Series, db: pd.Series, o5: pd.Series, es: float) -> pd.Series:
    cur_u = cur_u.reindex(tw.index).fillna(0.0)
    o5 = o5.reindex(tw.index)
    cw = cur_u * o5.fillna(0) / es if es > 0 else cur_u * 0
    trade = ((tw - cw).abs() > db) | ((tw == 0) & (cw != 0)) | (np.sign(tw) != np.sign(cw))
    trade &= o5.notna()
    nw = cw.where(~trade, tw)
    return (nw * es / o5).where(nw != 0, 0.0).fillna(0.0)


# ---------------- trade-by-trade parity on captured engine state
def parity(snaps: list[dict], w_u1: float = 0.8, tol: float = 1e-9) -> dict:
    days = trades = bad = 0
    worst = 0.0
    first_bad = None
    prev_u1 = np.zeros(2)
    for s in snaps:
        nz = s["nw"][s["nw"] != 0]
        if len(nz):
            k = nz.index[0]
            es = float(s["nu"][k] * s["o5"][k] / s["nw"][k])
        else:
            prev_u1 = s["u1_u"]
            continue
        days += 1
        got5 = plan_s5(s["cur_u"], s["tw"], s["db"], s["o5"], es)
        exp5 = s["nu"].reindex(got5.index).fillna(0.0)
        cur = s["cur_u"].reindex(got5.index).fillna(0.0)
        t_exp, t_got = (exp5 != cur), (got5 != cur)
        eu = es * w_u1 / (1 - w_u1)
        got1 = plan_u1(prev_u1, s["lev"] if s["lev"] is not None else np.zeros(2), s["uo"], eu)
        exp1 = s["u1_u"] if not s["liq"] else got1
        t1 = (exp1 != prev_u1)
        trades += int(t_exp.sum() + t1.sum())
        den5 = exp5.abs().where(exp5 != 0, 1.0)
        e5 = float(((got5 - exp5).abs() / den5).max() if len(exp5) else 0.0)
        e1 = float(np.max(np.abs(got1 - exp1) / np.where(exp1 != 0, np.abs(exp1), 1.0)))
        err = max(e5, e1)
        worst = max(worst, err)
        if err > tol or (t_exp != t_got).any() or ((got1 != prev_u1) != t1).any():
            bad += 1
            first_bad = first_bad or s["date"]
        prev_u1 = s["u1_u"]
    return {"days": days, "trades": trades, "mismatch_days": bad, "max_rel_err": worst, "first_mismatch": first_bad,
            "pass": days > 0 and bad == 0, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ---------------- orders from targets (first-day book from flat, scaled to CAPITAL)
def first_day_orders(targets: dict, prices: dict, equity: float = CAPITAL) -> list[dict]:
    w = float(targets.get("w_u1", 0.8))
    out = []
    for sym, wt in (targets.get("u1_w") or {}).items():
        if wt and prices.get(sym):
            out.append({"symbol": sym, "side": "BUY" if wt > 0 else "SELL", "qty": abs(wt) * w * equity / prices[sym], "sleeve": "u1"})
    for sym, wt in (targets.get("s5_w") or {}).items():
        if wt and prices.get(sym):
            out.append({"symbol": sym, "side": "BUY" if wt > 0 else "SELL", "qty": abs(wt) * (1 - w) * equity / prices[sym], "sleeve": "s5"})
    return out


def ordering_check(client, targets: dict) -> dict:
    """Validate every first-day order on Demo with /fapi/v1/order/test (no fills). Returns summary."""
    from futures_client import round_qty_futures
    info = client.exchange_info()
    prices = {}
    for sym in list((targets.get("u1_w") or {})) + list((targets.get("s5_w") or {})):
        try:
            prices[sym] = client.mark_price(sym)
        except Exception:  # noqa: BLE001
            pass
    orders = first_day_orders(targets, prices)
    ok = fail = skipped_min = 0
    errs = []
    for o in orders:
        q = round_qty_futures(info, o["symbol"], o["qty"])
        if q <= 0:
            skipped_min += 1
            continue
        try:
            client.test_order(symbol=o["symbol"], side=o["side"], type="MARKET", quantity=q,
                              newClientOrderId=f"demoE-test-{o['symbol'][:12]}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            errs.append(f"{o['symbol']}: {str(e)[:80]}")
    return {"orders": len(orders), "accepted": ok, "rejected": fail, "below_min_qty": skipped_min, "errors": errs[:10],
            "pass": len(orders) > 0 and fail == 0, "decided_bar": targets.get("decided_bar"),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ---------------- daily reconcile / weekly deviation pause (only when enabled)
def reconcile(state: dict, ledger_equity: float | None, demo_equity: float | None, today: str) -> dict | None:
    g = state.setdefault("demo_candidates", {}).setdefault(SLOT, {})
    if not g.get("enabled") or ledger_equity is None or demo_equity is None:
        return None
    hist = g.setdefault("daily", [])
    if not hist or hist[-1]["date"] != today:
        hist.append({"date": today, "demo": demo_equity, "ledger": ledger_equity, "dev": demo_equity / ledger_equity - 1})
    del hist[:-60]
    if len(hist) >= 7 and datetime.fromisoformat(today).weekday() == 0:
        dev = hist[-1]["dev"]
        if abs(dev) > WEEKLY_DEV_LIMIT:
            state.setdefault("paused_slots", {})[SLOT] = {"by": "reconcile", "reason": f"每週偏差 {dev*100:.2f}% 超過 0.5%", "at": today}
            log.warning("demo_e weekly_dev_pause dev=%.4f", dev)
            return {"paused": True, "dev": dev}
    return {"paused": False, "dev": hist[-1]["dev"]}
