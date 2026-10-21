"""Binance reconcile (spec 3.3): every 15 min (Cloud Run job crypto-trader-reconcile) and at the end of
each runner job. Read-only against Binance. On an unexplained mismatch the affected slot is paused via the
single-slot pause (paused_slots, paused_by=reconcile) and an alert is recorded for the dashboard.
It never resumes anything, never places/cancels orders and never touches approvals or allocation."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SPOT_TOL = 0.95  # exchange may hold less than saved qty only by fee dust
MAX_ALERTS = 100
MANUAL_LOOKBACK_MS = 7 * 86400 * 1000


def MANUAL_LOOKBACK_MS_FN() -> int:  # noqa: N802
    import time
    return int(time.time() * 1000) - MANUAL_LOOKBACK_MS


def now_iso_taipei() -> str:
    from execution import now_iso_taipei as f
    return f()


def futures_quote_reconcile(state: dict, quote: str, alloc_doc: dict | None, fc=None) -> dict:
    from api import _futures_quote_reconcile
    return _futures_quote_reconcile(state, quote, alloc_doc, fc=fc)


def _u1_slot_id(alloc_doc: dict | None) -> str | None:
    try:
        import u1_live
        s = u1_live.u1_slot(alloc_doc)
        return (s or {}).get("slot") or (s or {}).get("id")
    except Exception:  # noqa: BLE001
        return None


def _futures_slots_by_symbol(state: dict, quote: str, alloc_doc: dict | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sid, pos in (state.get("positions") or {}).items():
        if isinstance(pos, dict) and pos.get("status") == "FILLED" and (pos.get("venue") or "spot") == "futures" \
                and str(pos.get("symbol") or "").upper().endswith(quote):
            out.setdefault(str(pos["symbol"]).upper(), []).append(sid)
    sl = state.get("u1_live") or {}
    if sl and (sl.get("quote") or "USDT").upper() == quote:
        u1 = _u1_slot_id(alloc_doc) or "u1"
        try:
            import u1_live
            pm = u1_live.perp_map(quote)
            for k, u in (sl.get("units") or {}).items():
                if k in pm and float(u or 0) != 0:
                    out.setdefault(pm[k], []).append(u1)
        except Exception:  # noqa: BLE001
            pass
    return out


def _spot_checks(client, state: dict) -> list[dict]:
    from execution import base_asset
    held: dict[str, float] = {}
    for b in client.nonzero_balances() or []:
        held[str(b.get("asset"))] = float(b.get("free") or 0) + float(b.get("locked") or 0)
    want: dict[str, list[tuple[str, dict]]] = {}
    for sid, pos in (state.get("positions") or {}).items():
        if isinstance(pos, dict) and pos.get("status") == "FILLED" and (pos.get("venue") or "spot") == "spot" \
                and float(pos.get("qty") or 0) > 0:
            want.setdefault(base_asset(str(pos["symbol"])), []).append((sid, pos))
    out = []
    for base, rows in sorted(want.items()):
        exp = sum(float(p["qty"]) for _, p in rows)
        got = held.get(base, 0.0)
        if got >= exp * SPOT_TOL:
            continue
        kind, pause = "spot_qty_short", True
        # Resting hard stop filled but the runner hasn't booked it yet → explained, alert only.
        for sid, p in rows:
            if p.get("stop_order_id"):
                try:
                    o = client.get_order(p["symbol"], p["stop_order_id"])
                    if str(o.get("status")).upper() == "FILLED":
                        kind, pause = "stop_filled_pending_record", False
                except Exception:  # noqa: BLE001
                    pass
        if pause and hasattr(client, "all_orders"):
            man = manual_orders_since(None, client, "spot", rows[0][1]["symbol"], MANUAL_LOOKBACK_MS_FN())
            if man:
                kind, pause = "manual_order", False
        out.append({"venue": "spot", "symbol": rows[0][1]["symbol"], "asset": base, "trader": exp,
                    "binance": got, "slots": [s for s, _ in rows], "kind": kind, "pause": pause})
    return out


# ---------------------------------------------------------------- manual orders (人工單)
MANUAL_NOTE = "自動交易專家 人工單（Robin 核准）"


def _manual_fields(coids) -> dict:
    from execution import MANUAL_TAG, is_manual_coid
    if is_manual_coid(coids):
        return {"closed_by": "manual", "manual": True, "tag": MANUAL_TAG, "note": MANUAL_NOTE}
    return {"closed_by": "outside_runner"}


def tag_manual_closed_trades(st: dict) -> int:
    """Existing closed trades whose Binance client order id marks a manual order get the 人工單 tag."""
    n = 0
    for t in st.get("closed_trades") or []:
        if not t.get("manual") and t.get("client_order_id") and _manual_fields(t["client_order_id"]).get("manual"):
            t.update(_manual_fields(t["client_order_id"]))
            n += 1
    return n


def manual_orders_since(fc, client, venue: str, symbol: str, since_ms: int) -> list[dict]:
    """Manual-prefixed orders on Binance for this symbol since since_ms (read-only)."""
    from execution import is_manual_coid
    try:
        if venue == "futures":
            rows = fc.all_orders(symbol, start_ms=since_ms) or []
        else:
            rows = [o for o in (client.all_orders(symbol, 0) or []) if int(o.get("time") or 0) >= since_ms]
    except Exception:  # noqa: BLE001
        return []
    return [{"orderId": o.get("orderId"), "clientOrderId": o.get("clientOrderId"), "side": o.get("side"),
             "executedQty": o.get("executedQty"), "time": o.get("time")}
            for o in rows if is_manual_coid(o.get("clientOrderId")) and float(o.get("executedQty") or 0) > 0]


def u1_funding_true_up(state: dict, fc) -> dict | None:
    """Sleeve funding must equal Binance FUNDING_FEE income on the U1 perps since the sleeve started,
    paused or not (late-posted income rows can slip past the runner's cursor)."""
    sl = state.get("u1_live") or {}
    if not sl or not sl.get("started_at"):
        return None
    import u1_live
    pm = u1_live.perp_map(sl.get("quote") or "USDT")
    start = _iso_ms(sl["started_at"]) or 0
    tot = 0.0
    for sym in pm.values():
        for r in fc.income(sym, "FUNDING_FEE", start) or []:
            if r.get("incomeType", "FUNDING_FEE") == "FUNDING_FEE" and int(r.get("time") or start) >= start:
                tot += float(r.get("income") or 0)
    diff = round(tot - float(sl.get("funding") or 0), 10)
    return {"binance_funding": round(tot, 10), "sleeve_funding": sl.get("funding"), "diff": diff}


# ---------------------------------------------------------------- U1 closed outside the runner
def _iso_ms(v) -> int | None:
    try:
        return int(datetime.fromisoformat(str(v)).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _ms_taipei(ms: int) -> str:
    from datetime import timedelta
    return datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def find_u1_external_closes(state: dict, fc, quote: str = "USDC") -> list[dict]:
    """U1 sleeve holds units but Binance is flat: rebuild the close from Binance orders/trades/income
    (fills not placed by the runner, i.e. clientOrderId not in sleeve.fills). Read-only."""
    sl = state.get("u1_live") or {}
    if not sl or (sl.get("quote") or "USDT").upper() != quote:
        return []
    import u1_live
    pm = u1_live.perp_map(quote)
    known = {str(f.get("order_id")) for f in sl.get("fills") or []}
    out = []
    for k, units in (sl.get("units") or {}).items():
        units = float(units or 0)
        if k not in pm or units == 0:
            continue
        sym = pm[k]
        amt = sum(float(p.get("positionAmt") or 0) for p in fc.position_risk() or [] if p.get("symbol") == sym)
        if amt != 0:
            continue
        # entry = runner's own fills that built the current units (newest first)
        need, ent = abs(units), []
        for f in reversed(sl.get("fills") or []):
            if f.get("symbol") == sym or u1_live.perp_map(quote).get(f.get("coin") or f.get("k") or "") == sym \
                    or str(f.get("client_id") or "").endswith("-" + k):
                if (f.get("qty_signed") or 0) * units > 0 and need > 1e-12:
                    ent.append(f)
                    need -= abs(float(f.get("qty") or 0))
        since = min([_iso_ms(f.get("filled_at")) or 0 for f in ent] or [_iso_ms(sl.get("started_at")) or 0])
        orders = {str(o["orderId"]): o for o in fc.all_orders(sym, start_ms=since) or []}
        trades = [t for t in fc.user_trades_since(sym, since) or []
                  if str(t.get("orderId")) not in known and str(t.get("orderId")) in orders
                  and (t.get("side") == ("SELL" if units > 0 else "BUY"))]
        qty = sum(float(t["qty"]) for t in trades)
        if not trades or abs(qty - abs(units)) > 1e-9:
            continue
        quote_qty = sum(float(t["qty"]) * float(t["price"]) for t in trades)
        comm = sum(float(t["commission"]) for t in trades if str(t.get("commissionAsset")).upper() == quote)
        close_ms = max(int(t["time"]) for t in trades)
        fund = sum(float(i["income"]) for i in fc.income(sym, "FUNDING_FEE", since, close_ms + 1) or [])
        e_qty = sum(abs(float(f["qty"])) for f in ent) or abs(units)
        entry = (sum(abs(float(f["qty"])) * float(f["price"]) for f in ent) / e_qty) if ent else None
        entry_fee = sum(float(f.get("fee") or 0) for f in ent) if ent else None
        oids = sorted({str(t["orderId"]) for t in trades})
        out.append({
            "slot": sl.get("slot"), "code": sl.get("code"), "family": sl.get("family"),
            "strategy_id": sl.get("strategy_id"), "symbol": sym, "coin": k, "venue": "futures", "quote": quote,
            "side": "LONG" if units > 0 else "SHORT", "qty": abs(units), "entry": entry,
            "exit": round(quote_qty / qty, 8), "quote_qty": round(quote_qty, 8),
            "pnl_gross_usdt": round(sum(float(t.get("realizedPnl") or 0) for t in trades), 8),
            "entry_fee_usdt": entry_fee, "exit_fee_usdt": round(comm, 8),
            "exit_commissions": [{"asset": quote, "amount": round(comm, 8)}],
            "funding_usdt": round(fund, 8), "order_id": oids[0] if len(oids) == 1 else oids,
            "client_order_id": [orders[o].get("clientOrderId") for o in oids],
            "trade_ids": [t.get("id") for t in trades], "closed_at": _ms_taipei(close_ms),
            "reason": "external_close", "source": "binance_userTrades",
            **_manual_fields([orders[o].get("clientOrderId") for o in oids]),
            "entry_order_id": [f.get("order_id") for f in ent],
        })
    return out


def apply_u1_external_closes(st: dict, closes: list[dict]) -> list[str]:
    """Book Binance-sourced closes into the sleeve + closed_trades; idempotent by order id."""
    sl = st.get("u1_live") or {}
    done = []
    seen = {str(t.get("order_id")) for t in st.get("closed_trades") or []}
    for c in closes:
        k = c["coin"]
        if str(c["order_id"]) in seen or float((sl.get("units") or {}).get(k) or 0) == 0:
            continue
        sgn = 1.0 if c["side"] == "LONG" else -1.0
        sl["cash"] = float(sl.get("cash") or 0) + sgn * c["quote_qty"] - c["exit_fee_usdt"]
        sl["fees"] = float(sl.get("fees") or 0) + c["exit_fee_usdt"]
        sl["units"][k] = 0.0
        sl.setdefault("fills", []).append({"symbol": c["symbol"], "side": "SELL" if sgn > 0 else "BUY",
                                           "qty": c["qty"], "qty_signed": -sgn * c["qty"], "price": c["exit"],
                                           "fee": c["exit_fee_usdt"], "order_id": c["order_id"],
                                           "client_id": c["client_order_id"], "units_after": 0.0,
                                           "filled_at": c["closed_at"], "external": True})
        sl.setdefault("events", []).append({"at": c["closed_at"], "event": "external_close_adopted",
                                            "symbol": c["symbol"], "order_id": c["order_id"]})
        fees = [c.get("entry_fee_usdt"), c["exit_fee_usdt"]]
        net = c["pnl_gross_usdt"] - sum(float(f) for f in fees if f is not None)
        st.setdefault("closed_trades", []).append({**c, "pnl_usdt": round(net, 4),
                                                   "fee_usdt": round(sum(float(f) for f in fees if f is not None), 6),
                                                   "fees_complete": all(f is not None for f in fees)})
        done.append(c["symbol"])
    st["u1_live"] = sl
    return done


def build_report(state: dict, *, client=None, fc=None, alloc_doc=None) -> dict:
    """Read-only Binance snapshot + mismatch list. Errors are reported, never raised."""
    rep: dict = {"at": now_iso_taipei(), "at_utc": datetime.now(timezone.utc).isoformat(),
                 "wallets": {}, "mismatches": [], "errors": []}
    if alloc_doc is None:
        try:
            from allocation import resolve_allocation
            alloc_doc, _ = resolve_allocation()
        except Exception:  # noqa: BLE001
            alloc_doc = {}
    rep["u1_external_closes"] = []
    if fc is None:
        try:
            from futures_client import FuturesDemoClient
            fc = FuturesDemoClient() if FuturesDemoClient().api_key else None
        except Exception:  # noqa: BLE001
            fc = None
    if fc is not None:
        try:
            rep["u1_external_closes"] = find_u1_external_closes(state, fc)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"u1_external: {str(e)[:200]}")
        try:
            rep["u1_funding"] = u1_funding_true_up(state, fc)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"u1_funding: {str(e)[:200]}")
    adopted = {c["symbol"] for c in rep["u1_external_closes"]}
    for q in ("USDT", "USDC"):
        f = futures_quote_reconcile(state, q, alloc_doc, fc=fc)
        if f.get("error"):
            rep["errors"].append(f"futures_{q}: {f['error']}")
        rep["wallets"][q] = {k: f.get(k) for k in ("wallet", "margin_balance", "unrealized_pnl", "available")}
        rep["wallets"][q]["positions"] = f.get("positions") or []
        by_sym = _futures_slots_by_symbol(state, q, alloc_doc)
        for m in (f.get("reconcile") or {}).get("mismatches") or []:
            if m["symbol"] in adopted and float(m.get("binance") or 0) == 0:
                continue  # explained: closed outside the runner, booked from Binance trades
            slots = by_sym.get(m["symbol"], [])
            man = manual_orders_since(fc, client, "futures", m["symbol"], MANUAL_LOOKBACK_MS_FN()) if fc else []
            if man:
                rep["mismatches"].append({"venue": "futures", "quote": q, **m, "slots": slots, "kind": "manual_order",
                                          "tag": "人工單", "manual_orders": man, "pause": False})
                continue
            rep["mismatches"].append({"venue": "futures", "quote": q, **m, "slots": slots,
                                      "kind": "futures_position_mismatch" if slots else "untracked_exchange_position",
                                      "pause": bool(slots)})
    if client is not None:
        try:
            rep["mismatches"].extend(_spot_checks(client, state))
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"spot: {str(e)[:200]}")
    rep["ok"] = not rep["mismatches"] and not rep["errors"]
    return rep


