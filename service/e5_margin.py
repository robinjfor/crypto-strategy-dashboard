"""Top up isolated margin on E5 legs so the wallet matches backtest leverage 1.75.

Live E5 opens at exchange leverage 2 (older legs are still 3). Binance then sets the
isolated wallet to entry_notional / leverage, so the liquidation price is the 2x (or 3x)
price. The backtest wallet is entry_notional / 1.75 (demo_e5.LV). On 2026-11-17 03:47
Taipei an AIOT long liquidated at its 2x price 0.0792; the 1.75 wallet liquidates near
0.0679, and that day's mark low was 0.0732.

This module only calls POST /fapi/v1/positionMargin type=1 (add). It does not place
orders, does not set a stop, and does not change leverage. A legacy 3x position stays
at 3x; the extra wallet is what moves the liquidation price.

The target notional is abs(qty) * entry, not the mark notional. After a drop, mark/1.75
can already sit under the 2x wallet while the liquidation price is still the 2x price.
Mark notional is used only when the exchange row has no entry price.

Funding: the transfer is capped by E5's sizing pool (orchestrator quote-pool target,
otherwise the virtual-book equity) minus isolated margin already posted on E5 legs,
and by free USDT after leaving VBO's 1000 reserve. A shortfall the pool cannot cover
is topped up only for the remainder and logged. VBO's own positions are never topped up.

Idempotent: a later run sees the wallet already at the target and does not transfer.
A shortfall is skipped only when it is below min(0.05 USDT, 1% of the target margin).
The old 0.5 USDT band left MOVR, W, ICNT, APR, and PROM at 2x.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import demo_e5
import isolated_liq

log = logging.getLogger("e5_margin")

TARGET_LV = float(demo_e5.LV)  # 1.75
SKIP_CAP_USDT = 0.05
SKIP_PCT = 0.01
_EPS = 1e-9
# cum=0 long: liq(L) / entry = (1 - 1/L) / (1 - mmr). mmr cancels across two leverages.
FORMULA = "wallet >= abs(qty)*entry/1.75; long liq ratio (1-1/1.75)/(1-1/2) when cum=0"


def skip_threshold(target_margin: float) -> float:
    """Skip a top-up only below the smaller of 0.05 USDT and 1% of the 1.75 wallet."""
    return min(SKIP_CAP_USDT, SKIP_PCT * max(0.0, abs(float(target_margin))))


def shortfall(notional: float, margin: float, lv: float = TARGET_LV) -> float:
    """USDT still needed so isolated wallet >= abs(notional) / lv. Never negative."""
    if lv <= 0:
        return 0.0
    return max(0.0, abs(float(notional)) / float(lv) - float(margin))


def margin_base(entry_notional: float, mark_notional: float) -> float:
    """Notional the 1.75 wallet is measured against. Entry wins when the exchange has one."""
    if entry_notional > 0:
        return float(entry_notional)
    return abs(float(mark_notional))


def liquidation_price_for_margin(entry: float, qty: float, margin: float, mmr: float = 0.0, cum: float = 0.0) -> float | None:
    """Isolated liquidation price for an arbitrary wallet.

    effective leverage = abs(qty) * entry / margin, then the standard isolated formula:
      long  (entry * (1 - 1/L) - cum/qty) / (1 - mmr)
      short (entry * (1 + 1/L) + cum/|qty|) / (1 + mmr)
    """
    notional = abs(float(qty)) * float(entry)
    if notional <= 0 or margin <= 0:
        return None
    return isolated_liq.liquidation_price(float(entry), float(qty), notional / float(margin), float(mmr), float(cum))


def liq_scale_long(liq_at_lev: float, lev_from: float, lev_to: float) -> float:
    """Rescale a long liquidation price from lev_from to lev_to. cum=0; mmr cancels."""
    return float(liq_at_lev) * (1.0 - 1.0 / float(lev_to)) / (1.0 - 1.0 / float(lev_from))


def distance(mark: float | None, liq: float | None) -> float | None:
    """|mark - liq| / mark. Same definition as liq_distance.distance. None when liq is not positive."""
    if mark is None or liq is None or mark <= 0 or liq <= 0:
        return None
    return abs(float(mark) - float(liq)) / float(mark)


def posted_margin(row: dict) -> float | None:
    """Isolated wallet, excluding unrealized PnL. None when the row does not say."""
    wallet = _f(row, "isolatedWallet")
    if wallet is not None:
        return wallet
    margin = _f(row, "isolatedMargin")
    if margin is None:
        return None
    upnl = _f(row, "unRealizedProfit")
    if upnl is None:
        return margin
    return margin - upnl


def ceil_cent(amount: float) -> float:
    if amount <= 0:
        return 0.0
    return math.ceil(amount * 100 - 1e-9) / 100.0


def floor_cent(amount: float) -> float:
    if amount <= 0:
        return 0.0
    return math.floor(amount * 100 + 1e-9) / 100.0


def transfer_amount(need: float, budget: float, threshold: float | None = None) -> float:
    """Cents to add. 0 when the shortfall or the remaining budget is under the skip threshold.

    The full shortfall is rounded up to the cent so the wallet is not left short by a
    fraction the API would drop. A partial fill is rounded down so it cannot exceed the budget.
    """
    floor = SKIP_CAP_USDT if threshold is None else max(0.0, float(threshold))
    if need < floor - 1e-6 or budget < floor - 1e-6:
        return 0.0
    if need <= budget + 1e-9:
        amount = ceil_cent(need)
        if amount > budget + 1e-9:
            amount = floor_cent(budget)
    else:
        amount = floor_cent(min(need, budget))
    if amount < floor - 1e-6:
        return 0.0
    return amount


def pool_base(state: dict, slot: str, virtual_equity: float, now: datetime | None = None) -> tuple[float, str]:
    """E5 margin budget base: fresh quote-pool sizing target, else virtual-book equity.

    Does not read orchestrator.load's process globals, so a stale plan from another
    slot cannot leak in. A quote-pool target is not also capped at this slot's equity
    (E and R2S share the USDT-M wallet). An explicit state['slot_caps'] equity still applies.
    """
    equity = max(0.0, float(virtual_equity))
    plan = state.get("orchestrator") if isinstance(state.get("orchestrator"), dict) else None
    if not plan or plan.get("sizing") != "quote_pool":
        return equity, "virtual_equity"
    try:
        import orchestrator
        at = datetime.fromisoformat(str(plan["generated_at"]))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if (now or datetime.now(timezone.utc)) - at > orchestrator.MAX_AGE:
            return equity, "virtual_equity"
    except (TypeError, ValueError, KeyError):
        return equity, "virtual_equity"
    raw = (plan.get("targets") or {}).get(slot)
    if raw is None:
        return equity, "virtual_equity"
    try:
        value = max(0.0, float(raw))
    except (TypeError, ValueError):
        return equity, "virtual_equity"
    cap_pool = (plan.get("quote_pool") or {}).get(slot)
    if cap_pool is not None:
        try:
            value = min(value, max(0.0, float(cap_pool)))
        except (TypeError, ValueError):
            pass
    spec = (state.get("slot_caps") or {}).get(slot) or {}
    if isinstance(spec, dict) and spec.get("equity") is not None:
        try:
            value = min(value, max(0.0, float(spec["equity"])))
        except (TypeError, ValueError):
            pass
    return value, "sizing_pool"


def topup(state: dict, fut, *, slot: str, key: str, now: datetime | None = None) -> dict:
    """Bring each same-side E5 isolated leg up to entry_notional / 1.75.

    Writes state[key]['margin_topup'] (latest check) and, when USDT actually moves,
    appends that transfer to state[key]['margin_topup_log']. Does not change book or cash.
    """
    ex = state.setdefault(key, {})
    if not isinstance(ex, dict):
        ex = {}
        state[key] = ex
    book = {s: float(q) for s, q in (ex.get("book") or {}).items() if abs(float(q or 0)) > 1e-12}
    at = (now or datetime.now(timezone.utc)).isoformat()
    if not book:
        report = _report(at, symbols={}, added=0.0, status="no_positions")
        ex["margin_topup"] = report
        return report
    try:
        rows = _rows(fut)
    except Exception as e:  # noqa: BLE001
        log.warning("e5_margin_positions_fail err=%s", e)
        report = _report(at, symbols={}, added=0.0, status="error", error=str(e)[:200])
        ex["margin_topup"] = report
        return report
    try:
        available = _available(fut)
    except Exception as e:  # noqa: BLE001
        log.warning("e5_margin_balance_fail err=%s", e)
        report = _report(at, symbols={}, added=0.0, status="error", error=str(e)[:200])
        ex["margin_topup"] = report
        return report

    plans = [_plan_symbol(sym, qty, rows) for sym, qty in sorted(book.items())]
    prices = {}
    for p in plans:
        if p.get("mark"):
            prices[p["symbol"]] = p["mark"]
        elif p.get("entry"):
            prices[p["symbol"]] = p["entry"]
    equity = _equity(ex, prices)
    base, source = pool_base(state, slot, equity, now=now)
    posted = sum(float(p["margin_before"]) for p in plans if p.get("counts_posted"))
    remaining_pool = max(0.0, base - posted)
    vbo_posted = _vbo_locked(state, rows)
    import orchestrator
    vbo_keep = max(0.0, float(orchestrator.VBO_RESERVE_USDT) - vbo_posted)
    if available is None:
        spendable = 0.0
        wallet_limit = "available"
    else:
        spendable = max(0.0, float(available) - vbo_keep)
        wallet_limit = "vbo_reserve" if vbo_keep > _EPS else "available"
    if remaining_pool <= spendable + 1e-9:
        limit = "e5_pool"
    else:
        limit = wallet_limit
    budget = min(spendable, remaining_pool)
    left = budget
    stop = False
    added_total = 0.0
    for p in sorted(plans, key=lambda r: (-float(r.get("shortfall") or 0), r["symbol"])):
        if not p.get("actionable"):
            p["added"] = 0.0
            continue
        need = float(p["shortfall"])
        floor = float(p.get("skip_threshold") if p.get("skip_threshold") is not None else SKIP_CAP_USDT)
        if stop and need >= floor - 1e-6:
            p["status"] = "skipped_balance"
            p["limit"] = "available"
            p["added"] = 0.0
            continue
        if need <= 1e-6:
            p["status"] = "ok"
            p["added"] = 0.0
            continue
        if need < floor - 1e-6:
            p["status"] = "deadband"
            p["added"] = 0.0
            continue
        amount = transfer_amount(need, left, floor)
        if amount <= 0:
            p["status"] = "skipped_pool" if limit == "e5_pool" else "skipped_balance"
            p["limit"] = limit
            p["added"] = 0.0
            continue
        try:
            fut.add_isolated_margin(p["symbol"], amount)
        except Exception as e:  # noqa: BLE001
            p["status"] = "error"
            p["error"] = str(e)[:160]
            p["added"] = 0.0
            log.warning("e5_margin_add_fail sym=%s err=%s", p["symbol"], str(e)[:160])
            if any(code in str(e) for code in ("-2019", "-2018", "-4050", "insufficient", "Insufficient")):
                stop = True
            continue
        p["added"] = amount
        left = max(0.0, left - amount)
        added_total += amount
        p["status"] = "partial" if amount + 1e-6 < need else "topped_up"
        if p["status"] == "partial":
            p["limit"] = limit
        _log_transfer(state, slot, p)

    refreshed: dict = {}
    if added_total > 0:
        try:
            refreshed = _by_symbol(_rows(fut))
        except Exception as e:  # noqa: BLE001
            log.warning("e5_margin_reread_fail err=%s", e)
    symbols = {}
    for p in plans:
        again = _match_row(refreshed, p["symbol"], float(p.get("e5_qty") or 0)) if refreshed else None
        symbols[p["symbol"]] = _finalize(p, again)
    statuses = {s.get("status") for s in symbols.values()}
    if "error" in statuses:
        top_status = "error"
    elif statuses & {"partial", "skipped_pool", "skipped_balance"}:
        top_status = "partial"
    else:
        top_status = "ok"
    unmet = bool(statuses & {"partial", "skipped_pool", "skipped_balance"})
    report = _report(at, symbols=symbols, added=round(added_total, 2), status=top_status,
                     pool=round(base, 4), pool_source=source, equity=round(equity, 4),
                     margin_posted=round(posted, 4), pool_remaining=round(remaining_pool, 4),
                     available=None if available is None else round(float(available), 4),
                     vbo_reserve=float(orchestrator.VBO_RESERVE_USDT), vbo_margin_posted=round(vbo_posted, 4),
                     vbo_kept=round(vbo_keep, 4), budget=round(budget, 4), limit=limit if unmet else None)
    ex["margin_topup"] = report
    if added_total > 0:
        moved = {s: {k: v[k] for k in ("notional", "margin_before", "margin_after", "liq_before", "liq_after", "added", "status") if k in v}
                 for s, v in symbols.items() if v.get("added")}
        ex["margin_topup_log"] = (list(ex.get("margin_topup_log") or []) + [
            {"at": at, "added": report["added"], "symbols": moved}])[-30:]
    log.info("e5_margin_topup slot=%s added=%.2f budget=%.2f limit=%s symbols=%s",
             slot, added_total, budget, report.get("limit"), len(symbols))
    return report


def run_for_reconcile(state0: dict, scratch: dict | None, traded: bool, key: str, slot: str, fut) -> dict | None:
    """Hourly (and post-flush) entry. No-op unless this client can add isolated margin.

    When this run traded, the report is written onto the scratch ledger that reconcile
    saves. Otherwise it is written onto a copy of the stored ledger and returned for merge.
    """
    if fut is None or getattr(fut, "unavailable", False) or not hasattr(fut, "add_isolated_margin"):
        return None
    import copy
    host = copy.deepcopy({k: state0.get(k) for k in ("demo_vbo1h_exec", "orchestrator", "slot_caps") if (state0 or {}).get(k) is not None})
    scratch = scratch or {}
    if traded and isinstance(scratch.get(key), dict):
        host[key] = scratch[key]
    else:
        host[key] = copy.deepcopy((state0 or {}).get(key) or {})
    host["control_log"] = []
    report = topup(host, fut, slot=slot, key=key)
    return {"key": key, "ledger": host.get(key), "events": list(host.get("control_log") or []), "report": report}


def _plan_symbol(sym: str, e5_qty: float, rows: list[dict]) -> dict:
    row = _match_row(rows, sym, e5_qty)
    base = {"symbol": sym, "actionable": False, "counts_posted": False, "shortfall": 0.0, "added": 0.0, "e5_qty": e5_qty}
    if row is None:
        base["status"] = "skipped_flat"
        return base
    try:
        exch = float(row.get("positionAmt") or 0)
    except (TypeError, ValueError):
        base["status"] = "skipped_flat"
        return base
    if abs(exch) <= 1e-12:
        base["status"] = "skipped_flat"
        return base
    if e5_qty * exch <= 0:
        base["status"] = "skipped_opposite"
        return base
    margin_type = str(row.get("marginType") or "ISOLATED").upper()
    if margin_type not in ("ISOLATED", ""):
        base["status"] = "skipped_cross"
        return base
    margin = posted_margin(row)
    entry_n, mark_n = _notionals(row)
    notional = margin_base(entry_n, mark_n)
    entry = _f(row, "entryPrice") or 0.0
    mark = _f(row, "markPrice") or 0.0
    base.update(entry_notional=entry_n, mark_notional=mark_n, notional=notional, entry=entry, mark=mark,
                qty=exch, side="LONG" if exch > 0 else "SHORT", leverage=_lev(row),
                margin_before=margin, liq_exchange=_f(row, "liquidationPrice"))
    if margin is None:
        base["status"] = "margin_unknown"
        return base
    if notional <= 0:
        base["status"] = "skipped_flat"
        return base
    need = shortfall(notional, margin)
    target_margin = abs(notional) / TARGET_LV
    mmr = _mmr(row, entry, exch, margin, base.get("liq_exchange"), mark_n)
    liq = base.get("liq_exchange")
    if liq is None or liq <= 0:
        liq = liquidation_price_for_margin(entry, exch, margin, mmr)
    base.update(actionable=True, counts_posted=True, shortfall=need, margin_before=margin, mmr=mmr,
                target_margin=target_margin, skip_threshold=skip_threshold(target_margin),
                liq_before=liq, status="ok" if need <= 1e-6 else "short")
    return base


def _finalize(p: dict, refreshed: dict | None) -> dict:
    added = float(p.get("added") or 0)
    before = p.get("margin_before")
    after = None if before is None else float(before) + added
    liq_after = p.get("liq_before")
    if refreshed:
        got = posted_margin(refreshed)
        if got is not None:
            after = got
        exch_liq = _f(refreshed, "liquidationPrice")
        if exch_liq is not None and exch_liq > 0:
            liq_after = exch_liq
        elif after and p.get("entry") and p.get("qty"):
            liq_after = liquidation_price_for_margin(p["entry"], p["qty"], after, float(p.get("mmr") or 0.0))
    elif added > 0 and after and p.get("entry") and p.get("qty"):
        liq_after = liquidation_price_for_margin(p["entry"], p["qty"], after, float(p.get("mmr") or 0.0))
    mark = p.get("mark") or None
    out = {
        "notional": _round(p.get("notional"), 4),
        "entry_notional": _round(p.get("entry_notional"), 4),
        "mark_notional": _round(p.get("mark_notional"), 4),
        "leverage": p.get("leverage"),
        "side": p.get("side"),
        "margin_before": _round(before, 4),
        "margin_after": _round(after, 4),
        "target": _round((abs(p["notional"]) / TARGET_LV) if p.get("notional") else None, 4),
        "shortfall": _round(p.get("shortfall"), 4),
        "added": round(added, 2),
        "liq_before": _round(p.get("liq_before"), 8),
        "liq_after": _round(liq_after, 8),
        "distance_before": _round(distance(mark, p.get("liq_before")), 6),
        "distance_after": _round(distance(mark, liq_after), 6),
        "mark": _round(mark, 8),
        "status": p.get("status") or "ok",
        "skip_threshold": _round(p.get("skip_threshold"), 4),
    }
    if p.get("limit"):
        out["limit"] = p["limit"]
    if p.get("error"):
        out["error"] = p["error"]
    return out


def _report(at: str, *, symbols: dict, added: float, status: str, **extra) -> dict:
    report = {"at": at, "target_lv": TARGET_LV, "skip_cap_usdt": SKIP_CAP_USDT, "skip_pct": SKIP_PCT,
              "formula": FORMULA,
              "orders": 0, "leverage_changes": 0, "added": added, "status": status, "symbols": symbols}
    report.update(extra)
    return report


def _log_transfer(state: dict, slot: str, p: dict) -> None:
    from guards import log_event
    log_event(state, "e5_margin_topup", slot, symbol=p["symbol"], added=round(float(p["added"]), 2),
              notional=round(float(p.get("notional") or 0), 4), margin_before=round(float(p.get("margin_before") or 0), 4),
              shortfall=round(float(p.get("shortfall") or 0), 4), leverage=p.get("leverage"), status=p.get("status"),
              orders=0)


def _vbo_locked(state: dict, rows: list[dict]) -> float:
    """Isolated margin already sitting in VBO's own 2x wallet. E5 top-ups are not counted.

    Unknown rows count as 0 locked, so the full reserve stays out of E5's reach.
    """
    import netting
    import orchestrator
    key = netting.SLOTS.get(orchestrator.VBO_SLOT)
    book = ((state.get(key) or {}).get("book") or {}) if key else {}
    total = 0.0
    for sym, qty in book.items():
        try:
            qty = float(qty or 0)
        except (TypeError, ValueError):
            continue
        if abs(qty) <= 1e-12:
            continue
        row = _match_row(rows, sym, qty)
        if row is None:
            continue
        margin = posted_margin(row)
        if margin is None or margin <= 0:
            continue
        try:
            exch = float(row.get("positionAmt") or 0)
        except (TypeError, ValueError):
            continue
        if exch == 0 or qty * exch <= 0:
            continue
        share = min(1.0, abs(qty) / abs(exch))
        entry = _f(row, "entryPrice") or 0.0
        mark = _f(row, "markPrice") or 0.0
        px = mark or entry
        if px <= 0:
            continue
        own_initial = abs(qty) * px / float(netting.LEV_BY_SLOT.get(orchestrator.VBO_SLOT, 2))
        total += min(margin * share, own_initial)
    return total


def _equity(ex: dict, prices: dict) -> float:
    cash = ex.get("cash", demo_e5.CAPITAL)
    try:
        cash = float(cash)
    except (TypeError, ValueError):
        cash = float(demo_e5.CAPITAL)
    total = cash
    for sym, qty in (ex.get("book") or {}).items():
        try:
            total += float(qty or 0) * float(prices.get(sym) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _rows(fut) -> list[dict]:
    raw = fut.position_risk() or []
    if isinstance(raw, dict):
        raw = raw.get("positions") or []
    return [r for r in raw if isinstance(r, dict)]


def _by_symbol(rows: list[dict]) -> dict[str, list]:
    out: dict[str, list] = {}
    for row in rows:
        out.setdefault(row.get("symbol"), []).append(row)
    return out


def _match_row(rows, sym: str, e5_qty: float) -> dict | None:
    grouped = rows if isinstance(rows, dict) else _by_symbol(rows)
    cands = []
    for row in grouped.get(sym) or []:
        try:
            amt = float(row.get("positionAmt") or 0)
        except (TypeError, ValueError):
            continue
        if abs(amt) > 1e-12:
            cands.append(row)
    same = []
    for row in cands:
        if float(row.get("positionAmt") or 0) * e5_qty > 0:
            same.append(row)
    use = same or cands
    if not use:
        return None
    return max(use, key=lambda r: abs(float(r.get("positionAmt") or 0)))


def _notionals(row: dict) -> tuple[float, float]:
    try:
        qty = abs(float(row.get("positionAmt") or 0))
    except (TypeError, ValueError):
        qty = 0.0
    entry = _f(row, "entryPrice") or 0.0
    mark = _f(row, "markPrice") or 0.0
    mark_n = _f(row, "notional")
    if mark_n is None:
        mark_n = qty * mark
    else:
        mark_n = abs(mark_n)
    entry_n = qty * entry if entry > 0 else 0.0
    return entry_n, mark_n


def _mmr(row: dict, entry: float, qty: float, margin: float, liq: float | None, mark_notional: float) -> float:
    implied = _implied_mmr(entry, qty, margin, liq)
    if implied is not None:
        return implied
    maint = _f(row, "maintMargin")
    if maint is not None and mark_notional > 0 and maint >= 0:
        return min(0.5, maint / mark_notional)
    return isolated_liq.DEFAULT_TIER[0][1]


def _implied_mmr(entry: float, qty: float, margin: float, liq: float | None) -> float | None:
    """Invert the cum=0 isolated formula. None when the exchange price does not fit it."""
    if not entry or not qty or margin <= 0 or liq is None or liq <= 0:
        return None
    unit = margin / abs(qty)
    if qty > 0:
        raw = (entry - unit) / liq
        if raw <= 0:
            return None
        mmr = 1.0 - raw
    else:
        mmr = (entry + unit) / liq - 1.0
    if 0.0 <= mmr < 0.5:
        return mmr
    return None


def _available(fut) -> float | None:
    account = fut.account() or {}
    for asset in account.get("assets") or []:
        if str(asset.get("asset") or "").upper() != "USDT":
            continue
        raw = asset.get("availableBalance")
        if raw not in (None, ""):
            return max(0.0, float(raw))
    raw = account.get("availableBalance")
    if raw in (None, ""):
        return None
    return max(0.0, float(raw))


_SKIPPED = {"deadband", "skipped_pool", "skipped_balance", "skipped_flat", "skipped_opposite", "skipped_cross", "margin_unknown"}


def review_view(state: dict | None) -> dict:
    """Read-only top-up record for /review/positions. Does not call the exchange."""
    state = state if isinstance(state, dict) else {}
    ex = state.get("demo_e5v_exec") if isinstance(state.get("demo_e5v_exec"), dict) else {}
    top = ex.get("margin_topup") if isinstance(ex.get("margin_topup"), dict) else {}
    last_topup: dict[str, dict] = {}
    for entry in ex.get("margin_topup_log") or []:
        if not isinstance(entry, dict):
            continue
        for sym, row in (entry.get("symbols") or {}).items():
            if isinstance(row, dict):
                last_topup[str(sym)] = {"at": entry.get("at"), "added": row.get("added"), "status": row.get("status")}
    symbols = []
    for sym, row in sorted((top.get("symbols") or {}).items()):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        symbols.append({
            "symbol": sym,
            "target": row.get("target"),
            "isolated_margin": row.get("margin_after") if row.get("margin_after") is not None else row.get("margin_before"),
            "last_topup": last_topup.get(str(sym)),
            "skipped_reason": status if status in _SKIPPED else None,
            "status": status or None,
            "shortfall": row.get("shortfall"),
            "skip_threshold": row.get("skip_threshold"),
        })
    last_run = ex.get("last_run") if isinstance(ex.get("last_run"), dict) else {}
    return {
        "ledger": "demo_e5v_exec",
        "reconcile_last_run_at": last_run.get("at"),
        "checked_at": top.get("at"),
        "symbols": symbols,
    }


def _lev(row: dict):
    raw = row.get("leverage")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return raw


def _f(row: dict, *names: str) -> float | None:
    for name in names:
        raw = row.get(name)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def _round(value, places: int):
    if value is None:
        return None
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None
