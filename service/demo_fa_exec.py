"""Order-placing path for demo_funding_arb_1x. Runs from reconcile once per decided bar (fa_targets.json).
Active ONLY when the slot is enabled AND approved AND accepted AND both gates pass; otherwise a no-op.
Exits (both legs) always run when active (risk-reducing). Entries are skipped while the account or the slot is
paused, or a macro/guard entry block is active. One position per coin: no entry if any other slot or the futures
account already holds that coin. Fills are read back from Binance; demo_fa_positions holds the real filled qty."""
from __future__ import annotations

import logging
import time

import demo_fa
from guards import log_event

log = logging.getLogger("demo_fa_exec")
PERP_LEVERAGE = 3  # perp margin = 1/3 notional (isolated), net 1x with the spot leg


def active(state: dict) -> tuple[bool, str]:
    from demo_candidates import accepted
    g = (state.get("demo_candidates") or {}).get(demo_fa.SLOT) or {}
    for k, why in (("enabled", "未啟用"), ("approved", "未批准"), ("parity_pass", "對帳未通過"), ("cloud_ordering_ok", "下單測試未通過")):
        if not g.get(k):
            return False, why
    if not accepted(demo_fa.SLOT):
        return False, "未驗收"
    return True, ""


def entry_block(state: dict) -> str | None:
    import guards
    from main import slot_paused
    if state.get("paused"):
        return "帳戶暫停"
    if slot_paused(state, demo_fa.SLOT):
        return "本倉暫停"
    blocked, why = guards.entry_blocked(state)
    return f"風控封鎖：{why}" if blocked else None


def _spot_net_qty(order: dict) -> tuple[float, float]:
    qty = float(order.get("executedQty") or 0); quote = float(order.get("cummulativeQuoteQty") or 0)
    base = (order.get("symbol") or "")[:-4]
    fee = sum(float(f.get("commission") or 0) for f in order.get("fills") or [] if f.get("commissionAsset") == base)
    return max(qty - fee, 0.0), (quote / qty if qty else 0.0)


def _perp_fill(fut, sym: str, order: dict, coid: str) -> tuple[float, float]:
    o = order
    for _ in range(5):
        if float(o.get("executedQty") or 0) > 0 and o.get("status") == "FILLED":
            break
        time.sleep(0.5)
        o = fut.get_order(sym, coid) or o
    return float(o.get("executedQty") or 0), float(o.get("avgPrice") or 0)


