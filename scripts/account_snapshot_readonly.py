#!/usr/bin/env python3
"""READ-ONLY Demo account snapshot. Runs inside the Cloud Run job (asia-east1).

GET requests only (hard-guarded). Never places, cancels or transfers. Never prints keys.
Prints one JSON line prefixed SNAPSHOT_JSON.
"""
from __future__ import annotations
import hashlib, hmac, json, os, time, urllib.parse
import requests

SPOT = os.environ.get("BINANCE_BASE_URL", "https://demo-api.binance.com")
FUT = os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com")
B1_UNIVERSE = ["SOL", "ETH", "AVAX", "LINK", "ARB", "FET", "DOT", "OP", "NEAR", "INJ"]


def get(base, path, params=None):
    method = "GET"  # hard guard: this script never sends anything but GET
    key = (os.environ.get("BINANCE_DEMO_API_KEY") or "").strip()
    sec = (os.environ.get("BINANCE_DEMO_API_SECRET") or "").strip()
    p = dict(params or {}); p["timestamp"] = int(time.time() * 1000); p["recvWindow"] = 10000
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = requests.request(method, f"{base}{path}?{qs}&signature={sig}", headers={"X-MBX-APIKEY": key}, timeout=30)
    if not r.ok:
        return {"_error": f"{r.status_code}: {r.text[:200]}"}
    return r.json()


def main():
    out = {}
    pr = get(FUT, "/fapi/v2/positionRisk")
    out["fut_positions_nonzero"] = [
        {k: x.get(k) for k in ("symbol", "positionAmt", "entryPrice", "markPrice", "unRealizedProfit", "leverage", "marginType", "positionSide")}
        for x in (pr if isinstance(pr, list) else []) if float(x.get("positionAmt") or 0) != 0
    ] if isinstance(pr, list) else pr
    near = [x for x in pr if x.get("symbol") == "NEARUSDT"] if isinstance(pr, list) else []
    out["near_positionAmt"] = [x.get("positionAmt") for x in near]
    oo = get(FUT, "/fapi/v1/openOrders")
    out["fut_open_orders"] = [{k: o.get(k) for k in ("symbol", "orderId", "type", "side", "origQty", "price", "stopPrice", "reduceOnly", "time")} for o in oo] if isinstance(oo, list) else oo
    ao = get(FUT, "/fapi/v1/openAlgoOrders")
    if isinstance(ao, dict) and "orders" in ao: ao = ao["orders"]
    out["fut_open_algo_orders"] = [{k: o.get(k) for k in ("symbol", "algoId", "orderType", "algoType", "side", "quantity", "triggerPrice", "reduceOnly", "algoStatus", "createTime")} for o in ao] if isinstance(ao, list) else ao
    # recent futures fills on B1 universe (last 3 days)
    since = int((time.time() - 3 * 86400) * 1000)
    fills = {}
    for c in B1_UNIVERSE:
        t = get(FUT, "/fapi/v1/userTrades", {"symbol": c + "USDT", "startTime": since, "limit": 50})
        fills[c] = (len(t), [x.get("time") for x in t][-3:]) if isinstance(t, list) else t
    out["fut_fills_3d"] = fills
    acct = get(SPOT, "/api/v3/account")
    out["spot_apt_balance"] = [b for b in (acct.get("balances") or []) if b.get("asset") == "APT"] if isinstance(acct, dict) else acct
    out["spot_nonzero"] = [b for b in (acct.get("balances") or []) if float(b.get("free") or 0) + float(b.get("locked") or 0) > 0] if isinstance(acct, dict) else None
    so = get(SPOT, "/api/v3/openOrders")
    out["spot_open_orders"] = [{k: o.get(k) for k in ("symbol", "orderId", "type", "side", "origQty", "price", "stopPrice", "time")} for o in so] if isinstance(so, list) else so
    try:
        from allocation import resolve_allocation
        doc, src = resolve_allocation()
        out["alloc_source"] = src
        out["alloc_slots"] = [{"slot": s.get("slot"), "enabled": s.get("enabled"), "family": s.get("family"), "sid": s.get("strategy_id"), "notional": s.get("notional_usdt")} for s in doc.get("slots") or []]
    except Exception as e:  # noqa: BLE001
        out["alloc_error"] = str(e)[:200]
    out["taken_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = json.dumps(out, separators=(",", ":"))
    print("SNAPSHOT_JSON " + body)
    bucket = os.environ.get("GCS_BUCKET") or ""
    if bucket:  # write snapshot only to a dedicated object (not trader state)
        from google.cloud import storage
        storage.Client().bucket(bucket).blob("trader/snapshots/account_snapshot.json").upload_from_string(body, content_type="application/json")


main()
