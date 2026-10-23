"""Order-placing path for demo_e5_min5_x175 (pattern: demo_fa_exec). Runs from reconcile once per decided bar
(trader/shadow_ab/e5_targets.json, written by the daily shadow job from ledger_e_minnot.py on fresh data).
Active ONLY when the slot is enabled AND approved AND accepted AND both gates pass; otherwise a no-op.
Decision = demo_e5.plan_live (plan_u1 / plan_s5 / plan_leg: the functions that passed trade-by-trade parity), with the
ledger's drawdown regime m and U1 return window from the targets, per-symbol min notional + stepSize (spec values,
then the real Demo exchangeInfo filter as a safety net) and the < min skip rule. Sizes use the slot's own virtual
equity (5,000U start + fills + fees + funding on its own book), not the shared Demo account.
Closes always run (risk-reducing). Opens/increases are skipped while the account or slot is paused, a guard entry block
is active, the coin is held by another slot (FA book / foreign positionAmt), or the symbol isn't listed on Demo.
Targets older than yesterday (UTC) are never traded."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

import demo_e5
from guards import log_event

log = logging.getLogger("demo_e5_exec")
LEVERAGE = 3          # isolated, per symbol (futures_client hard cap 3)
MAX_LAG_H = 4         # execute only within 4h after the 00:00 UTC (08:00 Taipei) open
FEE = 0.0005          # taker estimate for the virtual equity when the fill has no commission field


def fresh(targets: dict | None, now: datetime | None = None) -> tuple[bool, str]:
    if not targets or not targets.get("decided_bar") or targets.get("w1") is None:
        return False, "no targets"
    if targets.get("m") is None or targets.get("rw") is None:
        return False, "targets missing m/rw"
    now = now or datetime.now(timezone.utc)
    want = str((now - timedelta(days=1)).date())
    if str(targets["decided_bar"]) != want:
        return False, f"stale targets {targets['decided_bar']} (want {want})"
    lag_h = (now - datetime(now.year, now.month, now.day, tzinfo=timezone.utc)).total_seconds() / 3600
    if lag_h > MAX_LAG_H:  # spec fills at the 08:00 (Taipei) open; never chase a missed open later in the day
        return False, f"missed open window ({lag_h:.1f}h > {MAX_LAG_H}h after 08:00 Taipei)"
    return True, ""


def entry_block(state: dict) -> str | None:
    import guards
    from main import slot_paused
    if state.get("paused"):
        return "帳戶暫停"
    if slot_paused(state, demo_e5.SLOT):
        return "本倉暫停"
    blocked, why = guards.entry_blocked(state)
    return f"風控封鎖：{why}" if blocked else None


def lot_step(info: dict, sym: str) -> float:
    for s in info.get("symbols") or []:
        if s.get("symbol") == sym:
            for f in s.get("filters") or []:
                if f.get("filterType") == "LOT_SIZE":
                    return float(f.get("stepSize") or 0)
    return 0.0


def round_qty(info: dict, sym: str, qty: float) -> float:
    """floor to LOT_SIZE step with a 1e-9 tolerance (plan_leg already truncated to step; 4.3/0.1 = 42.999.. must stay 4.3)."""
    import math
    st = lot_step(info, sym)
    if st <= 0:
        return qty
    n = math.floor(qty / st + 1e-9); dec = max(0, -int(math.floor(math.log10(st)))) if st < 1 else 0
    return float(f"{n * st:.{dec}f}")


def listed(info: dict) -> set:
    return {s["symbol"] for s in info.get("symbols") or [] if s.get("status", "TRADING") == "TRADING"}


def equity(ex: dict, prices: dict) -> float:
    return float(ex.get("cash", demo_e5.CAPITAL)) + sum(q * float(prices.get(s) or 0) for s, q in (ex.get("book") or {}).items())


def _fill(fut, sym: str, order: dict, coid: str) -> tuple[float, float]:
    o = order
    for _ in range(6):
        if o.get("status") == "FILLED" and float(o.get("executedQty") or 0) > 0:
            break
        time.sleep(0.5)
        o = fut.get_order(sym, coid) or o
    return float(o.get("executedQty") or 0), float(o.get("avgPrice") or 0)


def _funding(fut, ex: dict, now_ms: int) -> float:
    """Funding on the slot's own book since the last run (pro-rata share if another holder also has the symbol is not
    possible on a one-way account; E5 never opens a coin someone else holds, so the symbol's funding is E5's)."""
    since = int(ex.get("funding_since") or now_ms); tot = 0.0
    for s, q in (ex.get("book") or {}).items():
        if q == 0:
            continue
        try:
            tot += sum(float(r.get("income") or 0) for r in fut.income(s, "FUNDING_FEE", since, now_ms))
        except Exception:  # noqa: BLE001
            pass
    ex["funding_since"] = now_ms
    return tot


def execute(state: dict, fut, targets: dict | None, now: datetime | None = None) -> dict:
    ok, why = demo_e5.active(state)
    if not ok:
        return {"skipped": why}
    ok, why = fresh(targets, now)
    if not ok:
        return {"skipped": why}
    ex = state.setdefault("demo_e5_exec", {})
    bar = str(targets["decided_bar"])
    if ex.get("last_bar") == bar:
        return {"skipped": "already executed", "bar": bar}
    ex.setdefault("cash", demo_e5.CAPITAL); book = ex.setdefault("book", {})
    info = fut.exchange_info(); lst = listed(info)
    now_ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    fnd = _funding(fut, ex, now_ms); ex["cash"] = float(ex["cash"]) + fnd   # income is signed (+ received / - paid)
    syms = sorted(set(demo_e5.U1_SYMS) | set(targets.get("ws") or {}) | set(book))
    prices = {}
    for s in syms:
        if s in lst:
            try:
                prices[s] = float(fut.mark_price(s))
            except Exception:  # noqa: BLE001
                pass
    if any(s not in prices for s in demo_e5.U1_SYMS):
        return {"skipped": "no BTC/ETH price", "bar": bar}
    eq = equity(ex, prices)
    m = float(targets["m"]); lt = demo_e5.plan_lt(np.array(targets["rw"], dtype=float), m)
    mins = {**(targets.get("min_notional") or {})}; steps = {**(targets.get("step") or {})}
    for s in prices:  # the real exchange filter wins when stricter
        mins[s] = max(float(mins.get(s, demo_e5.MIN_ORDER)), demo_e5.exchange_min(info, s))
        steps[s] = max(float(steps.get(s, 0.0)), lot_step(info, s))
    plan = demo_e5.plan_live(targets, book, prices, eq, m=m, lt=lt, mins=mins, steps=steps)
    send, blocked = demo_e5.exchange_filter(plan, mins)
    demo_e5.log_blocked(state, blocked, bar)
    blk = entry_block(state)
    fa_book = set(state.get("demo_fa_positions") or {})
    pos = {r["symbol"]: float(r.get("positionAmt") or 0) for r in fut.position_risk()}
    out = {"bar": bar, "equity": round(eq, 2), "m": m, "lt": round(lt, 4), "planned": len(plan),
           "skipped_lt_min": sum(1 for o in plan if o["kind"] == "skip_min"), "blocked_min": len(blocked),
           "sent": [], "skipped": [], "funding": round(fnd, 4)}
    tag = bar.replace("-", "")[2:]
    for o in sorted(send, key=lambda o: not o["reduce_only"]):  # closes first (free margin)
        s = o["symbol"]; mine = float(book.get(s, 0.0))
        closing = o["reduce_only"] or (mine != 0 and abs(mine + o["delta_qty"]) < abs(mine) and np.sign(mine + o["delta_qty"]) in (0, np.sign(mine)))
        if s not in lst:
            out["skipped"].append(f"{s} not listed"); log_event(state, "e5_skip_unlisted", demo_e5.SLOT, symbol=s, bar=bar); continue
        if not closing:
            foreign = abs(pos.get(s, 0.0) - mine) > 1e-12 or s in fa_book
            if blk or foreign:
                why = blk or "held by another slot"
                out["skipped"].append(f"{s} {why}"); log_event(state, "e5_entry_skip", demo_e5.SLOT, symbol=s, why=why, bar=bar); continue
        try:
            q = abs(mine) if o["kind"] in ("close", "residual_close") else abs(o["delta_qty"])
            q = round_qty(info, s, q)
            if q <= 0:
                out["skipped"].append(f"{s} below step"); continue
            if not closing:
                fut.set_margin_type(s, "ISOLATED"); fut.set_leverage(s, LEVERAGE)
            side = "BUY" if o["delta_qty"] > 0 else "SELL"
            coid = f"e5-{s[:14]}-{tag}-{len(out['sent'])}"[:36]
            kw = {"reduceOnly": "true"} if o["reduce_only"] else {}
            r = fut.new_order(symbol=s, side=side, type="MARKET", quantity=q, newClientOrderId=coid, **kw)
            fq, px = _fill(fut, s, r, coid)
            if fq <= 0:
                out["skipped"].append(f"{s} not filled"); log_event(state, "e5_order_error", demo_e5.SLOT, symbol=s, error="not filled", bar=bar); continue
            d = fq if side == "BUY" else -fq
            nb = mine + d
            if o["kind"] in ("close", "residual_close") or abs(nb) < 1e-12:
                nb = 0.0
            pos[s] = pos.get(s, 0.0) + d
            ex["cash"] = float(ex["cash"]) - d * px - abs(d) * px * FEE
            if nb == 0.0:
                book.pop(s, None)
            else:
                book[s] = nb
            log_event(state, "e5_order", demo_e5.SLOT, symbol=s, side=side, qty=fq, px=px, kind=o["kind"], sleeve=o.get("sleeve"), bar=bar)
            out["sent"].append({"symbol": s, "side": side, "qty": fq, "px": px, "kind": o["kind"]})
        except Exception as e:  # noqa: BLE001
            out["skipped"].append(f"{s} error"); log_event(state, "e5_order_error", demo_e5.SLOT, symbol=s, error=str(e)[:120], bar=bar)
    out["equity_after"] = round(equity(ex, prices), 2)
    ex.update(last_bar=bar, last=out)
    ex["history"] = (list(ex.get("history") or []) + [{"bar": bar, "equity": out["equity_after"], "sent": len(out["sent"])}])[-400:]
    return out
