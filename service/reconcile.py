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
        out.append({"venue": "spot", "symbol": rows[0][1]["symbol"], "asset": base, "trader": exp,
                    "binance": got, "slots": [s for s, _ in rows], "kind": kind, "pause": pause})
    return out


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
    for q in ("USDT", "USDC"):
        f = futures_quote_reconcile(state, q, alloc_doc, fc=fc)
        if f.get("error"):
            rep["errors"].append(f"futures_{q}: {f['error']}")
        rep["wallets"][q] = {k: f.get(k) for k in ("wallet", "margin_balance", "unrealized_pnl", "available")}
        rep["wallets"][q]["positions"] = f.get("positions") or []
        by_sym = _futures_slots_by_symbol(state, q, alloc_doc)
        for m in (f.get("reconcile") or {}).get("mismatches") or []:
            slots = by_sym.get(m["symbol"], [])
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
    st["reconcile"] = {**rep, "paused_now": paused_now,
                       "consecutive_ok": (int(prev.get("consecutive_ok") or 0) + 1) if rep["ok"] else 0}
    return st


def run_once(store=None, *, client=None, fc=None) -> dict:
    from state_store import StateStore
    store = store or StateStore()
    if client is None:
        from binance_client import BinanceClient
        client = BinanceClient()
        if not client.api_key:
            client = None
    rep = build_report(store.load(), client=client, fc=fc)
    st = store.mutate(lambda s: apply_report(s, rep))
    out = (st or {}).get("reconcile") or rep
    log.info("reconcile ok=%s mismatches=%s paused_now=%s errors=%s", out.get("ok"),
             len(out.get("mismatches") or []), out.get("paused_now"), out.get("errors"))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run_once()
    sys.exit(0)
