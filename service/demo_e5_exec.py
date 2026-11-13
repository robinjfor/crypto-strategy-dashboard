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
Targets older than yesterday (UTC) are never traded.
Capital changes (2026-11-13): the orchestrator backtest scales E's whole book with its capital, so before planning,
each held leg is resized by sizing capital / the capital it was last sized at (ledger frame_cap), with the same
step, min-notional and 0.5% threshold rules; the day's signal trades are then planned on the resized book."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

import demo_e5
import exchange_close_sync
import netting
import tradfi
from guards import log_event

log = logging.getLogger("demo_e5_exec")
LEVERAGE = 2          # isolated, per symbol; applied by netting.LEV_BY_SLOT (2026-10-28)
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


def _target_weight(targets: dict, symbol: str) -> float:
    ws = targets.get("ws") or {}
    if symbol in ws:
        return float(ws[symbol] or 0.0)
    if symbol in demo_e5.U1_SYMS:
        w1 = list(targets.get("w1") or [])
        i = demo_e5.U1_SYMS.index(symbol)
        if i < len(w1) and w1[i] is not None:
            return float(w1[i])
    return 0.0


# One-off migration seed for the capital frame (see frame_caps). The 2026-11-13 08:10 Taipei run
# (bar 2026-11-12) sized at 1959.85. Holdings it did not trade were last sized under the 2026-11-12
# plan, E target 3974.03 USDT (orchestrator action "sizing capital 3974.03 -> 1959.85").
FRAME_SEED_PREV_CAP = {"2026-11-12": 3974.03}


def frame_caps(ex: dict) -> dict:
    """Sizing capital each held symbol was last sized at ({symbol: USDT}).

    Ledgers written before this field existed are seeded once: symbols sent on the last executed bar
    were sized at that bar's capital (last.equity); the rest at FRAME_SEED_PREV_CAP for that bar,
    or at last.equity when no seed is known (no rescale, the old behaviour).
    """
    fc = ex.get("frame_cap")
    if isinstance(fc, dict):
        return {k: float(v) for k, v in fc.items() if v}
    last = ex.get("last") or {}
    last_eq = last.get("equity")
    sent = {o.get("symbol") for o in last.get("sent") or []}
    prev = FRAME_SEED_PREV_CAP.get(str(last.get("bar") or ""))
    out = {}
    for k, q in (ex.get("book") or {}).items():
        if not q:
            continue
        v = last_eq if (k in sent or prev is None) else prev
        if v:
            out[k] = float(v)
    return out


def rescale_book(book: dict, frames: dict, eq: float, prices: dict, mins: dict, steps: dict,
                 thr_notional: dict) -> tuple[dict, list[dict], set]:
    """Scale every held qty by eq / the capital it was sized at (orchestrator backtest: E's PnL is
    caps['E'] x E's return, so a capital change resizes the whole E book at once).

    Same leg rules as the backtest planner: qty truncated to stepSize; a resized leg below the symbol
    min notional is a residual close; a resize whose own notional is below max(MN, 0.5% of the S5
    sleeve) is not sent and the symbol keeps its old frame, so the drift is retried next bar.
    Returns (scaled book, resize orders, symbols whose resize was not sent)."""
    out = dict(book); orders = []; held_back = set()
    for s, u in book.items():
        u = float(u or 0.0); f = frames.get(s)
        if not u or not f or f <= 0 or s not in prices:
            continue
        k = float(eq) / float(f)
        if abs(k - 1.0) < 1e-9:
            continue
        p = float(prices[s]); st = float(steps.get(s, 0.0) or 0.0); mn = float(mins.get(s, demo_e5.MIN_ORDER))
        nu = u * k
        if st > 0:
            nu = float(np.trunc(nu / st + np.sign(nu) * 1e-9) * st)
        if abs(nu) * p < mn:
            orders.append({"symbol": s, "delta_qty": -u, "notional": abs(u) * p, "kind": "residual_close", "sleeve": "rescale", "k": round(k, 6)})
            out[s] = 0.0
        elif abs(nu - u) * p < max(mn, float(thr_notional.get(s, 0.0))):
            held_back.add(s)
        else:
            orders.append({"symbol": s, "delta_qty": nu - u, "notional": abs(nu - u) * p, "kind": "rescale", "sleeve": "rescale", "k": round(k, 6)})
            out[s] = nu
    return out, orders, held_back


FULL = ("close", "residual_close")


def merge_rescale(plan: list[dict], resize: list[dict], prices: dict, mins: dict) -> list[dict]:
    """One instruction list against the REAL book: plan_live ran on the scaled book, so a plan
    order is a delta from the scaled qty. A plan close/flip wins (the executor closes the whole
    real qty). Otherwise resize + plan order deltas are summed into one order."""
    rs = {o["symbol"]: o for o in resize}
    out = [o for o in plan if o["symbol"] not in rs]
    for s, r in rs.items():
        mine = [o for o in plan if o["symbol"] == s]
        if any(o["kind"] in FULL for o in mine):
            out += mine
            continue
        ords = [o for o in mine if o["kind"] == "order"]
        out += [o for o in mine if o["kind"] != "order"]
        if not ords:
            out.append(r)
            continue
        d = float(r["delta_qty"]) + sum(float(o["delta_qty"]) for o in ords)
        p = float(prices.get(s) or 0.0); n = abs(d) * p
        if d == 0:
            continue
        kind = "order" if n >= float(mins.get(s, demo_e5.MIN_ORDER)) else "skip_min"
        out.append({"symbol": s, "delta_qty": d, "notional": n, "kind": kind, "sleeve": ords[0].get("sleeve"), "k": r.get("k")})
    return out


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


