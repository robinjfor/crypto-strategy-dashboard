#!/usr/bin/env python3
"""One-off: move only the USDT shortfall (+ small buffer) from Demo spot to USD-M futures.

Runs inside the Cloud Run job (asia-east1). Never places orders. Never prints keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse

import requests

# ls_op_4h failed enter at 00:01 TPT 2026-10-04: quote 633.9436 USDT, leverage 1x isolated.
NOTIONAL = 633.94362565327
LEVERAGE = 1.0
# 0.1% fee cushion + 15 USDT so the same-size retry is not short by a rounding tick.
REQUIRED = NOTIONAL / LEVERAGE * 1.001 + 15.0
# Hard cap: never move the whole spot book.
MAX_TRANSFER = 800.0
SPOT_FLOOR = 100.0  # leave at least this much free USDT on spot


def _signed(base: str, method: str, path: str, params: dict | None = None) -> dict | list:
    key = (os.environ.get("BINANCE_DEMO_API_KEY") or "").strip()
    sec = (os.environ.get("BINANCE_DEMO_API_SECRET") or "").strip()
    if not key or not sec:
        raise SystemExit("missing demo api key/secret")
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base.rstrip('/')}{path}?{qs}&signature={sig}"
    r = requests.request(method, url, headers={"X-MBX-APIKEY": key}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def _usdt_spot(spot_base: str) -> float:
    acct = _signed(spot_base, "GET", "/api/v3/account")
    for b in acct.get("balances") or []:
        if b.get("asset") == "USDT":
            return float(b.get("free") or 0)
    return 0.0


def _usdt_futures(fut_base: str) -> tuple[float, float]:
    acct = _signed(fut_base, "GET", "/fapi/v2/account")
    wallet = avail = 0.0
    for a in acct.get("assets") or []:
        if a.get("asset") == "USDT":
            wallet = float(a.get("walletBalance") or 0)
            avail = float(a.get("availableBalance") or 0)
            break
    return wallet, avail


def main() -> int:
    spot_base = os.environ.get("BINANCE_BASE_URL", "https://demo-api.binance.com")
    fut_base = os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com")
    spot = _usdt_spot(spot_base)
    wallet, avail = _usdt_futures(fut_base)
    short = max(0.0, REQUIRED - avail)
    amt = math.ceil(short * 100) / 100.0
    amt = min(amt, MAX_TRANSFER)
    cap = max(0.0, spot - SPOT_FLOOR)
    if amt > cap:
        amt = math.floor(cap * 100) / 100.0
    before = {
        "spot_usdt_free": round(spot, 4),
        "futures_wallet": round(wallet, 4),
        "futures_available": round(avail, 4),
        "required_available": round(REQUIRED, 4),
        "transfer_usdt": amt,
        "slot": "ls_op_4h",
        "notional": NOTIONAL,
        "leverage": LEVERAGE,
    }
    print(json.dumps({"phase": "before", **before}), flush=True)
    if amt < 1.0:
        print(json.dumps({"phase": "done", "transferred": 0, "reason": "futures_margin_sufficient_or_spot_floor"}), flush=True)
        return 0
    amount = f"{amt:.2f}"
    err1 = None
    resp = None
    try:
        resp = _signed(
            spot_base, "POST", "/sapi/v1/asset/transfer",
            {"type": "MAIN_UMFUTURE", "asset": "USDT", "amount": amount},
        )
        via = "MAIN_UMFUTURE"
    except Exception as e:  # noqa: BLE001
        err1 = str(e)[:300]
        # type 1 = spot → USDT-M
        resp = _signed(
            spot_base, "POST", "/sapi/v1/futures/transfer",
            {"asset": "USDT", "amount": amount, "type": 1},
        )
        via = "futures_transfer_type1"
    time.sleep(1.5)
    spot2 = _usdt_spot(spot_base)
    wallet2, avail2 = _usdt_futures(fut_base)
    print(json.dumps({
        "phase": "after",
        "via": via,
        "fallback_error": err1,
        "transfer_resp_keys": sorted(list(resp.keys())) if isinstance(resp, dict) else type(resp).__name__,
        "tranId": (resp.get("tranId") if isinstance(resp, dict) else None),
        "spot_usdt_free": round(spot2, 4),
        "futures_wallet": round(wallet2, 4),
        "futures_available": round(avail2, 4),
        "transferred": amt,
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
