"""Order-placing path for demo_e5_min5_x175 (pattern: demo_fa_exec). Runs from reconcile once per decided bar
(trader/shadow_ab/e5_targets.json, written by the daily shadow job from ledger_e_minnot.py on fresh data).
Active ONLY when the slot is enabled AND approved AND accepted AND both gates pass; otherwise a no-op.
Decision = demo_e5.plan_live (plan_u1 / plan_s5 / plan_leg: the functions that passed trade-by-trade parity), with the
ledger's drawdown regime m and U1 return window from the targets, per-symbol min notional + stepSize (spec values,
then the real Demo exchangeInfo filter as a safety net) and the < min skip rule. Sizes use the slot's own virtual
equity (5,000U start + fills + fees + funding on its own book), not the shared Demo account.
Orders are virtual-ledger intents netted per symbol with the other Demo slots (netting.py): no "held by another slot"
skip, exchange min notional/stepSize apply to the net. Closes always run. Opens/increases are skipped while the account or
slot is paused, a guard entry block is active, the symbol is TradFi, or it isn't listed on Demo.
Targets older than yesterday (UTC) are never traded."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

import demo_e5
import netting
import tradfi
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


def entry_block(state: dict, slot: str | None = None) -> str | None:
    import guards
    from main import slot_paused
    if state.get("paused"):
        return "帳戶暫停"
    if slot_paused(state, slot or demo_e5.SLOT):
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


def _funding(fut, ex: dict, now_ms: int, state: dict | None = None, slot: str | None = None, key: str = "demo_e5_exec") -> float:
    """Funding on the slot's own book since the last run: the netted symbol's funding, pro rata to E5's virtual qty."""
    state = state if state is not None else {key: ex}
    since = int(ex.get("funding_since") or now_ms)
    tot = netting.funding_share(fut, state, slot or demo_e5.SLOT, ex.get("book") or {}, since, now_ms)
    ex["funding_since"] = now_ms
    return tot


def execute(state: dict, fut, targets: dict | None, now: datetime | None = None, netter=None, mod=None) -> dict:
    """mod: planner module (demo_e5 = original slot, demo_e5v = t05top20 variant); same order path, own ledger key."""
    mod = mod or demo_e5
    SLOT, KEY = mod.SLOT, getattr(mod, "STATE_KEY", "demo_e5_exec")
    ok, why = mod.active(state)
    if not ok:
        return {"skipped": why}
    ok, why = fresh(targets, now)
    if not ok:
        return {"skipped": why}
    ex = state.get(KEY) or {}
    state[KEY] = ex
    bar = str(targets["decided_bar"])
    if ex.get("last_bar") == bar:
        return {"skipped": "already executed", "bar": bar}
    ex.setdefault("cash", demo_e5.CAPITAL); book = ex.setdefault("book", {})
    info = fut.exchange_info(); lst = listed(info)
    now_ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    fnd = _funding(fut, ex, now_ms, state, SLOT, KEY); ex["cash"] = float(ex["cash"]) + fnd   # income is signed (+ received / - paid)
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
    eq_ledger = equity(ex, prices)
    import orchestrator  # sizing capital only (ledger untouched); fixed CAPITAL when no fresh plan
    eq = orchestrator.sizing_capital(SLOT, eq_ledger, now=now)
    m = float(targets["m"]); lt = demo_e5.plan_lt(np.array(targets["rw"], dtype=float), m)
    mins = {**(targets.get("min_notional") or {})}; steps = {**(targets.get("step") or {})}
    for s in prices:  # strategy rule = spec mins; the exchange MIN_NOTIONAL is applied to the per-symbol NET (netting)
        mins.setdefault(s, demo_e5.MIN_ORDER)
        steps[s] = max(float(steps.get(s, 0.0)), lot_step(info, s))
    plan = mod.plan_live(targets, book, prices, eq, m=m, lt=lt, mins=mins, steps=steps)
    blk = entry_block(state, SLOT)
    tag = bar.replace("-", "")[2:]
    out = {"bar": bar, "equity": round(eq, 2), "m": m, "lt": round(lt, 4), "planned": len(plan), "ledger_equity": round(eq_ledger, 2),
           "skipped_lt_min": sum(1 for o in plan if o["kind"] == "skip_min"),
           "skipped_thr": sum(1 for o in plan if o["kind"] == "skip_thr"), "slot": SLOT, "sent": [], "skipped": [], "funding": round(fnd, 4)}
    own = netter is None
    netter = netter or netting.Netter(fut, info, tag=f"e5{tag}")
    for o in plan:
        if o["kind"] in ("skip_min", "skip_thr", "hold"):
            continue
        s = o["symbol"]; mine = float(book.get(s, 0.0))
        full = o["kind"] in ("close", "residual_close")
        d = -mine if full else float(o["delta_qty"])
        closing = full or (mine != 0 and abs(mine + d) < abs(mine) and np.sign(mine + d) in (0, np.sign(mine)))
        if s not in lst:
            out["skipped"].append(f"{s} not listed"); log_event(state, "e5_skip_unlisted", SLOT, symbol=s, bar=bar); continue
        if not closing and (blk or tradfi.is_tradfi(s, info, state)):
            why = blk or "TradFi (crypto only)"
            out["skipped"].append(f"{s} {why}"); log_event(state, "e5_entry_skip", SLOT, symbol=s, why=why, bar=bar); continue
        if not full:
            d = float(np.sign(d)) * round_qty(info, s, abs(d))
            if d == 0:
                out["skipped"].append(f"{s} below step"); continue

        def logf(q, px, fee, s=s, o=o):
            if q:
                side = "BUY" if q > 0 else "SELL"
                log_event(state, "e5_order", SLOT, symbol=s, side=side, qty=abs(q), px=px, kind=o["kind"], sleeve=o.get("sleeve"), bar=bar)
                out["sent"].append({"symbol": s, "side": side, "qty": abs(q), "px": px, "kind": o["kind"]})
            else:
                out["skipped"].append(f"{s} net not filled")
        netter.add(state, SLOT, s, d, prices.get(s, 0.0), netting.ledger_cb(ex, s, on_fill=logf), kind=o["kind"])

    def finish():
        out["equity_after"] = round(equity(ex, prices), 2)
        ex.update(last_bar=bar, last=out)
        ex["history"] = (list(ex.get("history") or []) + [{"bar": bar, "equity": out["equity_after"], "sent": len(out["sent"])}])[-400:]
    netter.after.append(finish)
    if own:
        netter.flush()
    return out