def execute(state: dict, fut, targets: dict | None, now: datetime | None = None, netter=None, mod=None, only: set | None = None) -> dict:
    """mod: planner module (demo_e5 = original slot, demo_e5v = t05top20 variant); same order path, own ledger key.
    only: late re-send of the given symbols of the ALREADY executed bar (legs wrongly skipped); same gates/window/plan/netter,
    no funding re-booking, last_bar/history untouched."""
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
    if only is not None:
        if ex.get("last_bar") != bar:
            return {"skipped": "late re-send needs the executed bar", "bar": bar}
    elif ex.get("last_bar") == bar:
        return {"skipped": "already executed", "bar": bar}
    ex.setdefault("cash", demo_e5.CAPITAL); book = ex.setdefault("book", {})
    info = fut.exchange_info(); lst = listed(info)
    now_ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    fnd = 0.0 if only is not None else _funding(fut, ex, now_ms, state, SLOT, KEY); ex["cash"] = float(ex["cash"]) + fnd   # income is signed (+ received / - paid)
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
    # Capital change: resize the held book to the new sizing capital first (backtest return scaling),
    # then plan the day's signal trades on the resized book. Only when the orchestrator plan sized it.
    plan_eq = orchestrator.sizing_capital(SLOT, None, now=now) if only is None else None
    frames = frame_caps(ex) if plan_eq is not None else {}
    es = (1 - mod.W_U1) * eq
    thr_n = {s: (0.0 if s in demo_e5.U1_SYMS else float(getattr(mod, "THR", 0.0)) * es) for s in prices}
    book0 = {s: float(q) for s, q in book.items()}
    pbook, resize, held_back = rescale_book(book0, frames, eq, prices, mins, steps, thr_n) if frames else (book0, [], set())
    plan = mod.plan_live(targets, pbook, prices, eq, m=m, lt=lt, mins=mins, steps=steps)
    if resize:
        plan = merge_rescale(plan, resize, prices, mins)
    if only is not None:
        plan = [o for o in plan if o["symbol"] in only]
    blk = entry_block(state, SLOT)
    tag = bar.replace("-", "")[2:]
    out = {"bar": bar, "equity": round(eq, 2), "m": m, "lt": round(lt, 4), "planned": len(plan), "ledger_equity": round(eq_ledger, 2),
           "skipped_lt_min": sum(1 for o in plan if o["kind"] == "skip_min"),
           "skipped_thr": sum(1 for o in plan if o["kind"] == "skip_thr"), "slot": SLOT, "sent": [], "skipped": [], "funding": round(fnd, 4),
           "rescale": {"orders": len(resize), "held_back": sorted(held_back),
                       "k": {o["symbol"]: o["k"] for o in resize}}}
    own = netter is None
    netter = netter or netting.Netter(fut, info, tag=f"e5{tag}")
    for s in list((ex.get("forced_flat") or {})):
        if exchange_close_sync.observe_target(ex, s, _target_weight(targets, s)) == "released":
            log_event(state, "forced_flat_released", SLOT, symbol=s, bar=bar, target=_target_weight(targets, s))
    try:  # exchange legs: a close of a leg the exchange no longer holds (liquidated/ADL'd outside) would OPEN a position
        xpos = {r["symbol"]: float(r.get("positionAmt") or 0) for r in fut.position_risk()}
    except Exception:  # noqa: BLE001
        xpos = None
    for o in plan:
        if o["kind"] in ("skip_min", "skip_thr", "hold"):
            continue
        s = o["symbol"]; mine = float(book.get(s, 0.0))
        full = o["kind"] in FULL
        d = -mine if full else float(o["delta_qty"])
        closing = full or (mine != 0 and abs(mine + d) < abs(mine) and np.sign(mine + d) in (0, np.sign(mine)))
        if s not in lst:
            out["skipped"].append(f"{s} not listed"); log_event(state, "e5_skip_unlisted", SLOT, symbol=s, bar=bar); continue
        if closing and mine and xpos is not None:
            x = xpos.get(s, 0.0)
            if netting.leg_missing(state, SLOT, s, x, mine):   # vs exchange net minus other slots' legs (netting)
                # Refuse the order only. A missing leg is booked by exchange_close_sync from the Binance fill.
                out["skipped"].append(f"{s} leg missing on exchange")
                log_event(state, "e5_skip_missing_leg", SLOT, symbol=s, ledger=mine, exchange=x, bar=bar); continue
        if not closing and exchange_close_sync.reentry_blocked(ex, s):
            out["skipped"].append(f"{s} forced flat (target unchanged)")
            log_event(state, "e5_skip_forced_flat", SLOT, symbol=s, bar=bar); continue
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
        if only is not None:
            out["late"] = sorted(only); ex["late_runs"] = (list(ex.get("late_runs") or []) + [out])[-50:]; return
        if plan_eq is not None:
            # Frame: the capital each held symbol is now sized at. A symbol whose resize was held back
            # (or not filled) and whose qty did not move keeps its old frame, so the resize is retried.
            nf = {}
            for s2, q2 in (ex.get("book") or {}).items():
                if not q2:
                    continue
                old_f = frames.get(s2)
                unchanged = abs(float(q2) - book0.get(s2, 0.0)) < 1e-12
                nf[s2] = round(float(old_f if (old_f and unchanged and (s2 in held_back or s2 in {r["symbol"] for r in resize})) else eq), 2)
            ex["frame_cap"] = nf
            ex["sizing_equity"] = round(float(eq), 2)
        ex.update(last_bar=bar, last=out)
        ex["history"] = (list(ex.get("history") or []) + [{"bar": bar, "equity": out["equity_after"], "sent": len(out["sent"])}])[-400:]
    netter.after.append(finish)
    if own:
        netter.flush()
    return out
