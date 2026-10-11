"""U1 live orders — family btc_eth_trend_voltarget_perp on the Binance Demo USDT-M futures account.

Robin put U1 into 待審核 as an exception (neighbor gate not passed). This module is the live order path;
it sends NOTHING unless ALL of these hold (checked on every run, `gate()`):
  1. the family is approved (state.approved_families — Robin presses 核准), AND
  2. the allocation has an ENABLED slot of this family (the analyst adds it; notional_usdt = U1 capital),
  3. the job runs in live mode and the run may trade (not paused / not an ONLY_SLOTS run for other slots).
Approval alone opens nothing (tests/test_u1_live_parity.py).

Decisions = the paper engine's (paper_u1): the live step executes paper's `last_fill_decision`
(targets w1x×L with L from the shadow 1x book, and the same 00:00 UTC bar-open reference prices).
Only the book differs: the live sleeve holds the real Demo fills, commissions and FUNDING_FEE income.
Band rule identical to the spec: trade a coin only on entry / exit_to_cash / |wt−wc| > 25%·|wt|,
then go to wt·V/O (V = sleeve cash + units·O at the 00:00 open). Long/cash only, fractional qty
(rounded to the exchange LOT_SIZE step), MARKET orders, no stop orders.

Margin / liquidation (cross margin shares the futures wallet with the isolated A/S slots):
  * BTCUSDT/ETHUSDT are set to CROSSED, exchange leverage 3 (margin only; = the spec 3x cap).
    A/S positions stay ISOLATED: their margin is ring-fenced, so their losses cannot reach U1 and a U1
    loss cannot liquidate them. What U1's cross losses CAN do is eat the shared free balance
    (availableBalance) that A/S need to open new isolated positions — and, in the extreme, the whole
    cross wallet. Guards:
  * Notional cap by allocation slot: after every daily decision, gross notional ≤ 3 × slot.notional_usdt
    and per coin ≤ 1.5 × slot.notional_usdt (slot params.max_gross_notional_usdt may only lower it);
    a holding that drifted above the cap is trimmed back (reduce-only, reason cap_trim). Sizing still
    compounds the sleeve like the backtest; the cap only binds once the sleeve grows well beyond its
    capital (then the analyst can raise the slot).
  * Free-margin check before any increase: added initial margin (notional/3 × 1.10 buffer) must fit in
    availableBalance minus a reserve for every other live, flat futures slot (its notional/leverage), so
    A/S can still open. Otherwise the increase is scaled down (or skipped below minNotional).
  * Sleeve liquidation emulation = the backtest's own rule, every hourly run on each closed 1h bar:
    if sleeve cash + Σ units·Low ≤ 0.5% · Σ units·Low, U1 closes everything (reduce-only MARKET).
    This caps U1's loss at about its own capital instead of the whole wallet (an intra-hour gap can
    still exceed it).
  * Account margin ratio (totalMaintMargin/totalMarginBalance): ≥ 50% → no U1 increases;
    ≥ 80% → U1 closes everything (reduce-only), before the exchange would liquidate the cross wallet.
  * One direction per coin: U1 never adds while any other slot holds a SHORT on BTC/ETH (any venue)
    or any futures position on BTCUSDT/ETHUSDT (one-way mode would net it with U1); the main runner
    in turn blocks other slots from shorting BTC/ETH or trading those perps while U1 holds them.
  * Exchange BTCUSDT/ETHUSDT positions must equal the sleeve units (± one step) and the account must
    be in one-way mode; otherwise no orders and an alert.
  * Idempotent client order ids (u1-<fill bar>-<coin>): a rerun never doubles an order. A decision is
    executed only within 2h of its 00:00 UTC fill bar (no late catch-up orders); a missed day waits for
    the next decision.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd

import paper_u1 as P

log = logging.getLogger("u1_live")

CODE = "U1"
FAMILY = P.FAMILY
STRATEGY_ID = P.STRATEGY_ID
SYMS = P.SYMS
PERP = {k: k + "USDT" for k in SYMS}
MARGIN_TYPE = "CROSSED"
EXCHANGE_LEVERAGE = 3            # Binance per-symbol leverage (initial margin only) = spec cap
NOTIONAL_CAP_MULT = P.LEV_CAP    # gross ≤ 3 × slot capital
POS_CAP_MULT = P.SLEEVE * P.LEV_CAP  # per coin ≤ 1.5 × slot capital (w ≤ 0.5, L ≤ 3)
IM_BUFFER = 1.10
ACCOUNT_MR_BLOCK = 0.50
ACCOUNT_MR_DERISK = 0.80
LIVE_WINDOW = timedelta(hours=2)
EST_FEE = 0.0005                 # only when the exchange commission can't be read
KEEP = 400


def _norm(f) -> str:
    from allocation import normalize_family
    return normalize_family(f)


def u1_slot(doc: dict | None) -> dict | None:
    for s in (doc or {}).get("slots") or []:
        if s.get("enabled") is True and _norm(s.get("family")) == FAMILY:
            return s
    return None


def gate(approved_families, doc: dict | None) -> tuple[dict | None, str]:
    """(slot, '') only when the family is approved AND an enabled allocation slot exists."""
    fams = {_norm(f) for f in (approved_families or [])}
    slot = u1_slot(doc)
    if FAMILY not in fams:
        return None, "family_not_approved"
    if slot is None:
        return None, "no_allocation_slot"
    return slot, ""


def caps_for(capital: float, params: dict | None = None) -> tuple[float, float]:
    gross = NOTIONAL_CAP_MULT * float(capital)
    lim = (params or {}).get("max_gross_notional_usdt")
    if lim is not None:
        gross = min(gross, float(lim))
    return gross, min(gross, POS_CAP_MULT * float(capital))


def new_sleeve(slot: dict, now: datetime) -> dict:
    c = float(slot.get("notional_usdt"))
    ms = int(now.timestamp() * 1000)
    return {"code": CODE, "family": FAMILY, "strategy_id": STRATEGY_ID, "slot": slot.get("slot"),
            "venue": "futures", "margin_type": "cross", "capital_usdt": c, "cash": c,
            "units": {k: 0.0 for k in SYMS}, "fees": 0.0, "funding": 0.0, "started_at": now.isoformat(),
            "income_cursor_ms": ms, "liq_checked_bar": None, "last_fill_bar": None,
            "fills": [], "events": [], "runs": []}


# ---------------------------------------------------------------- pure planning (shared band rule)
def _round_step(q: float, step: float) -> float:
    if not step or step <= 0:
        return q
    prec = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    return float(f"{round(q / step) * step:.{prec}f}")


def plan_orders(sleeve: dict, targets: dict, opens: dict, *, cap_gross: float, cap_pos: float,
                steps: dict | None = None, min_notional: dict | None = None,
                max_add_notional: float | None = None, allow_increase: bool = True,
                blocked_increase: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Band decision per coin exactly as paper_u1.rebalance, then caps/rounding. Returns (orders, skips)."""
    units = sleeve["units"]
    V = sleeve["cash"] + sum(units[k] * opens[k] for k in SYMS)
    skips: list[dict] = []
    if not V > 0:
        return [], [{"reason": "sleeve_equity_le_0", "V": V}]
    want, why, wcs = {}, {}, {}
    for k in SYMS:
        wt = float(targets[k])
        if wt < 0:
            raise ValueError("U1 is long/cash only (one direction per coin)")
        u, o = units[k], opens[k]
        wc = u * o / V
        wcs[k] = wc
        if wt == 0.0:
            tr, w = u != 0.0, "exit_to_cash"
        elif wc == 0.0:
            tr, w = True, "entry"
        elif abs(wt - wc) > P.BAND * abs(wt):
            tr, w = True, "rebalance_band"
        else:
            tr, w = False, ""
        if tr:
            want[k], why[k] = wt * V / o, w
    # notional cap by allocation slot, on ALL post-decision holdings: a coin inside its band whose
    # notional drifted above the cap is trimmed back (reduce-only, reason cap_trim)
    for k in SYMS:
        cur = want.get(k, units[k])
        if cur * opens[k] > cap_pos + 1e-9:
            want[k] = cap_pos / opens[k]
            why.setdefault(k, "cap_trim")
            skips.append({"symbol": PERP[k], "reason": "cap_per_coin", "cap": cap_pos})
    gross = sum(want.get(k, units[k]) * opens[k] for k in SYMS)
    if gross > cap_gross + 1e-9:
        sc = cap_gross / gross
        for k in SYMS:
            want[k] = want.get(k, units[k]) * sc
            why.setdefault(k, "cap_trim")
        skips.append({"reason": "cap_gross", "cap": cap_gross, "gross_wanted": gross})
    deltas = {k: want[k] - units[k] for k in want}
    for k in list(deltas):
        if deltas[k] > 0 and (not allow_increase or (blocked_increase or {}).get(k)):
            skips.append({"symbol": PERP[k], "reason": "increase_blocked:" + str((blocked_increase or {}).get(k) or "no_new_exposure")})
            del deltas[k]
    add = sum(d * opens[k] for k, d in deltas.items() if d > 0)
    if max_add_notional is not None and add > max_add_notional + 1e-9:
        s = max(0.0, max_add_notional) / add
        for k in deltas:
            if deltas[k] > 0:
                deltas[k] *= s
        skips.append({"reason": "margin_limited", "add_wanted": add, "add_allowed": max_add_notional})
    orders = []
    for k, d in deltas.items():
        u, o, sym = units[k], opens[k], PERP[k]
        if why[k] == "exit_to_cash":
            d = -u
        else:
            d = _round_step(d, (steps or {}).get(sym, 0.0))
            d = max(d, -u)
        if abs(d) <= 1e-15:
            continue
        if d > 0 and d * o < float((min_notional or {}).get(sym, 0.0)):
            skips.append({"symbol": sym, "reason": "below_min_notional", "notional": d * o})
            continue
        orders.append({"symbol": sym, "coin": k, "side": "BUY" if d > 0 else "SELL", "qty_delta": d,
                       "reduce_only": d < 0, "reason": why[k], "target_weight": float(targets[k]),
                       "weight_before": wcs[k], "ref_price": o, "equity_before": V})
    orders.sort(key=lambda x: (not x["reduce_only"], x["symbol"]))  # reductions first (free margin)
    return orders, skips


