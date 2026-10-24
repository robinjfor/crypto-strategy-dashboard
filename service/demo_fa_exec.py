"""Order-placing path for demo_funding_arb_1x. Runs from reconcile once per decided bar (fa_targets.json).
Active ONLY when the slot is enabled AND approved AND accepted AND both gates pass; otherwise a no-op.
Exits (both legs) always run when active (risk-reducing). Entries are skipped while the account or the slot is
paused, or a macro/guard entry block is active. The perp leg is a virtual-ledger intent netted per symbol with the
other Demo slots (netting.py, virtual qty = -perp_qty): no "held by another slot" skip; TradFi symbols are never
entered. Fills are read back from Binance; demo_fa_positions holds the filled qty allocated to FA."""
from __future__ import annotations

import logging
import time

import demo_fa
import netting
import tradfi
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


def _perp(fut, info, state, sym: str, delta: float, tag: str) -> tuple[float, float]:
    """one netted perp intent for FA, sent now (the spot leg depends on the fill). -> (signed filled qty, px)"""
    got = [0.0, 0.0]
    n = netting.Netter(fut, info, tag=tag)
    n.add(state, demo_fa.SLOT, sym, delta, 0.0, lambda q, px, fee: got.__setitem__(slice(0, 2), [q, px]))
    n.flush()
    return got[0], got[1]


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


SKIP = {"PUMPUSDT"}  # Demo futures has no PUMPUSDT contract (10/23 universe order test); never enter


def open_px(sym: str, leg: str, open_ms: int) -> float | None:
    """Backtest fill price = mainnet 1d open at open_ms (spot: api.binance.com, perp: fapi.binance.com), public data."""
    import json as _j, urllib.request as _u
    base = "https://api.binance.com/api/v3/klines" if leg == "spot" else "https://fapi.binance.com/fapi/v1/klines"
    try:
        r = _j.loads(_u.urlopen(f"{base}?symbol={sym}&interval=1d&startTime={open_ms}&limit=1", timeout=10).read())
        return float(r[0][1]) if r and int(r[0][0]) == open_ms else None
    except Exception:  # noqa: BLE001
        return None


def _slip(state, bar, sym, leg, side, fill, lag_s):
    """Record per-fill slippage vs the backtest 08:00 (Taipei) open; bps > 0 = worse than backtest."""
    import datetime as _dt
    try:
        op = _dt.datetime.fromisoformat(str(bar)).replace(tzinfo=_dt.timezone.utc) + _dt.timedelta(days=1)
    except Exception:  # noqa: BLE001
        return
    ref = open_px(sym, leg, int(op.timestamp() * 1000))
    bps = None
    if ref and fill:
        bps = round(((fill / ref - 1) if side == "BUY" else (ref / fill - 1)) * 1e4, 2)
    rec = {"date": str(op.date()), "symbol": sym, "leg": leg, "side": side, "fill": fill, "open": ref, "bps": bps, "lag_s": lag_s}
    state["demo_fa_slippage"] = (list(state.get("demo_fa_slippage") or []) + [rec])[-400:]


def _lag(bar):
    import datetime as _dt
    try:
        op = _dt.datetime.fromisoformat(str(bar)).replace(tzinfo=_dt.timezone.utc) + _dt.timedelta(days=1)
        return round((_dt.datetime.now(_dt.timezone.utc) - op).total_seconds())
    except Exception:  # noqa: BLE001
        return None