def apply_report(st: dict, rep: dict) -> dict:
    """Persist the report; pause slots with unexplained mismatches; record alerts. Never unpauses."""
    prev = st.get("reconcile") or {}
    adopted = apply_u1_external_closes(st, rep.get("u1_external_closes") or [])
    tag_manual_closed_trades(st)
    uf = rep.get("u1_funding") or {}
    sl = st.get("u1_live") or {}
    if sl and uf and abs(float(uf.get("diff") or 0)) > 1e-9 \
            and abs(float(sl.get("funding") or 0) - float(uf.get("sleeve_funding") or 0)) < 1e-12:
        d = float(uf["diff"])
        sl["cash"] = float(sl.get("cash") or 0) + d
        sl["funding"] = float(sl.get("funding") or 0) + d
        sl.setdefault("events", []).append({"at": rep["at"], "event": "funding_true_up_from_binance", "diff": d})
    for c in rep.get("u1_external_closes") or []:
        alerts_key = f"external_close|{c['symbol']}|{c['order_id']}"
        if alerts_key not in {a.get("key") for a in st.get("reconcile_alerts") or []}:
            st.setdefault("reconcile_alerts", []).append({
                "at": rep["at"], "key": alerts_key, "venue": "futures", "quote": c["quote"], "symbol": c["symbol"],
                "kind": "external_close_adopted", "slots": [c.get("slot")], "trader": c["qty"], "binance": 0.0,
                "pause": False, "order_id": c["order_id"], "client_order_id": c["client_order_id"],
                "closed_at": c["closed_at"]})
    alerts = list(st.get("reconcile_alerts") or [])
    open_keys = {a.get("key") for a in alerts[-20:]}
    ps = dict(st.get("paused_slots") or {})
    paused_now = []
    for m in rep["mismatches"]:
        key = f"{m['venue']}|{m.get('symbol')}|{m.get('trader')}|{m.get('binance')}"
        if key not in open_keys:
            alerts.append({"at": rep["at"], "key": key, **{k: m.get(k) for k in
                           ("venue", "quote", "symbol", "trader", "binance", "slots", "kind", "pause")}})
        if m.get("pause"):
            for s in m.get("slots") or []:
                if s not in ps:
                    ps[s] = {"paused_at": rep["at"], "paused_by": "reconcile",
                             "reason": f"{m['kind']} {m.get('symbol')} trader={m.get('trader')} binance={m.get('binance')}"}
                    paused_now.append(s)
    st["paused_slots"] = ps
    st["reconcile_alerts"] = alerts[-MAX_ALERTS:]
    st["reconcile"] = {**rep, "paused_now": paused_now, "adopted_now": adopted,
                       "consecutive_ok": (int(prev.get("consecutive_ok") or 0) + 1) if rep["ok"] else 0}
    return st