# ---------------------------------------------------------------- exchange helpers
def _lot(info: dict, sym: str) -> tuple[float, float]:
    from futures_client import _filters
    f = _filters(info, sym)
    lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
    mn = f.get("MIN_NOTIONAL") or {}
    return float(lot.get("stepSize") or 0.0), float(mn.get("notional") or mn.get("minNotional") or 0.0)


def _fmt(q: float, step: float) -> str:
    prec = max(0, int(round(-math.log10(step)))) if step and step < 1 else (12 if not step else 0)
    return f"{abs(q):.{prec}f}".rstrip("0").rstrip(".") if prec else f"{abs(q):.0f}"


def _commission(fc, sym: str, order_id) -> tuple[float | None, bool]:
    try:
        rows = [t for t in fc.user_trades(sym, limit=50) or [] if str(t.get("orderId")) == str(order_id)]
        if rows and all(str(t.get("commissionAsset") or "USDT") == "USDT" for t in rows):
            return sum(float(t.get("commission") or 0) for t in rows), False
    except Exception as e:  # noqa: BLE001
        log.warning("u1_commission_read_fail sym=%s err=%s", sym, e)
    return None, True


def _apply_fill(fc, sleeve: dict, od: dict, resp: dict, cid: str, tag: str, now: datetime) -> dict | None:
    """Book one exchange fill into the sleeve (once per orderId)."""
    sym, k = od["symbol"], od["coin"]
    if str(resp.get("orderId")) in {str(f.get("order_id")) for f in sleeve["fills"]}:
        return None
    exq = float(resp.get("executedQty") or 0)
    avg = float(resp.get("avgPrice") or 0)
    if exq <= 0 or avg <= 0:
        sleeve["events"].append({"at": now.isoformat(), "event": "order_not_filled", "symbol": sym,
                                 "client_id": cid, "status": resp.get("status")})
        return None
    signed = exq if str(resp.get("side") or od["side"]) == "BUY" else -exq
    fee, est = _commission(fc, sym, resp.get("orderId"))
    if fee is None:
        fee = exq * avg * EST_FEE
    sleeve["cash"] -= signed * avg + fee
    sleeve["fees"] += fee
    sleeve["units"][k] += signed
    if abs(sleeve["units"][k]) < 1e-12:
        sleeve["units"][k] = 0.0
    f = {**od, "client_id": cid, "order_id": resp.get("orderId"), "qty": exq, "qty_signed": signed,
         "price": avg, "notional": exq * avg, "fee": fee, "fee_estimated": est,
         "units_after": sleeve["units"][k], "filled_at": now.isoformat(), "fill_bar": tag}
    sleeve["fills"] = (sleeve["fills"] + [f])[-KEEP:]
    return f