def execute(state: dict, spot, fut, targets: dict | None) -> dict:
    ok, why = active(state)
    if not ok:
        return {"skipped": why}
    if not targets or targets.get("held") is None:
        return {"skipped": "no targets"}
    ex = state.setdefault("demo_fa_exec", {})
    bar = targets.get("decided_bar")
    if ex.get("last_bar") == bar:
        return {"skipped": "already executed", "bar": bar}
    from futures_client import round_qty_futures
    from main import same_coin_position_slot, opposite_position_slot
    book = state.setdefault("demo_fa_positions", {})
    want = list(dict.fromkeys(targets["held"]))
    info = fut.exchange_info()
    out = {"bar": bar, "exits": [], "entries": [], "skipped": []}
    tag = (bar or "").replace("-", "")[2:]

    for sym in [s for s in list(book) if s not in want]:
        pos = book[sym]
        try:
            pq = round_qty_futures(info, sym, float(pos.get("perp_qty") or 0))
            if pq > 0:
                coid = f"faX-p-{sym[:12]}-{tag}"
                o = fut.new_order(symbol=sym, side="BUY", type="MARKET", quantity=pq, reduceOnly="true", newClientOrderId=coid)
                fq, fpx = _perp_fill(fut, sym, o, coid)
            else:
                fq, fpx = 0.0, 0.0
            sq = float(pos.get("spot_qty") or 0)
            so = spot.market_sell(sym, sq, f"faX-s-{sym[:12]}-{tag}") if sq > 0 else {}
            sfq, spx = _spot_net_qty({**so, "fills": []}) if so else (0.0, 0.0)
            book.pop(sym)
            log_event(state, "fa_exit", demo_fa.SLOT, symbol=sym, perp_qty=fq, perp_px=fpx, spot_qty=sfq, spot_px=spx, bar=bar)
            out["exits"].append(sym)
        except Exception as e:  # noqa: BLE001
            log_event(state, "fa_exit_error", demo_fa.SLOT, symbol=sym, error=str(e)[:120]); out["skipped"].append(f"{sym} exit error")

    blk = entry_block(state)
    new = [s for s in want if s not in book]
    if blk and new:
        log_event(state, "fa_entries_blocked", demo_fa.SLOT, reason=blk, symbols=new, bar=bar)
        out["skipped"] += [f"{s} {blk}" for s in new]
        new = []
    held_fut = {r["symbol"] for r in fut.position_risk() if float(r.get("positionAmt") or 0) != 0} if new else set()
    notional = demo_fa.CAPITAL / int(targets.get("K", demo_fa.K)) / (1 + demo_fa.MARGIN)
    for sym in new:
        other = (same_coin_position_slot(state, demo_fa.SLOT, sym, "LONG") or opposite_position_slot(state, demo_fa.SLOT, sym, "LONG")
                 or ("futures_account" if sym in held_fut else None))
        bad = ((((state.get("demo_candidates") or {}).get(demo_fa.SLOT) or {}).get("ordering_detail") or {}).get("universe_check") or {}).get("failed") or {}
        if sym in bad:
            log_event(state, "fa_entry_skip_untradable", demo_fa.SLOT, symbol=sym, why=bad[sym], bar=bar)
            out["skipped"].append(f"{sym} order test failed"); continue
        if other:
            log_event(state, "fa_entry_skip_same_coin", demo_fa.SLOT, symbol=sym, held_by=other, bar=bar)
            out["skipped"].append(f"{sym} held by {other}"); continue
        try:
            fut.set_margin_type(sym, "ISOLATED"); fut.set_leverage(sym, PERP_LEVERAGE)
            mark = float(fut.mark_price(sym)); q = round_qty_futures(info, sym, notional / mark)
            if q <= 0:
                out["skipped"].append(f"{sym} below min"); continue
            coid = f"faE-p-{sym[:12]}-{tag}"
            o = fut.new_order(symbol=sym, side="SELL", type="MARKET", quantity=q, newClientOrderId=coid)
            pq, ppx = _perp_fill(fut, sym, o, coid)
            if pq <= 0:
                log_event(state, "fa_entry_error", demo_fa.SLOT, symbol=sym, error="perp not filled"); continue
            try:
                so = spot.market_buy(sym, pq * ppx, f"faE-s-{sym[:12]}-{tag}")
                sq, spx = _spot_net_qty(so)
                if sq <= 0:
                    raise RuntimeError("spot not filled")
            except Exception as e:  # noqa: BLE001  unwind perp so we never hold a naked short
                fut.new_order(symbol=sym, side="BUY", type="MARKET", quantity=pq, reduceOnly="true", newClientOrderId=f"faU-p-{sym[:12]}-{tag}")
                log_event(state, "fa_entry_unwound", demo_fa.SLOT, symbol=sym, error=str(e)[:120]); out["skipped"].append(f"{sym} spot failed, unwound"); continue
            book[sym] = {"spot_qty": sq, "perp_qty": pq, "spot_px": spx, "perp_px": ppx, "bar": bar}
            log_event(state, "fa_entry", demo_fa.SLOT, symbol=sym, spot_qty=sq, spot_px=spx, perp_qty=pq, perp_px=ppx, bar=bar)
            out["entries"].append(sym)
        except Exception as e:  # noqa: BLE001
            log_event(state, "fa_entry_error", demo_fa.SLOT, symbol=sym, error=str(e)[:120]); out["skipped"].append(f"{sym} error")
    import datetime as _dt
    try:  # residual gap vs the spec fill (open of the bar after decided_bar = decided_bar+1d 00:00 UTC = 08:00 Taipei)
        op = _dt.datetime.fromisoformat(str(bar)).replace(tzinfo=_dt.timezone.utc) + _dt.timedelta(days=1)
        out["lag_s"] = round((_dt.datetime.now(_dt.timezone.utc) - op).total_seconds())
    except Exception:  # noqa: BLE001
        pass
    ex.update(last_bar=bar, last=out)
    ex["lags"] = (list(ex.get("lags") or []) + ([[str(bar), out["lag_s"]]] if "lag_s" in out else []))[-60:]
    return out