def _ledger_and_lvb(store, state: dict, client, fc):
    """Incremental Binance ledger sync (backfill from 9/15 on first run) + daily live-vs-backtest."""
    led_sum = led_diff = lvb = None
    try:
        import ledger
        from slots import all_known_slots, slot_by_id
        if fc is None:
            from futures_client import FuturesDemoClient
            fc = FuturesDemoClient() if FuturesDemoClient().api_key else None
        known = all_known_slots()
        bals = client.nonzero_balances() if client is not None else []
        doc = store.load_feed(object_name=ledger.LEDGER_OBJECT)
        doc, summ = ledger.sync(doc if (doc and doc.get("fills") is not None) else None, client=client, fc=fc,
                                symbols=ledger.symbols_to_track(state, bals, known),
                                slot_ids=[k["id"] for k in known if k.get("id")])
        store.save_feed(doc, object_name=ledger.LEDGER_OBJECT)
        led_sum = {**ledger.summarize(doc), "last_sync": summ}
        led_diff = ledger.diff_closed_trades(doc, state.get("closed_trades") or [])
        import live_vs_backtest
        if client is not None and live_vs_backtest.due(state):
            by_id = {k.get("id"): k for k in known}
            lvb = live_vs_backtest.run(state, doc, client=client,
                                       slot_lookup=lambda i: slot_by_id(i) or by_id.get(i))
    except Exception as e:  # noqa: BLE001
        log.warning("ledger_or_lvb_fail err=%s", e)
    return led_sum, led_diff, lvb


def run_once(store=None, *, client=None, fc=None) -> dict:
    from state_store import StateStore
    store = store or StateStore()
    if client is None:
        from binance_client import BinanceClient
        client = BinanceClient()
        if not client.api_key:
            client = None
    state0 = store.load()
    rep = build_report(state0, client=client, fc=fc)
    led_sum, led_diff, lvb = _ledger_and_lvb(store, state0, client, fc)
    def mut(s):
        s = apply_report(s, rep)
        if led_sum is not None:
            s["ledger_summary"], s["ledger_diff"] = led_sum, led_diff
        if lvb is not None:
            import live_vs_backtest
            s = live_vs_backtest.apply(s, lvb)
        return s
    st = store.mutate(mut)
    out = (st or {}).get("reconcile") or rep
    log.info("reconcile ok=%s mismatches=%s paused_now=%s errors=%s", out.get("ok"),
             len(out.get("mismatches") or []), out.get("paused_now"), out.get("errors"))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run_once()
    sys.exit(0)