def execute(fc, sleeve: dict, orders: list[dict], *, tag: str, steps: dict, now: datetime) -> list[dict]:
    """MARKET orders with idempotent client ids; updates the sleeve from the real fills."""
    fills = []
    for od in orders:
        sym, k = od["symbol"], od["coin"]
        cid = f"u1-{tag}-{k}"[:36]
        resp = fc.get_order(sym, cid)
        if resp is None:
            params = {"symbol": sym, "side": od["side"], "type": "MARKET",
                      "quantity": _fmt(od["qty_delta"], steps.get(sym, 0.0)), "newClientOrderId": cid}
            if od["reduce_only"]:
                params["reduceOnly"] = "true"
            resp = fc.new_order(**params)
            if str(resp.get("status")) != "FILLED":
                resp = fc.get_order(sym, cid) or resp
        f = _apply_fill(fc, sleeve, od, resp, cid, tag, now)
        if f:
            fills.append(f)
    return fills


def recover_unbooked(fc, sleeve: dict, fill_bar: str, now: datetime) -> list[dict]:
    """A run that placed a U1 order but crashed before saving state: book that order now (by client id)."""
    tag = P._ts(fill_bar).strftime("%Y%m%d%H")
    out = []
    for k in SYMS:
        cid = f"u1-{tag}-{k}"
        o = fc.get_order(PERP[k], cid)
        if o and float(o.get("executedQty") or 0) > 0:
            od = {"symbol": PERP[k], "coin": k, "side": str(o.get("side")), "qty_delta": None,
                  "reduce_only": str(o.get("side")) == "SELL", "reason": "recovered_unbooked_fill",
                  "target_weight": None, "weight_before": None, "ref_price": None, "equity_before": None}
            f = _apply_fill(fc, sleeve, od, o, cid, tag, now)
            if f:
                sleeve["events"].append({"at": now.isoformat(), "event": "recovered_unbooked_fill", "client_id": cid})
                out.append(f)
    return out


