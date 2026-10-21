"""Ledger from Binance trade history (spec 3.2 / 3.6). Backfills from 2026-09-15 on the first run, then
syncs incrementally with a fromId cursor per (venue, symbol) on every reconcile run. Read-only against
Binance; stored in GCS ledger/fills.json (dedup by venue|symbol|tradeId; rows never rewritten).
Fills not placed by a slot (manual-/u1-manualclose- ids, Binance UI orders) are slot=manual, tag 人工單."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from execution import MANUAL_TAG, base_asset, is_manual_coid

log = logging.getLogger(__name__)

LEDGER_OBJECT = "ledger/fills.json"
BACKFILL_START_MS = int(datetime(2026, 9, 15, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
TZ = timezone(timedelta(hours=8))
STABLES = {"USDT", "USDC", "FDUSD"}


def _quote_of(sym: str) -> str:
    b = base_asset(sym)
    return sym[len(b):] if sym.startswith(b) else "USDT"


def empty() -> dict:
    return {"version": 1, "start_ms": BACKFILL_START_MS, "cursors": {}, "fills": [], "income": [],
            "income_cursor": {}, "orders": {}}


def symbols_to_track(state: dict, balances: list | None, known_slots: list | None) -> dict[str, set]:
    spot, fut = set(), set()
    for s in known_slots or []:
        sym = str(s.get("symbol") or "").upper()
        if sym:
            (fut if (s.get("venue") or "spot") == "futures" else spot).add(sym)
    for p in list((state.get("positions") or {}).values()) + list(state.get("closed_trades") or []):
        if isinstance(p, dict) and p.get("symbol"):
            (fut if p.get("venue") == "futures" else spot).add(str(p["symbol"]).upper())
    for b in balances or []:
        a = str(b.get("asset") or "").upper()
        if a and a not in STABLES:
            spot.add(f"{a}USDT")
    for q in ("USDT", "USDC"):
        fut.update({f"BTC{q}", f"ETH{q}"})
    return {"spot": spot, "futures": fut}


def classify(coid: str | None, slot_ids: list[str]) -> tuple[str, bool]:
    """(slot, manual). Runner ids start with '<slot>-' (execution.client_order_id) or 'u1-'."""
    c = str(coid or "")
    if is_manual_coid(c):
        return "manual", True
    for sid in sorted(slot_ids, key=len, reverse=True):
        if c.startswith(sid + "-") or c.startswith(sid[:30]):
            return sid, False
    if c.startswith("u1-"):
        return "u1", False
    return "manual", True


def _row(venue: str, sym: str, t: dict, coid: str | None, slot: str, manual: bool) -> dict:
    qty, px = float(t.get("qty") or 0), float(t.get("price") or 0)
    comm, asset = float(t.get("commission") or 0), str(t.get("commissionAsset") or "").upper()
    base, quote = base_asset(sym), _quote_of(sym)
    cq = comm if asset == quote else (comm * px if asset == base else (0.0 if comm == 0 else None))
    side = t.get("side") or ("BUY" if t.get("isBuyer") else "SELL")
    ms = int(t.get("time") or 0)
    return {"exchange": "binance_demo", "venue": venue, "symbol": sym, "orderId": t.get("orderId"),
            "tradeId": t.get("id"), "clientOrderId": coid, "slot": slot, "manual": manual,
            **({"tag": MANUAL_TAG} if manual else {}), "side": side, "qty": qty, "price": px,
            "quoteQty": float(t.get("quoteQty") or qty * px), "commission": comm, "commissionAsset": asset,
            "commission_quote": cq, "realizedPnl": float(t["realizedPnl"]) if "realizedPnl" in t else None,
            "time_ms": ms, "time_utc": datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(),
            "time_taipei": datetime.fromtimestamp(ms / 1000, tz=TZ).isoformat(timespec="seconds")}


def _coid(doc: dict, venue: str, sym: str, oid, client, fc) -> str | None:
    key = f"{venue}|{sym}|{oid}"
    if key in doc["orders"]:
        return doc["orders"][key]
    try:
        o = (fc.get_order_by_id(sym, oid) if venue == "futures" else client.get_order(sym, oid)) or {}
        doc["orders"][key] = o.get("clientOrderId")
    except Exception as e:  # noqa: BLE001
        log.warning("ledger_order_lookup_fail %s err=%s", key, e)
        return None
    return doc["orders"][key]


def sync(doc: dict | None, *, client, fc, symbols: dict[str, set], slot_ids: list[str],
         max_pages: int = 20) -> tuple[dict, dict]:
    """Append new Binance fills (fromId cursor). Returns (doc, summary). Errors are per symbol."""
    doc = doc or empty()
    seen = {f"{r['venue']}|{r['symbol']}|{r['tradeId']}" for r in doc["fills"]}
    added, errors = 0, []
    for venue, syms in (("spot", symbols.get("spot") or set()), ("futures", symbols.get("futures") or set())):
        api = client if venue == "spot" else fc
        if api is None:
            continue
        for sym in sorted(syms):
            ck = f"{venue}|{sym}"
            cur = int(doc["cursors"].get(ck, 0))
            try:
                for _ in range(max_pages):
                    rows = (api.my_trades_from(sym, cur) if venue == "spot" else api.user_trades_from(sym, cur)) or []
                    if not rows:
                        break
                    for t in rows:
                        k = f"{venue}|{sym}|{t.get('id')}"
                        if k in seen or int(t.get("time") or 0) < doc["start_ms"]:
                            continue
                        coid = _coid(doc, venue, sym, t.get("orderId"), client, fc)
                        slot, manual = classify(coid, slot_ids)
                        doc["fills"].append(_row(venue, sym, t, coid, slot, manual))
                        seen.add(k)
                        added += 1
                    cur = max(int(t.get("id") or 0) for t in rows) + 1
                    doc["cursors"][ck] = cur
                    if len(rows) < 1000:
                        break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "-1121" in msg or "Invalid symbol" in msg:
                    doc["cursors"][ck] = cur
                    continue
                errors.append(f"{ck}: {msg[:120]}")
    # futures funding income (USDT- and USDC-margined)
    for sym in sorted(symbols.get("futures") or set()):
        if fc is None:
            break
        start = int(doc["income_cursor"].get(sym, doc["start_ms"]))
        try:
            rows = fc.income(sym, "FUNDING_FEE", start) or []
            have = {(r["symbol"], r["tranId"]) for r in doc["income"]}
            for r in rows:
                if (sym, r.get("tranId")) not in have:
                    doc["income"].append({"symbol": sym, "tranId": r.get("tranId"), "type": "FUNDING_FEE",
                                          "income": float(r.get("income") or 0), "asset": r.get("asset"),
                                          "time_ms": int(r.get("time") or 0)})
            # keep a 1-day overlap: Binance can post income rows late
            if rows:
                doc["income_cursor"][sym] = max(int(r.get("time") or 0) for r in rows) - 86400 * 1000
        except Exception as e:  # noqa: BLE001
            errors.append(f"income|{sym}: {str(e)[:120]}")
    doc["fills"].sort(key=lambda r: (r["time_ms"], str(r["tradeId"])))
    doc["synced_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    return doc, {"added": added, "errors": errors, "n_fills": len(doc["fills"])}


def summarize(doc: dict | None) -> dict:
    """Per-quote fees / funding from the ledger, manual fills, fee coverage."""
    if not doc:
        return {"available": False}
    fees: dict[str, float] = {}
    missing = 0
    manual = []
    for r in doc["fills"]:
        q = _quote_of(r["symbol"])
        if r.get("commission_quote") is None:
            missing += 1
        else:
            fees[q] = fees.get(q, 0.0) + float(r["commission_quote"])
        if r.get("manual"):
            manual.append({k: r.get(k) for k in ("time_taipei", "venue", "symbol", "side", "qty", "price",
                                                 "quoteQty", "commission", "commissionAsset", "clientOrderId", "tag")})
    funding: dict[str, float] = {}
    for i in doc.get("income") or []:
        a = str(i.get("asset") or _quote_of(i["symbol"]))
        funding[a] = funding.get(a, 0.0) + float(i["income"])
    return {"available": True, "synced_at": doc.get("synced_at"), "n_fills": len(doc["fills"]),
            "fees_by_quote": {k: round(v, 6) for k, v in fees.items()}, "fee_unpriced_fills": missing,
            "funding_by_asset": {k: round(v, 6) for k, v in funding.items()},
            "manual_fills": manual[-50:], "n_manual_fills": len(manual)}


def diff_closed_trades(doc: dict | None, closed: list[dict]) -> list[dict]:
    """Spec 3.6: each closed trade vs the ledger (exit order fills, and the slot's entry fills before it)."""
    if not doc:
        return []
    by_oid: dict[str, list] = {}
    for r in doc["fills"]:
        by_oid.setdefault(f"{r['venue']}|{r['orderId']}", []).append(r)
    out = []
    for t in closed or []:
        venue = t.get("venue") or "spot"
        oids = t.get("order_id") if isinstance(t.get("order_id"), list) else [t.get("order_id")]
        ex = [r for o in oids for r in by_oid.get(f"{venue}|{o}", [])]
        row = {"slot": t.get("slot"), "code": t.get("code"), "symbol": t.get("symbol"), "closed_at": t.get("closed_at"),
               "order_id": t.get("order_id"), "manual": bool(t.get("manual")), "tag": t.get("tag"),
               "local_qty": t.get("qty"), "local_exit": t.get("exit"), "local_pnl": t.get("pnl_usdt"),
               "local_fee": t.get("fee_usdt")}
        if not ex:
            out.append({**row, "status": "not_in_ledger"})
            continue
        q = sum(r["qty"] for r in ex)
        px = sum(r["qty"] * r["price"] for r in ex) / q if q else None
        exit_side = ex[0]["side"]
        t_ms = min(r["time_ms"] for r in ex)
        # entry fills: same symbol, opposite side, before the exit, newest first until qty covered
        need, ent = q, []
        for r in sorted((r for r in doc["fills"] if r["symbol"] == t.get("symbol") and r["venue"] == venue
                         and r["side"] != exit_side and r["time_ms"] < t_ms), key=lambda r: -r["time_ms"]):
            if need <= 1e-12:
                break
            ent.append(r)
            need -= r["qty"]
        eq = sum(r["qty"] for r in ent)
        e_px = sum(r["qty"] * r["price"] for r in ent) / eq if eq else None
        fee_x = [r["commission_quote"] for r in ex]
        fee_e = [r["commission_quote"] for r in ent]
        fee = None if None in fee_x + fee_e else sum(fee_x) + sum(fee_e)
        sgn = 1.0 if exit_side == "SELL" else -1.0
        # base-asset buy fee: fewer coins were sold than bought → value only the sold qty at entry
        gross = sgn * (px - e_px) * q if (px and e_px) else None
        net = (gross - fee) if (gross is not None and fee is not None) else None
        row.update({"ledger_qty": round(q, 10), "ledger_exit": px, "ledger_entry": e_px,
                    "ledger_fee": round(fee, 6) if fee is not None else None,
                    "ledger_pnl": round(net, 4) if net is not None else None,
                    "diff_pnl": (round(net - float(t["pnl_usdt"]), 4)
                                 if net is not None and t.get("pnl_usdt") is not None else None),
                    "diff_exit_bps": (round((float(t["exit"]) / px - 1) * 1e4, 2)
                                      if px and t.get("exit") else None),
                    "status": "ok" if net is not None else "partial"})
        out.append(row)
    return out