def execute(state: dict, spot, fut, targets: dict | None) -> dict:
    ok, why = active(state)
    if not ok:
        return {"skipped": why}
    if not targets or targets.get("held") is None:
        return {"skipped": "no targets"}
    ex = state.get("demo_fa_exec") or {}
    state["demo_fa_exec"] = ex
    bar = targets.get("decided_bar")
    if ex.get("last_bar") == bar:
        return {"skipped": "already executed", "bar": bar}
    from futures_client import round_qty_futures
    book = state.get("demo_fa_positions") or {}
    state["demo_fa_positions"] = book
    want = list(dict.fromkeys(targets["held"]))
    info = fut.exchange_info()
    out = {"bar": bar, "exits": [], "entries": [], "skipped": []}
    tag = (bar or "").replace("-", "")[2:]

    for sym in [s for s in list(book) if s not in want]:
        pos = book[sym]
        try:
            pq = round_qty_futures(info, sym, float(pos.get("perp_qty") or 0))
            if pq > 0:
                fq, fpx = _perp(fut, info, state, sym, pq, f"faX{tag}")
            else:
                fq, fpx = 0.0, 0.0
            sq = float(pos.get("spot_qty") or 0)
            so = spot.market_sell(sym, sq, f"faX-s-{sym[:12]}-{tag}") if sq > 0 else {}
            sfq, spx = _spot_net_qty({**so, "fills": []}) if so else (0.0, 0.0)
            book.pop(sym)
            if fq: _slip(state, bar, sym, "perp", "BUY", fpx, _lag(bar))
            if sfq: _slip(state, bar, sym, "spot", "SELL", spx, _lag(bar))
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
    import orchestrator  # sizing capital only; existing positions/exits untouched
    out["sizing_capital"] = cap = orchestrator.sizing_capital(demo_fa.SLOT, demo_fa.CAPITAL)
    notional = cap / int(targets.get("K", demo_fa.K)) / (1 + demo_fa.MARGIN)
    for sym in new:
        # netted Demo slots / the futures account no longer block (virtual ledgers); a legacy (non-netted) slot holding
        # the same coin still does (its spot/perp balance would mix with FA's)
        from execution import base_asset
        other = next((pid for pid, p in (state.get("positions") or {}).items() if isinstance(p, dict) and p.get("status") == "FILLED"
                      and base_asset(p.get("symbol") or "") == base_asset(sym)), None)
        if tradfi.is_tradfi(sym, info, state):
            log_event(state, "fa_entry_skip_tradfi", demo_fa.SLOT, symbol=sym, bar=bar); out["skipped"].append(f"{sym} TradFi"); continue
        bad = ((((state.get("demo_candidates") or {}).get(demo_fa.SLOT) or {}).get("ordering_detail") or {}).get("universe_check") or {}).get("failed") or {}
        if sym in SKIP and sym not in bad:
            bad = {**bad, sym: ["fixed skip"]}
        if sym in bad:
            log_event(state, "fa_entry_skip_untradable", demo_fa.SLOT, symbol=sym, why=bad[sym], bar=bar)
            out["skipped"].append(f"{sym} order test failed"); continue
        if other:
            log_event(state, "fa_entry_skip_same_coin", demo_fa.SLOT, symbol=sym, held_by=other, bar=bar)
            out["skipped"].append(f"{sym} held by {other}"); continue
        try:
            mark = float(fut.mark_price(sym)); q = round_qty_futures(info, sym, notional / mark)
            if q <= 0:
                out["skipped"].append(f"{sym} below min"); continue
            pq, ppx = _perp(fut, info, state, sym, -q, f"faE{tag}"); pq = -pq
            if pq <= 0:
                log_event(state, "fa_entry_error", demo_fa.SLOT, symbol=sym, error="perp not filled"); continue
            try:
                so = spot.market_buy(sym, pq * ppx, f"faE-s-{sym[:12]}-{tag}")
                sq, spx = _spot_net_qty(so)
                if sq <= 0:
                    raise RuntimeError("spot not filled")
            except Exception as e:  # noqa: BLE001  unwind perp so we never hold a naked short
                _perp(fut, info, state, sym, pq, f"faU{tag}")
                log_event(state, "fa_entry_unwound", demo_fa.SLOT, symbol=sym, error=str(e)[:120]); out["skipped"].append(f"{sym} spot failed, unwound"); continue
            _slip(state, bar, sym, "perp", "SELL", ppx, _lag(bar)); _slip(state, bar, sym, "spot", "BUY", spx, _lag(bar))
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