def _prepare(fc, sym: str) -> None:
    fc.set_margin_type(sym, MARGIN_TYPE)
    fc.set_leverage(sym, EXCHANGE_LEVERAGE, cap=EXCHANGE_LEVERAGE)


def coin_conflicts(state: dict) -> dict:
    """{coin: reason} when another slot holds a SHORT on the coin or any futures position on the U1 perp."""
    from main import _base_asset
    out = {}
    for pid, p in (state.get("positions") or {}).items():
        if not isinstance(p, dict) or p.get("status") != "FILLED":
            continue
        base = _base_asset(p.get("symbol") or "")
        if base not in SYMS:
            continue
        if str(p.get("side") or "LONG").upper() == "SHORT":
            out[base] = f"opposite_position:{pid}"
        elif (p.get("venue") or "spot") == "futures":
            out[base] = f"shared_perp:{pid}"
    return out


def margin_reserve(doc: dict | None, state: dict, approved_families) -> float:
    """Isolated margin other live, currently flat futures slots need to open (notional / leverage)."""
    from allocation import FUTURES_FAMILIES
    fams = {_norm(f) for f in (approved_families or [])}
    pos = state.get("positions") or {}
    r = 0.0
    for s in (doc or {}).get("slots") or []:
        fam = _norm(s.get("family"))
        if not s.get("enabled") or fam == FAMILY or fam not in FUTURES_FAMILIES or fam not in fams:
            continue
        p = pos.get(s.get("slot"))
        if isinstance(p, dict) and p.get("status") == "FILLED":
            continue
        lev = max(1.0, float((s.get("params") or {}).get("leverage") or 1.0))
        r += float(s.get("notional_usdt") or 0) / lev
    return r


def sleeve_equity(sleeve: dict, px: dict) -> float:
    return sleeve["cash"] + sum(sleeve["units"][k] * px[k] for k in SYMS)


def _close_all(fc, sleeve, steps, now, why) -> list[dict]:
    orders = [{"symbol": PERP[k], "coin": k, "side": "SELL", "qty_delta": -sleeve["units"][k], "reduce_only": True,
               "reason": why, "target_weight": 0.0, "weight_before": None, "ref_price": None, "equity_before": None}
              for k in SYMS if sleeve["units"][k] > 0]
    tag = why[:6] + pd.Timestamp(now).strftime("%Y%m%d%H")
    return execute(fc, sleeve, orders, tag=tag, steps=steps, now=now)


# ---------------------------------------------------------------- one hourly run
def run_u1_live(state: dict, paper_state: dict | None, now: datetime, *, alloc_doc: dict | None,
                approved_families, live: bool, allow_increase: bool, fc_factory, fetch_1h=None) -> dict:
    now = now.astimezone(timezone.utc)
    slot, why_not = gate(approved_families, alloc_doc)
    sleeve = state.get("u1_live")
    holding = bool(sleeve) and any(abs(sleeve["units"][k]) > 0 for k in SYMS)
    if slot is None and not holding:
        return {"active": False, "reason": why_not, "orders": 0}
    if not live:
        return {"active": False, "reason": "not_live_mode", "orders": 0}
    if sleeve is None:
        sleeve = state["u1_live"] = new_sleeve(slot, now)
        sleeve["events"].append({"at": now.isoformat(), "event": "activated", "slot": slot.get("slot"),
                                 "capital_usdt": sleeve["capital_usdt"]})
    can_add = bool(allow_increase and slot is not None)
    if slot is not None and float(slot.get("notional_usdt")) != sleeve["capital_usdt"]:
        new_c = float(slot.get("notional_usdt"))
        sleeve["cash"] += new_c - sleeve["capital_usdt"]
        sleeve["events"].append({"at": now.isoformat(), "event": "capital_changed",
                                 "from": sleeve["capital_usdt"], "to": new_c})
        sleeve["capital_usdt"] = new_c
    run = {"at": now.isoformat(), "slot_live": slot is not None, "gate": why_not or "ok", "orders": 0,
           "blocked": None, "skips": [], "alerts": []}
    fc = fc_factory()
    info = fc.exchange_info()
    lots = {PERP[k]: _lot(info, PERP[k]) for k in SYMS}
    steps = {s: v[0] for s, v in lots.items()}
    mins = {s: v[1] for s, v in lots.items()}
    dec = (paper_state or {}).get("last_fill_decision")
    recovered = []
    if dec and dec.get("fill_bar") != sleeve.get("last_fill_bar"):
        recovered = recover_unbooked(fc, sleeve, dec["fill_bar"], now)
    # --- safety pre-checks
    if fc.position_mode_dual():
        run["blocked"] = "hedge_mode_account"
    exch = {}
    for p in fc.position_risk() or []:
        if p.get("symbol") in PERP.values():
            exch[p["symbol"]] = exch.get(p["symbol"], 0.0) + float(p.get("positionAmt") or 0)
    for k in SYMS:
        if abs(exch.get(PERP[k], 0.0) - sleeve["units"][k]) > max(steps[PERP[k]], 1e-9) * 0.51:
            run["blocked"] = run["blocked"] or f"position_mismatch:{PERP[k]}"
            run["alerts"].append({"symbol": PERP[k], "exchange": exch.get(PERP[k], 0.0), "sleeve": sleeve["units"][k]})
    # --- funding income since last run (only U1 may hold BTCUSDT/ETHUSDT)
    end_ms = int(now.timestamp() * 1000)
    fund = 0.0
    for k in SYMS:
        for r in fc.income(PERP[k], "FUNDING_FEE", sleeve["income_cursor_ms"], end_ms) or []:
            if r.get("incomeType") == "FUNDING_FEE" and int(r.get("time") or 0) >= sleeve["income_cursor_ms"]:
                fund += float(r.get("income") or 0)
    sleeve["cash"] += fund
    sleeve["funding"] += fund
    sleeve["income_cursor_ms"] = end_ms + 1
    run["funding_usdt"] = fund
    acct = fc.account() or {}
    mb, mm = float(acct.get("totalMarginBalance") or 0), float(acct.get("totalMaintMargin") or 0)
    mr = mm / mb if mb > 0 else None
    run["account_margin_ratio"] = mr
    fills: list[dict] = []
    # --- sleeve liquidation emulation (backtest rule) on closed 1h bars, then account margin guard
    if holding and fetch_1h is not None and not run["blocked"]:
        last = pd.Timestamp(now).floor("h") - P.H
        bars = {k: fetch_1h(PERP[k], now) for k in SYMS}
        since = P._ts(sleeve["liq_checked_bar"]) if sleeve.get("liq_checked_bar") else pd.Timestamp(sleeve["started_at"]).floor("h") - P.H
        idx = [t for t in bars[SYMS[0]].index if since < t <= last and all(t in bars[k].index for k in SYMS)]
        for t in idx:
            low = {k: float(bars[k].loc[t, "low"]) for k in SYMS}
            gw = sum(max(sleeve["units"][k], 0.0) * low[k] for k in SYMS)
            if gw > 0 and sleeve["cash"] + gw <= P.MAINT * gw:
                sleeve["events"].append({"at": now.isoformat(), "event": "sleeve_liquidation", "bar": P._iso(t)})
                fills += _close_all(fc, sleeve, steps, now, "sleeve_liquidation")
                break
        if idx:
            sleeve["liq_checked_bar"] = P._iso(idx[-1])
    if mr is not None and mr >= ACCOUNT_MR_DERISK and any(sleeve["units"][k] > 0 for k in SYMS) and not run["blocked"]:
        sleeve["events"].append({"at": now.isoformat(), "event": "account_margin_derisk", "margin_ratio": mr})
        fills += _close_all(fc, sleeve, steps, now, "account_margin_derisk")
    if mr is not None and mr >= ACCOUNT_MR_BLOCK:
        can_add = False
        run["alerts"].append({"account_margin_ratio": mr, "action": "no_increases"})
    # --- the daily decision (identical to paper)
    fills += recovered
    if dec and dec.get("fill_bar") != sleeve.get("last_fill_bar") and not run["blocked"]:
        fb = P._ts(dec["fill_bar"])
        if pd.Timestamp(sleeve["started_at"]) > fb + LIVE_WINDOW or now - fb.to_pydatetime() > LIVE_WINDOW:
            run["decision"] = {"fill_bar": dec["fill_bar"], "skipped": "outside_2h_window"}
        else:
            cap_gross, cap_pos = caps_for(sleeve["capital_usdt"], (slot or {}).get("params"))
            reserve = margin_reserve(alloc_doc, state, approved_families)
            avail = float(acct.get("availableBalance") or 0)
            max_add = max(0.0, (avail - reserve) / IM_BUFFER) * EXCHANGE_LEVERAGE
            conflicts = coin_conflicts(state)
            orders, skips = plan_orders(sleeve, dec["targets_lev"], dec["fill_opens"], cap_gross=cap_gross,
                                        cap_pos=cap_pos, steps=steps, min_notional=mins, max_add_notional=max_add,
                                        allow_increase=can_add, blocked_increase=conflicts)
            for od in orders:
                if not od["reduce_only"]:
                    _prepare(fc, od["symbol"])
            tag = fb.strftime("%Y%m%d%H")
            fills += execute(fc, sleeve, orders, tag=tag, steps=steps, now=now)
            run["skips"] = skips
            run["decision"] = {"fill_bar": dec["fill_bar"], "indicator_day": dec["indicator_day"], "L": dec["L"],
                               "targets_lev": dec["targets_lev"], "n_orders": len(orders),
                               "margin_reserve_usdt": reserve, "available_usdt": avail, "caps": [cap_gross, cap_pos]}
        sleeve["last_fill_bar"] = dec["fill_bar"]
    run["orders"] = len(fills)
    run["fills"] = [{k: f[k] for k in ("symbol", "side", "qty", "price", "reason", "fee")} for f in fills]
    sleeve["runs"] = (sleeve["runs"] + [run])[-48:]
    return {"active": True, **run}


def public_view(sleeve: dict | None, marks: dict | None = None) -> dict | None:
    if not sleeve:
        return None
    out = {k: sleeve.get(k) for k in ("code", "slot", "venue", "margin_type", "capital_usdt", "cash", "units",
                                     "fees", "funding", "started_at", "last_fill_bar")}
    if marks:
        out["equity"] = sleeve_equity(sleeve, marks)
    out["last_run"] = (sleeve.get("runs") or [None])[-1]
    out["fills_recent"] = (sleeve.get("fills") or [])[-6:]
    out["events_recent"] = (sleeve.get("events") or [])[-6:]
    return out
